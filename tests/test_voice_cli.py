"""alice/voice/cli.py: parsing and dispatch of voice commands (hardware and network faked)."""

from __future__ import annotations

import io
from alice.config import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from rich.console import Console

from alice.config import alice_home, init_home
from alice.voice import cli as vcli
from alice.voice import tts as tts_mod


@pytest.fixture
def out(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(vcli, "console", Console(file=buf, width=200, highlight=False, color_system=None))
    return buf.getvalue


def _voice_cfg() -> dict:
    return tomllib.loads((alice_home() / "config.toml").read_text(encoding="utf-8"))["voice"]


def _cfg(**voice):
    from alice.config import Config, default_config_dict

    data = default_config_dict()
    data["voice"].update(voice)
    return Config(data)


# ------------------------------------------------------------------- dispatch
def test_voice_cmd_dispatch(monkeypatch, out):
    seen = []
    for name in ("_run", "_say", "_status"):
        monkeypatch.setattr(vcli, name, lambda cfg, argv, n=name: seen.append((n, argv)) or 0)
    assert vcli.voice_cmd([]) == 0
    assert vcli.voice_cmd(["--ptt", "<f13>"]) == 0  # started with a flag: run
    assert vcli.voice_cmd(["say", "merhaba", "--emotion", "happy"]) == 0
    assert vcli.voice_cmd(["status"]) == 0
    assert seen == [("_run", []), ("_run", ["--ptt", "<f13>"]), ("_say", ["merhaba", "--emotion", "happy"]),
                    ("_status", [])]
    assert vcli.voice_cmd(["uç"]) == 2
    assert "alice voice audition" in out()


def test_off_and_use_write_config(out):
    init_home()
    assert vcli.voice_cmd(["off"]) == 0
    assert _voice_cfg()["enabled"] is False
    assert vcli.voice_cmd(["use"]) == 2
    assert vcli.voice_cmd(["use", "gemini:Kore"]) == 0
    assert _voice_cfg()["tts_cloud"] == "gemini:Kore"
    assert vcli.voice_cmd(["use", "chatterbox"]) == 0
    assert _voice_cfg()["tts_local"] == "chatterbox"
    assert vcli.voice_cmd(["use", "openai:alloy"]) == 2
    text = out()
    assert "voice off" in text and "cloud voice: gemini:Kore" in text and "local voice: chatterbox" in text
    assert "unknown candidate" in text


def test_on_starts_core_and_autostart(monkeypatch, out, tmp_path):
    from alice.core import service

    init_home()
    started, installed = [], []
    monkeypatch.setattr(service, "read_state", lambda: {"running": False})
    monkeypatch.setattr(service, "start_detached", lambda: started.append(True) or True)
    monkeypatch.setattr(service, "autostart_path", lambda: tmp_path / "yok.desktop")
    monkeypatch.setattr(service, "install_autostart", lambda: installed.append(True))
    assert vcli.voice_cmd(["on"]) == 0
    assert _voice_cfg()["enabled"] is True and started == [True] and installed == [True]
    assert "Core started" in out()


def test_on_when_core_running_and_autostart_fails(monkeypatch, out, tmp_path):
    from alice.core import service

    init_home()
    monkeypatch.setattr(service, "read_state", lambda: {"running": True})
    monkeypatch.setattr(service, "start_detached", lambda: pytest.fail("çalışan çekirdek yeniden başlatılmamalı"))
    monkeypatch.setattr(service, "autostart_path", lambda: tmp_path / "yok.lnk")

    def boom():
        raise PermissionError("erişim yok")

    monkeypatch.setattr(service, "install_autostart", boom)
    assert vcli.voice_cmd(["on"]) == 0
    text = out()
    assert "The core is already running" in text and "could not set up auto-start: erişim yok" in text


def test_install_local(monkeypatch, out):
    from alice.voice import install_local

    def ok(emit):
        emit("indiriliyor [x]")
        return Path("/venv/python")

    monkeypatch.setattr(install_local, "install", ok)
    assert vcli.voice_cmd(["install-local"]) == 0
    assert "indiriliyor [x]" in out() and "local voice installed" in out()

    def fail(emit):
        raise RuntimeError("Python 3.11 bulunamadı")

    monkeypatch.setattr(install_local, "install", fail)
    assert vcli.voice_cmd(["install-local"]) == 1
    assert "Python 3.11 bulunamadı" in out()


# --------------------------------------------------------------------- models
def test_models_compares_account_with_order(monkeypatch, out):
    eng = SimpleNamespace(api_key="k", models=["m-a", "m-b"])
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: eng)
    monkeypatch.setattr(tts_mod, "list_gemini_tts_models", lambda key: ["m-a", "m-c"])
    assert vcli._models(_cfg(), []) == 0
    text = out()
    assert "Voice models on your account: m-a, m-c" in text
    assert "✓ m-a" in text and "✗ missing m-b" in text and "On your account but not in the order: m-c" in text


