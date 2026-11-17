"""Speaker verification: only the user's voice wakes Alice (alice voice enroll)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from alice.voice import speaker_id as sid
from alice.voice.assistant import VoiceAssistant
from alice.voice.audio import Audio
from tests.test_voice import FakeAgent, FakeSpeaker, wait_for


class FakeEmbedder:
    """A fixed vector based on the audio's first byte: 1 = user, 2 = someone else."""

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
    va.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=False)  # a friend said "Hey Alice"
    time.sleep(0.3)
    assert agent.turns == []
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)  # the user
    assert wait_for(lambda: agent.turns == ["saat kaç?"])


def test_push_to_talk_and_hush_skip_the_voice_check():
    agent = FakeAgent()
    va = VoiceAssistant(agent, FakeSpeaker(), stt=CloudSTT(), speaker_gate=gate())
    va.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=True, ptt=True)  # the user is the one pressing the key
    assert wait_for(lambda: agent.turns == ["saat kaç?"])
    va2 = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=CloudSTT("Alice sus"), speaker_gate=gate())
    va2.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=False)
    # Open mic: others' voices are never transcribed ("sadece benim sesimi alsın"), muting included
    assert not va2.muted


def test_short_or_broken_audio_is_not_rejected():
    g = gate()
    assert g.check(b"\2\0" * 4000)  # 0.25 s: no verdict possible, the user is not penalized

    class Boom:
        def embed(self, pcm, rate=16000):
            raise RuntimeError("onnx")

    g.embedder = Boom()
    assert g.check(b"\2\0" * 16000)


def test_profile_roundtrip_and_calibrated_threshold(tmp_path):
    close = [[1.0, 0.1, 0.0], [1.0, 0.0, 0.1], [0.9, 0.1, 0.1], [1.0, 0.05, 0.05]]
    prof = sid.save_profile(close, tmp_path / "s.json")
    assert sid.load_profile(tmp_path / "s.json")["samples"] == 4
    assert prof["threshold"] == sid.THRESHOLD_RANGE[1]  # consistent recordings: threshold at the upper bound
    noisy = [[1.0, 0.0, 0.0], [0.3, 1.0, 0.0], [0.4, 0.0, 1.0]]
    assert sid.save_profile(noisy, tmp_path / "n.json")["threshold"] == sid.THRESHOLD_RANGE[0]


def test_gate_load_needs_profile_package_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    logs = []
    g = sid.SpeakerGate.load(None, log=logs.append)
    assert g is not None and not g.active and g.check(b"\1\0" * 16000)  # no profile: everyone accepted
    monkeypatch.setattr(sid, "available", lambda: False)
    sid.save_profile([[1.0, 0.0], [1.0, 0.1]])
    assert g.check(b"\1\0" * 16000) and g.active  # once enrolled from the app, it takes effect without a restart
    assert "sherpa-onnx" in logs[-1] and "kontrol edilmedi" in g.last_note
    monkeypatch.setattr(sid, "available", lambda: True)
    assert g.check(b"\1\0" * 16000) and "modeli yok" in logs[-1]
    monkeypatch.setattr(sid, "Embedder", lambda: FakeEmbedder())
    (tmp_path / "models").mkdir()
    sid.model_path().write_bytes(b"x")
    g.make_embedder = sid.Embedder
    g.check(b"\1\0" * 16000)
    assert g.embedder is not None and not g.last_note
    sid.profile_path().unlink()
    assert g.check(b"\1\0" * 16000) and not g.active  # deleting the profile turns the check off
    assert sid.SpeakerGate.load(SimpleNamespace(data={"voice": {"speaker_check": False}})) is None


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
    takes = iter([None] + [Audio(b"\1\0" * 24000, 16000)] * 20)  # first sentence not heard, read again
    monkeypatch.setattr(vcli, "_record_one", lambda mic, vad, seconds=15.0: next(takes))
    assert vcli._enroll(None, []) == 0
    out = capsys.readouterr().out
    assert "tekrar oku" in out and "sesin kaydedildi" in out and "seni tanıdı" in out
    assert sid.load_profile()["samples"] == len(sid.ENROLL_SENTENCES)
    assert vcli._enroll(None, ["--off"]) == 0 and sid.load_profile() is None


# ---- selective wake (talking on Discord / in a game)
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
    va.handle_text("Bak şimdi Alice müziği aç", direct=False)  # telling a friend
    va.armed_until = time.monotonic() + 5
    va.handle_text("peki ya dün?", direct=False)  # follow-up window closed
    time.sleep(0.3)
    assert agent.turns == []
    va.handle_text("Alice, müziği aç", direct=False)
    assert wait_for(lambda: agent.turns == ["müziği aç"])
    social["on"] = ""
    va.handle_text("Bak şimdi Alice sesi kıs lütfen", direct=False)
    assert wait_for(lambda: agent.turns[-1] == "sesi kıs lütfen")


