"""Audio pipeline robustness: barge-in races, Live connection leak, local voice server input checks,
Turkish sentence splitting/number reading, mic errors. No real audio device or network is used (server test on 127.0.0.1)."""

from __future__ import annotations

import http.client
import json
import queue
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from alice.tools.base import ToolResult
from alice.voice.audio import Audio
from alice.voice.spoken_numbers import normalize, say_ordinal
from alice.voice.tone import SentenceSplitter, drop_emoji, for_speech


def wait_for(cond, t=3.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def split(text: str) -> list[str]:
    s = SentenceSplitter()
    out: list[str] = []
    for ch in text:  # one character at a time, like a stream
        out += s.feed(ch)
    return out + s.flush()


# ------------------------------------------------------------- Turkish text
def test_splitter_keeps_abbreviations_and_ordinals_in_one_sentence():
    out = split("Osmanlı İmparatorluğu 15. yüzyılda büyüdü. Prof. Dr. Ahmet Yılmaz geldi, elma armut vb. "
                "şeyler aldı. Skor toplam 15. Sonra bitti. GPU 3.5 derece arttı!")
    assert out == ["Osmanlı İmparatorluğu on beşinci yüzyılda büyüdü.",
                   "Prof. Dr. Ahmet Yılmaz geldi, elma armut vb. şeyler aldı.",
                   "Skor toplam on beş.", "Sonra bitti.", "GPU üç virgül beş derece arttı!"]


def test_splitter_waits_for_next_char_after_number_dot():
    s = SentenceSplitter()
    assert s.feed("Bu iş tam 3. ") == []  # ordinal number or sentence end? the next character decides
    assert s.feed("kez oldu. ") == ["Bu iş tam üçüncü kez oldu."]


@pytest.mark.parametrize("raw,want", [
    ("15.10.2026", "on beş ekim iki bin yirmi altı"),
    ("5.1.2025 günü", "beş ocak iki bin yirmi beş günü"),
    ("sürüm 3.12.1", "sürüm üç nokta on iki nokta bir"),
    ("1.234,56 TL", "bin iki yüz otuz dört virgül elli altı TL"),
    ("1,234.56", "bin iki yüz otuz dört virgül elli altı"),
    ("1.200 kg", "bin iki yüz kilo"),
    ("3.5 G", "üç virgül beş ge"),
    ("20. yüzyıl", "yirminci yüzyıl"),
    ("Toplam 15. Sonra", "Toplam on beş. Sonra"),
])
def test_normalize_dates_versions_decimals_ordinals(raw, want):
    assert normalize(raw) == want


def test_say_ordinal():
    assert [say_ordinal(n) for n in (1, 4, 6, 9, 10, 40, 100, 1000, 21)] == [
        "birinci", "dördüncü", "altıncı", "dokuzuncu", "onuncu", "kırkıncı", "yüzüncü", "bininci", "yirmi birinci"]


def test_for_speech_numbered_list_and_emoji():
    assert split("Liste:\n1. Elma al\n2. Armut al\n") == ["Liste:", "birinci, Elma al", "ikinci, Armut al"]
    assert drop_emoji("Harika 😄 oldu 👍🏽 🇹🇷!") == "Harika oldu !"
    assert drop_emoji("🎉🎉") == ""
    assert "°" in for_speech("°") and drop_emoji("Yolda… 5 km") == "Yolda… 5 km"


class _TTS:
    def __init__(self):
        self.calls = []

    def synth(self, text, emotion):
        self.calls.append((text, emotion))
        return Audio(b"\0\0" * 10, 16000)


class _Player:
    def __init__(self):
        self.played = 0

    def play(self, audio):
        self.played += 1
        return True

    def stop(self):
        pass


def test_speaker_drops_emoji_but_keeps_playful_tone():
    from alice.voice.speaker import Speaker

    tts = _TTS()
    sp = Speaker(tts, player=_Player())
    assert sp.say("Yine mi sen 😄") is True
    assert sp.say("🎉") is False  # emoji only: nothing to say
    assert sp.wait(3)
    assert tts.calls == [("Yine mi sen", "playful")]


# ---------------------------------------------------------- Live: barge-in
class _FakeStream:
    def __init__(self, written):
        self.written = written

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, data):
        self.written.append(bytes(data))
        return False