def test_models_errors(monkeypatch, out):
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: None)
    assert vcli._models(_cfg(), []) == 1
    assert "no GEMINI_API_KEY" in out()

    def boom(key):
        raise httpx.ConnectError("bağlantı yok")

    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: SimpleNamespace(api_key="k", models=[]))
    monkeypatch.setattr(tts_mod, "list_gemini_tts_models", boom)
    assert vcli._models(_cfg(), []) == 1
    assert "could not fetch the model list: bağlantı yok" in out()


def test_models_empty_account(monkeypatch, out):
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: SimpleNamespace(api_key="k", models=["m-a"]))
    monkeypatch.setattr(tts_mod, "list_gemini_tts_models", lambda key: [])
    assert vcli._models(_cfg(), []) == 0
    assert "on your account: none" in out()


# --------------------------------------------------------------------- status
def _status_fakes(monkeypatch, cloud, local=None, engines=(), mic=True):
    from alice.voice import audio, stt

    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: cloud)
    monkeypatch.setattr(tts_mod, "make_local", lambda cfg: local)
    monkeypatch.setattr(stt, "make_stt", lambda cfg: SimpleNamespace(engines=list(engines)))
    monkeypatch.setattr(audio, "Player", lambda: SimpleNamespace(backend="sahte"))
    monkeypatch.setattr(audio, "mic_available", lambda: mic)


def test_status_minimal(monkeypatch, out):
    _status_fakes(monkeypatch, cloud=None, mic=False)
    assert vcli._status(_cfg(tts_local=""), []) == 0
    text = out()
    assert "no GEMINI_API_KEY" in text and "Local voice: none" in text and "Emotion library: none" in text
    assert "Speech recognition: none" in text and "Playback: sahte" in text and "Microphone: no" in text
    assert "sounddevice" in text and "openwakeword" in text


def test_status_with_library_mismatch_and_chatterbox(monkeypatch, out, tmp_path):
    import subprocess
    import sys

    lib = tts_mod.library_dir()
    lib.mkdir(parents=True)
    for e in ("calm", "urgent"):
        (lib / f"{e}.wav").write_bytes(b"RIFF")
    (lib / "meta.json").write_text('{"source": "gemini:Kore"}', encoding="utf-8")
    cloud = SimpleNamespace(name="gemini:Aoede")
    local = SimpleNamespace(healthy=lambda: True)
    _status_fakes(monkeypatch, cloud=cloud, local=local, engines=[SimpleNamespace(name="groq")])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="True\n"))
    assert vcli._status(_cfg(tts_local="chatterbox", local_python=sys.executable), []) == 0
    text = out()
    assert "Cloud voice: gemini:Aoede ready" in text
    assert "Local voice: chatterbox server running" in text and "local Whisper): yes" in text
    assert "(gemini:Kore; sakin, acil)" in text and "the library differs" in text
    assert "Speech recognition: groq" in text and "Microphone: yes" in text


def test_status_chatterbox_not_installed(monkeypatch, out, tmp_path):
    _status_fakes(monkeypatch, cloud=None, local=SimpleNamespace(healthy=lambda: False))
    assert vcli._status(_cfg(tts_local="chatterbox", local_python=str(tmp_path / "yok")), []) == 0
    assert "not installed (alice voice install-local)" in out()


# ----------------------------------------------------------------- audition
class _Gem(tts_mod.GeminiTTS):
    def __init__(self, name):
        self.name = name
        self.verify = None


def test_audition_builds_engines_and_opens_page(monkeypatch, out, tmp_path):
    import webbrowser

    from alice.voice import audition, stt

    page = tmp_path / "audition.html"
    runs = []

    def run_audition(engines, emotions, emit):
        runs.append(([e.name for e in engines], emotions))
        emit("örnek [1]")
        return page

    monkeypatch.setattr(audition, "run_audition", run_audition)
    monkeypatch.setattr(audition, "audition_dir", lambda: tmp_path)
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg, ref: _Gem(ref))
    monkeypatch.setattr(tts_mod, "make_local", lambda cfg, name: SimpleNamespace(name=name, library=None)
                        if name == "xtts" else None)
    monkeypatch.setattr(stt, "make_stt", lambda cfg: SimpleNamespace(engines=["groq"]))
    monkeypatch.setattr(tts_mod, "make_verifier", lambda s: "doğrulayıcı")
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    assert vcli._audition(_cfg(), ["--voices", "Aoede, Kore", "--local", "xtts,bilinmez",
                                   "--emotions", "calm, urgent"]) == 0
    # no reference library: first render the "sakin" (calm) sample of the first cloud voice; the local engine clones it
    assert runs[0] == (["gemini:Aoede"], ["calm"])
    assert runs[1] == (["gemini:Aoede", "gemini:Kore", "xtts"], ["calm", "urgent"])
    assert opened == [page.as_uri()]
    assert "unknown local engine: bilinmez" in out() and "ready:" in out()