def test_unchecked_wake_is_logged_with_reason(monkeypatch):
    from alice.voice import assistant as va_mod

    logs = []
    monkeypatch.setattr(va_mod, "voice_log", logs.append)
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=CloudSTT(), speaker_gate=gate())
    assert va._is_user(Audio(b"\2\0" * 4000, 16000))  # 0.25 s
    assert "kısa ses 0.25 sn" in logs[-1]


# ---- from the app: "Sesimi tanıt" (introduce my voice)
class Mic:
    """Speech/silence frames in order: (frame count, whether it is loud)."""

    def __init__(self, script):
        self.frames = [b"\1\x40" * 480 if loud else b"\0\0" * 480 for n, loud in script for _ in range(n)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        time.sleep(0.0005)
        self.marked = sid.enrolling()
        return self.frames.pop(0) if self.frames else b"\0\0" * 480


class LoudVAD:
    def is_speech(self, frame, rate=16000):
        return frame[:1] == b"\1"


def wait_state(s, *states, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = s.view()
        if v["state"] in states:
            return v
        time.sleep(0.01)
    raise AssertionError(f"durum gelmedi: {states}, son: {s.view()['state']}")


def test_app_enroll_records_each_sentence_saves_and_tries_out(tmp_path, monkeypatch):
    from alice.voice.enroll import EnrollSession

    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    monkeypatch.setattr(sid, "available", lambda: True)
    (tmp_path / "models").mkdir()
    sid.model_path().write_bytes(b"x")
    talk = [(40, True), (40, False)]  # 1.2 s of speech + silence
    script = [(5, False)] + talk * len(sid.ENROLL_SENTENCES) + talk
    mic = Mic(script)
    s = EnrollSession(mic=lambda: mic, vad=LoudVAD, embedder=FakeEmbedder, listen_seconds=5, got_pause=0)
    assert s.start()["ok"] and not s.start()["ok"]  # cannot be started a second time
    s._thread.join(10)
    v = s.view()
    assert mic.marked  # a running ALICE must not wake while recording
    assert v["state"] == "done" and v["result"]["recognized"] and v["profile"]["samples"] == len(sid.ENROLL_SENTENCES)
    assert len(v["takes"]) == len(sid.ENROLL_SENTENCES) and not sid.enrolling()  # marker cleared
    result = s.remove()
    assert result["ok"]
    assert sid.load_profile() is None


def test_app_enroll_cancel_and_silent_mic(tmp_path, monkeypatch):
    from alice.voice.enroll import EnrollSession

    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    monkeypatch.setattr(sid, "available", lambda: True)
    (tmp_path / "models").mkdir()
    sid.model_path().write_bytes(b"x")
    s = EnrollSession(mic=lambda: Mic([]), vad=LoudVAD, embedder=FakeEmbedder, listen_seconds=0.05)
    s.start()
    s._thread.join(10)
    v = s.view()
    assert "yeterli kayıt" in v["message"] and sid.load_profile() is None
    s = EnrollSession(mic=lambda: Mic([]), vad=LoudVAD, embedder=FakeEmbedder, listen_seconds=30)
    s.start()
    wait_state(s, "recording")
    s.cancel()
    s._thread.join(10)
    assert s.view()["state"] == "cancelled" and s.view()["message"] == "iptal edildi"
    assert s.test()["ok"] is False  # no profile: nothing to try


def test_app_enroll_installs_package_and_downloads_with_progress(tmp_path, monkeypatch):
    from alice.voice.enroll import EnrollSession

    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    have = []
    monkeypatch.setattr(sid, "available", lambda: bool(have))
    s = EnrollSession(pip=lambda: have.append(1) or 0)
    assert not s.view()["available"]
    s.install()
    s._thread.join(10)
    assert s.view()["message"] == "sherpa-onnx kuruldu" and s.view()["available"]
    steps = []

    def fake_download(emit=None, progress=lambda d, t: None):
        for i in range(1, 5):
            progress(i * 10, 40)
            steps.append(s.view()["progress"])
        sid.model_path().parent.mkdir(parents=True, exist_ok=True)
        sid.model_path().write_bytes(b"x")

    monkeypatch.setattr(sid, "download_model", fake_download)
    s.make_embedder, s.make_mic, s.make_vad, s.listen_seconds = FakeEmbedder, (lambda: Mic([])), LoudVAD, 0.05
    s.start()
    s._thread.join(10)
    assert s.view()["state"] == "error" and steps == [0.25, 0.5, 0.75, 1.0]


def test_wake_ignored_while_enrolling(tmp_path, monkeypatch):
    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    sid.enrolling_path().parent.mkdir(parents=True, exist_ok=True)
    sid.enrolling_path().touch()
    va = VoiceAssistant.__new__(VoiceAssistant)
    va.speaker_gate = None
    assert va._is_user(Audio(b"\1\0" * 16000, 16000)) is False
    sid.enrolling_path().unlink()
    assert va._is_user(Audio(b"\1\0" * 16000, 16000)) is True


class NoisyMic(Mic):
    """Discord/game audio: sound goes on after speech ends (VAD still says "konuşma") but far quieter than speech."""

    def __init__(self, talk_frames=60, tail=b"\1\x02"):
        self.frames = [b"\1\x40" * 480] * talk_frames + [tail * 480] * 2000


class AlwaysSpeech:
    def is_speech(self, frame, rate=16000):
        return True


def _session(tmp_path, monkeypatch, mic, vad=AlwaysSpeech, **kw):
    from alice.voice.enroll import EnrollSession

    monkeypatch.setattr(sid, "voice_dir", lambda: tmp_path)
    return EnrollSession(mic=lambda: mic, vad=vad, embedder=FakeEmbedder, got_pause=0, **kw)


def test_record_ends_when_level_drops_under_background_noise(tmp_path, monkeypatch):
    # User: "okuyorum sonra öyle kalıyor, algıladı mı algılamadı mı belli değil" (it stays on; unclear if heard)
    s = _session(tmp_path, monkeypatch, NoisyMic())
    mic = NoisyMic()
    t0 = time.monotonic()
    audio = s._record(mic, AlwaysSpeech())
    took = 60 * 0.03
    assert audio is not None and took <= audio.seconds < took + 1.2  # did not wait 7 s
    assert time.monotonic() - t0 < 5


def test_record_caps_take_and_reports_progress(tmp_path, monkeypatch):
    from alice.voice import enroll

    monkeypatch.setattr(enroll, "MAX_TAKE", 1.5)
    s = _session(tmp_path, monkeypatch, None)
    mic = NoisyMic(talk_frames=2000)  # speech that never ends
    audio = s._record(mic, AlwaysSpeech())
    assert 1.4 <= audio.seconds <= 1.8
    v = s.view()
    assert v["took"] >= 1.4 and v["left"] >= 0


def test_done_button_takes_what_was_heard_and_skip_moves_on(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, None, listen_seconds=30)
    got = []

    class SlowLoud(Mic):
        def __init__(self):
            self.n = 0

        def read(self):
            self.n += 1
            if self.n == 60:
                s.done()  # the user pressed "Okudum" (done reading)
            time.sleep(0.0005)
            return b"\1\x40" * 480

    got.append(s._record(SlowLoud(), AlwaysSpeech()))
    assert got[0] is not None and got[0].seconds >= 1.0

    class Quiet(Mic):
        def __init__(self):
            self.n = 0

        def read(self):
            self.n += 1
            if self.n == 20:
                s.skip()
            return b"\0\0" * 480

    assert s._record(Quiet(), LoudVAD()) is None


def test_enroll_skip_sentence_continues_to_next(tmp_path, monkeypatch):
    monkeypatch.setattr(sid, "available", lambda: True)
    (tmp_path / "models").mkdir()
    sid.model_path().parent.mkdir(parents=True, exist_ok=True)
    talk = [(40, True), (40, False)]
    mic = Mic([(5, False)] + talk * (len(sid.ENROLL_SENTENCES) + 1))
    s = _session(tmp_path, monkeypatch, mic, vad=LoudVAD, listen_seconds=5)
    sid.model_path().write_bytes(b"x")
    s._skip.set()  # first sentence is skipped
    orig = s._record
    first = []

    def rec(m, v):
        if not first:
            first.append(1)
            return None
        return orig(m, v)

    s._record = rec
    s.start()
    s._thread.join(20)
    v = s.view()
    assert v["state"] == "done" and len(v["takes"]) == len(sid.ENROLL_SENTENCES) - 1


class CountingSTT(CloudSTT):
    def __init__(self, text="Hey Alice, saat kaç?"):
        super().__init__(text)
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.text


def test_only_users_voice_reaches_stt_short_audio_left_to_wake_check():
    stt = CountingSTT()
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=stt, speaker_gate=gate())
    va.handle_audio(Audio(bytes([2, 0]) * 16000, 16000), direct=False)  # someone else: never transcribed
    assert stt.calls == 0
    # 0.55 s: a quick "Alice naber" can be this short; voiceprint can't decide, so it's left to the wake-word check
    va.handle_audio(Audio(bytes([1, 0]) * 8800, 16000), direct=False)
    assert stt.calls == 1


def test_barge_in_waits_for_users_voice():
    sp = FakeSpeaker()
    va = VoiceAssistant(FakeAgent(), sp, stt=CountingSTT("bir dakika"), speaker_gate=gate())
    cut = []
    va._interrupt = lambda: cut.append(1)
    sp.speaking = True
    va._on_speech_start()
    assert cut == []  # speech started but whose it is unknown: don't stop
    va.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=False)
    assert cut == []  # it was someone else's
    va._on_speech_start()
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=False)
    assert cut == [1]  # the user: stop now


