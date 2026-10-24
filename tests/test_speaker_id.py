"""Konuşmacı doğrulama: sadece kullanıcının sesi uyandırır (alice voice enroll)."""

from __future__ import annotations

import time

from alice.voice import speaker_id as sid
from alice.voice.assistant import VoiceAssistant
from alice.voice.audio import Audio
from tests.test_voice import FakeAgent, FakeSpeaker, wait_for


class FakeEmbedder:
    """Sesin ilk baytına göre sabit bir vektör: 1 = kullanıcı, 2 = başkası."""

    def embed(self, pcm, rate=16000):
        return [1.0, 0.0, 0.0] if pcm[:1] == b"\1" else [0.0, 1.0, 0.0]


def gate(threshold=0.5):
    return sid.SpeakerGate({"vector": [1.0, 0.0, 0.0], "threshold": threshold}, FakeEmbedder())


class CloudSTT:
    wake_gate = None

    def __init__(self, text="Hey Alice, saat kaç?"):
        self.text = text

    def transcribe(self, audio):
        return self.text


def test_someone_elses_voice_does_not_wake_alice():
    agent = FakeAgent()
    va = VoiceAssistant(agent, FakeSpeaker(), stt=CloudSTT(), speaker_gate=gate())
    va.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=False)  # arkadaşın "Hey Alice" dedi
    time.sleep(0.3)
    assert agent.turns == []
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)  # kullanıcı
    assert wait_for(lambda: agent.turns == ["saat kaç?"])


def test_push_to_talk_and_hush_skip_the_voice_check():
    agent = FakeAgent()
    va = VoiceAssistant(agent, FakeSpeaker(), stt=CloudSTT(), speaker_gate=gate())
    va.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=True)  # tuşa basan kullanıcıdır
    assert wait_for(lambda: agent.turns == ["saat kaç?"])
    va2 = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=CloudSTT("Alice sus"), speaker_gate=gate())
    va2.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=False)
    assert va2.muted  # susturmak zararsız: kim derse desin


def test_short_or_broken_audio_is_not_rejected():
    g = gate()
    assert g.check(b"\2\0" * 4000)  # 0,25 sn: karar verilemez, kullanıcı cezalandırılmaz

    class Boom:
        def embed(self, pcm, rate=16000):
            raise RuntimeError("onnx")

    g.embedder = Boom()
    assert g.check(b"\2\0" * 16000)


def test_profile_roundtrip_and_calibrated_threshold(tmp_path):
    close = [[1.0, 0.1, 0.0], [1.0, 0.0, 0.1], [0.9, 0.1, 0.1], [1.0, 0.05, 0.05]]
    prof = sid.save_profile(close, tmp_path / "s.json")
    assert sid.load_profile(tmp_path / "s.json")["samples"] == 4
    assert prof["threshold"] == sid.THRESHOLD_RANGE[1]  # tutarlı kayıtlar: eşik üst sınırda
    noisy = [[1.0, 0.0, 0.0], [0.3, 1.0, 0.0], [0.4, 0.0, 1.0]]
    assert sid.save_profile(noisy, tmp_path / "n.json")["threshold"] == sid.THRESHOLD_RANGE[0]


def test_gate_load_needs_profile_package_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    assert sid.SpeakerGate.load(None) is None  # kayıt yok: herkes kabul
    sid.save_profile([[1.0, 0.0], [1.0, 0.1]])
    logs = []
    monkeypatch.setattr(sid, "available", lambda: False)
    assert sid.SpeakerGate.load(None, log=logs.append) is None and "sherpa-onnx" in logs[-1]
    monkeypatch.setattr(sid, "available", lambda: True)
    assert sid.SpeakerGate.load(None, log=logs.append) is None and "model yok" in logs[-1]


def test_enroll_command_records_saves_and_verifies(tmp_path, monkeypatch, capsys):
    from alice.voice import audio as audio_mod
    from alice.voice import cli as vcli

    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    monkeypatch.setattr(sid, "available", lambda: True)
    monkeypatch.setattr(sid, "download_model", lambda emit=None: tmp_path / "m.onnx")
    monkeypatch.setattr(sid, "Embedder", lambda: FakeEmbedder())

    class Mic:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(audio_mod, "Microphone", lambda: Mic())
    takes = iter([None] + [Audio(b"\1\0" * 24000, 16000)] * 20)  # ilk cümle duyulmadı, tekrar okundu
    monkeypatch.setattr(vcli, "_record_one", lambda mic, vad, seconds=15.0: next(takes))
    assert vcli._enroll(None, []) == 0
    out = capsys.readouterr().out
    assert "tekrar oku" in out and "sesin kaydedildi" in out and "seni tanıdı" in out
    assert sid.load_profile()["samples"] == len(sid.ENROLL_SENTENCES)
    assert vcli._enroll(None, ["--off"]) == 0 and sid.load_profile() is None


# ---- seçici uyanma (Discord'da konuşurken / oyunda)
def test_social_watch_reads_mic_users_and_games():
    from alice.voice.social import SocialWatch

    logs = []
    mics = [r"C:#Users#kresu#AppData#Local#Discord#app-1.0.9#Discord.exe", r"C:#Python311#python.exe"]
    w = SocialWatch(None, mics=lambda: mics, procs=lambda: set(), every=0, log=logs.append)
    assert w.reason() == "Discord sesli konuşmada" and "seçici uyanma" in logs[-1]
    mics = []
    assert w.reason() == "" and logs[-1] == "seçici uyanma kapandı"
    w.procs = lambda: {"dcs.exe", "explorer.exe"}
    assert w.reason() == "DCS açık"
    off = SocialWatch(type("C", (), {"data": {"voice": {"social_mode": False}}})(), mics=lambda: mics, every=0)
    mics = [r"C:#x#Discord.exe"]
    assert off.reason() == ""


def test_social_mode_needs_name_first_and_closes_follow_up():
    agent = FakeAgent()
    social = {"on": "Discord sesli konuşmada"}
    va = VoiceAssistant(agent, FakeSpeaker(), stt=None, social=lambda: social["on"])
    va.handle_text("Bak şimdi Alice müziği aç", direct=False)  # arkadaşına anlatıyor
    va.armed_until = time.monotonic() + 5
    va.handle_text("peki ya dün?", direct=False)  # takip penceresi kapalı
    time.sleep(0.3)
    assert agent.turns == []
    va.handle_text("Alice, müziği aç", direct=False)
    assert wait_for(lambda: agent.turns == ["müziği aç"])
    social["on"] = ""
    va.handle_text("Bak şimdi Alice sesi kıs lütfen", direct=False)
    assert wait_for(lambda: agent.turns[-1] == "sesi kıs lütfen")