def test_audio_out_drops_chunk_dequeued_right_before_interrupt(monkeypatch):
    """If barge-in (clear) happens right as the playback thread dequeues a chunk, that chunk must not play."""
    from alice.voice.live import AudioOut

    written: list[bytes] = []
    sd = ModuleType("sounddevice")
    sd.RawOutputStream = lambda **k: _FakeStream(written)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)

    out = AudioOut.__new__(AudioOut)
    out.rate, out._gen, out.busy_until, out.underruns = 24000, 0, 0.0, 0
    old = b"\x01\x00" * 4800

    class RacyQueue(queue.Queue):
        first = True

        def get(self, *a, **k):
            item = super().get(*a, **k)
            if self.first and item is not None:
                self.first = False
                out.clear()  # the user spoke at this exact moment
                out.put(b"\x02\x00" * 4800)  # audio of the new answer
                super().put(None)
            return item

    out.q = RacyQueue()
    out.put(old)
    out._play()
    played = b"".join(written)
    assert b"\x01\x00" not in played
    assert played.count(b"\x02\x00") == 4800


def test_audio_out_plays_and_stops_on_close(monkeypatch):
    from alice.voice.live import AudioOut

    written: list[bytes] = []
    sd = ModuleType("sounddevice")
    sd.RawOutputStream = lambda **k: _FakeStream(written)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    out = AudioOut(rate=8000)
    out.put(b"\x03\x00" * 800)
    assert wait_for(lambda: written)
    out.close()
    out._t.join(3)
    assert not out._t.is_alive() and out.dead is None
    assert b"".join(written).count(b"\x03\x00") == 800


# ------------------------------------------------------- Live: connections
class _WS:
    def __init__(self, reply: bool = True):
        self.reply = reply
        self.closed = False
        self.sent: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()

    def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if "setup" in msg:
            self.inbox.put({"setupComplete": {}})
        elif "clientContent" in msg and self.reply:
            self.inbox.put({"serverContent": {"turnComplete": True}})

    def recv(self, timeout=None):
        while not self.closed:
            try:
                return json.dumps(self.inbox.get(timeout=0.05))
            except queue.Empty:
                continue
        raise ConnectionError("closed")

    def close(self):
        self.closed = True


def _session(monkeypatch, connect, **voice):
    from alice.voice.live import LiveSession

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr("alice.voice.live.screen_frame", lambda: None)
    monkeypatch.setattr("alice.voice.live.monitor_layout", lambda: "tek monitör (1)")
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None,
                            _execute_calls=lambda c: [ToolResult("ok")])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"], **voice}})
    out = SimpleNamespace(put=lambda p: None, clear=lambda: None, close=lambda: None, playing=False)
    s = LiveSession(cfg, agent, connect=connect, out_factory=lambda: out)
    s.names = lambda: []
    return s


def test_slow_prefetch_connection_is_closed_after_discard(monkeypatch):
    gate = threading.Event()
    opened: list[_WS] = []

    def connect(url, **k):
        gate.wait(5)  # slow network: the connection opens late
        ws = _WS()
        opened.append(ws)
        return ws

    s = _session(monkeypatch, connect)
    s.PREFETCH_WAIT = 0.05
    s.prefetch()
    s.discard()
    time.sleep(0.2)  # discard stopped waiting
    gate.set()
    assert wait_for(lambda: opened and opened[0].closed)  # it used to stay open on the server


def test_take_abandons_slow_prefetch_and_closes_it_later(monkeypatch):
    gate = threading.Event()
    opened: list[_WS] = []
    calls = [0]

    def connect(url, **k):
        calls[0] += 1
        if calls[0] == 1:
            gate.wait(5)
        ws = _WS()
        opened.append(ws)
        return ws

    s = _session(monkeypatch, connect)
    s.PREFETCH_WAIT = 0.05
    s.prefetch()
    s.converse("selam", queue.Queue(), threading.Event())
    assert len(opened) == 1 and opened[0].closed  # converse opened a new connection and closed it
    gate.set()
    assert wait_for(lambda: len(opened) == 2 and opened[1].closed)


