from __future__ import annotations

import array
import base64
import json
import math
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from alice.voice.audio import Audio, rms
from alice.voice.assistant import ACKS, VoiceAssistant, VoiceUI, pick_ack
from alice.voice.audition import run_audition
from alice.voice.listen import VAD, strip_wake, utterances
from alice.voice.speaker import Speaker
from alice.voice.stt import GroqWhisper, STTError, STTRouter
from alice.voice.tone import SentenceSplitter, for_speech, pick
from alice.voice.tts import EmotionLibrary, GeminiTTS, TTSError, TTSRouter, build_library


def tone(seconds=0.1, rate=16000, amp=0.3, freq=220):
    a = array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(seconds * rate))))
    return Audio(a.tobytes(), rate)


# --------------------------------------------------------------------- audio
def test_audio_wav_roundtrip_and_resample():
    a = tone(0.5, 24000)
    b = Audio.from_wav(a.to_wav())
    assert b.rate == 24000 and b.pcm == a.pcm
    r = a.resample(16000)
    assert r.rate == 16000 and abs(r.seconds - 0.5) < 0.01
    assert rms(Audio.silence(0.1).pcm) == 0 and rms(a.pcm) > 0.1
    assert (Audio.silence(0.1, 16000) + a).rate == 16000


# ---------------------------------------------------------------------- tone
@pytest.mark.parametrize("text,sev,topic,expected", [
    ("Bingo yakıt, eve dön.", "warn", "flight", "urgent"),
    ("Yedekleme başarısız oldu.", "", "", "serious"),
    ("Derleme tamamlandı, testler geçti!", "", "", "happy"),
    ("Saat on bir.", "", "", "calm"),
    ("Disk çok dolu", "critical", "pc", "urgent"),
])
def test_pick(text, sev, topic, expected):
    assert pick(text, sev, topic) == expected


def test_for_speech_strips_markdown():
    out = for_speech("**Tamam.** Şuna bak: `x.py` ve [doküman](https://a.b)\n```py\nprint(1)\n```\n- madde")
    assert "**" not in out and "`" not in out and "https" not in out
    assert "Kodu ekrana yazdım" in out and "doküman" in out and "madde" in out


def test_splitter_streams_sentences():
    s = SentenceSplitter()
    out = []
    for ch in "Tamam, hemen bakıyorum. GPU 3.5 derece arttı! Kod:\n```\nx=1. y=2\n```\nBitti.":
        out += s.feed(ch)
    out += s.flush()
    assert out == ["Tamam, hemen bakıyorum.", "GPU üç virgül beş derece arttı!", "Kod:", "Kodu ekrana yazdım.", "Bitti."]


# ----------------------------------------------------------------------- TTS
def test_gemini_tts_request_and_decode():
    seen = {}
    pcm = tone(0.1, 24000).pcm

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["key"] = req.headers["x-goog-api-key"]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})

    eng = GeminiTTS("k", "Kore", client=httpx.Client(transport=httpx.MockTransport(handler)))
    audio = eng.synth("Yakıt az.", "urgent")
    assert audio.rate == 24000 and audio.pcm.startswith(b"\x00\x00") and len(audio.pcm) > len(pcm)
    assert seen["key"] == "k" and "gemini-3.8-flash-lite-tts:generateContent" in seen["url"]
    cfg = seen["body"]["generationConfig"]
    assert cfg["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    # The directive goes under its own heading; the spoken text is only under TRANSCRIPT
    assert prompt.endswith("#### TRANSCRIPT\nYakıt az.") and "DIRECTOR'S NOTES\nStyle: Clear, firm and urgent" in prompt
    assert prompt.index("Do NOT speak them") < prompt.index("#### TRANSCRIPT")

    bad = GeminiTTS("k", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="quota"))))
    with pytest.raises(TTSError):
        bad.synth("x")


class FakeTTS:
    def __init__(self, name, fail=False, delay=0.0):
        self.name = name
        self.fail = fail
        self.delay = delay
        self.calls = []

    def synth(self, text, emotion="calm"):
        self.calls.append((text, emotion))
        time.sleep(self.delay)
        if self.fail:
            raise TTSError("yok")
        return Audio(text.encode("utf-8")[:20].ljust(20, b"\0"), 16000)


def test_tts_router_fallback_and_flight_policy():
    cloud, local = FakeTTS("cloud", fail=True), FakeTTS("local")
    r = TTSRouter(cloud, local, policy=lambda: {})
    r.synth("a")
    assert r.last == "local"
    r.synth("b")
    assert len(cloud.calls) == 1  # cloud is being held back

    cloud2, local2 = FakeTTS("cloud"), FakeTTS("local")
    r2 = TTSRouter(cloud2, local2, prefer="local", policy=lambda: {"local_gpu": False})
    r2.synth("uçuşta")
    assert r2.last == "cloud"
    r3 = TTSRouter(cloud2, local2, prefer="local", policy=lambda: {"local_gpu": True})
    r3.synth("yerde")
    assert r3.last == "local"


def test_emotion_library(tmp_path):
    out, missing = build_library(FakeTTS("gemini:Aoede"), tmp_path / "lib")
    assert missing == []
    lib = EmotionLibrary(tmp_path / "lib")
    assert lib.exists() and lib.source == "gemini:Aoede" and lib.clip("urgent").name == "urgent.wav"
    (out / "urgent.wav").unlink()
    assert lib.clip("urgent").name == "calm.wav"


# ----------------------------------------------------------------------- STT
def test_groq_whisper():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content
        return httpx.Response(200, json={"text": " Hey Alice, saat kaç? "})

    stt = GroqWhisper("g", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert stt.transcribe(tone(0.2, 24000)) == "Hey Alice, saat kaç?"
    assert seen["auth"] == "Bearer g" and b"whisper-large-v3-turbo" in seen["body"] and b'name="language"' in seen["body"]


def test_stt_router_fallback():
    class Bad:
        name = "bad"

        def transcribe(self, a):
            raise STTError("kota")

    class Good:
        name = "good"

        def transcribe(self, a):
            return "merhaba"

    r = STTRouter([Bad(), Good()])
    assert r.transcribe(tone()) == "merhaba" and r.last == "good"
    with pytest.raises(STTError):
        STTRouter([Bad()]).transcribe(tone())


# -------------------------------------------------------------------- listen
def test_strip_wake():
    assert strip_wake("Hey Alice, saat kaç?") == "saat kaç?"
    assert strip_wake("alis bugün hava nasıl") == "bugün hava nasıl"
    assert strip_wake("Alice.") == ""
    assert strip_wake("bugün hava nasıl") is None
    assert strip_wake("Alicem nerede") is None


def frames_of(*parts):
    """30 ms frames built from (seconds, sound present?) pieces."""
    for sec, loud in parts:
        a = tone(sec) if loud else Audio.silence(sec, 16000)
        for i in range(0, len(a.pcm) - 959, 960):
            yield a.pcm[i:i + 960]


def test_utterances_split_and_barge_in_callback():
    vad = VAD()
    vad._vad = None  # energy-based, deterministic
    starts = []
    outs = list(utterances(frames_of((0.5, False), (0.6, True), (1.0, False), (0.4, True), (1.0, False)), vad,
                           on_start=lambda: starts.append(1)))
    assert len(outs) == 2 and len(starts) == 2
    assert 0.5 < outs[0].seconds < 1.2
    # a short click doesn't count as speech
    assert list(utterances(frames_of((0.06, True), (1.0, False)), vad)) == []


# ------------------------------------------------------------------- speaker
class FakePlayer:
    def __init__(self, dur=0.0):
        self.played = []
        self.dur = dur
        self._stop = threading.Event()
        self.available = True

    def play(self, audio):
        self._stop.clear()
        self.played.append(audio.pcm.rstrip(b"\0").decode())
        return not self._stop.wait(self.dur)

    def stop(self):
        self._stop.set()


def test_speaker_keeps_order_and_stops():
    tts, player = FakeTTS("t", delay=0.01), FakePlayer()
    sp = Speaker(tts, player)
    for s in ("bir", "iki", "üç"):
        sp.say(s)
    assert sp.wait(5)
    assert " ".join(player.played) == "bir iki üç"  # queued items are synthesized in one batch, order is preserved
    assert tts.calls[0][1] == "calm"

    slow = FakePlayer(dur=5)
    sp2 = Speaker(FakeTTS("t"), slow)
    sp2.say("uzun cümle")
    for _ in range(100):
        if slow.played:
            break
        time.sleep(0.01)
    sp2.say("ikinci")  # arrived while the first was playing: not spoken once it is cut off
    sp2.stop()
    assert sp2.wait(2) and slow.played == ["uzun cümle"]


# ----------------------------------------------------------------- assistant
class FakeSpeaker:
    def __init__(self):
        self.said = []
        self.speaking = False

    def say(self, text, emotion=None, severity="", topic=""):
        self.said.append(text)
        return True

    def stop(self):
        self.said.append("<stop>")

    def wait(self, timeout=None):
        return True


def test_voice_ui_streams_and_fillers():
    sp = FakeSpeaker()
    ui = VoiceUI(sp)
    ui.begin_turn()
    ui.tool_start("1", "metrics", "")
    for ch in "Dün en yüksek 78 dereceydi. Gayet normal.":
        ui.stream_delta(ch)
    ui.stream_end("Dün en yüksek 78 dereceydi. Gayet normal.", "x")
    assert sp.said[0] in ("Hemen bakıyorum.", "Bir saniye, bakıyorum.", "Tamam, bakıyorum.")
    assert sp.said[1:] == ["Dün en yüksek yetmiş sekiz dereceydi.", "Gayet normal."]
    ui.mute()
    ui.stream_end("söylenmemeli", "x")
    assert "söylenmemeli" not in sp.said


class FakeAgent:
    def __init__(self):
        self.ui = None
        self.turns = []
        self.stopped = 0

    def run_turn(self, text):
        assert "[now: " in text  # the time is added to every sentence
        self.turns.append(text.split("\n\n[now: ")[0])
        self.ui.stream_delta("Tamam, hallettim.")
        self.ui.stream_end("Tamam, hallettim.", "x")
        return "ok"

    def stop(self):
        self.stopped += 1


def wait_for(cond, t=3):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_assistant_wake_gating_and_follow_up():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None, follow_up=0.5)
    va.handle_text("bugün hava nasıl", direct=False)          # no wake word -> ignored
    time.sleep(0.1)
    assert agent.turns == []
    va.handle_text("Hey Alice, GPU kaç derece?", direct=False)
    assert wait_for(lambda: agent.turns == ["GPU kaç derece?"])
    assert wait_for(lambda: "Tamam, hallettim." in sp.said)
    # no wake word needed right after a reply
    assert wait_for(lambda: va.armed_until > time.monotonic())
    va.handle_text("peki ya dün?", direct=False)
    assert wait_for(lambda: agent.turns[-1] == "peki ya dün?")
    va.handle_text("Alice", direct=True)
    assert any(a in sp.said for a in ACKS)