def test_second_ear_finds_missed_wake_word():
    class Gate:
        last_conf = 0.0
        model_name = "base"
        last_seconds = 0.1

        def heard(self, audio):
            return "Ayşe nabe"  # the small model missed "Alice naber"

    class Scribe:
        name = "eleven:scribe_v2"

        def transcribe(self, audio):
            return "Alice naber?"

    class STT:
        wake_gate = Gate()
        engines = [Scribe()]

        def transcribe(self, audio):
            return "Alice naber?"

    agent = FakeAgent()
    va = VoiceAssistant(agent, FakeSpeaker(), stt=STT(), speaker_gate=gate())
    va.handle_audio(Audio(bytes([1, 0]) * 16000, 16000), direct=False)
    assert wait_for(lambda: agent.turns == ["naber?"])


def test_paused_alice_then_naber_joined_for_second_ear():
    # A pause between "Alice" ... "naber" made it two sentences; "Alice" alone was heard as "Altyazı" and dropped
    alice, naber = Audio(bytes([1, 0]) * 6400, 16000), Audio(bytes([1, 0]) * 9600, 16000)

    class Gate:
        last_conf, model_name, last_seconds = 0.0, "base", 0.1

        def heard(self, audio):
            return "Altyazı." if audio.seconds < 0.5 else "Nabeş."

    class Groq:
        name = "groq:x"

        def __init__(self):
            self.heard = []

        def transcribe(self, audio):
            self.heard.append(round(audio.seconds, 2))
            return "Ali? Ne haber?" if audio.seconds > 1.0 else "Altyazı M.K."

    groq = Groq()

    class STT:
        wake_gate = Gate()
        engines = [groq]

        def transcribe(self, audio):
            return groq.transcribe(audio)

    agent = FakeAgent()
    va = VoiceAssistant(agent, FakeSpeaker(), stt=STT(), speaker_gate=gate(),
                        social=lambda: "Discord sesli konuşmada")
    va.handle_audio(alice, direct=False)
    assert agent.turns == []
    va.handle_audio(naber, direct=False)
    assert wait_for(lambda: agent.turns == ["Ne haber?"])
    assert 1.15 in groq.heard  # 0.4 + 0.15 gap + 0.6: both sentences were heard together