def test_dropped_connection_ends_session_instead_of_hanging(monkeypatch):
    ws = _WS(reply=False)  # the connection drops before a reply arrives
    s = _session(monkeypatch, lambda url, **k: ws, live_idle=30)
    logs: list[str] = []
    s.log = logs.append

    def drop():
        time.sleep(0.2)
        ws.close()  # connection dropped

    threading.Thread(target=drop, daemon=True).start()
    t0 = time.monotonic()
    s.converse("selam", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 < 5
    assert any("bağlantı bitti" in m for m in logs) and any("live kapandı" in m for m in logs)


def test_task_control_error_is_returned_to_model_not_raised(monkeypatch):
    from alice.voice.live import LiveSession

    sent: list[dict] = []
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    s = LiveSession.__new__(LiveSession)
    s.log = lambda m: None

    def boom(calls):
        raise RuntimeError("görev yok")

    s.agent = SimpleNamespace(_execute_calls=boom)
    s._run_call(ws, {"id": "t1", "name": "task_control", "args": {"action": "stop"}})
    res = sent[0]["toolResponse"]["functionResponses"][0]
    assert res["id"] == "t1" and "görev yok" in res["response"]["result"]


def test_background_counter_returns_to_zero(monkeypatch):
    from alice.voice.live import LiveSession

    sent: list[dict] = []
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    s = LiveSession.__new__(LiveSession)
    s.log, s._bg, s._tasks = (lambda m: None), 0, 0
    done = threading.Event()
    s.agent = SimpleNamespace(_enable_groups_for=lambda **k: None,
                              _execute_calls=lambda c: done.wait(3) and [ToolResult("bitti")])
    s._run_call(ws, {"id": "b1", "name": "computer_task", "args": {"goal": "x"}})
    assert s._tasks == 1 and s._bg == 0  # the screen task counts, but does not keep the session open
    s._run_call(ws, {"id": "b2", "name": "computer_task", "args": {"goal": "y"}})  # the second one is rejected
    assert "zaten" in json.dumps(sent[-1], ensure_ascii=False)
    done.set()
    assert wait_for(lambda: s._tasks == 0)


# ---------------------------------------------------- local voice server
@pytest.fixture()
def server(monkeypatch):
    from http.server import ThreadingHTTPServer

    from alice.voice import tts_server

    got: list[dict] = []

    def fake_synth(d):
        got.append(d)
        return Audio(b"\0\0" * 10, 24000).to_wav(), {"X-Alice-Verified": "0"}

    monkeypatch.setattr(tts_server, "synth", fake_synth)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), tts_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], got
    srv.shutdown()
    srv.server_close()


