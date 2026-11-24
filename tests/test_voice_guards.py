"""Gates of spoken conversation: false wake-ups, language/junk, unbacked "yaptım" claims, confirmation timing,
microphone going deaf, old sentences, gray zone, background results, fixes (12 items from the voice.log review)."""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from types import SimpleNamespace

from alice.tools.base import ToolResult
from alice.voice import speaker_id as sid
from alice.voice.assistant import VoiceAssistant
from alice.voice.audio import Audio
from alice.voice.lang import correction, foreign, junk
from alice.voice.listen import SpeechGate, strip_wake
from alice.voice.live import LiveSession, LiveUI
from tests.test_live import FakeOut, FakeWS, no_filler_synth, no_screen  # noqa: F401 - fixtures
from tests.test_voice import FakeAgent, FakeSpeaker, wait_for


# ------------------------------------------------------------------ language, junk, corrections
def test_foreign_and_junk_from_real_log_lines():
    for t in ("Abre la canción Capiaça", "Alles bestens, ich bin bereit", "Sí, te veo ahí en el juego"):
        assert foreign(t), t
    for t in ("YouTube'u aç", "Alice şu oyunu kapat", "Bu kadar yeter", "para lazım bin lira", "Tamam"):
        assert not foreign(t), t
    assert junk("---") and junk("...") and junk("<no speech detected>") and not junk("Tamam, açtım.")


def test_correction_words():
    for t in ("Sana demedim.", "Yanlış anladın beni.", "Hayır, şarkı aç demedim", "saçmaladın yine"):
        assert correction(t), t
    assert not correction("Hayatım güzel") and not correction("şarkı aç")


# ------------------------------------------------------------------ false wake-ups
def test_alis_roots_and_mentions_do_not_wake():
    # voice.log: "Alışanlar.", "Alışver.", "Alışıyorsun ya" woke it; "Alice'e de", "Alice'dir bu" are mentions.
    for t in ("Alışanlar.", "Alışver.", "Alışıyorsun ya", "Alice'e de söyle", "Alice'dir bu.",
              "Bak şimdi Alice'ten bahsediyorum"):
        assert strip_wake(t) is None and strip_wake(t, strict=True) is None, t
    assert strip_wake("Alisneber") == "neber"  # "Alice naber" written together ("Alisneber") still wakes
    assert strip_wake("Alice'cim naber") == "naber"  # the affectionate suffix ("Alice'cim") is a call
    assert strip_wake("Alice, saat kaç") == "saat kaç"


