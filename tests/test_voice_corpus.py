"""Tests with real audio (instead of fake audio).

1. tests/fixtures/voice: 10 Turkish/Spanish sentences made with Edge TTS (no user voice; in the repo). Tests the
   wake decision pipeline end to end with real local Whisper ("Alışveriş" does not wake, "Alice, saat kaç?" wakes).
2. ~/.alice/voice/corpus: the user's real sentences (outside the repo, collected by the core). Runs locally; in CI
   skipped, where faster-whisper/sherpa-onnx and the corpus are absent. Root: ALICE_CORPUS or the real home dir.
3. Collection logic (fake audio, also runs in CI): wake record, correction label, daily limit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alice.voice import corpus
from alice.voice.audio import Audio

FIXTURES = Path(__file__).parent / "fixtures" / "voice"
REAL_HOME = Path(os.environ.get("USERPROFILE") or Path.home()) / ".alice" / "voice"


def _need_whisper():
    pytest.importorskip("faster_whisper")
    try:
        from faster_whisper.utils import download_model  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("faster-whisper modeli yok")


# ------------------------------------------------------------------ 1. TTS samples in the repo
@pytest.fixture(scope="module")
def fixture_assistant():
    _need_whisper()
    try:
        return corpus.build_replay_assistant(speaker=False, names=[])
    except Exception as e:  # noqa: BLE001 - model could not be downloaded (offline CI)
        pytest.skip(f"yerel Whisper kurulamadı: {e}")


FIXTURE_CASES = corpus.load_manifest(FIXTURES)


@pytest.mark.parametrize("case", FIXTURE_CASES, ids=[c.id for c in FIXTURE_CASES])
def test_tts_sentence_wake_decision(case, fixture_assistant):
    d = corpus.replay(case, fixture_assistant, FIXTURES)
    assert d.woke == (case.label == "uyanmaliydi"), f"{case.note} -> {d.reason}: {d.head!r}"


# ------------------------------------------------------------------ 2. the user's real corpus
def _real_root() -> Path:
    return Path(os.environ["ALICE_CORPUS"]) if os.environ.get("ALICE_CORPUS") else REAL_HOME / "corpus"


@pytest.fixture(scope="module")
def real():
    base = _real_root()
    cases = corpus.load_manifest(base)
    if not cases:
        pytest.skip("gerçek ses korpusu yok (çekirdek toplar; alice voice corpus import)")
    _need_whisper()
    pytest.importorskip("sherpa_onnx")
    if not (REAL_HOME / "speaker.json").exists():
        pytest.skip("ses izi profili yok")
    va = corpus.build_replay_assistant(home=REAL_HOME)
    return base, cases, corpus.evaluate(cases, va, base)


def test_no_false_wakes_on_confirmed_negatives(real):
    _, _, rep = real
    assert rep["false_wakes_strong"] == []


def test_other_speakers_never_pass_the_voice_print(real):
    _, _, rep = real
    others = [r for r in rep["rows"] if r["bucket"] == "baska-konusmaci"]
    assert not [r["id"] for r in others if r["woke"]]


def test_missed_wakes_stay_rare(real):
    _, _, rep = real
    strong_pos = [r for r in rep["rows"] if r["expect"] is True and r["confidence"] in corpus.STRONG]
    if len(strong_pos) < 20:
        pytest.skip(f"yeterli 'uyanmalıydı' kaydı yok ({len(strong_pos)}/20): ALICE'e seslendikçe birikir")
    missed = [r for r in strong_pos if not r["woke"]]
    assert len(missed) / len(strong_pos) <= 0.10, missed


def test_replay_is_deterministic(real):
    base, cases, _ = real
    va = corpus.build_replay_assistant(home=REAL_HOME)
    sample = cases[:8]
    first = [corpus.replay(c, va, base) for c in sample]
    again = [corpus.replay(c, va, base) for c in sample]
    assert [(d.woke, d.reason) for d in first] == [(d.woke, d.reason) for d in again]


# ------------------------------------------------------------------ 3. collection (fake audio, also runs in CI)
def _audio():
    return Audio(b"\1\0" * 16000, 16000)


def test_wake_is_collected_and_confirmed_without_correction(tmp_path, monkeypatch):
    c = corpus.Collector(tmp_path)
    c.on_decision(_audio(), {"woke": True, "reason": "uyandi", "spk": 0.5, "head": "Alice saat kaç",
                             "armed": False, "social": ""})
    (case,) = corpus.load_manifest(tmp_path)
    assert case.label == "uyanmaliydi" and case.confidence == "zayif" and case.wav(tmp_path).exists()
    c.CONFIRM_AFTER = -1  # time window expired, no correction came
    c.on_decision(_audio(), {"woke": False, "reason": "kisa"})
    assert corpus.load_manifest(tmp_path)[0].confidence == "guclu"


def test_correction_relabels_the_last_wake(tmp_path):
    c = corpus.Collector(tmp_path)
    for text, label in (("Sana demedim.", "uyanmamaliydi"), ("Yanlış anladın beni", "yanlis_dokum"),
                        ("[gemini duymadı] Alışacak", "belirsiz")):
        c.on_decision(_audio(), {"woke": True, "reason": "uyandi", "head": "Alice"})
        c.on_correct(text)
        assert corpus.load_manifest(tmp_path)[-1].label == label


def test_task_corrections_do_not_touch_wake_labels(tmp_path):
    """"Hayır, YouTube'da" or "saçmaladın": the wake was right, the task was wrong; the wake label does not change."""
    c = corpus.Collector(tmp_path)
    c.on_decision(_audio(), {"woke": True, "reason": "uyandi", "head": "Alice Spotify'ı aç"})
    for text in ("Hayır, YouTube'da", "saçmaladın yine"):
        c.on_correct(text)
    assert corpus.load_manifest(tmp_path)[0].label == "uyanmaliydi"


def test_correction_sentence_itself_is_not_the_false_wake(tmp_path):
    """After the session closes, "Alice sana demedim" itself is also recorded as a wake; the label must go to
    the wake before it (review finding)."""
    c = corpus.Collector(tmp_path)
    c.on_decision(_audio(), {"woke": True, "reason": "uyandi", "head": "Alışacak"})
    c.on_decision(_audio(), {"woke": True, "reason": "uyandi", "head": "Alice sana demedim"})
    c.on_correct("Alice sana demedim")
    first, second = corpus.load_manifest(tmp_path)
    assert first.label == "uyanmamaliydi" and second.label == "uyanmaliydi"


def test_case_limit_survives_restart(tmp_path):
    c = corpus.Collector(tmp_path)
    c.on_decision(_audio(), {"woke": True, "reason": "uyandi", "head": "Alice"})
    fresh = corpus.Collector(tmp_path)  # core restarted
    fresh.MAX_CASES = 1
    fresh.on_decision(_audio(), {"woke": True, "reason": "uyandi", "head": "Alice"})
    assert len(corpus.load_manifest(tmp_path)) == 1


def test_negatives_are_capped_and_other_voices_keep_no_text(tmp_path):
    c = corpus.Collector(tmp_path)
    c.NEG_PER_DAY = 2
    for _ in range(5):
        c.on_decision(_audio(), {"woke": False, "reason": "uyandirma_yok", "spk": 0.5, "thr": 0.3,
                                 "head": "otur çabuk"})
    c.on_decision(_audio(), {"woke": False, "reason": "uyandirma_yok", "spk": 0.25, "thr": 0.3, "head": "gri söz"})
    c.on_decision(_audio(), {"woke": False, "reason": "uyandirma_yok", "spk": None, "head": "profil yok"})
    c.on_decision(_audio(), {"woke": False, "reason": "ses_izi", "spk": 0.1, "head": "gizli söz"})
    cases = corpus.load_manifest(tmp_path)
    assert sum(c.bucket == "negatif-konusma" for c in cases) == 2  # grey zone and profile-less voices were not stored
    other = [c for c in cases if c.bucket == "baska-konusmaci"]
    assert len(other) == 1 and other[0].orig["head"] == "" and other[0].confidence == "guclu"


def test_daily_limit_survives_restart(tmp_path):
    c = corpus.Collector(tmp_path)
    c.NEG_PER_DAY = 2
    neg = {"woke": False, "reason": "uyandirma_yok", "spk": 0.5, "thr": 0.3, "head": "otur çabuk"}
    c.on_decision(_audio(), neg)
    c.on_decision(_audio(), neg)
    fresh = corpus.Collector(tmp_path)  # core restarted: the counter must not reset
    fresh.NEG_PER_DAY = 2
    fresh.on_decision(_audio(), neg)
    assert len(corpus.load_manifest(tmp_path)) == 2


def test_voice_print_reject_imports_as_unsure():
    assert corpus.label_from_decision({"woke": False, "reason": "ses_izi", "spk": 0.27})[0] == "belirsiz"
    assert corpus.label_from_decision({"woke": False, "reason": "uyandirma_yok", "spk": 0.5, "head": ""})[1] == \
        "konusma-yok"


def test_assistant_writes_decision_next_to_recent_audio(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from alice.voice.assistant import VoiceAssistant
    from tests.test_voice import FakeAgent, FakeSpeaker

    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    seen = []
    stt = SimpleNamespace(wake_gate=None, transcribe=lambda a: "naber, nasılsın")
    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=stt)
    va.keep_recent = True
    va.on_decision = lambda audio, d, path: seen.append((d["reason"], path))
    va.handle_audio(_audio(), direct=False)
    (reason, path), = seen
    assert reason == "uyandirma_yok" and json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["head"]