def test_assistant_yes_no():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None)
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("ok", va.ask_yes_no("Silinsin mi?", timeout=3)))
    t.start()
    assert wait_for(lambda: va._awaiting_answer)
    va.handle_text("evet sil", direct=False)
    t.join(3)
    assert res["ok"] is True and agent.turns == []


# ------------------------------------------------------------------ audition
def test_audition_page(tmp_path):
    engines = [FakeTTS("gemini:Aoede"), FakeTTS("xtts", fail=True)]
    page = run_audition(engines, ["calm", "urgent"], out=tmp_path)
    html = page.read_text(encoding="utf-8")
    assert (tmp_path / "gemini_Aoede_calm.wav").exists()
    assert "alice voice use gemini:Aoede" in html and "audio" in html and "yok" in html
    n = len(engines[0].calls)
    run_audition(engines[:1], ["calm", "urgent"], out=tmp_path)
    assert len(engines[0].calls) == n  # existing ones are not regenerated


GEMINI_429 = json.dumps({"error": {"code": 429, "message": "Quota exceeded... Please retry in 41.9s.", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "3"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}})


def test_gemini_quota_parsing():
    from alice.voice.tts import QuotaError, gemini_quota_error

    q = gemini_quota_error(GEMINI_429)
    assert isinstance(q, QuotaError) and q.retry_after == 41 and not q.daily and "per-minute" in str(q)
    d = gemini_quota_error(GEMINI_429.replace("PerMinute", "PerDay"))
    assert d.daily and "daily" in str(d)
    eng = GeminiTTS("k", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, text=GEMINI_429))))
    with pytest.raises(QuotaError):
        eng.synth("x")


def test_audition_waits_on_minute_quota_and_stops_on_daily(tmp_path):
    from alice.voice.tts import QuotaError

    class Flaky(FakeTTS):
        def __init__(self, name, errors):
            super().__init__(name)
            self.errors = list(errors)

        def synth(self, text, emotion="calm"):
            if self.errors:
                raise self.errors.pop(0)
            return super().synth(text, emotion)

    waits = []
    eng = Flaky("gemini:Kore", [QuotaError("dk", 30, False)])
    run_audition([eng], ["calm", "urgent"], out=tmp_path, sleep=waits.append)
    assert waits == [31] and (tmp_path / "gemini_Kore_calm.wav").exists() and (tmp_path / "gemini_Kore_urgent.wav").exists()

    a = Flaky("gemini:Leda", [QuotaError("gün", None, True)])
    b = FakeTTS("gemini:Aoede")
    run_audition([a, b], ["calm", "happy"], out=tmp_path / "2", sleep=waits.append)
    assert len(a.calls) == 0 and b.calls == []  # daily quota: no request is sent to gemini's remaining models
    assert len(waits) == 1


def test_gemini_rotates_models_on_quota():
    from alice.voice.tts import QuotaError

    pcm = tone(0.05, 24000).pcm
    daily = GEMINI_429.replace("PerMinute", "PerDay")
    plan = {"m1": daily, "m2": "404", "m3": "ok"}
    hits = []

    def handler(req):
        model = req.url.path.split("/")[-1].split(":")[0]
        hits.append(model)
        r = plan[model]
        if r == "404":
            return httpx.Response(404, text="not found")
        if r == "ok":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
                "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})
        return httpx.Response(429, text=r)

    eng = GeminiTTS("k", "Aoede", ["m1", "m2", "m3"], client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert eng.synth("a").rate == 24000 and eng.last_model == "m3" and hits == ["m1", "m2", "m3"]
    eng.synth("b")
    assert hits[3:] == ["m3"]  # exhausted and missing models are skipped

    plan["m3"] = GEMINI_429  # per-minute
    with pytest.raises(QuotaError) as ei:
        eng.synth("c")
    assert not ei.value.daily and ei.value.retry_after
    eng.down["m3"] = 0
    plan["m3"] = daily
    with pytest.raises(QuotaError) as ei:
        eng.synth("d")
    assert ei.value.daily


def test_speech_cache(tmp_path):
    from alice.voice.tts import SpeechCache

    cloud = FakeTTS("cloud")
    r = TTSRouter(cloud, None, policy=lambda: {}, cache=SpeechCache(tmp_path))
    a = r.synth("Hemen bakıyorum.", "calm")
    b = r.synth("Hemen bakıyorum.", "calm")
    assert a.pcm == b.pcm and len(cloud.calls) == 1 and r.last.endswith("(cache)")
    r.synth("Hemen bakıyorum.", "urgent")
    r.synth("uzun " * 40, "calm")
    r.synth("uzun " * 40, "calm")
    assert len(cloud.calls) == 4  # a different tone and a long sentence are not cached


def test_polish_removes_click_and_noisy_tail():
    import array as arr

    rate = 24000
    body = tone(0.4, rate, amp=0.5)
    # light but non-silent crackle at the end (~2%), and a signal that cuts off abruptly
    noise = arr.array("h", [((i * 7919) % 1300) - 650 for i in range(int(0.3 * rate))]).tobytes()
    out = Audio(body.pcm + noise, rate).polish()
    a = arr.array("h", out.pcm)
    speech_end = int(0.4 * rate)
    assert abs(a[0]) <= 1                                         # no click at the start
    nz = max(i for i, x in enumerate(a) if x != 0)
    assert nz < speech_end + int(0.08 * rate)                     # most of the crackling tail is cut
    assert all(abs(x) < 400 for x in a[nz - 10:nz + 1])           # ramps down to zero toward the end (no click)
    assert len(a) - nz >= int(0.14 * rate)                        # there is silence at the end


def test_leak_detection_and_format_fallback(tmp_path):
    from alice.voice.tts import leaked

    assert leaked("Style: serious, composed. Yedekleme yarıda kaldı.", "Yedekleme yarıda kaldı.")
    assert leaked("Ciddi bir sesle söyle yedekleme yarıda kaldı hedef diskte yer yok", "Yedekleme yarıda kaldı.")
    assert not leaked("Yedekleme yarıda kaldı.", "Yedekleme yarıda kaldı.")
    assert not leaked("yedeklemeler yarıda kaldı", "Yedekleme yarıda kaldı.")  # minor Whisper difference

    pcm = tone(0.2, 24000).pcm
    prompts = []

    def handler(req):
        prompts.append(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})

    seen = []
    verify = lambda audio, text: (seen.append(prompts[-1]) or not prompts[-1].startswith("Synthesize"))  # noqa: E731
    f = tmp_path / "fmt.json"
    eng = GeminiTTS("k", "Kore", ["m1"], client=httpx.Client(transport=httpx.MockTransport(handler)),
                    verify=verify, formats_file=f)
    eng.synth("Yedekleme yarıda kaldı.", "serious")
    assert eng.last_format == "tag" and prompts[1] == "[serious, composed] Yedekleme yarıda kaldı."
    assert json.loads(f.read_text(encoding="utf-8")) == {"m1": ["notes"]}
    eng2 = GeminiTTS("k", "Kore", ["m1"], client=httpx.Client(transport=httpx.MockTransport(handler)), formats_file=f)
    eng2.synth("Merhaba.", "calm")
    assert eng2.last_format == "tag" and prompts[-1].startswith("[calm")  # saved good format, single request