def test_gemini_must_hear_alice_in_selective_mode(monkeypatch):
    """In selective mode (Discord), the local model had heard "Alışacak" as "Alice, take"; Gemini heard "Alışacak" and
    still answered. Now, without "Alice" in Gemini's transcript, no audio plays and the session quietly closes."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pcm = base64.b64encode(b"\x01\x00" * 100).decode()

    def run(heard: str):
        script = [{"serverContent": {"inputTranscription": {"text": heard}}},
                  {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": pcm}}]}}},
                  {"serverContent": {"outputTranscription": {"text": "Evet?"}, "turnComplete": True}}]
        ws = AudioWS(script)
        out, logs = FakeOut(), []
        s = LiveSession(cfg(), agent(), log=logs.append, connect=lambda url, **k: ws, out_factory=lambda: out)
        s.names = lambda: []
        # Wait for the answer long enough for a slow CI runner. With 0.5 s the session could close before the reader
        # thread had read the fake connection's reply, so no audio arrived (flaky on Windows CI, #216). It does not
        # slow the test down: the wait ends as soon as the turn completes.
        s.ANSWER_WAIT = 5.0
        s.converse("", queue.Queue(), threading.Event(), first_audio=b"\0\0" * 1600, need_wake=True)
        return out.chunks, logs

    chunks, logs = run("Alışacak, alışacak.")
    assert chunks == [] and any("duymadı" in line for line in logs)
    chunks, _ = run("Alice saat kaç")
    assert chunks == [b"\x01\x00" * 100]


# ------------------------------------------------------------------ Live: gates
class AudioWS(FakeWS):
    """Fake connection that starts its script on the first audio (the first sentence is sent as voice, not text)."""

    fired = False

    def send(self, data):
        msg = json.loads(data)
        if "realtimeInput" in msg and not self.fired:
            self.fired = True
            self.sent.append(msg)
            for m in self.script:
                self.inbox.put(m)
            return
        super().send(data)


def cfg():
    return SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"], "live_fillers": False}})


def agent(run=lambda calls: [ToolResult("tamam")], risk="exec"):
    tool = SimpleNamespace(spec=lambda: {"name": "youtube", "description": "d", "parameters": {}},
                           risk_for=lambda args: risk)
    return SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                           tools={"youtube": tool, "windows": tool}, _enable_groups_for=lambda **k: None,
                           _execute_calls=run)


def test_foreign_request_does_not_run_action_tools(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ran = []
    ws = FakeWS([{"toolCall": {"functionCalls": [{"id": "c1", "name": "youtube", "args": {"q": "Capiaça"}}]}}])
    logs = []
    s = LiveSession(cfg(), agent(lambda c: ran.append(c) or [ToolResult("açıldı")]), log=logs.append,
                    connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.ANSWER_WAIT = 0.5  # the fake connection may answer before the tool result: avoids a 20 s wait
    s.converse("Abre la canción Capiaça", queue.Queue(), threading.Event())
    assert ran == []
    reply = next(m for m in ws.sent if "toolResponse" in m)
    assert "NOT DONE" in json.dumps(reply, ensure_ascii=False)


def test_foreign_or_junk_answer_is_muted(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for said in ("Alles bestens, ich bin bereit.", "---"):
        ws = FakeWS([{"serverContent": {"outputTranscription": {"text": said}, "turnComplete": True}}])
        logs = []
        s = LiveSession(cfg(), agent(), log=logs.append, connect=lambda url, ws=ws, **k: ws, out_factory=FakeOut)
        s.names = lambda: []
        s.ANSWER_WAIT = 0.5  # the fake connection may answer before the tool result: avoids a 20 s wait
        s.converse("nasılsın", queue.Queue(), threading.Event())
        assert any("susturuldu" in line for line in logs), said


def test_claim_without_tool_result_gets_corrected(monkeypatch):
    """2026-11-03: in the turn that said "Tema rengini yeşile kaydırdım" (I shifted the theme to green), no tool ran."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"serverContent": {"outputTranscription": {"text": "Tema rengini yeşile kaydırdım."},
                                    "turnComplete": True}}])
    logs = []
    s = LiveSession(cfg(), agent(), log=logs.append, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.ANSWER_WAIT = 0.5  # the fake connection may answer before the tool result: avoids a 20 s wait
    s.converse("temayı yeşil yap", queue.Queue(), threading.Event())
    notes = [m for m in ws.sent if "clientContent" in m and "system note" in json.dumps(m, ensure_ascii=False)]
    assert len(notes) == 1  # once: the correction itself is not corrected again
    assert any("doğrulanmamış iddia" in line for line in logs)


def test_claim_backed_by_tool_result_is_left_alone(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"toolCall": {"functionCalls": [{"id": "c1", "name": "youtube", "args": {"q": "x"}}]}}])
    ws_send = ws.send

    def send(data):  # after the tool result, says "açtım" (I opened it)
        msg = json.loads(data)
        if "toolResponse" in msg:
            ws.sent.append(msg)
            ws.inbox.put({"serverContent": {"outputTranscription": {"text": "Videoyu açtım."}, "turnComplete": True}})
            return
        ws_send(data)

    ws.send = send
    s = LiveSession(cfg(), agent(), log=lambda m: None, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.ANSWER_WAIT = 0.5  # the fake connection may answer before the tool result: avoids a 20 s wait
    s.converse("videoyu aç", queue.Queue(), threading.Event())
    assert not [m for m in ws.sent if "clientContent" in m and "system note" in json.dumps(m, ensure_ascii=False)]


def test_repeated_failures_steer_model_elsewhere(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"toolCall": {"functionCalls": [{"id": "c1", "name": "windows", "args": {"target": "a"}}]}},
                 {"toolCall": {"functionCalls": [{"id": "c2", "name": "windows", "args": {"target": "b"}}]}}])
    s = LiveSession(cfg(), agent(lambda c: [ToolResult("pencere yok", is_error=True)]), log=lambda m: None,
                    connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.ANSWER_WAIT = 0.5  # the fake connection may answer before the tool result: avoids a 20 s wait
    s.converse("tarayıcıya geç", queue.Queue(), threading.Event())
    replies = [json.dumps(m, ensure_ascii=False) for m in ws.sent if "toolResponse" in m]
    assert "in a row" not in replies[0] and "in a row" in replies[1]


def test_permission_comes_from_words_after_the_question():
    """In Live, the "yap" (do it) in a "şunu yap" request granted permission without asking at all."""
    ui = LiveUI()
    ui.last_user = "WhatsApp'ta merhaba yaz ve gönder, yap hadi"
    assert ui.ask_permission("ui_act", "gönder", "computer") == "n"  # asked first
    ui.heard("evet")
    assert ui.ask_permission("ui_act", "gönder", "computer") == "y"
    assert ui.ask_permission("ui_act", "gönder", "computer") == "n"  # new task: asked again
    ui.heard("hayır yapma")
    assert ui.ask_permission("ui_act", "gönder", "computer") == "n"


def test_prefetch_belongs_to_the_utterance_that_opened_it():
    s = LiveSession.__new__(LiveSession)
    s._pre_lock = threading.Lock()
    t = threading.Thread(target=lambda: None)
    t.start()
    mine, other = object(), object()
    s._pre = {"owner": mine, "thread": t}
    s.discard(owner=other)  # a sentence without a wake word cannot close the connection of the one that woke it
    assert s._pre.get("owner") is mine
    s.discard(owner=mine)
    assert s._pre == {}


def test_background_result_reaches_user_after_session_closed():
    s = LiveSession.__new__(LiveSession)
    s._ws, got = None, []
    s.announce = got.append
    s._deliver("computer_task", "Masaüstü simgeleri açıldı. Ayrıntı: ...")
    assert got == ["Screen task finished: Masaüstü simgeleri açıldı."]
    sent = []
    s._ws = SimpleNamespace(send=sent.append)
    s._deliver("computer_task", "tamamlandı")
    assert sent and got == ["Screen task finished: Masaüstü simgeleri açıldı."]  # open session: deliver there


def test_background_task_does_not_keep_session_open():
    s = LiveSession.__new__(LiveSession)
    s._bg_lock, s._tasks, s._bg = threading.Lock(), 0, 0
    s.log = lambda m: None
    s._turn = {"ok": 0}
    s.agent = SimpleNamespace(_enable_groups_for=lambda **k: None,
                              _execute_calls=lambda c: time.sleep(0.2) or [ToolResult("bitti")])
    s._ws, s.announce = None, lambda t: None
    ws = SimpleNamespace(send=lambda d: None)
    s._run_background(ws, {"id": "1", "name": "computer_task", "args": {"goal": "x"}})
    assert s._tasks == 1 and s._bg == 0  # _bg (which keeps the session open) was not incremented
    s._run_background(ws, {"id": "2", "name": "computer_task", "args": {"goal": "x"}})  # the second one is rejected
    assert wait_for(lambda: s._tasks == 0)


def test_correction_goes_to_the_test_set(monkeypatch):
    got = []
    monkeypatch.setattr("alice.evals.record_feedback", lambda say, reply, note="", source="": got.append(
        (say, reply, source)) or True)
    s = LiveSession.__new__(LiveSession)
    s.log, s.model, s.agent = (lambda m: None), "m", SimpleNamespace(memory=None)
    s._history = [(time.time(), "Durumachwitz'i deneyelim", "Unciv'i başlatıyorum")]
    s._flush_log({"heard": ["Yanlış anladın beni."], "said": []}, LiveUI())
    assert got == [("Durumachwitz'i deneyelim", "Unciv'i başlatıyorum", "ses-düzeltme")]


# ------------------------------------------------------------------ microphone, queue, gray zone
def test_mic_is_not_left_deaf_when_live_goes_down_before_the_job():
    """The queue opens on wake-up; if Live isn't ready by then, nobody closes it and the microphone stayed deaf."""
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=SimpleNamespace(transcribe=lambda a: "", wake_gate=None))
    va._live_q = queue.Queue()
    va._handle_job(Audio(b"\1\0" * 16000, 16000))
    assert va._live_q is None


