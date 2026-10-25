"""Konuşmacı doğrulama: ALICE sadece kullanıcının sesine cevap versin.

Discord'daki arkadaşların, odadakiler ya da videodaki biri "Alice" deyince ALICE uyanıyordu. Ayarlar → Ses → Sesimi
tanıt (ya da `alice voice enroll`) kullanıcıdan birkaç cümle okur, sesinden bir "ses izi" (konuşmacı gömmesi) çıkarıp ~/.alice/voice/speaker.json'a
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


def enrolling_path() -> Path:
    return voice_dir() / "enrolling"


def enrolling() -> bool:
    """Uygulamada ses tanıtılıyor mu? Okunan cümleler ("Hey Alice, saat kaç?") çalışan ALICE'i uyandırmasın.
    Sihirbaz dosyaya birkaç saniyede bir dokunur; yarıda çökerse iki dakika sonra kendiliğinden geçersiz olur."""
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
    """Modeli bir kez indir (~40 MB). Yarım kalan indirme bir sonrakini bozmasın diye önce .part'a yazılır.
    progress(indirilen, toplam bayt) her parçada çağrılır (toplam bilinmiyorsa 0)."""
    path = model_path()
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    emit(f"ses tanıma modeli indiriliyor (~40 MB): {MODEL_NAME}")
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
    """Uyanmada "bu ses kullanıcının mı?" sorusu. Profil, model ya da paket yoksa her şeyi kabul eder.

    path verilirse profil dosyası izlenir: uygulamadan sesini tanıtınca ya da kapatınca çalışan çekirdek yeniden
    başlamadan yeni profili kullanır. Model (Embedder) ilk gerektiğinde yüklenir."""

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
        self.last_note = ""  # karar verilemediyse nedeni (kısa ses, hata): voice.log'a yazılır
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
        """Profil dosyası değiştiyse yeniden oku (silindiyse kontrol kapanır)."""
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
            self.log(f"ses tanıma profili yüklendi (eşik {self.threshold:.2f})" if self.active
                     else "ses tanıma profili okunamadı")

    @classmethod
    def load(cls, cfg: Any = None, log: Callable[[str], None] = lambda s: None) -> "SpeakerGate | None":
        """Sesli asistan açılırken: profil henüz yoksa da kapı kurulur, profil gelince kendiliğinden devreye girer."""
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
            why = "model yüklenemiyor"
        elif not available():
            why = "sherpa-onnx kurulu değil (Ayarlar → Ses → Sesimi tanıt kurar)"
        elif not model_path().is_file():
            why = "ses tanıma modeli yok (Ayarlar → Ses → Sesimi tanıt indirir)"
        else:
            try:
                self.embedder = self.make_embedder()
                return True
            except Exception as e:  # noqa: BLE001 - bozuk model ya da uyumsuz paket: asistan yine çalışsın
                why = f"ses tanıma açılamadı: {e}"
        self.last_note = why + ", kontrol edilmedi"
        if why != self._warned:
            self._warned = why
            self.log("ses tanıma: " + why)
        return False

    def check(self, pcm: bytes, rate: int = 16000) -> bool:
        """True: kullanıcının sesi (ya da profil yok / karar verilemeyecek kadar kısa)."""
        self.refresh()
        self.last_note = ""
        if not self.active:
            self.last_score = 1.0
            return True
        if len(pcm) / 2 / rate < MIN_SECONDS:
            self.last_score = 1.0
            self.last_note = f"kısa ses {len(pcm) / 2 / rate:.2f} sn, kontrol edilmedi"
            return True
        if not self._ensure_embedder():
            self.last_score = 1.0
            return True
        try:
            self.last_score = cosine(self.vector or [], self.embedder.embed(pcm, rate))
        except Exception as e:  # noqa: BLE001 - ses tanıma hata verirse kullanıcıyı cezalandırma
            self.last_score = 1.0
            self.last_note = f"ses tanıma hatası, kontrol edilmedi: {type(e).__name__}: {str(e)[:120]}"
            return True
        return self.last_score >= self.threshold