def test_gemini_too_long_audio_counts_as_leak(tmp_path):
    """Even without a verifier: audio too long for the text (it read the instruction) is dropped; try next format."""
    prompts = []

    def handler(req):
        p = json.loads(req.content)["contents"][0]["parts"][0]["text"]
        prompts.append(p)
        pcm = tone(16.0 if p.startswith("Synthesize") else 2.0, 24000).pcm
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})

    f = tmp_path / "fmt.json"
    eng = GeminiTTS("k", "Kore", ["m1"], client=httpx.Client(transport=httpx.MockTransport(handler)), formats_file=f)
    audio = eng.synth("Söyle bakalım ne yapacağız?")
    assert eng.last_format == "tag" and audio.seconds < 5
    assert json.loads(f.read_text(encoding="utf-8")) == {"m1": ["notes"]}


def test_gemini_timeout_moves_to_next_model():
    pcm = tone(0.1, 24000).pcm

    def handler(req):
        if "m1" in req.url.path:
            raise httpx.ReadTimeout("yavaş", request=req)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})

    eng = GeminiTTS("k", "Kore", ["m1", "m2"], client=httpx.Client(transport=httpx.MockTransport(handler)))
    eng.synth("Merhaba.")
    assert eng.last_model == "m2" and eng.down["m1"] > 0


def test_list_models_and_status_detail():
    from alice.voice.tts import QuotaError, list_gemini_tts_models

    pages = {"": {"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/gemini-2.5-flash-preview-tts"}],
                  "nextPageToken": "p2"},
             "p2": {"models": [{"name": "models/gemini-3.8-flash-tts"}]}}
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken", "")])))
    assert list_gemini_tts_models("k", c) == ["gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts"]

    daily = GEMINI_429.replace("PerMinute", "PerDay")
    eng = GeminiTTS("k", "Kore", ["yok", "dolu"], client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, text="nf") if "yok" in r.url.path else httpx.Response(429, text=daily))))
    with pytest.raises(QuotaError) as ei:
        eng.synth("x")
    assert "yok: not found" in str(ei.value) and "dolu: daily quota" in str(ei.value)


FAKE_TORCH = """
class cuda:
    @staticmethod
    def is_available():
        return False
    @staticmethod
    def empty_cache():
        pass
"""
FAKE_CHATTERBOX = """
import numpy as np
LOADS = []
class ChatterboxMultilingualTTS:
    sr = 24000
    @classmethod
    def from_pretrained(cls, device):
        LOADS.append(device)
        m = cls(); m.device = device
        return m
    def generate(self, text, language_id="en", audio_prompt_path=None, exaggeration=0.5, cfg_weight=0.5):
        assert language_id == "tr" and text.endswith((".", "!", "?"))
        n = int(0.15 * len(text) * self.sr)
        t = np.arange(n) / self.sr
        return (0.3 * np.sin(2 * np.pi * (220 + 400 * exaggeration) * t)).astype("float32")
"""


def test_chatterbox_local_server_end_to_end(tmp_path, monkeypatch):
    """Start the real tts_server.py process with fake chatterbox/torch modules and talk to it."""
    import socket
    import sys

    pytest.importorskip("numpy")
    from alice.voice.tts import Chatterbox, EmotionLibrary

    fake = tmp_path / "fake"
    (fake / "chatterbox").mkdir(parents=True)
    (fake / "chatterbox" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "chatterbox" / "mtl_tts.py").write_text(FAKE_CHATTERBOX, encoding="utf-8")
    (fake / "torch.py").write_text(FAKE_TORCH, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(fake))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    lib = tmp_path / "lib"
    lib.mkdir()
    eng = Chatterbox(sys.executable, port, library=EmotionLibrary(lib), allow_gpu=lambda: True, start_timeout=30)
    try:
        a = eng.synth("Merhaba, ben ALICE.", "urgent")
        assert a.rate == 24000 and a.seconds > 0.5
        assert eng.healthy()
        b = eng.synth("Tekrar.", "calm")  # same server, no restart
        assert b.seconds > 0.2 and eng._proc.poll() is None
    finally:
        if eng._proc is not None:
            eng._proc.kill()
            eng._proc.wait(5)


def test_chatterbox_not_installed():
    from alice.voice.tts import Chatterbox, TTSError

    with pytest.raises(TTSError, match="install-local"):
        Chatterbox("", 1).synth("x")


def test_install_local_flow(tmp_path, monkeypatch):
    from alice.voice import install_local as il

    calls = []
    monkeypatch.setattr(il, "find_python", lambda: (["py", "-3.11"], "3.11"))
    monkeypatch.setattr(il, "has_nvidia", lambda: True)
    outs = {"torch": "2.6.0+cpu  False", "torchaudio": "2.6.0", "chatterbox": "ok"}

    def fake_out(cmd):
        code = cmd[-1]
        if "import torch;" in code:
            return outs["torch"]
        if "torchaudio" in code:
            return outs["torchaudio"]
        if "chatterbox" in code:
            return outs["chatterbox"]
        return None

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["-m", "venv"] or "venv" in cmd:
            py = il.venv_python(tmp_path / "v")
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(il, "_out", fake_out)
    monkeypatch.setattr(il, "venv_dir", lambda: tmp_path / "v")
    py = il.install(emit=lambda s: None, run=fake_run)
    flat = [" ".join(c) for c in calls]
    assert any("install chatterbox-tts" in c for c in flat)
    assert any("torch==2.6.0" in c and "whl/cu126" in c for c in flat)
    from alice.config import load_config

    v = load_config().data["voice"]
    assert v["tts_local"] == "chatterbox" and v["local_python"] == str(py)


def test_trim_by_words_cuts_hallucinated_tail():
    from alice.voice.trim import by_words

    audio = tone(4.0, 16000)
    words = [("Merhaba,", 0.0, 0.5), ("ben", 0.6, 0.8), ("ALICE.", 0.9, 1.4), ("ve", 2.0, 2.2), ("şey", 2.3, 2.6)]
    out = by_words(audio, words, "Merhaba, ben ALICE.")
    assert abs(out.seconds - 1.55) < 0.01
    # even if the last word repeats in the hallucinated tail, it is cut at the first real end
    words2 = [("Yakıt", 0.0, 0.4), ("az.", 0.5, 0.9), ("az", 1.5, 1.8), ("az", 2.0, 2.3)]
    assert abs(by_words(audio, words2, "Yakıt az.").seconds - 1.05) < 0.01
    assert by_words(audio, [("alakasız", 0, 1)], "Merhaba ben ALICE") is None


def test_trim_by_length_drops_late_segments():
    from alice.voice.trim import by_length

    speech = tone(1.0, 16000) + Audio.silence(0.6, 16000) + tone(1.5, 16000)  # the second segment is hallucinated
    out = by_length(speech, "Merhaba, ben ALICE.")  # ~1.7 + 0.8 s expected -> second segment starts at 1.6
    assert abs(out.seconds - speech.seconds) < 0.01  # kept because it starts within the expected duration
    late = tone(1.0, 16000) + Audio.silence(2.0, 16000) + tone(1.0, 16000)
    out2 = by_length(late, "Merhaba, ben ALICE.")
    assert out2.seconds < 1.2


def test_make_trimmer_falls_back(monkeypatch):
    from alice.voice.trim import make_trimmer

    class Bad:
        engines = [type("E", (), {"words": lambda self, a: (_ for _ in ()).throw(STTError("x"))})()]

    late = tone(1.0, 16000) + Audio.silence(2.0, 16000) + tone(1.0, 16000)
    assert make_trimmer(Bad())(late, "Merhaba.").seconds < 1.2
    assert make_trimmer(None)(late, "Merhaba.").seconds < 1.2