def test_stale_utterances_are_skipped():
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=SimpleNamespace(transcribe=lambda a: "", wake_gate=None))
    seen = []
    va.handle_audio = lambda audio, direct, started=None, ptt=False: seen.append(audio.seconds)
    a, b = Audio(b"\1\0" * 16000, 16000), Audio(b"\1\0" * 8000, 16000)
    va._utter_q.put((a, False, None, False, time.monotonic() - 60))  # waited in the queue for minutes during a game
    va._utter_q.put((b, False, None, False, time.monotonic()))
    assert wait_for(lambda: seen == [0.5])


def test_gray_zone_voice_needs_the_wake_word(monkeypatch):
    class Embedder:  # \1 user (1,0), \3 gray zone (0,46), \2 someone else (0)
        def embed(self, pcm, rate=16000):
            return {b"\1": [1.0, 0.0, 0.0], b"\3": [0.46, 0.888, 0.0]}.get(pcm[:1], [0.0, 1.0, 0.0])

    gate = sid.SpeakerGate({"vector": [1.0, 0.0, 0.0], "threshold": 0.5}, Embedder())
    ag = FakeAgent()
    stt = SimpleNamespace(wake_gate=None, text="Hey Alice, saat kaç?")
    stt.transcribe = lambda a: stt.text
    va = VoiceAssistant(ag, FakeSpeaker(), stt=stt, speaker_gate=gate)
    va.handle_audio(Audio(b"\3\0" * 16000, 16000), direct=False)  # gray zone, but said "Alice": accepted
    assert wait_for(lambda: ag.turns == ["saat kaç?"])
    stt.text = "naber"
    va.armed_until = time.monotonic() + 5  # follow-up window: without saying "Alice"
    va.handle_audio(Audio(b"\3\0" * 16000, 16000), direct=False)
    time.sleep(0.3)
    assert ag.turns == ["saat kaç?"]  # in the gray zone, a sentence without "Alice" is not taken


