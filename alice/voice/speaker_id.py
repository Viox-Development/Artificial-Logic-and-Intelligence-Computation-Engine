"""Speaker verification: ALICE only answers the user's voice.

Friends on Discord, people in the room, or someone in a video used to wake ALICE by saying "Alice".
The user enrolls their voice with Sistem → Ses → Sesimi tanıt (or `alice voice enroll`): they read a few sentences,
a "voice fingerprint" (speaker embedding) is extracted from them and saved to ~/.alice/voice/speaker.json.
Then, on every wake-up and in the follow-up window where the user keeps talking without saying "Alice", the voice of
the sentence is compared with this fingerprint. If it does not match, no answer is given. Push-to-talk (the user
pressed the key) is always accepted.

Model: NVIDIA TitaNet-small (sherpa-onnx, ~40 MB, ~20 ms per recording, offline). In a test with 14 different voices,
it never rejected the voice enrolled with 4 sentences; at threshold 0.6 it accepted 3% of the other voices.
The threshold is adjusted per person from how similar their enrollment recordings are, and stays between 0.45-0.6
(a wrong rejection is more annoying than a wrong acceptance: an assistant that does not answer the user is useless).
The score of every comparison is written to voice.log.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from alice.i18n import spoken
from alice.config import alice_home

MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_NAME)
MIN_SECONDS = 0.6  # voice fingerprints are unreliable on shorter audio: no decision is made, it is accepted
THRESHOLD_RANGE = (0.45, 0.6)
# Read aloud while enrolling: English keys, read in the speech language (enroll_sentences()).
ENROLL_SENTENCES = [
    "Hey Alice, what time is it?",
    "Open YouTube and play a song.",
    "What will the weather be like today, will it rain?",
    "Can you look at the latest messages on Discord?",
    "How fast am I flying right now, do I have enough fuel?",
    "Remind me to call my friend in an hour.",
]


def enroll_sentences() -> list[str]:
    return [spoken(s) for s in ENROLL_SENTENCES]


def voice_dir() -> Path:
    return alice_home() / "voice"


def model_path() -> Path:
    return voice_dir() / "models" / MODEL_NAME


def profile_path() -> Path:
    return voice_dir() / "speaker.json"


def enrolling_path() -> Path:
    return voice_dir() / "enrolling"


def enrolling() -> bool:
    """Is a voice being enrolled in the app? The sentences read out ("Hey Alice, saat kaç?") must not wake
    the running ALICE. The wizard touches the file every few seconds; if it crashes midway, it becomes
    invalid on its own after two minutes."""
    try:
        return time.time() - enrolling_path().stat().st_mtime < 120
    except OSError:
        return False


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return True


def download_model(emit: Callable[[str], None] = lambda s: None,
                   progress: Callable[[int, int], None] = lambda done, total: None) -> Path:
    """Download the model once (~40 MB). It is first written to .part so that an unfinished download does not
    break the next one. progress(downloaded, total bytes) is called for each chunk (0 if the total is unknown)."""
    path = model_path()
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    emit(f"downloading the voice recognition model (~40 MB): {MODEL_NAME}")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(part, "wb") as f:  # noqa: S310 - sabit https adresi
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 18):
            f.write(chunk)
            done += len(chunk)
            progress(done, total)
    part.replace(path)
    return path


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(_norm(a), _norm(b)))


def mean_vector(vectors: list[list[float]]) -> list[float]:
    return _norm([sum(col) / len(vectors) for col in zip(*vectors)])


def calibrate(embeddings: list[list[float]]) -> tuple[float, float]:
    """(threshold, consistency). Consistency: how similar each recording is to the fingerprint made from the others
    (the worst one). If the user's own recordings are not very similar to each other (noisy room, distant mic),
    the threshold goes down too."""
    if len(embeddings) < 2:
        return THRESHOLD_RANGE[0], 0.0
    loo = [cosine(mean_vector([e for j, e in enumerate(embeddings) if j != i]), embeddings[i])
           for i in range(len(embeddings))]
    worst = min(loo)
    lo, hi = THRESHOLD_RANGE
    return round(min(hi, max(lo, worst - 0.15)), 3), round(worst, 3)


class Embedder:
    """sherpa-onnx TitaNet: 16 kHz int16 PCM -> voice fingerprint vector."""

    def __init__(self, path: Path | None = None):
        import sherpa_onnx

        self._so = sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path or model_path()), num_threads=1)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self._lock = threading.Lock()  # the extractor is not thread-safe

    def embed(self, pcm: bytes, rate: int = 16000) -> list[float]:
        import array

        samples = array.array("h", pcm)
        floats = [s / 32768.0 for s in samples]
        with self._lock:
            st = self._ex.create_stream()
            st.accept_waveform(rate, floats)
            st.input_finished()
            return list(self._ex.compute(st))


def save_profile(embeddings: list[list[float]], path: Path | None = None) -> dict[str, Any]:
    threshold, consistency = calibrate(embeddings)
    data = {"model": MODEL_NAME, "vector": mean_vector(embeddings), "threshold": threshold,
            "consistency": consistency, "samples": len(embeddings), "created": time.time()}
    path = path or profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def load_profile(path: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads((path or profile_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("vector") else None


class SpeakerGate:
    """On wake-up, the question: "is this voice the user's?". If there is no profile, model or package, everything
    is accepted.

    If path is given, the profile file is watched: when the voice is enrolled or removed in the app, the running core
    uses the new profile without restarting. The model (Embedder) is loaded when first needed."""

    def __init__(self, profile: dict[str, Any] | None = None, embedder: Any = None, threshold: float | None = None,
                 path: Path | None = None, make_embedder: Callable[[], Any] | None = None,
                 log: Callable[[str], None] = lambda s: None):
        self.fixed_threshold = threshold
        self.vector: list[float] | None = None
        self.threshold = THRESHOLD_RANGE[0]
        self._apply(profile)
        self.embedder = embedder
        self.make_embedder = make_embedder
        self.path = path
        self._mtime: float | None = None
        self.log = log
        self._warned = ""
        self.last_score = 0.0
        self.last_note = ""  # if no decision was made, the reason (short audio, error): written to voice.log
        if path is not None:
            self.refresh()

    def _apply(self, profile: dict[str, Any] | None) -> None:
        self.vector = list(profile["vector"]) if profile and profile.get("vector") else None
        thr = self.fixed_threshold if self.fixed_threshold is not None else (profile or {}).get("threshold")
        self.threshold = float(thr if thr is not None else THRESHOLD_RANGE[0])

    @property
    def active(self) -> bool:
        return self.vector is not None

    def refresh(self) -> None:
        """Re-read the profile file if it changed (if it was deleted, the check is turned off)."""
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self._apply(load_profile(self.path) if mtime is not None else None)
        if mtime is not None:
            self.log(f"voice recognition profile loaded (threshold {self.threshold:.2f})" if self.active
                     else "could not read the voice recognition profile")

    @classmethod
    def load(cls, cfg: Any = None, log: Callable[[str], None] = lambda s: None) -> "SpeakerGate | None":
        """When the voice assistant starts: the gate is set up even without a profile yet; it switches on by itself
        once a profile arrives."""
        v = (getattr(cfg, "data", None) or {}).get("voice", {}) if cfg is not None else {}
        if v.get("speaker_check") is False:
            return None
        thr = v.get("speaker_threshold")
        return cls(threshold=float(thr) if thr is not None else None, path=profile_path(),
                   make_embedder=Embedder, log=log)

    def _ensure_embedder(self) -> bool:
        if self.embedder is not None:
            return True
        why = ""
        if self.make_embedder is None:
            why = "the model can't be loaded"
        elif not available():
            why = "sherpa-onnx is not installed (System → Voice → Teach my voice installs it)"
        elif not model_path().is_file():
            why = "no voice recognition model (System → Voice → Teach my voice downloads it)"
        else:
            try:
                self.embedder = self.make_embedder()
                return True
            except Exception as e:  # noqa: BLE001 - broken model or incompatible package: the assistant still runs
                why = f"could not start voice recognition: {e}"
        self.last_note = why + ", kontrol edilmedi"
        if why != self._warned:
            self._warned = why
            self.log("voice recognition: " + why)
        return False

    def usable(self) -> bool:
        """A profile exists and the model can be opened: the voice can really be compared."""
        self.refresh()
        return self.active and self._ensure_embedder()

    def check(self, pcm: bytes, rate: int = 16000) -> bool:
        """True: the user's voice (or there is no profile / the audio is too short to decide)."""
        self.refresh()
        self.last_note = ""
        if not self.active:
            self.last_score = 1.0
            return True
        if len(pcm) / 2 / rate < MIN_SECONDS:
            self.last_score = 1.0
            self.last_note = f"short audio {len(pcm) / 2 / rate:.2f} s, not checked"
            return True
        if not self._ensure_embedder():
            self.last_score = 1.0
            return True
        try:
            self.last_score = cosine(self.vector or [], self.embedder.embed(pcm, rate))
        except Exception as e:  # noqa: BLE001 - if voice recognition fails, do not penalize the user
            self.last_score = 1.0
            self.last_note = f"voice recognition error, not checked: {type(e).__name__}: {str(e)[:120]}"
            return True
        return self.last_score >= self.threshold