def test_groq_words_request():
    seen = {}

    def handler(req):
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "Merhaba", "words": [{"word": "Merhaba", "start": 0.1, "end": 0.6}]})

    stt = GroqWhisper("g", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert stt.words(tone(0.5)) == [("Merhaba", 0.1, 0.6)]
    assert b"verbose_json" in seen["body"] and b"timestamp_granularities[]" in seen["body"]


FAKE_WHISPER = """
CALLS = []
class _M:
    def transcribe(self, audio, language=None, word_timestamps=False, **kw):
        CALLS.append(len(audio))
        dur = len(audio) / 16000
        if len(CALLS) == 1:
            # ilk deneme: model saçmaladı (istenen kelimeler yok)
            return {"segments": [{"words": [{"word": "alakasız", "start": 0.0, "end": 0.5},
                                            {"word": "şeyler", "start": 0.6, "end": 1.0},
                                            {"word": "söyledi", "start": 1.1, "end": 1.5}]}]}
        # ikinci deneme: doğru cümle + sonda uydurma kuyruk
        return {"segments": [{"words": [{"word": "Yakıt", "start": 0.0, "end": 0.4},
                                        {"word": "az,", "start": 0.5, "end": 0.8},
                                        {"word": "eve", "start": 0.9, "end": 1.1},
                                        {"word": "dön.", "start": 1.2, "end": 1.5},
                                        {"word": "hmm", "start": dur - 0.3, "end": dur - 0.1}]}]}
def load_model(name, device=None):
    return _M()
"""


def test_local_server_whisper_trims_and_retries(tmp_path, monkeypatch):
    import socket
    import sys

    pytest.importorskip("numpy")
    from alice.voice.tts import Chatterbox, EmotionLibrary

    fake = tmp_path / "fake"
    (fake / "chatterbox").mkdir(parents=True)
    (fake / "chatterbox" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "chatterbox" / "mtl_tts.py").write_text(FAKE_CHATTERBOX, encoding="utf-8")
    (fake / "torch.py").write_text(FAKE_TORCH, encoding="utf-8")
    (fake / "whisper.py").write_text(FAKE_WHISPER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(fake))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    lib = tmp_path / "lib"
    lib.mkdir()
    eng = Chatterbox(sys.executable, port, library=EmotionLibrary(lib), start_timeout=30,
                     trim=lambda a, t: (_ for _ in ()).throw(AssertionError("sunucu kesti, istemci kesmemeli")))
    try:
        # the fake model produces 0.15 s per character (~2.7 s); Whisper hears "dön" at 1.5 s
        a = eng.synth("Yakıt az, eve dön.", "urgent")
        assert float(eng.last_quality) >= 0.85                 # the second attempt was selected
        assert a.seconds < 1.5 + 0.15 + 0.06 + 0.15 + 0.05      # everything after "dön" was cut (+fade/gap)
    finally:
        if eng._proc is not None:
            eng._proc.kill()
            eng._proc.wait(5)


def test_server_assess():
    import importlib.util
    from pathlib import Path

    import alice.voice.tts as t

    spec = importlib.util.spec_from_file_location("tts_server_mod", Path(t.__file__).with_name("tts_server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    q, end = srv.assess([("Merhaba,", 0, 0.5), ("ben", 0.6, 0.8), ("ALICE.", 0.9, 1.3), ("bla", 2, 2.5),
                         ("bla", 2.6, 3)], "Merhaba, ben ALICE.")
    assert end == 1.3 and 0.5 < q < 1.0
    q2, end2 = srv.assess([("tamamen", 0, 1), ("başka", 1, 2)], "Merhaba, ben ALICE.")
    assert q2 == 0 and end2 is None


def test_library_resumes_after_quota(tmp_path):
    from alice.voice.tts import QuotaError

    class Limited(FakeTTS):
        def __init__(self, name, budget):
            super().__init__(name)
            self.budget = budget

        def synth(self, text, emotion="calm"):
            if self.budget <= 0:
                raise QuotaError("günlük", None, True)
            self.budget -= 1
            return super().synth(text, emotion)

    lib = tmp_path / "lib"
    _, missing = build_library(Limited("gemini:Kore", 4), lib)
    assert missing == ["playful"] and len(list(lib.glob("*.wav"))) == 4
    e = Limited("gemini:Kore", 5)
    _, missing = build_library(e, lib)
    assert missing == [] and e.calls == [(e.calls[0][0], "playful")]   # only the missing one
    e2 = Limited("gemini:Aoede", 10)
    build_library(e2, lib)                                               # voice changed: regenerate everything
    assert len(e2.calls) == 5 and EmotionLibrary(lib).source == "gemini:Aoede"


def test_strip_wake_turkish_variants():
    for heard in ("Hey Alis, saat kaç?", "Ey Alıs saat kaç?", "Hey Ellis saat kaç", "Heyalis saat kaç?",
                  "ee hey Alice saat kaç?", "Hey Ali, saat kaç?", "Hay Alize saat kaç", "Hey Alis' saat kaç?"):
        assert strip_wake(heard) == "saat kaç?".rstrip("?") or strip_wake(heard) == "saat kaç?", heard
    assert strip_wake("Ali bugün geldi") is None
    assert strip_wake("bugün hava nasıl") is None
    assert strip_wake("hey bakar mısın") is None
    assert strip_wake("Alice.") == ""


def test_local_wake_gate_keeps_non_wake_audio_off_the_cloud():
    class Gate:
        def __init__(self, text):
            self.text = text

        def heard(self, audio):
            return self.text

    class CloudSTT:
        def __init__(self):
            self.calls = 0
            self.wake_gate = None

        def transcribe(self, audio):
            self.calls += 1
            return "Hey Alis, saat kaç?"

    agent, sp = FakeAgent(), FakeSpeaker()
    stt = CloudSTT()
    va = VoiceAssistant(agent, sp, stt=stt)
    audio = Audio(b"\0\0" * 16000, 16000)
    stt.wake_gate = Gate("bugün hava güzel")
    va.handle_audio(audio, direct=False)
    assert stt.calls == 0 and agent.turns == []
    stt.wake_gate = Gate("Hey Alice saat")
    va.handle_audio(audio, direct=False)
    assert stt.calls == 1
    assert wait_for(lambda: agent.turns == ["saat kaç?"])
    stt.wake_gate = Gate(None)
    va.handle_audio(audio, direct=False)  # if there is no local model, it goes to the cloud as before
    assert stt.calls == 2


def test_local_voice_before_edge_backup():
    from alice.voice.audio import Audio
    from alice.voice.tts import QuotaError, TTSError, TTSRouter

    class E:
        def __init__(self, name, fail=None):
            self.name, self.fail, self.calls = name, fail, 0

        def synth(self, text, emotion="calm"):
            self.calls += 1
            if self.fail:
                raise self.fail
            return Audio(b"\0\0" * 10, 24000)

    cloud, edge, local = E("gemini", QuotaError("kota", None, True)), E("edge"), E("chatterbox")
    r = TTSRouter(cloud, local, policy=dict, backup=edge)
    r.synth("merhaba")
    assert (cloud.calls, local.calls, edge.calls) == (1, 1, 0) and r.last == "chatterbox"
    local.fail = TTSError("zaman aşımı")
    r.synth("nasılsın")
    assert edge.calls == 1 and r.last == "edge"


def test_decode_mp3_roundtrip():
    import io
    import math

    av = pytest.importorskip("av")
    from alice.voice.tts import decode_mp3

    buf = io.BytesIO()
    with av.open(buf, "w", format="mp3") as box:
        st = box.add_stream("mp3", rate=24000, layout="mono")
        import array

        samples = array.array("h", (int(8000 * math.sin(i / 10)) for i in range(24000)))
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(samples))
        frame.planes[0].update(samples.tobytes())
        frame.sample_rate = 24000
        for p in st.encode(frame):
            box.mux(p)
        for p in st.encode(None):
            box.mux(p)
    pcm = decode_mp3(buf.getvalue(), 24000)
    assert 0.9 < len(pcm) / 2 / 24000 < 1.2


def test_tts_server_chunks_and_token_cap():
    import importlib.util
    from pathlib import Path

    import alice.voice

    path = Path(alice.voice.__file__).with_name("tts_server.py")
    spec = importlib.util.spec_from_file_location("alice_tts_server_test", path)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert srv.chunks("Merhaba. Bugün hava çok güzel, dışarı çıkalım mı?") == [
        "Merhaba. Bugün hava çok güzel, dışarı çıkalım mı?"]
    long = "Birinci uzun cümle burada biraz daha uzuyor ve devam ediyor. " * 6
    parts = srv.chunks(long.strip())
    assert len(parts) > 1 and all(len(p) <= 160 for p in parts)
    assert srv.max_tokens_for("Saat on dokuz.") < 200 and srv.max_tokens_for("x" * 5000) == 1000

    calls = []

    class T3:
        def inference(self, *a, **kw):
            calls.append(kw["max_new_tokens"])

    class M:
        t3 = T3()

    m = M()
    srv.cap_tokens(m)
    srv.TOKENS["max"] = 150
    m.t3.inference(max_new_tokens=1000)
    m.t3.inference()
    assert calls == [150, 150]


def test_spoken_time_turkish():
    from alice.voice.assistant import spoken_time, with_clock

    assert spoken_time(1, 13) == "saat biri on üç geçiyor"
    assert spoken_time(0, 47) == "saat bire on üç var"
    assert spoken_time(14, 15) == "saat ikiyi çeyrek geçiyor"
    assert spoken_time(9, 45) == "saat ona çeyrek var"
    assert spoken_time(13, 30) == "saat bir buçuk"
    assert spoken_time(12, 0) == "saat tam on iki"
    assert spoken_time(23, 59) == "saat on ikiye bir var"
    assert "spoken: saat" in with_clock("saat kaç?")


def test_whisper_hallucinations_filtered():
    from alice.voice.stt import is_hallucination

    for t in ("Altyazı M.K.", "İzlediğiniz için teşekkürler.", "Abone olmayı unutmayın!", "", " . ", "DCS, F-16"):
        assert is_hallucination(t), t
    for t in ("YouTube'u aç", "Hey Alice", "saat kaç?", "Alice hava nasıl"):
        assert not is_hallucination(t), t


def test_trim_history_keeps_last_turns_whole():
    from alice.voice.assistant import trim_history

    msgs = [{"role": "system", "content": "s"}]
    for i in range(6):
        msgs += [{"role": "user", "content": f"u{i}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": "r"},
                 {"role": "assistant", "content": f"a{i}"}]
    out = trim_history(msgs, 2)
    assert out[0]["content"] == "s" and out[1]["content"] == "u4" and len(out) == 9
    assert trim_history(out, 4) == out


