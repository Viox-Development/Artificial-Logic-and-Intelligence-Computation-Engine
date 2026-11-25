from __future__ import annotations

import base64
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from alice.i18n import spoken
from alice.tools.base import ToolResult
from alice.voice.live import LiveSession, declarations, sanitize_schema


@pytest.fixture(autouse=True)
def no_screen(monkeypatch):
    monkeypatch.setattr("alice.voice.live.screen_frame", lambda: None)  # keep tests from capturing the real screen


@pytest.fixture(autouse=True)
def no_filler_synth(monkeypatch, tmp_path):
    from alice.voice import fillers

    # Keep tests away from Gemini TTS and don't write audio into the real ~/.alice.
    monkeypatch.setattr(fillers, "gemini_fillers", lambda cfg, voice, key: fillers.Fillers(voice, None, tmp_path))


def test_sanitize_schema_for_gemini():
    s = {"type": "object", "additionalProperties": False, "properties": {
        "a": {"type": ["string", "null"], "default": "x"}, "b": {"type": "array", "items": {"type": "integer"}}},
        "required": ["a"]}
    assert sanitize_schema(s) == {"type": "OBJECT", "properties": {
        "a": {"type": "STRING", "nullable": True}, "b": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        "required": ["a"]}
    assert sanitize_schema({"type": "string", "enum": ["", "pdf"]}) == {"type": "STRING", "enum": ["pdf"]}
    assert sanitize_schema({"type": "string", "enum": [""]}) == {"type": "STRING"}  # empty enum used to block Live
    d = declarations([{"type": "function", "function": {"name": "t", "description": "d",
                                                         "parameters": {"type": "object", "properties": {}}}}])
    assert d == [{"name": "t", "description": "d"}]



@pytest.fixture(autouse=True)
def _short_answer_wait(monkeypatch):
    """Without an answer from the fake server, the session stayed open 20 s on "cevap bekleniyor"; keep it short."""
    monkeypatch.setattr(LiveSession, "ANSWER_WAIT", 1.5)

class FakeWS:
    def __init__(self, script):
        self.sent: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self.script = script
        self.closed = False

    def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if "setup" in msg:
            self.inbox.put({"setupComplete": {}})
        elif "clientContent" in msg:
            for m in self.script:
                self.inbox.put(m)
        elif "toolResponse" in msg:
            self.inbox.put({"serverContent": {"outputTranscription": {"text": "YouTube açıldı."},
                                              "turnComplete": True}})

    def recv(self, timeout=None):
        while not self.closed:
            try:
                return json.dumps(self.inbox.get(timeout=0.1))
            except queue.Empty:
                continue
        raise ConnectionError("closed")

    def close(self):
        self.closed = True


class FakeOut:
    def __init__(self):
        self.chunks = []

    def put(self, pcm):
        self.chunks.append(pcm)

    def clear(self):
        pass

    playing = False

    def close(self):
        pass


def test_live_converse_runs_tools_and_plays_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pcm = base64.b64encode(b"\x01\x00" * 100).decode()
    ws = FakeWS([
        {"serverContent": {"inputTranscription": {"text": "YouTube'u aç"},
                           "modelTurn": {"parts": [{"inlineData": {"data": pcm}}]}}},
        {"toolCall": {"functionCalls": [{"id": "c1", "name": "windows",
                                         "args": {"action": "open", "target": "https://youtube.com"}}]}},
    ])
    ran = []
    agent = SimpleNamespace(
        messages=[{"role": "system", "content": "sys"}], ctx=SimpleNamespace(state={}), ui=None,
        tool_specs=lambda: [{"type": "function", "function": {"name": "windows", "description": "w",
                                                               "parameters": {"type": "object", "properties": {
                                                                   "action": {"type": "string"}}}}}],
        _enable_groups_for=lambda **k: None,
        _execute_calls=lambda calls: ran.append(calls) or [ToolResult("açıldı")])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.6, "live_models": ["m1"]}})
    out = FakeOut()
    logs = []
    s = LiveSession(cfg, agent, log=logs.append, connect=lambda url, **k: ws, out_factory=lambda: out)
    assert s.available
    t0 = time.monotonic()
    s.converse("YouTube'u aç", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 < 5
    setup = ws.sent[0]["setup"]
    assert setup["model"] == "models/m1" and setup["tools"][0]["functionDeclarations"][0]["name"] == "windows"
    assert json.loads(ran[0][0]["function"]["arguments"]) == {"action": "open", "target": "https://youtube.com"}
    resp = next(m for m in ws.sent if "toolResponse" in m)["toolResponse"]["functionResponses"][0]
    assert resp == {"id": "c1", "name": "windows", "response": {"result": "açıldı"}}
    assert out.chunks == [b"\x01\x00" * 100]
    assert any("YouTube açıldı." in line for line in logs)
    assert agent.ui is None  # old UI restored


def test_prefetch_reuses_connection_and_discard_closes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    opened = []

    def connect(url, **k):
        ws = FakeWS([])
        opened.append(ws)
        return ws

    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=connect, out_factory=FakeOut)
    s.prefetch()
    s.discard()
    assert _wait(lambda: len(opened) == 1 and opened[0].closed)
    s.prefetch()
    s.converse("", queue.Queue(), threading.Event(), first_audio=b"\0\0" * 1600)
    assert len(opened) == 2  # converse reused the connection opened earlier
    audio = [m for m in opened[1].sent if "realtimeInput" in m]
    assert len(audio) == 2  # sentence + silence after it