def test_audition_with_ref_and_no_open(monkeypatch, out, tmp_path):
    import webbrowser

    from alice.voice import audition, stt

    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    local = SimpleNamespace(name="chatterbox", library=None)
    monkeypatch.setattr(audition, "run_audition", lambda engines, emotions, emit: tmp_path / "p.html")
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg, ref: None)
    monkeypatch.setattr(tts_mod, "make_local", lambda cfg, name: local)
    monkeypatch.setattr(stt, "make_stt", lambda cfg: SimpleNamespace(engines=[]))
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("--no-open"))
    assert vcli._audition(_cfg(), ["--local", "chatterbox", "--ref", str(ref), "--no-open"]) == 0
    assert local.library.clip("urgent") == ref
    text = out()
    assert "skipping cloud voices" in text and "no Whisper" in text


def test_audition_nothing_to_try(monkeypatch, out):
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg, ref: None)
    assert vcli._audition(_cfg(), ["--voices", "Aoede"]) == 1
    assert "no voices to try" in out()


def test_single_ref_library(tmp_path):
    p = tmp_path / "a.wav"
    lib = vcli._single_ref_library(p)
    assert lib.clip("calm") is None and not lib.exists()
    p.write_bytes(b"x")
    assert lib.clip("happy") == p and lib.exists()


def test_attach_verifier_only_on_gemini(monkeypatch, out):
    from alice.voice import stt

    monkeypatch.setattr(stt, "make_stt", lambda cfg: SimpleNamespace(engines=["groq"]))
    monkeypatch.setattr(tts_mod, "make_verifier", lambda s: "V")
    g, other = _Gem("gemini:Aoede"), SimpleNamespace(name="xtts")
    vcli._attach_verifier(_cfg(), [g, other])
    assert g.verify == "V" and not hasattr(other, "verify")


# -------------------------------------------------------------------- library
def test_library(monkeypatch, out, tmp_path):
    from alice.voice import stt

    monkeypatch.setattr(stt, "make_stt", lambda cfg: SimpleNamespace(engines=[]))
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: None)
    assert vcli._library(_cfg(), []) == 1
    eng = _Gem("gemini:Leda")
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: eng)
    calls = []

    def build(engine, emit, redo):
        calls.append(redo)
        emit("sakin hazır")
        return (tmp_path, ["urgent", "playful"]) if len(calls) == 1 else (tmp_path, [])

    monkeypatch.setattr(tts_mod, "build_library", build)
    assert vcli._library(_cfg(), ["--redo"]) == 0
    assert vcli._library(_cfg(), []) == 0
    assert calls == [True, False]
    text = out()
    assert "still missing: urgent, playful" in text and f"ready: {tmp_path}" in text and "voice: gemini:Leda" in text


# ------------------------------------------------------------------------ say
class _Audio:
    seconds = 1.5

    def __init__(self):
        self.saved = None

    def save(self, path):
        self.saved = path


class _Router:
    def __init__(self, last="gemini:Aoede", fail=None):
        self.last = last
        self.fail = fail
        self.cloud = SimpleNamespace(last_model="tts-model", last_format="notes")
        self.calls = []
        self.audio = _Audio()

    def synth(self, text, emo):
        self.calls.append((text, emo))
        if self.fail:
            raise self.fail
        return self.audio


def test_say_saves_wav_with_detail(monkeypatch, out, tmp_path):
    r = _Router()
    monkeypatch.setattr(tts_mod, "make_tts", lambda cfg: r)
    target = str(tmp_path / "o.wav")
    assert vcli._say(_cfg(), ["Yakıt", "kritik!", "--emotion", "urgent", "--out", target]) == 0
    assert r.calls == [("Yakıt kritik!", "urgent")] and r.audio.saved == target
    assert "gemini:Aoede · tts-model · notes · urgent · 1.5 s" in out()


def test_say_engine_choice_and_playback(monkeypatch, out):
    from alice.voice import audio

    built = []

    class FakeRouter(_Router):
        def __init__(self, cloud, local):
            super().__init__(last="xtts")
            built.append((cloud, local))

    monkeypatch.setattr(tts_mod, "make_tts", lambda cfg: _Router())
    monkeypatch.setattr(tts_mod, "TTSRouter", FakeRouter)
    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg: "BULUT")
    monkeypatch.setattr(tts_mod, "make_local", lambda cfg: "YEREL")
    played = []
    monkeypatch.setattr(audio, "Player", lambda: SimpleNamespace(available=True, play=played.append))
    assert vcli._say(_cfg(), ["selam", "--engine", "local"]) == 0
    assert built == [(None, "YEREL")] and len(played) == 1
    assert vcli._say(_cfg(), ["selam", "--engine", "cloud"]) == 0
    assert built[-1] == ("BULUT", None)

    monkeypatch.setattr(audio, "Player", lambda: SimpleNamespace(available=False))
    assert vcli._say(_cfg(), ["selam"]) == 1
    assert "can't play audio" in out()