class FakeLive:
    available = True

    def __init__(self, fail=False):
        self.fail = fail
        self.texts = []
        self.prefetched = self.discarded = 0

    def prefetch(self, owner=None):
        self.prefetched += 1

    def discard(self, owner=None):
        self.discarded += 1

    def converse(self, text, frames, stop):
        from alice.voice.live import LiveError

        self.texts.append(text)
        if self.fail:
            raise LiveError("kota")


def test_assistant_uses_live_and_falls_back():
    agent, sp = FakeAgent(), FakeSpeaker()
    live = FakeLive()
    va = VoiceAssistant(agent, sp, stt=None, live=live)
    va.handle_text("Hey Alice, YouTube'u aç", direct=False)
    assert wait_for(lambda: live.texts == ["YouTube'u aç"])
    va.handle_text("Hey Alice", direct=False)                  # wake word only: the live conversation opens empty
    assert wait_for(lambda: live.texts == ["YouTube'u aç", ""])
    assert agent.turns == []

    agent2, live2 = FakeAgent(), FakeLive(fail=True)
    va2 = VoiceAssistant(agent2, FakeSpeaker(), stt=None, live=live2)
    va2.handle_text("Hey Alice, saat kaç?", direct=False)
    assert wait_for(lambda: agent2.turns == ["saat kaç?"])   # Live could not be opened -> old path
    assert not va2._live_ready()


def test_live_rate_limit_backs_off_for_an_hour_with_one_turkish_message():
    import time

    class LimitedLive(FakeLive):
        def converse(self, text, frames, stop):
            from alice.voice.live import LiveError

            raise LiveError("gemini-live: You have reached a rate limit. Set up billing to increase your limits.")

    events = []
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=None, live=LimitedLive(), emit=events.append)
    assert va._converse_live("merhaba") is False
    assert va._live_down - time.monotonic() > 3000
    assert any("quota is used up" in e for e in events)
    assert not any("rate limit" in e for e in events)  # the raw English error is not shown


def test_strip_wake_split_ali_spelling():
    from alice.voice.listen import strip_wake

    assert strip_wake("Ali C'e hava nasıl?") == "hava nasıl?"
    assert strip_wake("Hey Ali J.") == ""
    assert strip_wake("Ali bugün geldi") is None


def test_live_wake_sends_audio_without_cloud_stt():
    class Gate:
        model_name, last_seconds = "small", 0.1

        def heard(self, audio):
            return "Hey Alice"

    class CloudSTT:
        def __init__(self):
            self.calls = 0
            self.wake_gate = Gate()

        def transcribe(self, audio):
            self.calls += 1
            return "Hey Alice, saat kaç?"

    class AudioLive(FakeLive):
        def converse(self, text, frames, stop, first_audio=b"", **kw):
            self.texts.append((text, len(first_audio)))

    agent, stt, live = FakeAgent(), CloudSTT(), AudioLive()
    va = VoiceAssistant(agent, FakeSpeaker(), stt=stt, live=live)
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)
    assert wait_for(lambda: live.texts == [("", 32000)])
    assert stt.calls == 0 and agent.turns == [] and live.prefetched == 1 and live.discarded == 0


def test_wake_gate_falls_back_to_cpu_when_cuda_runtime_fails(monkeypatch):
    import sys
    import types

    from alice.voice import stt

    class Model:
        def __init__(self, name, device, compute_type, **k):
            self.device = device

        def transcribe(self, *a, **k):
            if self.device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found")
            return iter([]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    gate = stt.WakeGate("base", "tr", gpu_model="small", allow_gpu=lambda: True)
    m = gate._load()
    assert m.device == "cpu" and gate.device == "cpu" and gate.model_name == "base"


def test_mic_health_warns_once_on_digital_silence(monkeypatch):
    from alice.voice import assistant

    clock = [100.0]
    monkeypatch.setattr(assistant.time, "monotonic", lambda: clock[0])
    sp = FakeSpeaker()
    va = VoiceAssistant(FakeAgent(), sp, stt=None)
    va._mic_health(b"\0\0" * 480)
    clock[0] += 16
    va._mic_health(b"\0\0" * 480)
    va._mic_health(b"\0\0" * 480)
    assert sum("No sound is coming from your microphone" in s for s in sp.said) == 1
    va._mic_health(b"\1\0" + b"\0\0" * 479)
    assert va._mic_warned is False


def test_mic_auto_gain_boosts_quiet_speech():
    import array

    from alice.voice.audio import Microphone

    mic = Microphone.__new__(Microphone)
    mic.gain, mic._peak, mic.auto_gain = 1.0, 0.0, True
    quiet = array.array("h", [600, -600] * 240).tobytes()  # peak ~0.018
    for _ in range(200):
        out = mic._apply_gain(quiet)
    peak = max(array.array("h", out)) / 32768
    assert 0.2 < peak < 0.4 and mic.gain > 10
    loud = array.array("h", [20000, -20000] * 240).tobytes()
    for _ in range(200):
        mic._apply_gain(loud)
    assert mic.gain < 1.5


def test_wasapi_twin_finds_same_mic():
    from alice.voice.audio import wasapi_twin

    class SD:
        class default:
            device = (1, 3)

        @staticmethod
        def query_hostapis():
            return [{"name": "MME"}, {"name": "Windows WASAPI"}]

        @staticmethod
        def query_devices(i=None, kind=None):
            devs = [{"name": "Microsoft Ses Eşleyicisi", "hostapi": 0, "max_input_channels": 2},
                    {"name": "Mikrofon (Razer BlackShark V2 H", "hostapi": 0, "max_input_channels": 1},
                    {"name": "Mikrofon (Razer BlackShark V2 HS 2.4)", "hostapi": 1, "max_input_channels": 1}]
            return devs if i is None else devs[i]

    assert wasapi_twin(SD) == 2


def test_strip_wake_greetings_and_name_in_first_words():
    from alice.voice.listen import strip_wake

    assert strip_wake("Merhaba Alice nasılsın?") == "nasılsın?"
    assert strip_wake("Selamlar Alis, saat kaç") == "saat kaç"
    assert strip_wake("Bak şimdi Alice YouTube'u aç") == "YouTube'u aç"
    assert strip_wake("Merhaba nasılsın?") is None
    assert strip_wake("Ali bana bir çay getir") is None
    assert strip_wake("dün akşam yemekte Alice vardı") is None


def test_live_wake_passes_trusted_local_text():
    class Gate:
        model_name, last_seconds = "small", 0.1

        def __init__(self, conf):
            self.conf = conf

        def heard(self, audio):
            return "Hey Alice"

        def transcribe(self, audio, prompt=""):
            assert "GitHub" in prompt
            return "Hey Alice, GitHub'ı açar mısın?", self.conf

    class STT:
        def __init__(self, conf):
            self.wake_gate = Gate(conf)

    class HintLive(FakeLive):
        names = staticmethod(lambda: ["GitHub"])

        def converse(self, text, frames, stop, first_audio=b"", hint=None, **kw):
            self.texts.append(hint())

    for conf, want in ((-0.2, "GitHub'ı açar mısın?"), (-1.5, None)):
        live = HintLive()
        va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=STT(conf), live=live)
        va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)
        assert wait_for(lambda live=live, want=want: live.texts == [want])


def test_vocab_names_from_bookmarks_and_start_menu(tmp_path):
    from alice.voice import vocab

    bm = tmp_path / "Bookmarks"
    bm.write_text(json.dumps({"roots": {"bookmark_bar": {"type": "folder", "children": [
        {"type": "url", "name": "GitHub - Where the world builds software", "url": "https://github.com"},
        {"type": "folder", "children": [{"type": "url", "name": "YouTube Music", "url": "https://music.youtube.com"}]},
    ]}}}), encoding="utf-8")
    sm = tmp_path / "Programs"
    (sm / "Steam").mkdir(parents=True)
    (sm / "Steam" / "Steam.lnk").write_bytes(b"")
    (sm / "Steam" / "Uninstall Steam.lnk").write_bytes(b"")
    (sm / "Windows Tools").mkdir()
    (sm / "Windows Tools" / "Disk Cleanup.lnk").write_bytes(b"")
    assert vocab.bookmark_names([bm]) == ["GitHub", "YouTube Music"]
    assert vocab.start_menu_apps([sm]) == ["Steam"]
    assert vocab.merge([["GitHub"], ["github", "Steam"]]) == ["GitHub", "Steam"]
    assert vocab.whisper_prompt(["GitHub", "Steam"]) == "Hey Alice. GitHub, Steam."


def test_live_follow_up_without_wake_word_after_conversation():
    class Gate:
        model_name, last_seconds = "small", 0.1

        def heard(self, audio):
            return "simgeleri göster"

        def transcribe(self, audio, prompt=""):
            return "Masaüstü simgelerini göster.", -0.2

    class STT:
        wake_gate = Gate()

    class HintLive(FakeLive):
        names = staticmethod(lambda: [])

        def converse(self, text, frames, stop, first_audio=b"", hint=None, **kw):
            self.texts.append(hint())

    live = HintLive()
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=STT(), live=live)
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)
    time.sleep(0.2)
    assert live.texts == []  # no wake without "Alice" when there is no conversation
    va.armed_until = time.monotonic() + 5  # live conversation just ended
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)
    assert wait_for(lambda: live.texts == ["Masaüstü simgelerini göster."])