def test_answer_during_the_question_counts():
    """Classic path: saying "evet" (yes) while the question was spoken counted as a new request and stopped the turn."""
    sp = FakeSpeaker()
    va = VoiceAssistant(FakeAgent(), sp, stt=SimpleNamespace(transcribe=lambda a: "", wake_gate=None))
    sp.say = lambda text, emotion=None: va.handle_text("evet", direct=False) or True
    assert va.ask_yes_no("Göndereyim mi?", timeout=1) is True


def test_speech_gate_verifies_who_interrupts():
    """In Discord/games, if the voice that cuts in while ALICE is speaking is not the user's, Live gets silence."""
    vad = SimpleNamespace(is_speech=lambda f: True, boost=1.0)
    frame = b"\1\0" * 480
    g = SpeechGate(vad=vad, start_frames=2)
    g.verify = lambda pcm: False
    out = [f for _ in range(60) for f in g.feed(frame)]
    assert out and not any(any(f) for f in out)  # no real frame got through
    g = SpeechGate(vad=vad, start_frames=2)
    g.verify = lambda pcm: True
    out = [f for _ in range(60) for f in g.feed(frame)]
    assert any(any(f) for f in out)


# ------------------------------------------------------------------ fixes from the November 5 check
def test_recent_utterance_is_heard_even_if_late_but_only_with_wake_word():
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=SimpleNamespace(transcribe=lambda a: "", wake_gate=None))
    seen = []
    va.handle_audio = lambda audio, direct, started=None, ptt=False: seen.append((audio.seconds, direct, started))
    a = Audio(b"\1\0" * 16000, 16000)
    va._utter_q.put((a, True, 123.0, False, time.monotonic() - 12))  # waited 12 s in a game, nothing newer behind
    assert wait_for(lambda: seen == [(1.0, False, None)])  # it is heard, and does not count as a follow-up window


def test_late_utterance_with_newer_one_behind_is_skipped():
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=SimpleNamespace(transcribe=lambda a: "", wake_gate=None))
    gate, seen = threading.Event(), []

    def handle(audio, direct, started=None, ptt=False):
        gate.wait(2)
        seen.append(audio.seconds)

    va.handle_audio = handle
    first, old, new = (Audio(b"\1\0" * (4000 * n), 16000) for n in (1, 2, 3))
    va._utter_q.put((first, False, None, False, time.monotonic()))  # the worker is busy with this one
    time.sleep(0.1)
    va._utter_q.put((old, False, None, False, time.monotonic() - 12))
    va._utter_q.put((new, False, None, False, time.monotonic()))
    gate.set()
    assert wait_for(lambda: seen == [0.25, 0.75])


def test_gemini_near_spelling_of_a_lone_alice_counts():
    v = LiveSession._wake_verdict
    assert v("Annie") is True and v("Alex.") is True and v("Alice saat kaç") is True
    assert v("89") is False and v("Alışacak, alışacak.") is False and v("Aliye geldi") is False
    assert v("Bugün Annie ile maça gidiyoruz") is False and v("") is None


def test_selective_mode_sends_audio_without_waiting_for_local_text(monkeypatch):
    calls = []

    class Live:
        available, single_turn, hushed, last_active = True, False, False, 0.0

        def converse(self, text, frames, stop, **kw):
            calls.append(kw)

    gate = SimpleNamespace(transcribe=lambda *a: (_ for _ in ()).throw(AssertionError("yerel döküm çalışmamalı")))
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=SimpleNamespace(transcribe=lambda a: "", wake_gate=gate),
                        live=Live(), social=lambda: "Discord")
    a = Audio(b"\1\0" * 16000, 16000)
    va._converse_live("", a)
    assert "hint" not in calls[0] and calls[0]["need_wake"] is True
    va._pre_text = (None, True)  # said "Alice" and got a reaction; now "naber": "Alice" is not searched for
    va._converse_live("", a)
    assert "need_wake" not in calls[1]