def test_social_mode_takes_sentence_right_after_bare_wake():
    # In Discord, after the user said "Alice" and ALICE replied "Evet, söyle", the next sentence went unanswered
    class STT:
        wake_gate = None

        def __init__(self):
            self.texts = iter(["Alice.", "Naber?", "Ahmet gel buraya"])

        def transcribe(self, audio):
            return next(self.texts)

    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=STT(), speaker_gate=gate(), social=lambda: "Discord sesli konuşmada")
    one = Audio(bytes([1, 0]) * 16000, 16000)
    va.handle_audio(one, direct=False)
    assert sp.said  # short reaction
    va.handle_audio(one, direct=va._armed())
    assert wait_for(lambda: agent.turns == ["Naber?"])
    va._called_until = 0.0  # window closed: a sentence said to a friend is not sent
    va.handle_audio(one, direct=va._armed())
    time.sleep(0.2)
    assert agent.turns == ["Naber?"]


def test_discord_follow_up_continues_only_for_users_voice():
    """Discord: after a reply, follow-up without "Alice". Only the user's voiceprint match goes to the live model."""
    from tests.test_voice import FakeLive

    class AudioLive(FakeLive):
        prefers_audio = True

        def converse(self, text, frames, stop, first_audio=b"", **kw):
            self.texts.append(first_audio[:1])

    class NoSTT:
        def transcribe(self, audio):
            raise AssertionError("takip cümlesi yazıya çevrilmemeli")

    live = AudioLive()
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=NoSTT(), live=live, speaker_gate=gate(),
                        social=lambda: "Discord")
    va.armed_until = time.monotonic() + 5  # ALICE just replied
    va.handle_audio(Audio(b"\2\0" * 16000, 16000), direct=va._armed())  # noqa: SLF001 - a friend's voice
    va.handle_audio(Audio(b"\1\0" * 16000, 16000), direct=va._armed())  # noqa: SLF001 - the user's voice
    assert wait_for(lambda: live.texts == [b"\1"])
    va.social_follow = False
    assert not va._armed()  # noqa: SLF001 - once off, Discord needs "Alice" again