def _wait(cond, t=3):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_looks_internal_catches_leaked_notes_only():
    from alice.voice.live import looks_internal

    assert looks_internal("TIRED: I made a syntax error in the function call")
    assert looks_internal("FIXED: Call `browser` properly")
    assert not looks_internal("Tamam, YouTube'u açtım. Saat on üçü çeyrek geçiyor.")
    assert not looks_internal("Alice burada, nasıl yardımcı olayım?")


def test_live_mutes_leaked_internal_note(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    good = base64.b64encode(b"\x01\x00" * 10).decode()
    bad = base64.b64encode(b"\x02\x00" * 10).decode()
    ws = FakeWS([
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": good}, "thought": True}]}}},
        {"serverContent": {"outputTranscription": {"text": "TIRED: I made a syntax error"}}},
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": bad}}]}}},
        {"serverContent": {"turnComplete": True}},
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": good}}]}}},
    ])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "sys"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.6, "live_models": ["m1"]}})
    out = FakeOut()
    logs = []
    s = LiveSession(cfg, agent, log=logs.append, connect=lambda url, **k: ws, out_factory=lambda: out)
    s.converse("selam", queue.Queue(), threading.Event())
    assert out.chunks == [b"\x01\x00" * 10]  # thought and internal note were not played; the next turn was
    assert any("iç not susturuldu" in line for line in logs)


def test_live_tool_timeout_answers_model(monkeypatch):
    sent = []
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    agent = SimpleNamespace(_enable_groups_for=lambda **k: None,
                            _execute_calls=lambda c: time.sleep(0.3) or [ToolResult("geç sonuç")])
    s = LiveSession.__new__(LiveSession)
    s.agent, s.log = agent, lambda m: None
    s.TOOL_TIMEOUT = 0.05
    s._run_call(ws, {"id": "c1", "name": "browser", "args": {"action": "snapshot"}})
    assert len(sent) == 1  # a late result is not sent a second time
    assert "did not finish" in sent[0]["toolResponse"]["functionResponses"][0]["response"]["result"]