def test_say_reports_tts_error(monkeypatch, out):
    monkeypatch.setattr(tts_mod, "make_tts", lambda cfg: _Router(fail=tts_mod.TTSError("kota doldu")))
    assert vcli._say(_cfg(), ["merhaba"]) == 1
    assert "kota doldu" in out()


# ----------------------------------------------------------------------- test
def test_mic_test_failure_to_open(monkeypatch, out):
    from alice.voice import audio

    def boom():
        raise OSError("aygıt yok")

    monkeypatch.setattr(audio, "Microphone", boom)
    assert vcli._test(_cfg(), []) == 1
    assert "could not open the microphone: aygıt yok" in out()


def _clock(*ticks: float):
    """A fake time.monotonic that walks through ticks, then stays at the last one."""
    it = iter(ticks)
    last = [ticks[0]]

    def now() -> float:
        last[0] = next(it, last[0])
        return last[0]

    return now


def test_mic_test_records_and_transcribes(monkeypatch, out):
    import time as _time

    from alice.voice import audio, stt

    class Mic:
        RATE = 16000

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"\x00\x10" * 160

    # time.monotonic is global: anything else reading it during the test (rich's live display, a pytest-xdist
    # worker's background thread) used to exhaust a fixed list and raise StopIteration at random. The clock keeps
    # returning its last value instead.
    clock = _clock(0.0, 1.0, 2.0, 6.0, 10.0, 10.5)
    monkeypatch.setattr(_time, "monotonic", clock)
    monkeypatch.setattr(audio, "Microphone", Mic)
    heard = []

    def transcribe(a):
        heard.append(len(a.pcm) if hasattr(a, "pcm") else a)
        return "merhaba alice"

    monkeypatch.setattr(stt, "make_stt", lambda cfg: SimpleNamespace(transcribe=transcribe, last="groq"))
    assert vcli._test(_cfg(), []) == 0
    assert "heard (groq, 0.5 s): merhaba alice" in out()
    assert len(heard) == 1

    def fail(a):
        raise stt.STTError("anlaşılmadı")

    monkeypatch.setattr(_time, "monotonic", _clock(0.0, 6.0, 7.0))
    monkeypatch.setattr(stt, "make_stt", lambda cfg: SimpleNamespace(transcribe=fail, last=""))
    assert vcli._test(_cfg(), []) == 1
    assert "anlaşılmadı" in out()


# ------------------------------------------------------------------------ run
def test_run_requires_mic(monkeypatch, out):
    from alice.voice import audio

    monkeypatch.setattr(audio, "mic_available", lambda: False)
    assert vcli._run(_cfg(), []) == 1
    assert "no microphone" in out()


def test_run_wires_assistant(monkeypatch, out):
    from alice import tools
    from alice.voice import assistant, audio, stt

    monkeypatch.setattr(audio, "mic_available", lambda: True)
    monkeypatch.setattr(stt, "make_stt", lambda cfg: "STT")
    monkeypatch.setattr(assistant, "build_voice_agent", lambda cfg, ui: ("AJAN", "KONUŞMACI", "CTX"))
    monkeypatch.setattr(assistant, "make_live", lambda cfg, agent: None)
    monkeypatch.setattr(assistant, "live_text_options", lambda v: {})
    seen = {}

    class VA:
        def __init__(self, agent, speaker, stt_, **kw):
            seen.update(kw, agent=agent, stt=stt_)
            self.ptt_key, self.wake_phrase, self.wake_model = kw["ptt_key"], kw["wake_phrase"], kw["wake_model"]

        def run(self):
            raise KeyboardInterrupt

        def stop(self):
            seen["stopped"] = True

    monkeypatch.setattr(assistant, "VoiceAssistant", VA)
    monkeypatch.setattr(tools, "cleanup", lambda ctx: seen.update(cleaned=ctx))
    assert vcli._run(_cfg(follow_up=3, wake_model=""), ["--ptt", "<f13>", "--no-wake"]) == 0
    assert seen["ptt_key"] == "<f13>" and seen["wake_phrase"] is False and seen["follow_up"] == 3.0
    assert seen["stopped"] and seen["cleaned"] == "CTX" and seen["agent"] == "AJAN" and seen["stt"] == "STT"
    assert "listening (push-to-talk: <f13>)" in out()
