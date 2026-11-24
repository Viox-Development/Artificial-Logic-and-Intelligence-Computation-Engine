"""Unit tests for ALICE Live (ChatGPT Realtime): protocol, audio conversion, tool calls and backend selection."""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from types import SimpleNamespace as NS

from alice.tools.base import ToolResult
from alice.voice import assistant as voice_assistant
from alice.voice import live_chatgpt
from alice.voice.live_chatgpt import RealtimeSession, resample_16_to_24


class RecordingOut:
    def __init__(self):
        self.data = bytearray()
        self.playing = False
        self.cleared = 0

    def put(self, pcm):
        self.data.extend(pcm)

    def clear(self):
        self.cleared += 1

    def close(self):
        pass


class FakeWS:
    def __init__(self, events):
        self.events = list(events)
        self.sent = []
        self.closed = False

    def recv(self, timeout=None):
        if not self.events:
            raise TimeoutError()
        ev = self.events.pop(0)
        return json.dumps(ev) if isinstance(ev, dict) else ev

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


def make_session(**voice):
    v = {"mode": "live", "live_backend": "chatgpt", "live_realtime_model": "gpt-realtime-2.1",
         "live_realtime_voice": "marin", "live_idle": 0.2, "live_silence_ms": 600}
    v.update(voice)
    cfg = NS(data={"voice": v, "providers": {}})
    agent = NS(messages=[{"role": "system", "content": "Sen ALICE'sin."}], ui=NS(), tools={},
               refresh_memory=lambda force=False: None)
    return RealtimeSession(cfg, agent, out_factory=RecordingOut, connect=lambda url, secret: None)


def test_resample_16_to_24_length_and_shape():
    pcm = b"\x00\x00" * 480  # 30 ms @16 kHz
    out = resample_16_to_24(pcm)
    assert len(out) == 480 * 3 // 2 * 2  # 720 samples, 16-bit
    assert out[:2] == b"\x00\x00"


def test_hush_detection():
    assert RealtimeSession._is_hush("Alice sus lütfen")
    assert RealtimeSession._is_hush("tamam yeter")
    assert not RealtimeSession._is_hush("bugün hava nasıl")
    # Nov 16 log: punctuation and "sana demedim" (I didn't say that to you) did not trigger hush
    for t in ("Sus.", "Sus!", "Sus ya, sus ya. Ya sus ya.", "Sana demiyorum.", "Sana demedim, sana demedim.",
              "sesini kes", "Şşt"):
        assert RealtimeSession._is_hush(t), t
    for t in ("dosyayı kapat", "susam al", "Aile başlayan.", "kes şunu ortadan ikiye"):
        assert not RealtimeSession._is_hush(t), t


def test_hush_cancels_response_in_progress():
    """Saying "Sus" (shut up) must also cancel the server's in-progress response; otherwise ALICE kept talking."""
    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Sana demiyorum."},
    ])
    out = TimedOut(5.0)
    s = make_session()
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = lambda: out
    s.converse("selam", queue.Queue(), threading.Event())
    assert s.hushed
    assert {"type": "response.cancel"} in ws.sent


def test_session_config_has_audio_vad_and_tools():
    s = make_session()
    s.agent.tools = {"weather": NS(spec=lambda: {"name": "weather",
                                                 "description": "hava durumu",
                                                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}})}
    cfg = s._session_config()
    session = cfg["session"]
    assert session["model"] == "gpt-realtime-2.1"
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["audio"]["input"]["turn_detection"]["silence_duration_ms"] == 600
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is False
    names = [t["name"] for t in session["tools"]]
    assert "think" in names and "weather" in names