def test_speech_gate_sends_silence_for_noise_and_preroll_for_speech():
    from alice.voice.listen import SpeechGate

    class FakeVAD:
        def __init__(self):
            self.speech = False

        def is_speech(self, frame):
            return self.speech

    vad = FakeVAD()
    g = SpeechGate(vad, preroll=3, hangover=2, start_frames=2)
    noise = [bytes([i]) * 960 for i in range(1, 6)]
    out = [f for n in noise for f in g.feed(n)]
    assert out == [b"\x00" * 960] * 5  # noise: silence to Live
    vad.speech = True
    assert g.feed(b"a" * 960) == [b"\x00" * 960]  # a single frame is not enough
    sent = g.feed(b"b" * 960)
    assert sent[-1] == b"b" * 960 and b"a" * 960 in sent and noise[-1] in sent  # together with the pre-roll
    vad.speech = False
    assert g.feed(b"c" * 960) == [b"c" * 960]  # stays open briefly after speech ends
    g.feed(b"d" * 960)
    assert g.feed(b"e" * 960) == [b"\x00" * 960]


def _fake_whisper(monkeypatch, loaded):
    import sys
    import types

    class Model:
        def __init__(self, name, device, compute_type, **k):
            self.name, self.device, self.compute_type = name, device, compute_type
            loaded.append((name, device, compute_type))

        def transcribe(self, *a, **k):
            return iter([]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))


def test_wake_gate_moves_off_gpu_in_flight_and_back(monkeypatch):
    import threading

    from alice.voice import stt

    loaded = []
    _fake_whisper(monkeypatch, loaded)
    gpu = [True]
    gate = stt.WakeGate("base", "tr", gpu_model="small", allow_gpu=lambda: gpu[0])
    gate._model = gate._load()
    assert loaded[-1] == ("small", "cuda", "int8_float16")  # half-precision memory on the GPU

    def wait_moves():
        for t in threading.enumerate():
            if t.name == "alice-wake-move":
                t.join(2)

    gpu[0] = False
    gate.sync(False)
    wait_moves()
    assert gate.device == "cpu" and gate._model.device == "cpu" and gate._model.name == "base"
    gate.sync(False)  # already on CPU: no reload
    wait_moves()
    assert len(loaded) == 2
    gpu[0] = True
    gate.sync(True)
    wait_moves()
    assert gate.device == "cuda" and gate._model.name == "small"


def test_local_whisper_trim_drops_gpu_model_in_flight_and_idle_models(monkeypatch):
    from alice.voice import stt

    w = stt.LocalWhisper("large-v3-turbo", "small", "tr")
    w._models = {"cuda": object(), "cpu": object()}
    w.last_used = stt.time.monotonic()
    w.trim(True)
    assert set(w._models) == {"cuda", "cpu"}
    w.trim(False)
    assert set(w._models) == {"cpu"}
    w.last_used = stt.time.monotonic() - 3600
    w.trim(True)
    assert w._models == {}


def test_tts_router_release_gpu_reaches_local_engine():
    from alice.voice.tts import TTSRouter

    class Local:
        name = "chatterbox"
        released = 0

        def release_gpu(self):
            self.released += 1

    local = Local()
    TTSRouter(None, local, policy=lambda: {}).release_gpu()
    TTSRouter(None, None, policy=lambda: {}).release_gpu()  # no problem if there is no local engine
    assert local.released == 1

# --------------------------------------------------- wake accuracy
def test_speech_segments_drop_prompt_echo_noise_and_loops():
    from types import SimpleNamespace as S

    from alice.voice.stt import speech_segments

    keep = S(text="Hey Alice, saat kaç?", no_speech_prob=0.05, avg_logprob=-0.3, compression_ratio=1.1)
    real_wake = S(text="Hey Alice.", no_speech_prob=0.1, avg_logprob=-0.3, compression_ratio=0.9)
    echo = S(text="Hey Alice.", no_speech_prob=0.5, avg_logprob=-0.6, compression_ratio=0.9)
    noise = S(text="Müzik", no_speech_prob=0.8, avg_logprob=-1.1, compression_ratio=1.0)
    loop = S(text="YouTube YouTube YouTube YouTube YouTube YouTube", no_speech_prob=0.1, avg_logprob=-0.2,
             compression_ratio=3.1)
    assert speech_segments([keep, real_wake, echo, noise, loop]) == [keep, real_wake]


def test_vad_ignores_steady_noise_that_webrtcvad_calls_speech():
    import array as arr

    vad = VAD()

    class AlwaysSpeech:
        def is_speech(self, frame, rate):
            return True

    vad._vad = AlwaysSpeech()

    def frame(amp):
        return arr.array("h", [int(amp * 32767 * math.sin(i / 3)) for i in range(480)]).tobytes()

    hum = [vad.is_speech(frame(0.02)) for _ in range(200)]  # 6 s of game/fan noise
    assert sum(hum[-100:]) == 0
    assert vad.is_speech(frame(0.15))  # speech on top of it


def test_strip_wake_ali_only_after_real_hey():
    assert strip_wake("Hey Ali, saat kaç") == "saat kaç"
    assert strip_wake("Selam Ali nasılsın") is None
    assert strip_wake("Merhaba Ali") is None
    assert strip_wake("Selam Aliye") is None
    assert strip_wake("Selam Alice nasılsın") == "nasılsın"


def test_wake_found_later_in_noisy_recording():
    class Gate:
        model_name, last_seconds, HEAD_SECONDS = "small", 0.1, 2.5

        def __init__(self):
            self.calls = []

        def heard(self, audio):
            self.calls.append(audio.seconds)
            return "Hey Alice, saat kaç?" if audio.seconds < 7 else ""

    class STT:
        def __init__(self):
            self.wake_gate = Gate()
            self.heard_audio = []

        def transcribe(self, audio):
            self.heard_audio.append(audio.seconds)
            return "Hey Alice, saat kaç?"

    agent, sp = FakeAgent(), FakeSpeaker()
    stt = STT()
    va = VoiceAssistant(agent, sp, stt=stt)
    va.handle_audio(Audio(b"\1\0" * 16000 * 10, 16000), direct=False)  # 10 s: first 4 s are noise
    assert stt.heard_audio and stt.heard_audio[0] == pytest.approx(6.0)


def test_follow_up_without_wake_rejects_noise_text():
    class Gate:
        model_name, last_seconds, last_conf = "small", 0.1, -0.3

        def __init__(self, text):
            self.text = text

        def heard(self, audio):
            return self.text

    class STT:
        def __init__(self, text):
            self.wake_gate = Gate(text)
            self.calls = 0

        def transcribe(self, audio):
            self.calls += 1
            return self.wake_gate.text

    for text, want in (("Altyazı M.K.", 0), ("hava nasıl", 1)):
        stt = STT(text)
        va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=stt)
        va.armed_until = time.monotonic() + 5
        va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)
        assert stt.calls == want, text


def test_wake_gate_adds_flight_words_in_flight_mode():
    from alice.voice import stt

    class Model:
        def transcribe(self, *a, **k):
            self.prompt = k.get("initial_prompt")
            return iter([]), None

    gpu = [True]
    gate = stt.WakeGate("base", "tr", gpu_model="small", allow_gpu=lambda: gpu[0])
    gate.available = lambda: True
    gate._model = Model()
    audio = Audio(b"\1\0" * 16000, 16000)
    gate.transcribe(audio, "Hey Alice.")
    assert gate._model.prompt == "Hey Alice."
    gpu[0] = False  # DCS is open: flight mode
    gate.transcribe(audio, "Hey Alice.")
    assert "Bingo" in gate._model.prompt


def test_speech_segments_drop_echo_of_custom_prompt():
    from types import SimpleNamespace as S

    from alice.voice.stt import speech_segments

    echo = S(text="Bingo, cannon, waypoint.", no_speech_prob=0.5, avg_logprob=-1.0, compression_ratio=1.0)
    real = S(text="Cannon kaç mermi kaldı?", no_speech_prob=0.05, avg_logprob=-0.3, compression_ratio=1.0)
    assert speech_segments([echo, real], "Hey Alice. Bingo, cannon, waypoint, tanker.") == [real]


# ------------------------------------------------------------------ "Alice sus" and follow-up window
@pytest.mark.parametrize("text, hush", [
    ("Alice sus", True), ("Alice, sus artık.", True), ("sus", True), ("Tamam Alice sessiz ol lütfen", True),
    ("Hey Alice, kapa çeneni!", True), ("sus Alice", True),
    ("susam al", False), ("sus diye bağırdı", False), ("Alice saat kaç", False), ("bugün sessiz olacak mı", False),
])
def test_is_hush(text, hush):
    from alice.voice.listen import is_hush

    assert is_hush(text) is hush


