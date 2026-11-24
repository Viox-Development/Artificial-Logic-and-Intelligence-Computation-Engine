"""In-app call: the user calls ALICE, ALICE calls the user; the answer is spoken sentence by sentence."""

from __future__ import annotations

import queue
import time
from types import SimpleNamespace as NS

import httpx
import pytest

from alice.app import call
from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config


class FakeTTS:
    def __init__(self, fail=False):
        self.fail, self.said = fail, []

    def synth(self, text, emotion="calm"):
        if self.fail:
            raise RuntimeError("kota")
        self.said.append(text)
        return NS(to_wav=lambda: b"RIFF" + text.encode("utf-8"))


@pytest.fixture
def setup(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", [])
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    tts = FakeTTS()
    state._calls = call.CallManager(state, tts_factory=lambda: tts)
    events = state.bus.subscribe()
    yield state, prov, tts, events
    call.register(None)


def wait_for(events, pred, timeout=10.0):
    seen = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            ev = events.get(timeout=0.1)
        except queue.Empty:
            continue
        seen.append(ev)
        if pred(ev):
            return ev, seen
    raise AssertionError(f"olay gelmedi; görülenler: {[e.get('type') for e in seen]}")


def test_user_calls_alice_and_gets_spoken_answer_sentence_by_sentence(setup):
    state, prov, tts, events = setup
    r = state.call_action("start", {})
    assert r["ok"] and r["state"] == "active" and not r["outbound"]
    ev, _ = wait_for(events, lambda e: e["type"] == "call_audio")
    assert ev["audio"] and ev["text"] in call.GREETINGS
    assert state.calls.audio(r["id"], ev["n"]).startswith(b"RIFF")
    prov.script.append("Saat beşi on geçiyor. Bir de yağmur başlamış, şemsiye al.")
    assert state.call_action("say", {"id": r["id"], "text": "saat kaç"})["turn"] == 1
    done, seen = wait_for(events, lambda e: e["type"] == "call_turn_done")
    spoken = [e["text"] for e in seen if e["type"] == "call_audio"]
    while not done["end"] and len(spoken) < 2:  # speech may keep coming after the turn ends
        spoken.append(wait_for(events, lambda e: e["type"] == "call_audio")[0]["text"])
    assert spoken == ["Saat beşi on geçiyor.", "Bir de yağmur başlamış, şemsiye al."] and not done["end"]
    # The call has its own chat, with the spoken-conversation instruction and the time.
    sent = prov.calls[-1]["messages"]
    assert "Phone call" in sent[0]["content"] and "saat kaç" in sent[-1]["content"]
    assert [x["who"] for x in state.calls.view()["lines"]] == ["alice", "user", "alice"]


def test_alice_marks_end_and_bye_phrase_hangs_up_without_model(setup):
    state, prov, tts, events = setup
    cid = state.call_action("start", {})["id"]
    prov.script.append("Tamam, görüşürüz! [kapat]")
    state.call_action("say", {"id": cid, "text": "bu kadardı"})
    done, _ = wait_for(events, lambda e: e["type"] == "call_turn_done")
    assert done["end"] and all("[kapat]" not in t for t in tts.said)
    n = len(prov.calls)
    state.call_action("say", {"id": cid, "text": "görüşürüz"})
    done, _ = wait_for(events, lambda e: e["type"] == "call_turn_done" and e["turn"] == 2)
    assert done["end"] and len(prov.calls) == n  # the goodbye never went to the model
    assert state.call_action("hangup", {"id": cid})["ok"]
    wait_for(events, lambda e: e["type"] == "call_end" and e["reason"] == "hangup")
    assert not state.call_action("say", {"id": cid, "text": "alo"})["ok"]


def test_alice_rings_user_who_answers(setup):
    state, prov, tts, events = setup
    call.register(state.calls)
    from alice.tools.phonecall import PhoneCall

    res = PhoneCall().run(state.ctx, message="Selam, ilacını içme vakti.", reason="Hatırlatma: ilaç")
    assert "The phone is ringing" in res.text
    ev, _ = wait_for(events, lambda e: e["type"] == "call")
    assert ev["from"] == "alice" and ev["reason"] == "Hatırlatma: ilaç"
    # If the user calls at that moment, no new call opens; the ringing call opens instead.
    assert state.call_action("start", {})["id"] == ev["id"]
    view = state.calls.view()
    assert view["state"] == "active" and view["outbound"]
    spoken, _ = wait_for(events, lambda e: e["type"] == "call_audio")
    assert spoken["text"] == "Selam, ilacını içme vakti."
    ctx_msgs = state.calls.call.chat.agent.messages
    assert "You called the user's phone (Hatırlatma: ilaç)" in ctx_msgs[-2]["content"]


def test_declined_and_missed_calls(setup, monkeypatch):
    state, prov, tts, events = setup
    cid = state.call_action("ring", {"reason": "Proje bitti", "message": "Proje bitti, bakmak ister misin?"})["id"]
    state.call_action("decline", {"id": cid})
    ev, _ = wait_for(events, lambda e: e["type"] == "call_end")
    assert ev["reason"] == "declined" and ev["outbound"]
    monkeypatch.setattr(call, "RING_SECONDS", 0.2)
    state.call_action("ring", {"reason": "Uyarı", "message": "GPU 90 derece."})
    ev, seen = wait_for(events, lambda e: e["type"] == "call_end")
    assert ev["reason"] == "missed"
    info, _ = wait_for(events, lambda e: e["type"] == "info")
    assert "açmadın" in info["text"]


def test_without_speech_engine_text_still_goes_to_phone(setup):
    state, prov, tts, events = setup
    tts.fail = True
    cid = state.call_action("start", {})["id"]
    ev, _ = wait_for(events, lambda e: e["type"] == "call_audio")
    assert ev["audio"] is False and ev["text"]  # the phone reads it with its own voice
    assert state.calls.audio(cid, ev["n"]) is None


def test_phone_call_tool_without_bridge():
    from alice.tools.base import ToolError
    from alice.tools.phonecall import PhoneCall

    call.register(None)
    with pytest.raises(ToolError, match="phone bridge is off"):
        PhoneCall().run(NS(), message="selam")


def test_http_endpoints(setup):
    state, prov, tts, events = setup
    srv = AppServer(state).start()
    try:
        c = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        r = c.post("/api/call/start", json={}).json()
        ev, _ = wait_for(events, lambda e: e["type"] == "call_audio")
        a = c.get("/api/call/audio", params={"id": r["id"], "n": ev["n"]})
        assert a.status_code == 200 and a.headers["content-type"] == "audio/wav"
        assert c.get("/api/call").json()["state"] == "active"
        assert c.get("/api/call/audio", params={"id": r["id"], "n": 99}).status_code == 404
        assert not c.post("/api/call/say", json={"id": "yok", "text": "x"}).json()["ok"]
        assert httpx.get(srv.url.split("/?")[0] + "/api/call").status_code == 403  # not allowed without the key
        assert c.post("/api/call/hangup", json={"id": r["id"]}).json()["ok"]
    finally:
        srv.stop()


def test_permission_asked_in_call_is_answered_from_phone(workdir):
    """The call has its own chat: permission requests go there, not to the front chat (they used to go unanswered)."""
    from tests.conftest import tool_call

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", [tool_call("write_file", {"path": "not.txt", "content": "süt al"}), "Yazdım."])
    state = AppState(cfg, workdir, "ask", provider_factory=lambda pc: prov)
    state._calls = call.CallManager(state, tts_factory=FakeTTS)
    events = state.bus.subscribe()
    srv = AppServer(state).start()
    try:
        c = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        cid = c.post("/api/call/start", json={}).json()["id"]
        c.post("/api/call/say", json={"id": cid, "text": "not al: süt al"})
        perm, _ = wait_for(events, lambda e: e["type"] == "permission")
        assert perm["sid"] == state.calls.view()["sid"]
        assert any(p["id"] == perm["id"] for p in c.get("/api/state").json()["pending"])
        assert c.post("/api/permission", json={"id": perm["id"], "answer": "y"}).json()["ok"]
        wait_for(events, lambda e: e["type"] == "call_turn_done")
        assert (workdir / "not.txt").read_text(encoding="utf-8") == "süt al"
    finally:
        srv.stop()


class FakeRealtime:
    """Stand-in for RealtimeSession in a live call: greets, collects mic frames, returns when the call ends."""

    def __init__(self, agent, out_factory):
        self.agent, self.out_factory = agent, out_factory
        self.firsts, self.frames, self.injected, self.interrupts = [], [], [], 0
        self.on_heard = self.on_said = None
        self._out = None

    def converse(self, first, frames, stop):
        self.firsts.append(first)
        self._out = self.out_factory()
        self.on_said("Efendim?")
        self._out.put(b"\x01\x00" * 480)
        while not stop.is_set():
            try:
                self.frames.append(frames.get(timeout=0.05))
            except queue.Empty:
                pass

    def inject(self, text):
        self.injected.append(text)
        return True

    def interrupt(self):
        self.interrupts += 1
        self._out.clear()


def test_live_call_streams_mic_and_voice_like_the_pc(setup):
    """A phone call streams over Realtime like on the PC: no sentence-by-sentence TTS, audio goes out in chunks."""
    import base64

    state, prov, tts, events = setup
    made = []
    state._calls = call.CallManager(state, tts_factory=lambda: tts,
                                    live_factory=lambda agent, out: made.append(FakeRealtime(agent, out)) or made[-1])
    r = state.call_action("start", {})
    assert r["ok"] and r["live"]
    pcm, seen = wait_for(events, lambda e: e["type"] == "call_pcm")
    assert base64.b64decode(pcm["pcm"]) == b"\x01\x00" * 480
    assert any(e["type"] == "call_text" and e["who"] == "alice" and e["text"] == "Efendim?" for e in seen)
    rt = made[0]
    assert "greeting" in rt.firsts[0] and "live voice" in rt.agent.messages[0]["content"]
    assert state.call_action("mic", {"id": r["id"], "pcm": base64.b64encode(b"\x02\x00" * 960).decode()})["ok"]
    end = time.time() + 5
    while len(rt.frames) < 2 and time.time() < end:
        time.sleep(0.02)
    assert [len(f) for f in rt.frames] == [960, 960]  # 30 ms frames
    assert state.call_action("say", {"id": r["id"], "text": "saat kaç"})["ok"] and rt.injected == ["saat kaç"]
    state.call_action("interrupt", {"id": r["id"]})
    wait_for(events, lambda e: e["type"] == "call_pcm_clear")
    assert not tts.said and not prov.calls  # the old path (agent turn + TTS) never ran
    assert state.call_action("mic", {"id": r["id"], "pcm": "%%%"})["ok"] is False
    state.call_action("hangup", {"id": r["id"]})
    wait_for(events, lambda e: e["type"] == "call_end")
    assert state.calls.call.stop.is_set()


def test_live_call_warns_phone_when_no_mic_audio_arrives(setup):
    """No mic audio from the phone means ALICE can't hear the user: the phone is told to use its own recognition."""
    import base64

    state, prov, tts, events = setup
    state._calls = call.CallManager(state, tts_factory=lambda: tts,
                                    live_factory=lambda agent, out: FakeRealtime(agent, out))
    r = state.call_action("start", {})
    c = state.calls.call
    state.calls._mic_check(c, wait=0)
    wait_for(events, lambda e: e["type"] == "call_mic_missing" and e["id"] == r["id"])
    state.call_action("mic", {"id": r["id"], "pcm": base64.b64encode(b"\x00\x40" * 960).decode()})
    assert c.mic_bytes == 1920 and c.mic_peak == 0x4000
    state.call_action("hangup", {"id": r["id"]})