def test_tool_schema_types_are_lowercase_for_realtime():
    s = make_session(live_realtime_tools="*")
    s.agent.tools = {"read_file": NS(spec=lambda: {
        "name": "read_file", "description": "dosya oku",
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING"}, "count": {"type": "INTEGER"}}, "required": ["path"]}})}
    session = s._session_config()["session"]
    params = [t for t in session["tools"] if t["name"] == "read_file"][0]["parameters"]
    assert params["type"] == "object"
    assert params["properties"]["path"]["type"] == "string"
    assert params["properties"]["count"]["type"] == "integer"


def test_secret_uses_proxy_key_and_converse_sends_session_update():
    """Avoid the client_secrets 64 KiB limit: connect with the proxy key, update the session over WS."""
    s = make_session()
    s._proxy = {"base": "http://127.0.0.1:8317", "key": "proxy-key"}  # noqa: SLF001
    assert s._secret() == "proxy-key"  # noqa: SLF001
    assert s._send_update is True  # noqa: SLF001
    ws = FakeWS([{"type": "session.created"}])
    s._connect = lambda url, secret: ws
    s._out_factory = RecordingOut
    frames: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(target=s.converse, args=("", frames, stop), daemon=True)
    t.start()
    t.join(5)
    updates = [m for m in ws.sent if m.get("type") == "session.update"]
    assert updates and updates[0]["session"]["audio"]["output"]["voice"] == "marin"


def test_think_delegates_to_agent():
    s = make_session()
    s.agent.run_turn = lambda text: "Güçlü model cevabı: 23 derece"
    call = {"id": "c", "type": "function",
            "function": {"name": "think", "arguments": json.dumps({"request": "hava nasıl?"})}}
    res = s._run_tool(call)
    assert not res.is_error and "23 derece" in res.text


def test_silence_frames_do_not_keep_session_open():
    ws = FakeWS([{"type": "session.created"}])
    s = make_session(live_idle=0.3)
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = RecordingOut
    frames: queue.Queue = queue.Queue()
    stop = threading.Event()
    running = threading.Event()
    running.set()

    def mic():
        while running.is_set():
            frames.put(b"\x00\x00" * 480)  # silence
            time.sleep(0.03)

    threading.Thread(target=mic, daemon=True).start()
    t0 = time.monotonic()
    s.converse("", frames, stop)
    elapsed = time.monotonic() - t0
    running.clear()
    assert elapsed < 2.0  # closes on silence (the 5 s setting is 0.3 s here)


def test_converse_plays_audio_and_sends_mic_frames():
    events = [
        {"type": "session.created"},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "merhaba"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
        {"type": "response.done", "response": {"status": "completed"}},
    ]
    ws = FakeWS(events)
    out = RecordingOut()
    s = make_session()
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001 - no network call
    s._out_factory = lambda: out
    frames: queue.Queue = queue.Queue()
    frames.put(b"\x10\x00" * 480)  # 30 ms mic frame
    stop = threading.Event()
    t = threading.Thread(target=s.converse, args=("", frames, stop), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert out.data == b"\x01\x02" * 12  # ALICE's audio was played
    appends = [m for m in ws.sent if m.get("type") == "input_audio_buffer.append"]
    assert appends and len(base64.b64decode(appends[0]["audio"])) == 720 * 2
    assert ws.closed


def test_echo_guard_drops_mic_frames_while_assistant_audio_is_playing():
    """Speaker echo must not open a new turn or cancel the Realtime response."""
    class SpeakingOut(RecordingOut):
        def put(self, pcm):
            super().put(pcm)
            self.playing = True

    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\\x01\\x02" * 12).decode()},
    ])
    out = SpeakingOut()
    s = make_session(live_idle=0.5)
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = lambda: out
    frames: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(target=s.converse, args=("", frames, stop), daemon=True)
    t.start()
    end = time.monotonic() + 2
    while not out.playing and time.monotonic() < end:
        time.sleep(0.01)
    frames.put(b"\\x10\\x00" * 480)  # 30 ms frame echoed from the speaker back into the mic
    time.sleep(0.1)
    stop.set()
    t.join(3)
    assert not [m for m in ws.sent if m.get("type") == "input_audio_buffer.append"]


class TimedOut(RecordingOut):
    """Plays incoming audio in real time: playing is True for `secs` after put(); close() cuts the rest."""

    def __init__(self, secs):
        super().__init__()
        self.secs = secs
        self.until = 0.0
        self.closed_while_playing = None

    @property
    def playing(self):
        return time.monotonic() < self.until

    @playing.setter
    def playing(self, value):
        pass

    def put(self, pcm):
        super().put(pcm)
        self.until = time.monotonic() + self.secs

    def close(self):
        self.closed_while_playing = self.playing


def test_single_turn_waits_until_answer_is_played():
    """Single answer (Discord/games): on response.done, don't close before the audio ends and cut the sentence."""
    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    out = TimedOut(0.6)
    s = make_session()
    s.single_turn = True
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = lambda: out
    t0 = time.monotonic()
    s.converse("selam", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 >= 0.5
    assert out.closed_while_playing is False


def test_idle_window_starts_after_assistant_stops_talking():
    """Audio arrives faster than real time: count silence from when ALICE stops, not the last packet."""
    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    out = TimedOut(0.8)
    s = make_session(live_idle=0.3)
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = lambda: out
    t0 = time.monotonic()
    s.converse("selam", queue.Queue(), threading.Event())
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.8 + 0.3 - 0.05  # playback + full silence window
    assert out.closed_while_playing is False


def test_hush_still_cuts_immediately():
    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Alice sus"},
    ])
    out = TimedOut(5.0)
    s = make_session()
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = lambda: out
    t0 = time.monotonic()
    s.converse("selam", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 < 2.0 and s.hushed


def test_next_session_gets_previous_turns_and_log_has_reply():
    """Discord: each sentence is a new session, so earlier turns go into the instructions; log records reply and tool."""
    logs = []
    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.output_audio_transcript.done", "transcript": "Bir bakayım."},
        {"type": "response.function_call_arguments.done", "call_id": "c1", "name": "think",
         "arguments": "{\"request\": \"YouTube'da video aç\"}"},
        {"type": "response.done", "response": {"status": "completed"}},
        {"type": "response.output_audio_transcript.done", "transcript": "Açtım."},
    ])
    s = make_session()
    s.log = logs.append
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._run_tool = lambda call: ToolResult("tamam")  # noqa: SLF001
    s.converse("Alice, bana video açar mısın?", queue.Queue(), threading.Event())
    line = [m for m in logs if m.startswith("realtime duyulan:")][0]
    assert "video açar mısın" in line and "Bir bakayım. Açtım." in line and "think(" in line
    instr = s._session_config()["session"]["instructions"]
    assert "Recent conversation" in instr and "video açar mısın" in instr and "Açtım." in instr
    assert "accent" in instr  # pronunciation instruction was not truncated