# It used to check an exact-match list and most of these slipped through: ALICE kept answering everything said.
@pytest.mark.parametrize("text", [
    "Alice sus be", "sus dedim", "Alice susar mısın", "sussana", "Alice sussana", "susun", "Susss", "sus sus",
    "Alice sus sus", "alice susmanı istiyorum", "Alice sus artık be", "aşkım sus",
    "yeter", "Alice yeter", "yeter artık", "tamam yeter", "kes", "kes artık", "sesini kes", "Alice sesini kes",
    "şşt", "Şşşt", "konuşma", "Alice konuşma", "artık konuşma", "dur", "Alice dur", "tamam dur",
    "SESSİZ OL",            # capital İ: str.lower turns it into 'i̇' and breaks the match
    "sessiz olur musun", "çeneni kapat", "kapat çeneni",
    "Ali sus",              # Whisper often writes 'Alice' as 'Ali'; it used to be taken for a command and answered
    "tamam Alice teşekkürler", "Alice teşekkür ederim", "bu kadar", "sağ ol", "iyi geceler Alice",
])
def test_hush_turkce_halleri(text):
    from alice.voice.listen import is_hush

    assert is_hush(text), text


@pytest.mark.parametrize("text", [
    "sus diye bağırdı", "susam al", "susamlı simit", "pastayı ikiye kes", "kes kopyala yapıştır", "orada dur",
    "dur bir saniye şunu da ekle", "bu kadar yeter mi", "yeter mi", "konuşmayı kaydet",
    "teşekkürler peki yarın hava nasıl", "bugün sessiz olacak mı", "Alice saat kaç", "Ali'ye mesaj at",
])
def test_hush_yanlis_alarm_vermez(text):
    from alice.voice.listen import is_hush

    assert not is_hush(text), text


def test_ends_with_hush_on_streamed_transcript():
    from alice.voice.listen import ends_with_hush

    assert ends_with_hush("YouTube aç bakalım Alice sus")
    assert not ends_with_hush("olsun bakalım")
    # If it is the whole turn, a weak word also hushes; at the end of a longer sentence, only with the name
    assert ends_with_hush("yeter") and ends_with_hush("tamam anladım Alice yeter")
    assert not ends_with_hush("bence yeter") and not ends_with_hush("pastayı ikiye kes")
    assert ends_with_hush("bir şey diyeceğim sus")  # a strong word hushes at the end too
    assert not ends_with_hush("bu kadar yeter mi")


def test_follow_up_window_her_kelimeye_cevap_vermez():
    """User complaint: after 'sus' it did not go quiet and kept answering everything said in the follow-up window."""
    for hush in ("Alice sus be", "Ali sus", "yeter", "sussana", "SESSİZ OL", "tamam teşekkürler"):
        agent, sp = FakeAgent(), FakeSpeaker()
        va = VoiceAssistant(agent, sp, stt=None, follow_up=5)
        va.handle_text("Hey Alice, GPU kaç derece?", direct=False)
        assert wait_for(lambda va=va: va.armed_until > time.monotonic())
        va.handle_text(hush, direct=False)
        assert va.muted and va.armed_until == 0, hush
        for said in ("peki", "ya dün", "hmm bilmem", "sonra bakarız"):  # room chatter is no longer a command
            va.handle_text(said, direct=False)
        time.sleep(0.1)
        assert agent.turns == ["GPU kaç derece?"], (hush, agent.turns)


def test_strict_wake_needs_name_at_start():
    assert strip_wake("bak şimdi Alice saat kaç") == "saat kaç"
    assert strip_wake("bak şimdi Alice saat kaç", strict=True) is None
    assert strip_wake("Alice saat kaç", strict=True) == "saat kaç"


def test_hush_mutes_until_alice_is_said_again():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None, follow_up=5)
    va.handle_text("Hey Alice, GPU kaç derece?", direct=False)
    assert wait_for(lambda: va.armed_until > time.monotonic())
    va.handle_text("Alice sus", direct=False)
    assert va.muted and va.armed_until == 0 and agent.turns == ["GPU kaç derece?"]
    # while muted there is no window, and "Alice" mid-sentence doesn't wake it either
    va.handle_text("peki ya dün?", direct=False)
    va.handle_text("bak şimdi Alice ne diyecek", direct=False)
    time.sleep(0.1)
    assert agent.turns == ["GPU kaç derece?"] and va.muted
    va.handle_text("Alice, hava nasıl?", direct=False)
    assert wait_for(lambda: agent.turns[-1] == "hava nasıl?")
    assert not va.muted


def test_hush_from_a_friend_while_idle_is_ignored():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None)
    va.handle_text("sus", direct=False)  # no window, Alice is not speaking: said to someone else
    assert not va.muted


def test_push_to_talk_unmutes():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None)
    va._hush()
    va.handle_text("saat kaç", direct=True)
    assert wait_for(lambda: agent.turns == ["saat kaç"]) and not va.muted


def test_follow_up_is_capped_at_five_seconds():
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=None, follow_up=8, live_follow=12)
    assert va.follow_up == 5 and va.live_follow == 5


def test_follow_window_counts_from_speech_start():
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=None)
    now = time.monotonic()
    va.armed_until = now - 1  # the window just closed
    assert va._armed(now - 2)  # the sentence started inside the window: counts
    assert not va._armed(now - 0.5)  # started after the window closed: doesn't count
    va._hush()
    assert not va._armed(now - 2)


def test_live_window_does_not_extend_after_idle_close():
    class FakeLive:
        available = True
        hushed = False
        last_active = 0.0

        def converse(self, text, frames, stop, first_audio=b"", hint=None, **kw):
            self.last_active = time.monotonic() - 5  # closed after 5 s of silence

    live = FakeLive()
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=None, live=live)
    va._converse_live("merhaba")
    assert va.armed_until <= time.monotonic() + 0.1
    live.hushed = True
    va._converse_live("merhaba")
    assert va.muted and va.armed_until == 0


def test_old_girlfriend_personality_is_upgraded_to_assistant(tmp_path, monkeypatch):
    from alice import config

    monkeypatch.setattr(config, "alice_home", lambda: tmp_path)
    (tmp_path / "personality.md").write_text(config.PERSONALITY_TEMPLATE_V3, encoding="utf-8")
    body = config.personality_text()
    assert "kişisel asistanı" in body and "JARVIS" in body and "Mommy öyle dedi" not in body
    assert "JARVIS" in (tmp_path / "personality.md").read_text(encoding="utf-8")
    (tmp_path / "personality.md").write_text("# kendi kişiliğim", encoding="utf-8")
    assert config.personality_text() == "# kendi kişiliğim"  # user-written content is left untouched


def test_speaking_over_alice_counts_as_follow_up():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None)
    sp.speaking = True
    va._on_speech_start()
    assert va._armed(va._speech_at)
    va._hush()
    sp.speaking = True
    va._on_speech_start()
    assert not va._armed(va._speech_at)


# ---- real examples from voice.log: false wakes and repeated stock replies
def test_whisper_echoing_its_prompt_is_noise_not_a_wake():
    from alice.voice.stt import is_hallucination

    for t in ("ALICE, DIGITAL, Bingo, waypoint, kneeboard.", "ALICE, F-16, UNA, Bingo, waypoint, kneeboard.",
              "ALICE, DOR, Bingo, waypoint, kneeboard."):
        assert is_hallucination(t), t
    for t in ("Hey Alice, saat kaç?", "Alice, bingo yakıtım ne kadar kaldı?", "Alice waypoint üçe yön ver",
              "Alice War Thunder aç", "Alice, War Thunder'ı aç", "Alice, bingo ve waypoint söyle"):
        assert not is_hallucination(t), t


def test_name_mid_sentence_needs_a_real_request_to_wake():
    # Friends talking about ALICE (voice.log): it woke up and answered with an empty "Buradayım" (I'm here).
    assert strip_wake("Şuan Alice ya") is None
    assert strip_wake("Gideyse iyi Alice.") is None
    assert strip_wake("Bak şimdi Alice müziği aç") == "müziği aç"
    assert strip_wake("Alice") == "" and strip_wake("Hey Alice, saat kaç?") == "saat kaç?"


def test_bare_wake_ack_varies_and_never_repeats_back_to_back():
    recent: list[str] = []
    said = [pick_ack(recent) for _ in range(40)]
    assert all(a != b for a, b in zip(said, said[1:]))
    assert len(set(said)) >= 4 and "Efendim?" not in said


def test_short_prompt_echo_with_caps_alice_is_noise():
    # Night of 24 October, voice.log: "ALICE, DIGITI.", "ALICE, D'AMO.", "ALICE, DELİK." were opening Live.
    from alice.voice.stt import PROMPT, is_hallucination

    for t in ("ALICE, DIGITI.", "ALICE, D'AMO.", "ALICE, DELİK.", "ALICE, D'UNCAN.", "Hey Alice. ALICE, DCS, F-16"):
        assert is_hallucination(t), t
    for t in ("Alice, dur.", "Hey Alice.", "Alice, müziği aç", "Hey Alice, saat kaç?"):
        assert not is_hallucination(t), t
    assert "alice" not in PROMPT.lower()  # no name in the hint: its echo cannot wake it