def _post(port, body: bytes, headers=None, path="/synth"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request("POST", path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def test_tts_server_rejects_bad_input_with_400(server):
    port, got = server
    for body in (b"{bozuk", b"[1, 2]", b'"metin"', json.dumps({"text": " "}).encode(),
                 json.dumps({"text": 5}).encode(),
                 json.dumps({"text": "Merhaba", "temperature": "sıcak"}).encode(),
                 json.dumps({"text": "Merhaba", "temperature": True}).encode(),
                 json.dumps({"text": "Merhaba", "device": "cuda:7"}).encode(),
                 json.dumps({"text": "Merhaba", "ref": ["x"]}).encode(),
                 "\xff".encode("latin-1")):
        status, data = _post(port, body)
        assert status == 400, body
        assert "error" in json.loads(data)
    assert got == []  # the model was never called


def test_tts_server_limits_body_and_accepts_good_request(server):
    from alice.voice import tts_server

    port, got = server
    status, _ = _post(port, json.dumps({"text": "a" * (tts_server.MAX_TEXT + 1)}).encode())
    assert status == 400
    status, _ = _post(port, b"{}", headers={"Content-Length": str(tts_server.MAX_BODY + 1)})
    assert status == 413
    status, data = _post(port, json.dumps({"text": "Merhaba.", "temperature": "0.5", "device": "cpu"}).encode())
    assert status == 200 and data.startswith(b"RIFF")
    assert got[-1]["temperature"] == 0.5 and got[-1]["device"] == "cpu"
    status, _ = _post(port, b"{}", path="/yok")
    assert status == 404


def test_tts_server_rejects_negative_length_without_hanging(server):
    import socket

    port, _ = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"POST /synth HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -1\r\n\r\n")
        reply = sock.recv(200)
    assert reply.startswith(b"HTTP/1.0 400")


def test_tts_server_health_and_foreign_host(server):
    port, _ = server
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/health")
    r = c.getresponse()
    assert r.status == 200 and json.loads(r.read())["ok"] is True
    c.request("GET", "/health", headers={"Host": "evil.example"})
    assert c.getresponse().status == 404
    c.close()


def test_tts_server_pure_helpers():
    from alice.voice import tts_server

    long = "Bu çok uzun bir cümle, " * 12 + "bitti."
    parts = tts_server.chunks("Tamam. " + long)
    assert all(len(p) <= 200 for p in parts) and parts[0].startswith("Tamam.")
    assert tts_server.chunks("") == [""]
    assert tts_server.max_tokens_for("kısa") == 120 and tts_server.max_tokens_for("x" * 5000) == 1000
    assert tts_server.fold("Işık GELDİ!") == ["isik", "geldi"]
    words = [("Merhaba", 0.0, 0.4), ("dünya", 0.4, 0.9), ("asdf", 0.9, 1.5)]
    quality, end = tts_server.assess(words, "Merhaba dünya")
    assert quality == 1.0 and end == 0.9
    assert tts_server.assess([], "") == (1.0, None)


def test_tts_server_caps_model_tokens():
    from alice.voice import tts_server

    seen = {}

    def inference(*a, **k):
        seen.update(k)

    model = SimpleNamespace(t3=SimpleNamespace(inference=inference))
    tts_server.cap_tokens(model)
    tts_server.cap_tokens(model)  # a second call does not wrap again
    tts_server.TOKENS["max"] = 200
    model.t3.inference(max_new_tokens=1000)
    assert seen["max_new_tokens"] == 200
    model.t3.inference()
    assert seen["max_new_tokens"] == 200


# --------------------------------------------------------------- microphone
class _Speaker:
    speaking = False

    def say(self, *a, **k):
        return True

    def stop(self):
        pass

    def wait(self, timeout=None):
        return True


class _Agent:
    ui = None

    def stop(self):
        pass


class _Mic:
    RATE = 16000

    def __init__(self, frames=50, fail_reads=0):
        self.frames = frames
        self.fail_reads = fail_reads
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.exited = True

    def read(self):
        if self.fail_reads:
            self.fail_reads -= 1
            raise OSError("aygıt çıkarıldı")
        return b"\0\0" * 480


def test_assistant_retries_mic_open_and_reports_clearly(monkeypatch):
    from alice.voice import assistant
    from alice.voice.assistant import VoiceAssistant

    logs: list[str] = []
    monkeypatch.setattr(assistant, "voice_log", logs.append)
    tries = [0]
    mic = _Mic()

    def factory():
        tries[0] += 1
        if tries[0] < 3:
            raise OSError("Error opening RawInputStream: Device unavailable [PaErrorCode -9985]")
        return mic

    emitted: list[str] = []
    va = VoiceAssistant(_Agent(), _Speaker(), stt=None, mic_factory=factory, emit=emitted.append)
    real_wait = va.stop_event.wait
    monkeypatch.setattr(va.stop_event, "wait", lambda t=None: real_wait(0.01))
    reads = [0]
    orig = mic.read

    def read():
        reads[0] += 1
        if reads[0] > 20:
            va.stop_event.set()
        return orig()

    mic.read = read
    t = threading.Thread(target=va.run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and tries[0] == 3 and mic.exited
    assert sum("mikrofon açılamadı" in e for e in emitted) == 1  # announced once, retried silently
    assert any("mikrofon açıldı" in m for m in logs)


def test_assistant_reports_missing_sounddevice(monkeypatch):
    from alice.voice import assistant
    from alice.voice.assistant import VoiceAssistant

    monkeypatch.setattr(assistant, "voice_log", lambda s: None)

    def factory():
        raise ModuleNotFoundError("No module named 'sounddevice'", name="sounddevice")

    emitted: list[str] = []
    va = VoiceAssistant(_Agent(), _Speaker(), stt=None, mic_factory=factory, emit=emitted.append)
    t = threading.Thread(target=va.run, daemon=True)
    t.start()
    t.join(3)
    assert not t.is_alive()
    assert any("sounddevice kurulu değil" in e for e in emitted)


def test_push_to_talk_mic_error_does_not_kill_listening(monkeypatch):
    from alice.voice import assistant
    from alice.voice.assistant import VoiceAssistant

    monkeypatch.setattr(assistant, "voice_log", lambda s: None)
    mic = _Mic(fail_reads=1)
    reopened = []
    mic.reopen = lambda: reopened.append(1)
    va = VoiceAssistant(_Agent(), _Speaker(), stt=None, mic_factory=lambda: mic)
    real_wait = va.stop_event.wait
    monkeypatch.setattr(va.stop_event, "wait", lambda t=None: real_wait(0.01))

    class PTT:
        def __init__(self):
            self.pressed = threading.Event()
            self.pressed.set()

        def record(self, m):
            frames = []
            while self.pressed.is_set():
                frames.append(m.read())  # the first read raises an error
            return Audio(b"".join(frames), 16000)

        def stop(self):
            pass

    ptt = PTT()
    monkeypatch.setattr(assistant, "PushToTalk", lambda key: ptt)
    va.ptt_key = "<f9>"
    reads = [0]
    orig = mic.read

    def read():
        reads[0] += 1
        if reads[0] == 3:
            ptt.pressed.clear()  # key released
        if reads[0] > 10:
            va.stop_event.set()
        return orig()

    mic.read = read
    t = threading.Thread(target=va.run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and reopened and reads[0] > 10  # listening continued after the error


# ----------------------------------------------------------- audio devices
def _fake_sd(monkeypatch, fail_open=False):
    sd = ModuleType("sounddevice")
    state = {"opened": 0, "closed": 0}

    class Stream:
        def __init__(self, **k):
            if fail_open:
                raise OSError("Error opening RawInputStream [PaErrorCode -9996]")
            state["opened"] += 1
            state["latency"] = k.get("latency")

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            state["closed"] += 1

        def read(self, n):
            return b"\x10\x00" * n, False

    sd.RawInputStream = Stream
    sd.query_devices = lambda *a, **k: ({"name": "Test Mic"} if a or k else [{"max_input_channels": 1}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr(sys, "platform", "linux")
    return state


def test_microphone_open_read_reopen_with_fake_device(monkeypatch):
    from alice.voice.audio import Microphone, mic_available

    state = _fake_sd(monkeypatch)
    assert mic_available()
    mic = Microphone(auto_gain=False)
    with mic as m:
        assert m.name == "Test Mic" and m.read() == b"\x10\x00" * Microphone.FRAME
        # with the default 0.06 s buffer, audio silently dropped whenever the loop stalled (cutting off "Alice")
        assert state["latency"] >= 0.3
        m.reopen()
        assert state["opened"] == 2 and state["closed"] == 1
    assert state["closed"] == 2
    with pytest.raises(OSError, match="kapalı"):
        mic.read()


def test_microphone_open_error_is_raised_cleanly(monkeypatch):
    from alice.voice.audio import Microphone

    _fake_sd(monkeypatch, fail_open=True)
    with pytest.raises(OSError, match="PaErrorCode"):
        Microphone().__enter__()


def test_player_cli_backend_plays_and_stops(monkeypatch):
    from alice.voice import audio

    started = []

    class Proc:
        def __init__(self, args, **k):
            started.append(args)
            self.killed = False

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

    monkeypatch.setattr(audio.subprocess, "Popen", Proc)
    p = audio.Player.__new__(audio.Player)
    p._stop, p._proc, p.backend = threading.Event(), None, "paplay"
    threading.Timer(0.1, p.stop).start()
    assert p.play(Audio(b"\0\0" * 16000, 16000)) is False  # stopped
    assert started and started[0][0] == "paplay" and p._proc.killed
    none = audio.Player.__new__(audio.Player)
    none._stop, none._proc, none.backend = threading.Event(), None, "none"
    assert not none.available and none.play(Audio(b"\0\0", 16000)) is False


@pytest.mark.parametrize("path,claimed", [("/synth", "too_big"), ("/yok", "exact")])
def test_tts_server_drains_the_body_before_an_early_answer(server, path, claimed):
    """An early 413/404 used to close the socket with the body unread; the OS then sends a TCP reset, which on
    Windows discards the response before the client reads it (WinError 10053, flaky on Windows CI)."""
    import errno
    import socket
    import sys
    import time

    from alice.voice import tts_server

    port, _ = server
    body = b"x" * 200_000  # larger than the handler's read buffer: it stays unread in the kernel otherwise
    length = tts_server.MAX_BODY + 1 if claimed == "too_big" else len(body)
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.sendall(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {length}\r\n\r\n".encode() + body)
        time.sleep(0.6)  # let the server answer and close
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        assert data.split(b" ")[1] in (b"413", b"404")
        if sys.platform.startswith("linux"):  # a reset leaves an error on the socket (EPIPE/ECONNRESET)
            assert errno.errorcode.get(s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)) is None
    finally:
        s.close()