def test_voice_choice_and_unsupported_voice_falls_back():
    assert make_session(live_realtime_voice="Cedar")._session_config()["session"]["audio"]["output"]["voice"] == "cedar"
    # "quip" is not on the server: don't let it reject the whole session.update
    assert make_session(live_realtime_voice="quip")._session_config()["session"]["audio"]["output"]["voice"] == "marin"


def test_session_stays_open_while_tool_runs():
    """Even if think takes 1 s, don't close on 0.2 s silence; the result must reach the model."""
    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.function_call_arguments.done", "call_id": "c1", "name": "think",
         "arguments": "{\"request\": \"video aç\"}"},
        {"type": "response.done", "response": {"status": "completed"}},
    ])
    s = make_session(live_idle=0.2)
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001

    def slow(call):
        time.sleep(1.0)
        return ToolResult("açıldı")

    s._run_tool = slow  # noqa: SLF001
    s.converse("selam", queue.Queue(), threading.Event())
    assert any((m.get("item") or {}).get("type") == "function_call_output" for m in ws.sent)


def test_late_think_result_is_announced_when_session_closed():
    s = make_session()
    said = []
    s.announce = said.append
    s._run_tool = lambda call: ToolResult("YouTube'da bir video açtım.")  # noqa: SLF001

    def closed(obj):
        raise RuntimeError("bağlantı kapalı")

    s._handle_calls([{"id": "c", "type": "function", "function": {"name": "think", "arguments": "{}"}}], closed)
    assert said == ["YouTube'da bir video açtım."]


def test_think_uses_fast_think_models_and_restores_role():
    s = make_session(live_think_models=["gemini:fast", "chatgpt:big"])
    seen = []
    router = NS(role_overrides={}, auto_chain=lambda r: [], chain=lambda r: [])
    s.agent.router = router
    s.agent.role = "voice"
    s.agent.run_turn = lambda text: seen.append(s.agent.role) or "tamam"
    res = s._think(json.dumps({"request": "hava nasıl"}))  # noqa: SLF001
    assert not res.is_error and seen == ["live-think"] and s.agent.role == "voice"
    assert router.role_overrides["live-think"] == ["gemini:fast", "chatgpt:big"]


def test_think_routes_heavy_request_to_voice_strong(monkeypatch):
    from alice.providers import jev_route

    s = make_session(live_think_models=["gemini:fast"])
    s.cfg.data["routing"] = {"enabled": True, "voice_strong": ["cliproxy:claude-sonnet-5-5"]}
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: jev_route.Route("strong", "derin düşünme"))
    router = NS(role_overrides={}, auto_chain=lambda r: [], chain=lambda r: [])
    s.agent.router, s.agent.role = router, "voice"
    s.agent.run_turn = lambda text: "plan hazır"
    assert not s._think(json.dumps({"request": "haftalık spor programı çıkar"})).is_error  # noqa: SLF001
    assert router.role_overrides["live-think"] == ["cliproxy:claude-sonnet-5-5", "gemini:fast"]


def test_audio_out_counts_held_chunk_as_playing():
    from alice.voice.live import AudioOut

    o = AudioOut.__new__(AudioOut)
    o.q = queue.Queue()
    o.busy_until = 0.0
    assert not o.playing
    o._holding = True  # noqa: SLF001 - chunk taken from the queue, waiting on the pre-buffer
    assert o.playing