def test_flight_callouts_go_stale_and_urgent_jumps_queue(monkeypatch):
    slow = FakePlayer(dur=0.3)
    sp = Speaker(FakeTTS("t"), slow)
    sp.alert_volume = 1.0  # the fake TTS's "sound" carries text: if it is turned down it cannot be read
    monkeypatch.setattr(Speaker, "STALE", {"critical": 5.0, "warn": 5.0, "info": 0.2})
    sp.say("ilk cümle")  # no topic: never goes stale
    for _ in range(100):
        if slow.played:
            break
        time.sleep(0.01)
    sp.say("Düşman saat 2", severity="info", topic="flight")  # cannot play within 0.2 s: dropped
    sp.say("Waypoint 3", severity="info", topic="flight")
    sp.say("Füze! Saat 3", severity="critical", topic="flight")  # drops the unimportant ones queued next
    assert sp.wait(5)
    assert len(slow.played) == 2 and slow.played[1].startswith("Füze")  # enemy/waypoint dropped


def test_no_barge_in_while_gaming_or_on_discord():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None, social=lambda: "War Thunder")
    stopped = []
    va._interrupt = lambda: stopped.append(1)
    sp.speaking = True
    va._on_speech_start()
    assert stopped == []  # a friend's/game's voice does not interrupt ALICE
    va.social = lambda: ""
    va._on_speech_start()
    assert stopped == [1]


def test_mic_closed_to_alice_while_on_discord_unless_ptt():
    agent, sp = FakeAgent(), FakeSpeaker()
    reason = ["Discord sesli konuşmada"]
    va = VoiceAssistant(agent, sp, stt=None, ptt_key="<scroll_lock>", social=lambda: reason[0], social_ptt=True)
    assert va._social_skip()  # social_mode = "ptt": no sentence goes through
    reason[0] = ""
    assert not va._social_skip()
    no_ptt = VoiceAssistant(agent, sp, stt=None, social=lambda: "War Thunder")
    assert not no_ptt._social_skip()  # no key: old behavior: "Alice" must come first
    wake = VoiceAssistant(agent, sp, stt=None, ptt_key="<f13>", social=lambda: "DCS")
    assert not wake._social_skip()  # default: "Alice naber" is heard (the audio trace filters it)


def test_tts_stays_on_one_voice():
    from alice.voice.tts import SpeechCache, TTSRouter

    cloud, local = FakeTTS("cloud"), FakeTTS("local")
    cache = SpeechCache(path=Path(tempfile.mkdtemp()))
    r = TTSRouter(cloud, local, "cloud", policy=lambda: {}, cache=cache)
    cache.put("local", "calm", "merhaba", Audio(b"x" * 20, 16000))  # an old entry from another engine
    r.synth("merhaba")
    assert cloud.calls and r.last == "cloud"  # the local entry is not used: the voice would change
    cloud.fail = True
    r.synth("bir")
    cloud.fail = False
    r.down.clear()
    r.synth("iki")
    assert r.last == "local"  # after falling back, it stays there for a while, no sentence-by-sentence ping-pong


def test_elevenlabs_engine_picks_model_sends_turkish_and_falls_back_on_credits():
    from alice.voice.tts import ElevenLabsTTS, QuotaError, TTSRouter

    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/v1/models":
            return httpx.Response(200, json=[{"model_id": "eleven_multilingual_v2"}, {"model_id": "eleven_v4_turbo"},
                                             {"model_id": "eleven_turbo_v2", "can_do_text_to_speech": True}])
        body = json.loads(req.content)
        if body["text"] == "kredi yok":
            return httpx.Response(401, json={"detail": {"status": "quota_exceeded", "message": "credits"}})
        return httpx.Response(200, content=b"\1\0" * 2400)

    el = ElevenLabsTTS("k", "YxrwjAKoUKULGd0g8K9Y", client=httpx.Client(transport=httpx.MockTransport(handler)))
    a = el.synth("Füze, saat üç!", "urgent")
    assert a.rate == 24000 and len(a.pcm) == 4800
    post = seen[-1]
    body = json.loads(post.content)
    assert post.url.path == "/v1/text-to-speech/YxrwjAKoUKULGd0g8K9Y" and post.url.params["output_format"] == "pcm_24000"
    assert body["model_id"] == "eleven_v4_turbo" and body["language_code"] == "tr"
    assert post.headers["xi-api-key"] == "k" and body["voice_settings"]["stability"] < 0.5  # urgent: livelier
    with pytest.raises(QuotaError):
        el.synth("kredi yok")
    gem = FakeTTS("gemini")
    r = TTSRouter(gem, None, "cloud", policy=lambda: {"local_gpu": False}, premium=el)
    assert r.order()[0] is el  # in flight too, ElevenLabs first
    r.synth("kredi yok")
    assert r.last == "gemini"  # credits run out: it does not go silent


def test_eleven_scribe_first_with_alice_keyterm_and_groq_fallback():
    from alice.voice.stt import ElevenScribe, STTError, STTRouter

    calls = []

    def handler(req):
        calls.append(req)
        body = req.content.decode("latin-1")
        assert 'name="keyterms"' in body and "Alice" in body and 'name="language_code"' in body
        return httpx.Response(200, json={"text": "Alice naber?"})

    sc = ElevenScribe("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    audio = Audio(b"\1\0" * 16000, 16000)
    assert sc.transcribe(audio) == "Alice naber?" and calls[0].headers["xi-api-key"] == "k"
    broke = ElevenScribe("k", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"detail": "quota_exceeded"}))))
    with pytest.raises(STTError):
        broke.transcribe(audio)

    class Groq:
        name = "groq:x"

        def transcribe(self, a):
            return "yedek"

    assert STTRouter([broke, Groq()]).transcribe(audio) == "yedek"  # credits run out: Groq


def test_merged_alice_spellings_from_small_wake_model():
    # in the voice test the small model wrote "Alice naber" like this
    for t in ("Ali Cennaber", "Alisine ber.", "Alisneber", "Alisa naber"):
        assert strip_wake(t, strict=True) is not None, t
    for t in ("Ali Selim nerede", "Ali Cenk geldi", "Alişan geldi", "Ali Can geldi"):  # real names
        assert strip_wake(t, strict=True) is None, t


def test_ali_call_wakes_but_not_talk_to_real_ali():
    # The user's "Alice" reaches the engines as "Ali" (Scribe: "Ali? Ne haber?", real recording)
    assert strip_wake("Ali? Ne haber?", strict=True) == "Ne haber?"
    assert strip_wake("Ali.", strict=True) == ""
    assert strip_wake("Ali, nasılsın?", strict=True) == "nasılsın?"
    for t in ("Selam Ali", "Merhaba Ali", "Ali gel buraya", "Ali bak şimdi", "Ali Selim geldi"):
        assert strip_wake(t, strict=True) is None, t


def test_strip_wake_repeated_hey_in_strict_mode():
    """Live: "Hey Hey Alice." was ignored in Discord (selective mode); a repeated wake word must not push the name aside."""
    assert strip_wake("Hey Hey Alice.", strict=True) == ""
    assert strip_wake("Hey hey Alice, saat kaç?", strict=True) == "saat kaç?"
    assert strip_wake("Hey hey.", strict=True) is None


def test_follow_up_goes_to_realtime_as_audio_without_stt():
    """Follow-up (no "Alice") goes to the realtime model as audio, so STT does not mis-transcribe and send text."""
    class CloudSTT:
        def __init__(self):
            self.calls = 0

        def transcribe(self, audio):
            self.calls += 1
            return "İyi eğitim geçti gönöm"

    class AudioLive(FakeLive):
        prefers_audio = True

        def converse(self, text, frames, stop, first_audio=b"", **kw):
            self.texts.append((text, len(first_audio), kw.get("hint")))

    stt, live = CloudSTT(), AudioLive()
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=stt, live=live)
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=True)
    assert wait_for(lambda: live.texts == [("", 32000, None)])
    assert stt.calls == 0


def test_realtime_first_sentence_does_not_wait_for_local_text():
    class Gate:
        model_name, last_seconds = "small", 0.1

        def heard(self, audio):
            return "Hey Alice"

        calls = 0

        def transcribe(self, audio, prompt=""):  # check whether "Alice" was said (wake check) and the transcript hint
            Gate.calls += 1
            return "Hey Alice", 0.0

    class STT:
        wake_gate = Gate()

        def transcribe(self, audio):
            raise AssertionError("bulut STT çağrılmamalı")

    class AudioLive(FakeLive):
        prefers_audio = True

        def converse(self, text, frames, stop, first_audio=b"", **kw):
            self.texts.append(("hint" in kw, len(first_audio)))

    live = AudioLive()
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=STT(), live=live)
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)
    assert wait_for(lambda: live.texts == [(False, 32000)])  # audio was sent, no text hint
    assert Gate.calls <= 1  # wake check only; no second transcription was made for the hint


def test_alert_volume_scales_only_alerts():
    from alice.voice.speaker import scaled

    loud = Audio(array.array("h", [10000, -10000, 20000]).tobytes(), 16000)
    half = array.array("h")
    half.frombytes(scaled(loud, 0.5).pcm)
    assert list(half) == [5000, -5000, 10000]
    assert scaled(loud, 1.0) is loud
