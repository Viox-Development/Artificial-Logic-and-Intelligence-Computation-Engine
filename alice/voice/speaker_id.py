"""Konuşmacı doğrulama: ALICE sadece kullanıcının sesine cevap versin.

Discord'daki arkadaşların, odadakiler ya da videodaki biri "Alice" deyince ALICE uyanıyordu. `alice voice enroll`
kullanıcıdan birkaç cümle okur, sesinden bir "ses izi" (konuşmacı gömmesi) çıkarıp ~/.alice/voice/speaker.json'a
yazar. Sonra her uyanmada ve "Alice" demeden devam edilen takip penceresinde söylenen cümlenin sesi bu izle
karşılaştırılır; benzemiyorsa cevap verilmez. Bas-konuş (kullanıcı tuşa bastı) hep kabul edilir.

Model: NVIDIA TitaNet-small (sherpa-onnx, ~40 MB, kayıt başına ~20 ms, çevrimdışı). 14 farklı sesle yapılan
denemede, 4 cümleyle tanıtılan sesi hiç reddetmedi; başka sesleri eşik 0,6'da %3 kabul etti. Eşik kişiye göre
tanıtım kayıtlarının birbirine benzerliğinden ayarlanır ve 0,45-0,6 arasında kalır (yanlış red, yanlış kabulden
daha can sıkıcı: kullanıcıya cevap vermeyen asistan işe yaramaz). Her karşılaştırmanın skoru voice.log'a yazılır.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_NAME)
MIN_SECONDS = 0.6  # bundan kısa seste ses izi güvenilmez: karar verilmez, kabul edilir
THRESHOLD_RANGE = (0.45, 0.6)
ENROLL_SENTENCES = [
    "Hey Alice, saat kaç?",
    "YouTube'u aç ve bir şarkı çal.",
    "Bugün hava nasıl olacak, yağmur yağacak mı?",
    "Discord'daki son mesajlara bir bakar mısın?",
    "Şu an ne kadar hızlı uçuyorum, yakıtım yeter mi?",
    "Bir saat sonra arkadaşımı aramamı hatırlat.",
]


def voice_dir() -> Path:
    return alice_home() / "voice"


def model_path() -> Path:
    return voice_dir() / "models" / MODEL_NAME


def profile_path() -> Path:
    return voice_dir() / "speaker.json"


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return True


def download_model(emit: Callable[[str], None] = lambda s: None) -> Path:
    """Modeli bir kez indir (~40 MB). Yarım kalan indirme bir sonrakini bozmasın diye önce .part'a yazılır."""
    path = model_path()
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    emit(f"ses tanıma modeli indiriliyor (~40 MB): {MODEL_NAME}")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(part, "wb") as f:  # noqa: S310 - sabit https adresi
        while chunk := r.read(1 << 20):
            f.write(chunk)
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
    """(eşik, tutarlılık). Tutarlılık: her kayıt, diğerlerinden çıkarılan ize ne kadar benziyor (en kötüsü).
    Kendi kayıtları birbirine az benzeyen (gürültülü oda, uzak mikrofon) kullanıcıda eşik de aşağı iner."""
    if len(embeddings) < 2:
        return THRESHOLD_RANGE[0], 0.0
    loo = [cosine(mean_vector([e for j, e in enumerate(embeddings) if j != i]), embeddings[i])
           for i in range(len(embeddings))]
    worst = min(loo)
    lo, hi = THRESHOLD_RANGE
    return round(min(hi, max(lo, worst - 0.15)), 3), round(worst, 3)


class Embedder:
    """sherpa-onnx TitaNet: 16 kHz int16 PCM -> ses izi vektörü."""

    def __init__(self, path: Path | None = None):
        import sherpa_onnx

        self._so = sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path or model_path()), num_threads=1)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self._lock = threading.Lock()  # extractor iş parçacığı güvenli değil

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
    """Uyanmada "bu ses kullanıcının mı?" sorusu. Profil, model ya da paket yoksa her şeyi kabul eder."""

    def __init__(self, profile: dict[str, Any], embedder: Any, threshold: float | None = None):
        self.vector = profile["vector"]
        self.threshold = float(threshold if threshold is not None else profile.get("threshold", THRESHOLD_RANGE[0]))
        self.embedder = embedder
        self.last_score = 0.0

    @classmethod
    def load(cls, cfg: Any = None, log: Callable[[str], None] = lambda s: None) -> "SpeakerGate | None":
        v = (getattr(cfg, "data", None) or {}).get("voice", {}) if cfg is not None else {}
        if v.get("speaker_check") is False:
            return None
        profile = load_profile()
        if profile is None:
            return None
        if not available():
            log("ses tanıma: sherpa-onnx kurulu değil, herkesin sesine cevap veriliyor (pip install sherpa-onnx)")
            return None
        if not model_path().is_file():
            log("ses tanıma: model yok, herkesin sesine cevap veriliyor (alice voice enroll ile indirilir)")
            return None
        try:
            emb = Embedder()
        except Exception as e:  # noqa: BLE001 - bozuk model ya da uyumsuz paket: asistan yine çalışsın
            log(f"ses tanıma açılamadı: {e}")
            return None
        thr = v.get("speaker_threshold")
        return cls(profile, emb, float(thr) if thr is not None else None)

    def check(self, pcm: bytes, rate: int = 16000) -> bool:
        """True: kullanıcının sesi (ya da karar verilemeyecek kadar kısa)."""
        if len(pcm) / 2 / rate < MIN_SECONDS:
            self.last_score = 1.0
            return True
        try:
            self.last_score = cosine(self.vector, self.embedder.embed(pcm, rate))
        except Exception:  # noqa: BLE001 - ses tanıma hata verirse kullanıcıyı cezalandırma
            self.last_score = 1.0
            return True
        return self.last_score >= self.threshold