def test_converse_runs_tool_and_sends_result():
    events = [
        {"type": "session.created"},
        {"type": "response.function_call_arguments.done", "call_id": "c1", "name": "weather",
         "arguments": "{\"city\": \"Ankara\"}"},
        {"type": "response.done", "response": {"status": "completed"}},
    ]
    ws = FakeWS(events)
    s = make_session()
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = RecordingOut
    s._run_tool = lambda call: ToolResult("23 derece")  # noqa: SLF001
    frames: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(target=s.converse, args=("selam", frames, stop), daemon=True)
    t.start()
    end = time.time() + 5
    sent = []
    while time.time() < end:
        sent = [m for m in ws.sent if m.get("type") == "conversation.item.create"]
        if any((m.get("item") or {}).get("type") == "function_call_output" for m in sent):
            break
        time.sleep(0.05)
    t.join(10)
    outputs = [(m.get("item") or {}) for m in ws.sent if m.get("type") == "conversation.item.create"]
    results = [o for o in outputs if o.get("type") == "function_call_output"]
    assert results and results[0]["call_id"] == "c1" and "23 derece" in results[0]["output"]
    assert any(m.get("type") == "response.create" for m in ws.sent)


def test_make_live_prefers_realtime_and_falls_back(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cfg = NS(data={"voice": {"mode": "live", "live_backend": "auto"}, "providers": {}})
    agent = NS(messages=[{"role": "system", "content": "x"}], ui=NS(), tools={})

    monkeypatch.setattr(live_chatgpt.RealtimeSession, "available", property(lambda self: True))
    assert isinstance(voice_assistant.make_live(cfg, agent), live_chatgpt.RealtimeSession)

    monkeypatch.setattr(live_chatgpt.RealtimeSession, "available", property(lambda self: False))
    from alice.voice.live import LiveSession

    got = voice_assistant.make_live(cfg, agent)
    assert isinstance(got, LiveSession)

    cfg_only = NS(data={"voice": {"mode": "live", "live_backend": "chatgpt"}, "providers": {}})
    assert voice_assistant.make_live(cfg_only, agent) is None


def test_only_quick_tools_go_to_realtime_and_transcription_is_on():
    s = make_session()
    spec = lambda n: NS(spec=lambda: {"name": n, "description": "d", "parameters": {  # noqa: E731
        "type": "object", "properties": {"x": {"type": "string"}}}})
    s.agent.tools = {"youtube": spec("youtube"), "github": spec("github"), "blender": spec("blender")}
    session = s._session_config()["session"]
    assert [t["name"] for t in session["tools"]] == ["think", "youtube"]
    assert session["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe", "language": "tr"}
    assert make_session(live_realtime_tools="*")._tool_specs is not None
    s2 = make_session(live_realtime_tools="*")
    s2.agent.tools = s.agent.tools
    assert len(s2._session_config()["session"]["tools"]) == 4


def test_prefetched_session_is_reused_without_second_update():
    """Pre-warmed session: connection and settings ready ahead; no second session.update during the talk."""
    ws = FakeWS([{"type": "session.created"},
                 {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
                 {"type": "response.done", "response": {"status": "completed"}}])
    opened = []
    s = make_session()
    s._connect = lambda url, secret: opened.append(1) or ws
    s._proxy = {"base": "http://127.0.0.1:8317", "key": "k"}  # noqa: SLF001
    s.prefetch(owner="o")
    s._pre["thread"].join(5)  # noqa: SLF001
    s.discard(owner="baskası")  # another owner's wake-up does not close the pre-warmed session
    s.converse("selam", queue.Queue(), threading.Event())
    assert opened == [1]
    assert [m["type"] for m in ws.sent].count("session.update") == 1


def test_discard_closes_own_prefetch():
    ws = FakeWS([{"type": "session.created"}])
    s = make_session()
    s._connect = lambda url, secret: ws
    s._proxy = {"base": "http://127.0.0.1:8317", "key": "k"}  # noqa: SLF001
    s.prefetch(owner="o")
    s._pre["thread"].join(5)  # noqa: SLF001
    s.discard(owner="o")
    time.sleep(0.2)
    assert ws.closed and not s._pre  # noqa: SLF001


def test_verified_user_can_interrupt_alice():
    """Verified user audio while ALICE speaks: playback is cut, response cancelled, user's voice goes through."""
    class SpeakingOut(RecordingOut):
        def put(self, pcm):
            super().put(pcm)
            self.playing = True

        def clear(self):
            super().clear()
            self.playing = False

    ws = FakeWS([
        {"type": "session.created"},
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
    ])
    out = SpeakingOut()
    s = make_session(live_idle=0.5)
    s.barge_ok = lambda: True
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = lambda: out
    frames: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(target=s.converse, args=("", frames, stop), daemon=True)
    t.start()
    end = time.monotonic() + 2
    while not out.playing and time.monotonic() < end:
        time.sleep(0.01)
    for _ in range(5):  # user's verified voice; just after playback is cut it briefly looks "playing"
        out.playing = True
        frames.put(b"\x10\x00" * 480)
        time.sleep(0.03)
    time.sleep(0.2)
    stop.set()
    t.join(3)
    types = [m.get("type") for m in ws.sent]
    assert types.count("response.cancel") == 1 and out.cleared == 1  # one cancel per response
    assert "input_audio_buffer.append" in types
