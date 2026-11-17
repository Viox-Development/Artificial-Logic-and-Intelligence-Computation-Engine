"""Pure logic of alice/voice/stt.py: fake faster-whisper/ctranslate2, httpx MockTransport, fake clock."""

from __future__ import annotations

import importlib.util
import io
import sys
import time
import types
import wave
from types import SimpleNamespace

import httpx
import pytest

from alice.config import Config, default_config_dict
from alice.voice import stt
from alice.voice.audio import Audio
from alice.voice.stt import (PROMPT, GroqWhisper, LocalWhisper, STTError, STTRouter, WakeGate, is_hallucination,
                             make_stt)


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


class Seg:
    def __init__(self, text, logprob=-0.2):
        self.text = text
        self.avg_logprob = logprob


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel: records its instantiations and calls."""

    created: list[tuple[str, str, str]] = []
    fail_devices: set[str] = set()
    segments: list[Seg] = [Seg(" merhaba "), Seg(" dünya ")]
    raise_on_transcribe: Exception | None = None

    def __init__(self, name, device="cpu", compute_type="int8", **kw):
        if device in self.fail_devices:
            raise RuntimeError(f"{device} yok")
        self.name, self.device = name, device
        self.calls: list[dict] = []
        FakeWhisperModel.created.append((name, device, compute_type))

    def transcribe(self, f, **kw):
        data = f.read()
        self.calls.append({"bytes": data, **kw})
        if FakeWhisperModel.raise_on_transcribe is not None:
            raise FakeWhisperModel.raise_on_transcribe
        return iter(list(FakeWhisperModel.segments)), None


@pytest.fixture
def fake_fw(monkeypatch):
    FakeWhisperModel.created = []
    FakeWhisperModel.fail_devices = set()
    FakeWhisperModel.segments = [Seg(" merhaba "), Seg(" dünya ")]
    FakeWhisperModel.raise_on_transcribe = None
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    ct2 = types.ModuleType("ctranslate2")
    ct2.get_cuda_device_count = lambda: 1
    monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
    monkeypatch.setattr(stt, "add_cuda_dlls", lambda: None)
    monkeypatch.setattr(stt, "_cuda_ok", lambda m: True)
    return FakeWhisperModel


# ------------------------------------------------------------- hallucination
@pytest.mark.parametrize("text", ["", "  ...  ", "Altyazı M.K.", "Teşekkürler.", "Abone olmayı unutmayın!",
                                  "Thanks for watching!", "www.example.com", "İzlediğiniz için teşekkürler",
                                  "Bir sonraki videoda görüşmek üzere", "\"Sesli betimleme\""])
def test_hallucination_detected(text):
    assert is_hallucination(text)


@pytest.mark.parametrize("text", ["Alice, teşekkürler", "Hey Alice altyazı aç", "saat kaç", "DCS",
                                  "F-16 yakıt durumu ne", "unity projesini aç ve derle lütfen"])
def test_real_speech_not_hallucination(text):
    assert not is_hallucination(text)


def test_prompt_fragment_echo_is_hallucination():
    assert is_hallucination("War Thunder, bingo, waypoint, kneeboard.")
    assert is_hallucination("DCS, F-16")
    assert not is_hallucination("DCS")  # 3 letters or fewer: skips the hint check


def test_long_text_with_marker_word_is_kept():
    text = "teşekkürler, şimdi bana yarın sabah sekizde toplantıyı hatırlatır mısın lütfen"
    assert len(text) >= 60 and not is_hallucination(text)


def test_full_prompt_echo_is_hallucination():
    assert is_hallucination(PROMPT)
    assert is_hallucination("Hey Alice. ALICE, DCS, F-16")


@pytest.mark.parametrize("text", ["bingo", "Bingo.", "Hey Alice", "War Thunder", "kneeboard"])
def test_single_prompt_term_is_real_speech(text):
    assert not is_hallucination(text)


# ------------------------------------------------------------------- Groq
def groq(handler, prompt=""):
    return GroqWhisper("KEY", client=httpx.Client(transport=httpx.MockTransport(handler)), prompt=prompt)


def test_groq_sends_prompt_auth_and_16k_wav():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.read()
        return httpx.Response(200, json={"text": "  merhaba  "})

    g = groq(handler, prompt="Hey Alice.")
    assert g.name == "groq:whisper-large-v3-turbo"
    assert g.transcribe(Audio.silence(0.2, 24000)) == "merhaba"
    assert seen["auth"] == "Bearer KEY"
    body = seen["body"]
    assert b'name="prompt"' in body and b"Hey Alice." in body and b'name="language"' in body
    assert b'name="temperature"' in body and b"speech.wav" in body


def test_groq_omits_prompt_when_empty():
    bodies = []

    def handler(req):
        bodies.append(req.read())
        return httpx.Response(200, json={"text": "x"})

    g = groq(handler)
    g.transcribe(Audio.silence(0.1, 16000))
    g.transcribe_file(b"OGGDATA", "kayit.ogg")
    assert all(b'name="prompt"' not in b for b in bodies)
    assert b"kayit.ogg" in bodies[1] and b"OGGDATA" in bodies[1]


def test_groq_transcribe_file_with_prompt():
    bodies = []

    def handler(req):
        bodies.append(req.read())
        return httpx.Response(200, json={"text": " webm metni "})

    assert groq(handler, prompt="P!").transcribe_file(b"W", "a.webm") == "webm metni"
    assert b"P!" in bodies[0]


@pytest.mark.parametrize("method", ["transcribe", "words", "transcribe_file"])
def test_groq_errors_become_stterror(method):
    def bad_status(req):
        return httpx.Response(429, text="rate limit " + "x" * 500)

    def boom(req):
        raise httpx.ConnectError("ağ yok")

    args = (Audio.silence(0.1, 16000),) if method != "transcribe_file" else (b"d", "a.webm")
    with pytest.raises(STTError, match="groq 429: rate limit") as ei:
        getattr(groq(bad_status), method)(*args)
    assert len(str(ei.value)) < 230  # body is truncated to 200 characters
    with pytest.raises(STTError, match="bağlantı"):
        getattr(groq(boom), method)(*args)


def test_groq_words_handles_missing_and_null():
    assert groq(lambda r: httpx.Response(200, json={"words": None})).words(Audio.silence(0.1, 16000)) == []
    res = groq(lambda r: httpx.Response(200, json={"words": [{"word": "a"}, {"word": "b", "start": 1, "end": "2.5"}]}))
    assert res.words(Audio.silence(0.1, 16000)) == [("a", 0.0, 0.0), ("b", 1.0, 2.5)]


def test_groq_missing_text_is_empty_string():
    assert groq(lambda r: httpx.Response(200, json={})).transcribe(Audio.silence(0.1, 16000)) == ""


# ------------------------------------------------------------------ local
def test_local_whisper_cpu_when_gpu_not_allowed(fake_fw):
    lw = LocalWhisper(allow_gpu=lambda: False, cpu_model="tiny", language="en")
    assert lw.transcribe(Audio.silence(0.2, 24000)) == "merhaba dünya"
    assert fake_fw.created == [("tiny", "cpu", "int8")]
    m = lw._models["cpu"]
    call = m.calls[0]
    assert call["language"] == "en" and call["vad_filter"] and call["initial_prompt"] is None
    assert wav_seconds(call["bytes"]) == pytest.approx(0.2, abs=0.01)
    lw.transcribe_file(b"x", "a.webm")
    assert len(fake_fw.created) == 1  # model is cached


def test_local_whisper_prefers_cuda(fake_fw):
    lw = LocalWhisper(gpu_model="big", prompt="ipucu")
    lw.transcribe_file(b"x", "a.wav")
    assert fake_fw.created == [("big", "cuda", stt.GPU_COMPUTE)]
    assert lw._models["cuda"].calls[0]["initial_prompt"] == "ipucu"


def test_local_whisper_cuda_failure_falls_back_once(fake_fw):
    fake_fw.fail_devices = {"cuda"}
    lw = LocalWhisper(gpu_model="big", cpu_model="small")
    lw.transcribe_file(b"x", "a.wav")
    lw.transcribe_file(b"x", "a.wav")
    assert fake_fw.created == [("small", "cpu", "int8")]
    assert lw._models.get("cuda_failed") is True


def test_local_whisper_cuda_not_ok_marks_failed(fake_fw, monkeypatch):
    monkeypatch.setattr(stt, "_cuda_ok", lambda m: False)
    lw = LocalWhisper(gpu_model="big", cpu_model="small")
    lw.transcribe_file(b"x", "a.wav")
    assert "cuda" not in lw._models and lw._models["cuda_failed"]
    assert [c[1] for c in fake_fw.created] == ["cuda", "cpu"]


def test_local_whisper_no_cuda_devices(fake_fw):
    sys.modules["ctranslate2"].get_cuda_device_count = lambda: 0
    lw = LocalWhisper()
    lw.transcribe_file(b"x", "a.wav")
    assert [c[1] for c in fake_fw.created] == ["cpu"]


def test_local_whisper_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(STTError, match="faster-whisper"):
        LocalWhisper().transcribe_file(b"x", "a.wav")


def test_cuda_ok_probe():
    class Ok:
        def transcribe(self, f, **kw):
            assert wav_seconds(f.read()) == pytest.approx(0.5)
            return iter([]), None

    class Bad:
        def transcribe(self, f, **kw):
            raise OSError("cublas64_12.dll yok")

    assert stt._cuda_ok(Ok()) is True
    assert stt._cuda_ok(Bad()) is False


def test_add_cuda_dlls_noop_off_windows(monkeypatch):
    monkeypatch.setattr(stt, "_DLLS_DONE", False)
    monkeypatch.setattr(stt, "os", SimpleNamespace(name="posix"))
    stt.add_cuda_dlls()
    assert stt._DLLS_DONE is False


def test_add_cuda_dlls_on_windows_adds_torch_and_nvidia_dirs(monkeypatch, tmp_path):
    (tmp_path / "torch" / "lib").mkdir(parents=True)
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (tmp_path / "nvidia" / "cudnn" / "bin").mkdir(parents=True)
    specs = {"torch": SimpleNamespace(origin=str(tmp_path / "torch" / "__init__.py"), submodule_search_locations=None),
             "nvidia": SimpleNamespace(origin=None, submodule_search_locations=[str(tmp_path / "nvidia")])}
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: specs.get(name))
    added = []

    def add_dll(d):
        added.append(d)
        if "cudnn" in d:
            raise OSError("izin yok")  # should be ignored

    env = {"PATH": "orig"}
    monkeypatch.setattr(stt, "os", SimpleNamespace(name="nt", add_dll_directory=add_dll, environ=env, pathsep=";"))
    monkeypatch.setattr(stt, "_DLLS_DONE", False)
    stt.add_cuda_dlls()
    assert len(added) == 3 and any(d.endswith("lib") for d in added)
    assert env["PATH"].endswith("orig") and env["PATH"].count(";") == 3
    stt.add_cuda_dlls()  # a second call does nothing
    assert len(added) == 3


# ---------------------------------------------------------------- WakeGate
def test_wakegate_unavailable_without_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    g = WakeGate()
    assert g.available() is False and g.heard(Audio.silence(1, 16000)) is None
    assert g.transcribe(Audio.silence(1, 16000)) is None
    monkeypatch.delitem(sys.modules, "faster_whisper")
    assert g.available() is False  # permanent once it has failed once


def test_wakegate_heard_uses_only_head(fake_fw):
    g = WakeGate("base", "tr")
    text = g.heard(Audio.silence(6.0, 48000))
    assert text == "merhaba dünya" and g.device == "cpu"
    call = g._model.calls[0]
    assert wav_seconds(call["bytes"]) == pytest.approx(WakeGate.HEAD_SECONDS, abs=0.01)
    # No hint: the "Hey Alice." hint used to come back verbatim on noise and wake the assistant by itself.
    assert call["initial_prompt"] is None and call["condition_on_previous_text"] is False
    assert g.last_seconds >= 0
    g.heard(Audio.silence(1.0, 16000))
    assert fake_fw.created == [("base", "cpu", "int8")]


def test_wakegate_gpu_model_when_allowed(fake_fw):
    g = WakeGate("base", gpu_model="small", allow_gpu=lambda: True)
    g.heard(Audio.silence(0.5, 16000))
    assert g.device == "cuda" and g.model_name == "small"
    assert fake_fw.created == [("small", "cuda", stt.GPU_COMPUTE)]


def test_wakegate_gpu_failure_falls_back_to_cpu(fake_fw):
    fake_fw.fail_devices = {"cuda"}
    g = WakeGate("base", gpu_model="small", allow_gpu=lambda: True)
    g.heard(Audio.silence(0.5, 16000))
    assert g.device == "cpu" and g.model_name == "base"


def test_wakegate_heard_failure_disables_gate_after_three(fake_fw):
    fake_fw.raise_on_transcribe = RuntimeError("bozuk")
    g = WakeGate()
    for _ in range(2):  # an occasional error (CUDA OOM) does not close the gate; the model is reloaded
        assert g.heard(Audio.silence(0.5, 16000)) is None
        assert g.available() is True and g._model is None
    assert g.heard(Audio.silence(0.5, 16000)) is None
    assert g.available() is False


def test_wakegate_transcribe_confidence(fake_fw):
    fake_fw.segments = [Seg(" bir ", -0.1), Seg(" iki ", -0.7)]
    g = WakeGate()
    text, conf = g.transcribe(Audio.silence(3.0, 16000), prompt="Selam")
    assert text == "bir iki" and conf == -0.7
    call = g._model.calls[0]
    assert call["initial_prompt"] == "Selam" and wav_seconds(call["bytes"]) == pytest.approx(3.0, abs=0.01)
    fake_fw.segments = []
    assert g.transcribe(Audio.silence(0.5, 16000)) == ("", -9.0)
    fake_fw.raise_on_transcribe = RuntimeError("x")
    assert g.transcribe(Audio.silence(0.5, 16000)) is None


def test_wakegate_warm_up_loads_in_background(fake_fw):
    g = WakeGate()
    g.warm_up()
    for _ in range(200):
        if g._model is not None:
            break
        time.sleep(0.01)
    assert g._model is not None and g.device == "cpu"


def test_wakegate_warm_up_failure_marks_failed(fake_fw):
    fake_fw.fail_devices = {"cpu"}
    g = WakeGate()
    g.warm_up()
    for _ in range(200):
        if g._failed:
            break
        time.sleep(0.01)
    assert g._failed and g.available() is False


# ----------------------------------------------------------------- router
class Eng:
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error, self.calls = name, result, error, 0

    def transcribe(self, audio):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def transcribe_file(self, data, filename):
        self.calls += 1
        if self.error:
            raise self.error
        return f"{self.result}:{filename}"


def test_router_cooldown_skips_then_retries(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(stt.time, "monotonic", lambda: now[0])
    bad, good = Eng("bad", error=STTError("kota")), Eng("good", "ok")
    r = STTRouter([bad, good], cooldown=60)
    assert r.transcribe(Audio.silence(0.1)) == "ok" and r.last == "good"
    assert r.down["bad"] == 160
    r.transcribe(Audio.silence(0.1))
    assert bad.calls == 1  # skipped during cooldown
    now[0] = 161
    bad.error = None
    bad.result = "geri döndü"
    assert r.transcribe(Audio.silence(0.1)) == "geri döndü" and r.last == "bad"


def test_router_all_down_message_and_empty():
    r = STTRouter([Eng("a", error=STTError("x")), Eng("b", error=STTError("y"))])
    with pytest.raises(STTError, match="a: x; b: y"):
        r.transcribe(Audio.silence(0.1))
    with pytest.raises(STTError, match="konuşma tanıma motoru yok"):
        r.transcribe(Audio.silence(0.1))  # all are in cooldown
    with pytest.raises(STTError, match="motoru yok"):
        STTRouter([]).transcribe(Audio.silence(0.1))


def test_router_transcribe_file_routes():
    r = STTRouter([Eng("a", error=STTError("x")), Eng("b", "metin")])
    assert r.transcribe_file(b"d", "s.webm") == "metin:s.webm"


# ---------------------------------------------------------------- make_stt
def cfg_with(voice=None, providers=None) -> Config:
    data = default_config_dict()
    data.setdefault("voice", {}).update(voice or {})
    if providers:
        data["providers"] = providers
    return Config(data)


def test_make_stt_without_key_uses_local_only(monkeypatch):
    monkeypatch.setattr("alice.voice.tts.core_policy", lambda: {"local_gpu": False})
    r = make_stt(cfg_with({"stt": ["groq:whisper-large-v3-turbo", "local:medium"], "language": "de",
                           "stt_cpu_model": "base", "wake_local": "tiny", "wake_local_gpu": None}))
    assert [type(e) for e in r.engines] == [LocalWhisper]
    lw = r.engines[0]
    assert (lw.gpu_model, lw.cpu_model, lw.language, lw.prompt) == ("medium", "base", "de", PROMPT)
    assert lw.allow_gpu() is False
    assert r.wake_gate.model_name == "tiny" and r.wake_gate.gpu_model == "" and r.wake_gate.allow_gpu() is False


def test_make_stt_with_key_and_defaults(monkeypatch):
    monkeypatch.setattr("alice.voice.tts.core_policy", lambda: {})
    monkeypatch.setenv("MY_GROQ", "k1")
    r = make_stt(cfg_with({"stt": ["groq", "local", "bogus:x"], "wake_local": ""},
                          providers={"groq": {"api_key_env": "MY_GROQ"}}))
    g, lw = r.engines
    assert isinstance(g, GroqWhisper) and g.api_key == "k1" and g.model == "whisper-large-v3-turbo"
    assert g.prompt == PROMPT
    assert lw.gpu_model == "large-v3-turbo" and lw.allow_gpu() is True
    assert r.wake_gate is None


def test_make_stt_default_engine_list(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    cfg = cfg_with()
    cfg.data["voice"].pop("stt", None)
    r = make_stt(cfg)
    assert [e.name for e in r.engines] == ["groq:whisper-large-v3-turbo", "local-whisper"]