def test_live_first_turn_uses_local_text_and_names(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"serverContent": {"turnComplete": True}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: ["GitHub", "YouTube Music"]
    s.converse("", queue.Queue(), threading.Event(), first_audio=b"\0\0" * 1600, hint=lambda: "GitHub'ı açar mısın")
    assert "GitHub, YouTube Music" in ws.sent[0]["setup"]["systemInstruction"]["parts"][0]["text"]
    assert not [m for m in ws.sent if "realtimeInput" in m]  # text was sent instead of audio
    turn = next(m for m in ws.sent if "clientContent" in m)["clientContent"]["turns"][0]
    assert turn["parts"][0]["text"] == "GitHub'ı açar mısın"


def test_live_first_turn_falls_back_to_audio_without_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.converse("", queue.Queue(), threading.Event(), first_audio=b"\0\0" * 1600, hint=lambda: None)
    assert len([m for m in ws.sent if "realtimeInput" in m]) == 2
    assert not [m for m in ws.sent if "clientContent" in m]


def test_live_sends_screen_frame_each_turn(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"serverContent": {"inputTranscription": {"text": "ekrandaki"}}},
                 {"serverContent": {"turnComplete": True}},
                 {"serverContent": {"inputTranscription": {"text": "şunu aç"}}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.5, "live_models": ["m1"], "live_screen": "turn"}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.grab = lambda: "IMG"
    s.converse("ekrandakini aç", queue.Queue(), threading.Event())
    first = next(m for m in ws.sent if "clientContent" in m)["clientContent"]["turns"][0]["parts"]
    assert first[0] == {"inlineData": {"mimeType": "image/jpeg", "data": "IMG"}}
    frames = [m for m in ws.sent if "video" in m.get("realtimeInput", {})]
    assert len(frames) == 1  # first turn went with the text; one more frame at the start of the second turn


def test_live_screen_off_by_default(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cfg = SimpleNamespace(data={"voice": {}})
    assert LiveSession(cfg, SimpleNamespace()).screen == "off"


def test_reflex_works_in_users_visible_tab(monkeypatch):
    from alice.reflex import observers
    from alice.tools import browser

    calls = []
    visible = SimpleNamespace(url="https://music.youtube.com/")
    fake = SimpleNamespace(attached=True, page=SimpleNamespace(url="about:blank"), _visible_page=lambda: visible)
    monkeypatch.setattr(browser, "get_browser", lambda ctx, separate=False: calls.append(separate) or fake)
    ob = observers.BrowserObserver(SimpleNamespace())
    assert calls == [False]  # did not ask for a separate window
    assert ob.b.page is visible


def test_live_declares_all_builtin_tools_upfront(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    def tool(name):
        return SimpleNamespace(spec=lambda: {"type": "function", "function": {"name": name, "description": name}})

    agent = SimpleNamespace(tools={n: tool(n) for n in ("blender", "computer", "load_tools", "mcp__github__x")},
                            tool_specs=lambda: [])
    s = LiveSession(SimpleNamespace(data={"voice": {}}), agent)
    assert [d["function"]["name"] for d in s._specs()] == ["blender", "computer"]


def test_stuck_tool_does_not_lock_conversation(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    release = threading.Event()
    sent = []

    def execute(calls):
        if calls[0]["function"]["name"] == "ui_elements":
            release.wait(5)  # the stuck tool
        return [ToolResult("ok")]

    state = {"browser": object()}
    agent = SimpleNamespace(_enable_groups_for=lambda **k: None, _execute_calls=execute,
                            ctx=SimpleNamespace(state=state))
    s = LiveSession(SimpleNamespace(data={"voice": {}}), agent)
    s.TOOL_TIMEOUT = 0.2
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    t0 = time.monotonic()
    s._run_call(ws, {"id": "1", "name": "ui_elements", "args": {}})
    assert time.monotonic() - t0 < 2 and "browser" not in state
    # The second tool must not hang, so give it a realistic timeout. On a slow CI machine (Windows), 0.2 s was not
    # even enough for the thread to start and it returned "bitmedi"; what is tested is running in a new thread.
    s.TOOL_TIMEOUT = 5
    s._run_call(ws, {"id": "2", "name": "windows", "args": {}})  # runs right away in a new thread
    results = [m["toolResponse"]["functionResponses"][0]["response"]["result"] for m in sent]
    assert "NOT done" in results[0] and results[1] == "ok"
    release.set()


def _guard_session(budget=3):
    sent = []
    agent = SimpleNamespace(_enable_groups_for=lambda **k: None,
                            _execute_calls=lambda c: [ToolResult("sonuç " + c[0]["function"]["arguments"])])
    s = LiveSession(SimpleNamespace(data={"voice": {"live_tool_budget": budget}}), agent)
    s.TOOL_TIMEOUT = 2
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    results = lambda: [m["toolResponse"]["functionResponses"][0]["response"]["result"] for m in sent]  # noqa: E731
    return s, ws, results


def test_live_tool_budget_stops_runaway_loop():
    s, ws, results = _guard_session(budget=3)
    for i in range(5):
        s._run_call(ws, {"id": str(i), "name": "run_command", "args": {"command": f"komut {i}"}})
    r = results()
    assert r[:3] == [f'sonuç {{"command": "komut {i}"}}' for i in range(3)]
    assert all("STOP" in x and "NOT RUN" in x for x in r[3:])
    s._new_turn()  # the user spoke again: the counter is reset
    s._run_call(ws, {"id": "9", "name": "run_command", "args": {"command": "yeni"}})
    assert results()[-1].startswith("sonuç")


def test_live_consecutive_duplicate_call_not_rerun():
    s, ws, results = _guard_session()
    calls = []
    s.agent._execute_calls = lambda c: calls.append(c) or [ToolResult("adım 3")]
    s._run_call(ws, {"id": "1", "name": "flight", "args": {"action": "checklist_next"}})
    s._run_call(ws, {"id": "2", "name": "flight", "args": {"action": "checklist_next"}})
    assert len(calls) == 1 and "not run again" in results()[1] and "adım 3" in results()[1]
    s._run_call(ws, {"id": "3", "name": "computer", "args": {"action": "scroll"}})
    s._run_call(ws, {"id": "4", "name": "computer", "args": {"action": "scroll"}})
    assert len(calls) == 3  # repeatable actions such as scrolling are not blocked


def test_live_idle_is_capped_and_hush_closes_session(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"serverContent": {"inputTranscription": {"text": "Alice "}}},
                 {"serverContent": {"inputTranscription": {"text": "sus"}}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    assert LiveSession(SimpleNamespace(data={"voice": {"live_idle": 30}}), agent, connect=lambda u, **k: ws).idle == 5
    cfg = SimpleNamespace(data={"voice": {"live_idle": 3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    t0 = time.monotonic()
    s.converse("selam", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 < 2 and s.hushed and s.last_active > 0


def _session(ws_for, **voice):
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [])
    cfg = SimpleNamespace(data={"voice": {"live_models": ["m1"], **voice}})
    logs: list[str] = []
    return LiveSession(cfg, agent, log=logs.append, connect=ws_for), logs


def test_live_asks_for_affective_dialog_and_proactive_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    urls, sockets = [], []

    def connect(url, **k):
        urls.append(url)
        sockets.append(FakeWS([]))
        return sockets[-1]

    s0, _ = _session(connect)
    s0._open()  # off by default: a model that doesn't support it dropped the connection with 1007 on first audio
    assert "v1beta" in urls[0] and "proactivity" not in sockets[0].sent[0]["setup"]
    urls.clear()
    sockets.clear()
    s, _ = _session(connect, live_affective=True, live_proactive=True)
    s._open()
    setup = sockets[0].sent[0]["setup"]
    assert "v1alpha" in urls[0] and setup["generationConfig"]["enableAffectiveDialog"] is True
    assert setup["proactivity"] == {"proactiveAudio": True}
    s2, _ = _session(connect, live_affective=False, live_proactive=False)
    s2._open()
    plain = sockets[-1].sent[0]["setup"]
    assert "v1beta" in urls[-1] and "proactivity" not in plain and "enableAffectiveDialog" not in plain["generationConfig"]


def test_live_falls_back_without_extras_when_model_rejects_them(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    urls = []

    class Picky(FakeWS):
        def send(self, data):
            msg = json.loads(data)
            if "setup" in msg and "proactivity" in msg["setup"]:
                self.sent.append(msg)
                self.inbox.put({"error": {"message": "Unknown name proactivity"}})
                return
            super().send(data)

    def connect(url, **k):
        urls.append(url)
        return Picky([])

    s, logs = _session(connect, live_affective=True, live_proactive=True)
    s._open()
    assert s.model == "m1" and "v1alpha" in urls[0] and "v1beta" in urls[1]
    assert any("desteklenmiyor" in line for line in logs)
    urls.clear()
    s._open()  # a model that rejected it once is not tried again
    assert len(urls) == 1 and "v1beta" in urls[0]


def test_canned_replies_are_kept_out_of_history():
    from alice.voice.live import canned_reply

    for r in ("Buradayım efendim, size nasıl yardımcı olabilirim?", "Anladım efendim, buradayım. Başka bir konuda "
              "yardımcı olabilir miyim?", "Anlayamadım, tekrar eder misiniz?", "Rica ederim, her zaman buradayım."):
        assert canned_reply(r), r
    assert not canned_reply("Hızın 367 knot, irtifa 1100 metre.")
    s, _ = _session(lambda url, **k: FakeWS([]))
    now = time.time()
    s._history = [(now, "Aferin.", "Rica ederim, her zaman buradayım. Başka bir konuda yardımcı olabilir miyim?"),
                  (now, "hızım kaç", "367 knot.")]
    s._memory = lambda: None
    text = s.history_text()
    assert "Aferin." in text and "Rica ederim" not in text and "367 knot." in text


def test_live_same_read_is_not_rerun_even_with_calls_between():
    # voice.log (Oct 10): Get-Volume and İndirilenler listing ran 5-6x in one request, with other commands in between.
    s, ws, results = _guard_session(budget=20)
    calls = []
    s.agent._execute_calls = lambda c: calls.append(c) or [ToolResult("C: D:")]
    vol = {"id": "1", "name": "run_command", "args": {"command": "Get-Volume"}}
    s._run_call(ws, vol)
    s._run_call(ws, {"id": "2", "name": "list_dir", "args": {"path": "İndirilenler"}})
    s._run_call(ws, dict(vol, id="3"))
    assert len(calls) == 2 and "already did this read" in results()[2] and "C: D:" in results()[2]
    # running a command again is fine (like re-running tests after fixing them)
    build = {"id": "4", "name": "run_command", "args": {"command": "npm test"}}
    s._run_call(ws, build)
    s._run_call(ws, {"id": "5", "name": "list_dir", "args": {"path": "."}})
    s._run_call(ws, dict(build, id="6"))
    assert len(calls) == 5
    s._new_turn()
    s._run_call(ws, dict(vol, id="7"))  # new request: it can be read again
    assert len(calls) == 6


def test_read_only_detection():
    from alice.voice.live import read_only

    assert read_only("run_command", {"command": "Get-ChildItem C:\\Users\\x\\Downloads"})
    assert read_only("run_command", {"command": "dir C:\\"}) and read_only("list_dir", {"path": "."})
    assert not read_only("run_command", {"command": "Remove-Item x"}) and not read_only("browser", {"action": "click"})


def test_live_single_turn_closes_right_after_the_answer(monkeypatch):
    # Selective mode (Discord): the session closes when the answer ends; what is said to friends does not go to Live.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pcm = base64.b64encode(b"\x01\x00" * 100).decode()
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])

    def run(single):
        ws = FakeWS([{"serverContent": {"inputTranscription": {"text": "saat kaç"},
                                        "modelTurn": {"parts": [{"inlineData": {"data": pcm}}]},
                                        "outputTranscription": {"text": "Saat beş."}, "turnComplete": True}}])
        s = LiveSession(SimpleNamespace(data={"voice": {"live_idle": 5, "live_models": ["m1"]}}), agent,
                        connect=lambda url, **k: ws, out_factory=FakeOut)
        s.single_turn = single
        t0 = time.monotonic()
        s.converse("saat kaç", queue.Queue(), threading.Event())
        return time.monotonic() - t0

    assert run(True) < 3.0 < run(False)


def test_think_runs_one_at_a_time_and_drops_stale_answers(monkeypatch):
    from alice.voice import live as live_mod

    s, ws, results = _guard_session()
    s._bg = 0
    gate = threading.Event()
    s.think_answer = lambda q, c="": gate.wait(5) and "cevap"
    s.think_timeout = 60
    s._run_think(ws, {"id": "1", "name": "think", "args": {"question": "D&D nedir"}})
    s._run_think(ws, {"id": "2", "name": "think", "args": {"question": "başka soru"}})
    assert "still being thought about" in results()[0]  # the second think did not run; the model answers on its own
    gate.set()
    deadline = time.monotonic() + 3
    while len(results()) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "cevap" in results()[1] and s._thinking == 0
    # late answer: the timeout message was sent and the answer came after THINK_STALE -> not spoken
    sent = []
    late_ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    monkeypatch.setattr(live_mod, "THINK_STALE", 0.0)
    s.think_timeout = 0.01
    gate.clear()
    s._run_think(late_ws, {"id": "3", "name": "think", "args": {"question": "yavaş soru"}})
    time.sleep(0.2)
    gate.set()
    deadline = time.monotonic() + 3
    while s._thinking and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [m for m in sent if "toolResponse" in m]  # the "uzun sürüyor" (taking a long time) answer was sent
    assert not [m for m in sent if "clientContent" in m]  # an answer that arrived minutes later was not read out


def test_slow_think_does_not_keep_the_session_open(monkeypatch):
    # voice.log Oct 25 01:46: think took 189 s; during that time Live stayed open and answered Discord conversations.
    s, ws, results = _guard_session()
    s._bg = 0
    gate = threading.Event()
    s.think_answer = lambda q, c="": gate.wait(5) and "cevap"
    s.think_timeout = 0.05
    s._run_think(ws, {"id": "1", "name": "think", "args": {"question": "zor soru"}})
    assert s._bg == 1 and s._thinking == 1
    deadline = time.monotonic() + 2
    while s._bg and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s._bg == 0 and s._thinking == 0  # freed on timeout: session can close, new question can be asked
    assert "taking long" in results()[0]
    gate.set()
    time.sleep(0.2)
    assert s._bg == 0 and s._thinking == 0  # not decremented a second time when it finished


def test_live_prompt_keeps_simple_questions_off_think():
    from alice.voice.live import LIVE_PROMPT

    assert "destitute ne demek" in LIVE_PROMPT and "DON'T call think" in LIVE_PROMPT


def test_filler_plays_when_model_calls_a_tool_silently(monkeypatch, tmp_path):
    # voice.log Oct 28: on a weather question the model called a tool without saying anything; 10 s of silence.
    from alice.voice import fillers
    from alice.voice.audio import Audio

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    Audio(b"\x05\x00" * 50, 24000).save(tmp_path / "x.wav")
    for text in fillers.TOOL:
        Audio(b"\x05\x00" * 50, 24000).save(fillers.Fillers("Kore", None, tmp_path)._file(spoken(text)))
    ws = FakeWS([{"serverContent": {"inputTranscription": {"text": "Ankara'da hava nasıl"}}},
                 {"toolCall": {"functionCalls": [{"id": "c1", "name": "weather", "args": {"city": "Ankara"}}]}},
                 {"toolCall": {"functionCalls": [{"id": "c2", "name": "weather", "args": {"city": "İzmir"}}]}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None,
                            _execute_calls=lambda c: time.sleep(0.2) or [ToolResult("12 derece")])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.5, "live_models": ["m1"]}})
    out = FakeOut()
    logs = []
    s = LiveSession(cfg, agent, log=logs.append, connect=lambda url, **k: ws, out_factory=lambda: out)
    s.names = lambda: []
    s.FILL_AFTER = 0.05  # the tool takes longer than this: a filler is played (not for a tool that finishes right away)
    s.converse("Ankara'da hava nasıl", queue.Queue(), threading.Event())
    assert out.chunks[0] == b"\x05\x00" * 50  # a filler was played while the tool was running
    assert sum("dolgu" in line for line in logs) == 1  # not played again for a second tool in the same turn


def test_no_filler_after_model_already_spoke():
    s = LiveSession.__new__(LiveSession)
    s.fillers_on = True
    s.log = lambda m: None
    played = []
    s.fillers = lambda: SimpleNamespace(get=lambda kind, tone="calm": b"pcm")
    out = SimpleNamespace(playing=False, put=played.append)
    s._fill({"spoke": True, "asked": 1.0}, out, {"name": "weather"})
    s._fill({"spoke": False, "asked": 1.0}, out, {"name": "computer_task"})  # the model will say "başladım" itself
    assert played == []
    s._fill({"spoke": False, "asked": 1.0}, out, {"name": "think"})
    assert played == [b"pcm"]


def test_fillers_generate_in_background_and_do_not_repeat(tmp_path):
    from alice.voice.audio import Audio
    from alice.voice.fillers import Fillers

    made = []
    f = Fillers("Kore", lambda text, tone="calm": made.append(text) or Audio(text.encode("utf-8")[:20].ljust(20, b"\0"), 24000),
                tmp_path)
    assert f.get() is None  # no audio yet: returns without waiting and generates in the background
    _wait(lambda: not f._warming and len(made) == 5)
    a, b = f.get(), f.get()
    assert a and b and a != b  # the same sentence never comes twice in a row
    assert f.get("think")


def test_think_uses_fast_models_before_the_brain_chain():
    # providers.log Oct 25: nvidia:z-ai/glm-5.3 (brain chain head) stalled 180 s; median of 13 thinks: 189 s.
    router = SimpleNamespace(role_overrides={},
                             auto_chain=lambda role: ["cerebras:q", "cohere:c", "cf:g", "gemini:l"],
                             chain=lambda role: ["nvidia:glm", "cerebras:q", "gemini:f"])
    s = LiveSession.__new__(LiveSession)
    s.think_models = []
    assert s._think_role(router) == "live-think"
    assert router.role_overrides["live-think"] == ["cerebras:q", "cohere:c", "cf:g", "nvidia:glm", "gemini:f"]
    s.think_models = ["gemini:f"]
    s._think_role(router)
    assert router.role_overrides["live-think"] == ["gemini:f"]


def test_auto_screen_sends_frame_only_when_screen_is_mentioned(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cfg = SimpleNamespace(data={"voice": {"live_screen": "auto"}})
    s = LiveSession(cfg, SimpleNamespace())
    assert s.wants_frame("ekrandaki şarkıya tıkla") and s.wants_frame("şunu aç") and s.wants_frame("bu ne")
    assert not s.wants_frame("Ankara'da hava nasıl") and not s.wants_frame("Merhaba, nasılsın")
    assert s.wants_frame("", unknown=True)  # the first sentence went out as audio; what was said is unknown
    s.screen = "off"
    assert not s.wants_frame("ekrandaki")


def test_dolgu_cumlesi_kullanicinin_tonuna_uyar(tmp_path):
    from alice.voice.audio import Audio
    from alice.voice.fillers import PHRASES, Fillers, user_tone

    assert user_tone("hemen aç şunu acil") == "urgent" and user_tone("harika oldu!!") == "happy"
    assert user_tone("hava nasıl") == "calm"
    made = []
    f = Fillers("Kore", lambda text, tone="calm": made.append((tone, text)) or Audio(b"\x01\x00" * 10, 24000), tmp_path)
    for text in PHRASES["calm"]["tool"]:
        Audio(b"\x05\x00" * 50, 24000).save(f._file(spoken(text)))
    assert f.get("tool", "urgent") is not None  # no urgent audio yet: the calm one is played, urgent ones are generated
    _wait(lambda: not f._warming and len(made) == 3)
    assert {t for t, _ in made} == {"urgent"}
    f._last = ""
    pcm = f.get("tool", "urgent")
    assert pcm == b"\x01\x00" * 10


def test_live_single_turn_waits_for_model_after_tool_result(monkeypatch):
    """Live recording: in Discord (selective mode) "kurulumu yap" was said; the model spoke with the first click,
    the session closed after 1 s, job left half-done. It must stay open until the turn ends after a tool result."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    class SlowWS(FakeWS):
        def send(self, data):
            msg = json.loads(data)
            if "toolResponse" in msg:  # the model is thinking about the result: the rest comes 2 s later
                self.sent.append(msg)
                threading.Timer(2.0, lambda: self.inbox.put({"serverContent": {
                    "outputTranscription": {"text": "Kurulum bitti."}, "turnComplete": True}})).start()
                return
            super().send(data)

    ws = SlowWS([{"serverContent": {"inputTranscription": {"text": "kurulumu yap"},
                                    "outputTranscription": {"text": "Devam ediyorum."}, "turnComplete": True}},
                 {"toolCall": {"functionCalls": [{"id": "c1", "name": "browser", "args": {"action": "click"}}]}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None,
                            _execute_calls=lambda c: [ToolResult("tıklandı")])
    logs = []
    s = LiveSession(SimpleNamespace(data={"voice": {"live_idle": 8, "live_models": ["m1"]}}), agent, log=logs.append,
                    connect=lambda url, **k: ws, out_factory=FakeOut)
    s.single_turn = True
    s.converse("kurulumu yap", queue.Queue(), threading.Event())
    assert any("Kurulum bitti." in line for line in logs)  # the rest was heard, then it closed
