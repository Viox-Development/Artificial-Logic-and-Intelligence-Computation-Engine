"""Ses motorları.

Bulut: Gemini TTS. Sesin kendisi (ör. "Aoede") sabit kalır, ton stil talimatıyla değişir.
Yerel: XTTS v2 ve Chatterbox. İkisi de sesi bir referans kayıttan klonlar. Referans olarak
"duygu kütüphanesi" kullanılır: seçilen bulut sesinin her duyguda söylediği birer kayıt
(~/.alice/voice/library/<duygu>.wav). Böylece DCS açıkken buluttan, kapalıyken yerelden
konuşsa da hep aynı ALICE duyulur.

Son çare: Microsoft Edge'in sinir ağı sesleri (edge-tts). Anahtar ve kota yok, ~1 sn'de hazır; bulut
ve yerel ses düşünce (ya da yerel ses takılıp zaman aşımına uğrayınca) konuşur.

Son çare: Microsoft Edge'in sinir ağı sesleri (edge-tts). Anahtar ve kota yok, ~1 sn'de hazır; bulut
ve yerel ses düşünce (ya da yerel ses takılıp zaman aşımına uğrayınca) konuşur.

Seçim (TTSRouter): DCS açıkken (mod politikası local_gpu = false) yerel motor GPU'ya
dokunmaz; ya bulut kullanılır ya da yerel motor işlemcide çalışır. Hata veren motor bir
süre atlanır.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from alice.config import Config, alice_home
from alice.voice.audio import Audio
from alice.voice.tone import CHATTERBOX, DIRECTION, EMOTIONS, NAMES_TR

# Gemini'nin kadın sesleri (Google'ın tanımlarıyla). Dinleme testinde bunlardan seçilir.
GEMINI_VOICES = {
    "Aoede": "ferah", "Kore": "kararlı", "Leda": "genç", "Sulafat": "sıcak", "Despina": "akıcı",
    "Achernar": "yumuşak", "Vindemiatrix": "nazik", "Callirrhoe": "rahat", "Autonoe": "parlak",
}
# Gemini'nin (TTS ve Live ortak) bütün hazır sesleri ve Google'ın tarifleri; Ayarlar > Ses'te dinlenip seçilir.
ALL_GEMINI_VOICES = {
    "Kore": "kararlı", "Aoede": "ferah", "Leda": "genç", "Sulafat": "sıcak", "Despina": "akıcı",
    "Achernar": "yumuşak", "Vindemiatrix": "nazik", "Callirrhoe": "rahat", "Autonoe": "parlak", "Zephyr": "parlak",
    "Puck": "neşeli", "Charon": "bilgili", "Fenrir": "heyecanlı", "Orus": "kararlı", "Enceladus": "nefesli",
    "Iapetus": "net", "Umbriel": "rahat", "Algieba": "pürüzsüz", "Erinome": "net", "Algenib": "boğuk",
    "Rasalgethi": "bilgili", "Laomedeia": "neşeli", "Alnilam": "kararlı", "Schedar": "dengeli", "Gacrux": "olgun",
    "Pulcherrima": "atak", "Achird": "samimi", "Zubenelgenubi": "gündelik", "Sadachbia": "canlı",
    "Sadaltager": "bilgili",
}


class TTSError(Exception):
    pass


class QuotaError(TTSError):
    """Kota doldu. daily: günlük kota (yarına kadar yok); retry_after: kaç saniye sonra tekrar denenebilir."""

    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily


def gemini_quota_error(body: str) -> QuotaError:
    """Gemini'nin 429 cevabından kota türünü ve bekleme süresini çıkar."""
    import re

    retry, daily, limit = None, False, ""
    try:
        err = json.loads(body).get("error", {})
    except ValueError:
        err = {}
    for d in err.get("details", []) or []:
        t = d.get("@type", "")
        if t.endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
            retry = float(m.group(1)) if m else None
        if t.endswith("QuotaFailure"):
            for v in d.get("violations", []) or []:
                qid = str(v.get("quotaId", ""))
                daily = daily or "PerDay" in qid
                limit = limit or str(v.get("quotaValue", ""))
    if retry is None:
        m = re.search(r"retry in ([\d.]+)s", str(err.get("message", "")))
        retry = float(m.group(1)) if m else None
    kind = "günlük" if daily else "dakikalık"
    lim = f" (sınır {limit} istek)" if limit else ""
    wait = f", {retry:.0f} sn sonra tekrar denenebilir" if retry and not daily else ""
    return QuotaError(f"gemini {kind} ses kotası doldu{lim}{wait}", retry, daily)


class TTS(Protocol):
    name: str

    def synth(self, text: str, emotion: str = "calm") -> Audio: ...


def library_dir() -> Path:
    return alice_home() / "voice" / "library"


class EmotionLibrary:
    """Her duygu için bir referans kayıt. Yerel motorlar sesi buradan klonlar."""

    def __init__(self, path: Path | None = None):
        self.path = path or library_dir()

    def clip(self, emotion: str) -> Path | None:
        for e in (emotion, "calm", *EMOTIONS):
            p = self.path / f"{e}.wav"
            if p.exists():
                return p
        return None

    @property
    def source(self) -> str:
        try:
            return json.loads((self.path / "meta.json").read_text(encoding="utf-8")).get("source", "")
        except (OSError, ValueError):
            return ""

    def exists(self) -> bool:
        return self.clip("calm") is not None


# Referans cümleleri: yerel motorlar 6-15 sn temiz konuşmadan iyi klonlar.
LIBRARY_TEXT = {
    "calm": "Merhaba, ben ALICE. Bugün her şey yolunda görünüyor; bilgisayarın serin, görevlerin de zamanında "
            "bitti. Bir şeye ihtiyacın olursa buradayım.",
    "happy": "Harika haber! Derleme ilk denemede geçti ve bütün testler yeşil. Bence bunu kutlamayı hak ettin.",
    "serious": "Bir sorun var. Yedekleme görevi yarıda kaldı çünkü hedef disk dolu. Yer açmadan tekrar "
               "denersem yine başarısız olacak.",
    "urgent": "Yakıt kritik seviyede. Mevcut tüketimle on dakikan kaldı. Eve dönmek için şimdi ayrılman gerekiyor.",
    "playful": "Yine mi gece yarısı Unity? Peki, ben kahve içemiyorum ama sana eşlik ederim. Hadi şu hatayı bulalım.",
}


def build_library(engine: "TTS", out: Path | None = None, emit: Callable[[str], None] = lambda s: None,
                  redo: bool = False) -> tuple[Path, list[str]]:
    """Her duygu için referans kaydı üret. Var olanlar (redo değilse) atlanır, kota dolarsa kalanlar
    sonraki çalıştırmaya kalır. (klasör, eksik kalan duygular) döndürür."""
    out = out or library_dir()
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if meta.get("source") and meta["source"] != engine.name:
        redo = True  # başka bir sesle yapılmış kütüphaneyi karıştırma
        emit(f"önceki kütüphane {meta['source']} sesiyle yapılmış, {engine.name} ile yeniden yapılıyor")
    missing: list[str] = []
    for emo, text in LIBRARY_TEXT.items():
        path = out / f"{emo}.wav"
        if path.exists() and not redo:
            continue
        emit(f"{NAMES_TR[emo]} kaydı hazırlanıyor")
        try:
            engine.synth(text, emo).save(path)
        except QuotaError as e:
            emit(f"  {e}")
            rest = list(LIBRARY_TEXT)[list(LIBRARY_TEXT).index(emo):]
            missing += [m for m in rest if redo or not (out / f"{m}.wav").exists()]
            break
        except TTSError as e:
            emit(f"  atlandı: {e}")
            missing.append(emo)
    meta_path.write_text(json.dumps({"source": engine.name, "created": time.time()}), encoding="utf-8")
    return out, sorted(set(missing), key=list(LIBRARY_TEXT).index)


# ------------------------------------------------------------------- Gemini
DEFAULT_GEMINI_MODELS = ["gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview",
                         "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"]


FORMATS = ("notes", "tag", "plain")

TAGS = {"calm": "calm, warm", "happy": "cheerful", "serious": "serious, composed", "urgent": "urgent, firm",
        "playful": "playful"}


def formats_path() -> Path:
    return alice_home() / "voice" / "gemini-formats.json"


class GeminiTTS:
    """Gemini ses modelleri. Kota model başına sayıldığı için birinin kotası dolunca sıradakine geçer;
    ses (ör. Aoede) aynı kalır.

    Ton talimatı üç biçimde verilebilir: notes (ayrı başlıklar), tag (metnin başında [köşeli] etiket),
    plain (talimatsız, ton yok). Bazı modeller talimatı da sesli okuyor; verify verilirse (Whisper ile
    yazıya çevirip karşılaştıran fonksiyon) okunan biçim o model için 'kötü' işaretlenir, sıradaki biçim
    denenir ve sonuç diske yazılır. Günlük konuşma kayıtlı iyi biçimi kullanır."""

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    TOTAL_SECONDS = 20.0

    def __init__(self, api_key: str, voice: str = "Aoede", models: list[str] | str | None = None,
                 client: httpx.Client | None = None, verify: Callable[[Audio, str], bool] | None = None,
                 formats_file: Path | None = None):
        self.api_key = api_key
        self.voice = voice
        self.models = [models] if isinstance(models, str) else list(models or DEFAULT_GEMINI_MODELS)
        self.name = f"gemini:{voice}"
        # Uzun cümlelerde ses üretimi yavaş olabiliyor; bağlantı kurulamazsa hızlı vazgeç.
        self.client = client or httpx.Client(timeout=httpx.Timeout(45, connect=10))
        self.verify = verify
        self.formats_file = formats_file or formats_path()
        self.down: dict[str, float] = {}  # model -> bu ana kadar atla
        self.reason: dict[str, str] = {}  # model -> daily | minute | error
        self.last_model = ""
        self.last_format = ""
        try:
            self.bad: dict[str, list[str]] = json.loads(self.formats_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.bad = {}

    def _mark_bad(self, model: str, fmt: str) -> None:
        self.bad.setdefault(model, [])
        if fmt not in self.bad[model]:
            self.bad[model].append(fmt)
        try:
            self.formats_file.parent.mkdir(parents=True, exist_ok=True)
            self.formats_file.write_text(json.dumps(self.bad), encoding="utf-8")
        except OSError:
            pass

    def formats_for(self, model: str) -> list[str]:
        bad = self.bad.get(model, [])
        return [f for f in FORMATS if f not in bad] or ["plain"]

    @staticmethod
    def prompt(text: str, emotion: str, fmt: str = "notes") -> str:
        if fmt == "plain":
            return text
        if fmt == "tag":
            return f"[{TAGS.get(emotion, TAGS['calm'])}] {text}"
        # Yönerge ile konuşulacak metin ayrı başlıklarda (Google'ın önerdiği yapı).
        return (
            "Synthesize speech for the performance defined below. The audio profile and director's notes are "
            "direction only. Do NOT speak them. Speak ONLY the lines under #### TRANSCRIPT, exactly as written, "
            "in Turkish.\n\n"
            "# AUDIO PROFILE\nALICE: a young adult female AI assistant with a calm, warm voice, native Turkish "
            "speaker.\n\n"
            f"### DIRECTOR'S NOTES\nStyle: {DIRECTION.get(emotion, DIRECTION['calm'])}\n\n"
            f"#### TRANSCRIPT\n{text}")

    def _request(self, model: str, prompt: str):
        """("ok", Audio) | ("quota", QuotaError) | ("error", mesaj)."""
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
        }
        try:
            r = self.client.post(self.URL.format(model=model), json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.TimeoutException:
            # Model o an yavaş/takıldı: bu cümle için sıradaki modele geç.
            return "slow", f"{model}: zaman aşımı"
        except httpx.HTTPError as e:
            raise TTSError(f"gemini bağlantı hatası: {e}") from e
        if r.status_code == 429:
            return "quota", gemini_quota_error(r.text)
        if r.status_code in (400, 403, 404):
            return "missing", f"{model}: {r.status_code}"
        if r.status_code != 200:
            return "error", f"{model}: {r.status_code} {r.text[:120]}"
        try:
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
        except (KeyError, IndexError, ValueError):
            return "error", f"{model}: ses döndürmedi"
        rate = 24000
        mime = part.get("mimeType", "")
        if "rate=" in mime:
            try:
                rate = int(mime.split("rate=")[1].split(";")[0])
            except ValueError:
                pass
        return "ok", Audio(base64.b64decode(part["data"]), rate).polish()

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        now = time.monotonic()
        quota: list[QuotaError] = []
        errors: list[str] = []
        status: dict[str, str] = {}
        for model in self.models:
            # Toplam süre sınırı: her model 45 sn bekletirse 5 modelde Edge'e düşmek dakikalar sürüyordu.
            if time.monotonic() - now > self.TOTAL_SECONDS:
                errors.append(f"{self.TOTAL_SECONDS:.0f} sn içinde ses gelmedi")
                break
            if self.down.get(model, 0) > now:
                status[model] = {"daily": "günlük kota", "minute": "dakikalık kota"}.get(self.reason.get(model, ""),
                                                                                         "bekletiliyor")
                continue
            fallback: Audio | None = None
            for fmt in self.formats_for(model):
                kind, res = self._request(model, self.prompt(text, emotion, fmt))
                if kind == "quota":
                    # Günlük kota: o model bugün yok. Dakikalık: o model kısa süre beklesin, diğerine geç.
                    self.down[model] = now + (6 * 3600 if res.daily else (res.retry_after or 60))
                    self.reason[model] = "daily" if res.daily else "minute"
                    quota.append(res)
                    status[model] = "günlük kota" if res.daily else "dakikalık kota"
                    break
                if kind == "missing":
                    # Model yok, bu hesapta açık değil ya da ses/dil desteklenmiyor: bir gün atla.
                    self.down[model] = now + 86400
                    self.reason[model] = "error"
                    errors.append(res)
                    status[model] = "bulunamadı/desteklenmiyor (" + res.rsplit(" ", 1)[-1] + ")"
                    break
                if kind == "slow":
                    self.down[model] = now + 120  # iki dakika diğer modellerle devam et
                    self.reason[model] = "error"
                    errors.append(res)
                    status[model] = "zaman aşımı"
                    break
                if kind == "error":
                    self.down[model] = now + 60  # 500/503 veren model her cümlede ilk denenmesin
                    self.reason[model] = "error"
                    errors.append(res)
                    status[model] = res.split(": ", 1)[-1][:60]
                    break
                if fmt != "plain" and (too_long(res, text) or (self.verify is not None and not self.verify(res, text))):
                    self._mark_bad(model, fmt)  # model talimatı da okudu: bu biçimi bu modelde bir daha kullanma
                    if not too_long(res, text):  # talimatı sesli okuyan (fazla uzun) ses asla çalınmaz
                        fallback = res
                    continue
                self.last_model, self.last_format = model, fmt
                return res
            if fallback is not None:  # bütün biçimler doğrulamadan geçemediyse son sesi kullan
                self.last_model, self.last_format = model, "?"
                return fallback
        # Hiçbir model ses vermedi. Sadece kota yüzündense QuotaError (dinleme testi buna göre bekler/durur).
        skipped = [m for m in self.models if self.down.get(m, 0) > now and self.reason.get(m) != "error"]
        if quota or skipped:  # var olmayan modellerin hataları önemsiz; kota belirleyici
            minute = [max(1.0, self.down[m] - now) for m in self.models
                      if self.reason.get(m) == "minute" and self.down.get(m, 0) > now]
            daily = not minute
            kind = "günlük" if daily else "dakikalık"
            retry = None if daily else min(minute)
            wait = f", {retry:.0f} sn sonra tekrar denenebilir" if retry else ""
            detail = "; ".join(f"{m}: {st}" for m, st in status.items())
            raise QuotaError(f"gemini {kind} ses kotası doldu{wait} [{detail}]", retry, daily)
        raise TTSError("gemini: " + ("; ".join(f"{m}: {st}" for m, st in status.items()) or "kullanılabilir model yok"))


def list_gemini_tts_models(api_key: str, client: httpx.Client | None = None) -> list[str]:
    """Bu anahtarla erişilebilen ses (TTS) modelleri. Model listesi kota harcamaz."""
    c = client or httpx.Client(timeout=15)
    out, token = [], ""
    for _ in range(10):
        r = c.get("https://generativelanguage.googleapis.com/v1beta/models",
                  params={"pageSize": 1000, **({"pageToken": token} if token else {})},
                  headers={"x-goog-api-key": api_key})
        r.raise_for_status()
        data = r.json()
        out += [m["name"].split("/", 1)[-1] for m in data.get("models", []) if "tts" in m.get("name", "").lower()]
        token = data.get("nextPageToken", "")
        if not token:
            break
    return sorted(out)


def _fold(s: str) -> list[str]:
    import re
    import unicodedata

    s = s.lower().replace("ı", "i").replace("İ", "i")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", s)


LEAK_WORDS = {"style", "synthesize", "speech", "transcript", "director", "notes", "profile", "speak", "calm",
              "serious", "urgent", "cheerful", "playful", "composed", "warm", "voice", "assistant", "turkish"}


def leaked(heard: str, expected: str) -> bool:
    """Whisper'ın duyduğu, istenen metinden fazlasını içeriyor mu (talimat okunmuş mu)?"""
    want = set(_fold(expected))
    got = _fold(heard)
    if any(w in LEAK_WORDS and w not in want for w in got):
        return True
    extra = [w for w in got if w not in want and not any(w[:4] == x[:4] for x in want if len(w) > 3)]
    return len(extra) >= 3 and len(extra) > 0.3 * max(1, len(want))


def too_long(audio: Audio, text: str) -> bool:
    """Ses metne göre fazla uzun mu? Talimat okunduysa ses saniyelerce uzar (Türkçe ~12-15 harf/sn).
    Whisper gerektirmez, her cümlede bedava çalışır."""
    return audio.seconds > 2.5 + len(text) / 8


def make_verifier(stt) -> Callable[[Audio, str], bool]:
    def verify(audio: Audio, text: str) -> bool:
        try:
            return not leaked(stt.transcribe(audio), text)
        except Exception:
            return True  # doğrulayamıyorsak sesi kabul et
    return verify


# -------------------------------------------------------------- yerel motorlar
def _device(allow_gpu: bool) -> str:
    if not allow_gpu:
        return "cpu"
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


class XTTS:
    """Coqui XTTS v2 (pip install coqui-tts). Türkçe destekler, referans kayıttan klonlar."""

    def __init__(self, library: EmotionLibrary | None = None, allow_gpu: Callable[[], bool] = lambda: True):
        self.library = library or EmotionLibrary()
        self.allow_gpu = allow_gpu
        self.name = "xtts"
        self._model = None
        self._device = ""
        self._lock = threading.Lock()

    def _load(self):
        dev = _device(self.allow_gpu())
        if self._model is None or dev != self._device:
            # Lisans (CPML) sorusu konsolda y/n bekler; arka plandaki çekirdekte bu sonsuza dek takılmak demek.
            os.environ.setdefault("COQUI_TOS_AGREED", "1")
            try:
                from TTS.api import TTS as CoquiTTS
            except ImportError as e:
                raise TTSError("XTTS kurulu değil: pip install coqui-tts") from e
            self._model = CoquiTTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev)
            self._device = dev
        return self._model

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        ref = self.library.clip(emotion)
        if ref is None:
            raise TTSError("duygu kütüphanesi yok: alice voice library")
        with self._lock:
            wav = self._load().tts(text=text, speaker_wav=str(ref), language="tr")
        return Audio.from_floats(wav, 24000).polish()

    def release_gpu(self) -> None:
        """Uçuşta ekran kartındaki modeli at (bir sonraki cümle işlemcide yükler)."""
        if self._device != "cuda" or not self._lock.acquire(blocking=False):
            return
        try:
            self._model, self._device = None, ""
            import gc

            gc.collect()
            try:
                import torch

                torch.cuda.empty_cache()
            except ImportError:
                pass
        finally:
            self._lock.release()


class Chatterbox:
    """Resemble Chatterbox çok dilli, ayrı sanal ortamdaki yerel ses sunucusu üzerinden (tts_server.py).

    Sunucu çalışmıyorsa ilk istekte arka planda başlatılır (ilk açılışta model ~2 GB indirir).
    Sesi duygu kütüphanesindeki kayıttan taklit eder; kütüphane yoksa Chatterbox'ın kendi sesiyle konuşur.
    Uçuşta (local_gpu = false) model işlemciye taşınır, ekran kartı oyuna kalır."""

    def __init__(self, python: str = "", port: int = 8771, library: EmotionLibrary | None = None,
                 allow_gpu: Callable[[], bool] = lambda: True, client: httpx.Client | None = None,
                 start_timeout: float = 600, trim: Callable[[Audio, str], Audio] | None = None,
                 temperature: float = 0.6, verify: bool = True, whisper_model: str = "small",
                 synth_timeout: float = 30):
        from alice.voice.trim import by_length

        self.synth_timeout = synth_timeout
        self._warm = False
        self.trim = trim or by_length
        self.temperature = temperature
        self.verify = verify
        self.whisper_model = whisper_model
        self.last_quality = ""
        self.python = python
        self.port = port
        self.library = library or EmotionLibrary()
        self.allow_gpu = allow_gpu
        self.name = "chatterbox"
        self.client = client or httpx.Client(timeout=httpx.Timeout(180, connect=3))
        self.start_timeout = start_timeout
        self._proc = None
        self._lock = threading.Lock()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def healthy(self) -> bool:
        try:
            return self.client.get(self.base + "/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def release_gpu(self) -> None:
        """Uçuşta ses sunucusundaki model ekran kartındaysa (~3 GB) attır. Sunucu kapalıysa bir şey yapmaz."""
        try:
            r = self.client.get(self.base + "/health", timeout=1)
            if r.status_code == 200 and r.json().get("device") == "cuda":
                self.client.post(self.base + "/release", timeout=10)
        except (httpx.HTTPError, ValueError):
            pass

    def ensure_server(self) -> None:
        with self._lock:
            if self.healthy():
                return
            if not self.python or not Path(self.python).exists():
                raise TTSError("yerel ses kurulu değil: alice voice install-local")
            import subprocess
            import sys

            log = alice_home() / "voice" / "tts-server.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            device = "cuda" if self.allow_gpu() else "cpu"
            with open(log, "a", encoding="utf-8") as out:  # çocuk kendi kopyasını alır; bizdeki sızmasın
                self._proc = subprocess.Popen(
                    [self.python, str(Path(__file__).with_name("tts_server.py")), "--port", str(self.port),
                     "--device", device, "--preload"], stdout=out, stderr=subprocess.STDOUT, creationflags=flags)
            end = time.monotonic() + 30
            while time.monotonic() < end:
                if self._proc.poll() is not None:
                    raise TTSError(f"yerel ses sunucusu açılamadı, ayrıntı: {log}")
                if self.healthy():
                    return
                time.sleep(0.5)
            raise TTSError(f"yerel ses sunucusu cevap vermiyor, ayrıntı: {log}")

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        self.ensure_server()
        exaggeration, cfg_weight = CHATTERBOX.get(emotion, CHATTERBOX["calm"])
        ref = self.library.clip(emotion)
        body = {"text": text, "language": "tr", "ref": str(ref) if ref else "", "exaggeration": exaggeration,
                "cfg_weight": cfg_weight, "temperature": self.temperature,
                "device": "cuda" if self.allow_gpu() else "cpu", "verify": self.verify,
                "whisper_model": self.whisper_model}
        # İlk istekte model yüklenir/indirilir: uzun sürebilir. Sonra bir cümle saniyeler sürmeli; takılırsa
        # beklemeyip sıradaki sese (Edge) geç, kullanıcı cevapsız kalmasın.
        timeout = self.start_timeout if not self._warm else self.synth_timeout
        try:
            r = self.client.post(self.base + "/synth", json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise TTSError(f"yerel ses sunucusu hatası: {e}") from e
        if r.status_code != 200:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text
            raise TTSError(f"chatterbox: {str(msg)[:200]}")
        audio = Audio.from_wav(r.content)
        self._warm = True
        self.last_quality = r.headers.get("X-Alice-Quality", "")
        if r.headers.get("X-Alice-Verified") != "1":
            # Sunucuda Whisper yoksa kuyruğu burada kes (Groq Whisper ya da uzunluğa göre).
            try:
                audio = self.trim(audio, text)
            except Exception:
                pass
        return audio.polish()


# Duyguya göre konuşma hızı/perdesi (Edge sesi stil talimatı almaz).
EDGE_PROSODY = {"excited": ("+10%", "+4Hz"), "happy": ("+5%", "+2Hz"), "serious": ("-5%", "-2Hz"),
                "sad": ("-10%", "-4Hz"), "urgent": ("+15%", "+0Hz")}


class EdgeTTS:
    """Microsoft Edge sesleri (pip install edge-tts). MP3 gelir, av ile 24 kHz mono PCM'e çevrilir."""

    def __init__(self, voice: str = "tr-TR-EmelNeural", timeout: float = 15.0):
        self.voice = voice
        self.timeout = timeout
        self.name = f"edge:{voice}"

    def _mp3(self, text: str, emotion: str) -> bytes:
        import asyncio

        try:
            import edge_tts
        except ImportError as e:
            raise TTSError("edge-tts kurulu değil: pip install edge-tts") from e
        rate, pitch = EDGE_PROSODY.get(emotion, ("+0%", "+0Hz"))

        async def run() -> bytes:
            buf = bytearray()
            async for chunk in edge_tts.Communicate(text, self.voice, rate=rate, pitch=pitch).stream():
                if chunk.get("type") == "audio":
                    buf += chunk["data"]
            return bytes(buf)

        try:
            return asyncio.run(asyncio.wait_for(run(), self.timeout))
        except Exception as e:  # ağ/servis hataları tek türde dönsün
            raise TTSError(f"edge sesi alınamadı: {type(e).__name__}: {e}") from e

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        data = self._mp3(text, emotion)
        if not data:
            raise TTSError("edge sesi boş döndü")
        return Audio(decode_mp3(data, 24000), 24000)


def decode_mp3(data: bytes, rate: int) -> bytes:
    """MP3 -> int16 mono PCM (av, faster-whisper ile birlikte gelir)."""
    import io

    try:
        import av
    except ImportError as e:
        raise TTSError("MP3 çözmek için av gerekli: pip install av") from e
    out = bytearray()
    with av.open(io.BytesIO(data)) as box:
        res = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in box.decode(audio=0):
            for f in res.resample(frame):
                out += bytes(f.planes[0])[: f.samples * 2]
        for f in res.resample(None):
            out += bytes(f.planes[0])[: f.samples * 2]
    return bytes(out)


# --------------------------------------------------------------------- seçim
def core_policy() -> dict[str, Any]:
    from alice.core.service import read_state

    s = read_state()
    return s.get("policy", {}) if s.get("running") else {}


class SpeechCache:
    """Kısa, tekrar eden cümlelerin sesi diskte ("Hemen bakıyorum", "On speed, iyi"...): kota harcamaz, anında çalar."""

    def __init__(self, path: Path | None = None, max_chars: int = 120, max_files: int = 3000):
        self.path = path or alice_home() / "voice" / "cache"
        self.max_chars = max_chars
        self.max_files = max_files
        self._writes = 0

    def _file(self, engine: str, emotion: str, text: str) -> Path:
        import hashlib

        key = hashlib.sha1(f"v2|{engine}|{emotion}|{text.strip().lower()}".encode("utf-8")).hexdigest()
        return self.path / f"{key}.wav"

    def get(self, engine: str, emotion: str, text: str) -> Audio | None:
        if len(text) > self.max_chars:
            return None
        f = self._file(engine, emotion, text)
        try:
            return Audio.load(f) if f.exists() else None
        except Exception:  # noqa: BLE001 - yarım yazılmış/bozuk dosya (wave.Error dahil): sil, yeniden üretilsin
            try:
                f.unlink()
            except OSError:
                pass
            return None

    def put(self, engine: str, emotion: str, text: str, audio: Audio) -> None:
        if len(text) > self.max_chars:
            return
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            f = self._file(engine, emotion, text)
            tmp = f.with_suffix(".tmp")
            audio.save(tmp)
            os.replace(tmp, f)  # yarıda kesilirse bozuk .wav önbellekte kalmasın
            self._writes += 1
            if self._writes % 100 == 0:
                files = sorted(self.path.glob("*.wav"), key=lambda p: p.stat().st_mtime)
                for old in files[: max(0, len(files) - self.max_files)]:
                    old.unlink()
        except OSError:
            pass


class TTSRouter:
    """Sıradaki uygun motoru dener. Hata veren motor `cooldown` saniye atlanır."""

    def __init__(self, cloud: TTS | None, local: TTS | None, prefer: str = "cloud",
                 policy: Callable[[], dict[str, Any]] = core_policy, cooldown: float = 300,
                 cache: SpeechCache | None = None, backup: TTS | None = None):
        self.cloud = cloud
        self.local = local
        self.backup = backup  # hızlı son çare (Edge): bulut ve yerel ses düşünce
        self.prefer = prefer
        self.policy = policy
        self.cooldown = cooldown
        self.down: dict[str, float] = {}
        self.last = ""
        self.cache = cache

    @property
    def name(self) -> str:
        return self.last or (self.order()[0].name if self.order() else "yok")

    def release_gpu(self) -> None:
        """Çekirdek uçuş moduna geçince: yerel ses modeli ekran kartında duruyorsa bırak. Uçuşta önce bulut
        sesi kullanıldığı için yerel motor hiç çağrılmıyor ve modeli DCS boyunca ekran kartında kalıyordu."""
        fn = getattr(self.local, "release_gpu", None)
        if fn is not None:
            fn()

    def order(self) -> list[TTS]:
        # Yedek (Edge) en sonda: önce ALICE'in kendi sesi (bulut ya da onu taklit eden yerel motor).
        engines = ([self.local, self.cloud, self.backup] if self.prefer == "local"
                   else [self.cloud, self.local, self.backup])
        if self.policy().get("local_gpu") is False and self.local is not None:
            # Uçuşta GPU oyuna kalsın: önce bulut; yerel ancak işlemcide, son çare olarak.
            engines = [self.cloud, self.backup, self.local]
        return [e for e in engines if e is not None]

    def warm_up(self) -> None:
        """Yerel ses modelini arka planda ekran kartına yükle: ilk cevap 20-25 sn beklemesin."""
        load = getattr(self.local, "_load", None)
        if load is None or self.policy().get("local_gpu") is False:
            return

        def run() -> None:
            try:
                with self.local._lock:
                    load()
            except Exception:  # noqa: BLE001 - ısınma olmazsa ilk cümlede yüklenir
                pass

        threading.Thread(target=run, daemon=True, name="alice-tts-warm").start()

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        from alice.voice.spoken_numbers import normalize

        text = normalize(text)
        errors = []
        now = time.monotonic()
        if self.cache is not None:
            for e in self.order():
                hit = self.cache.get(e.name, emotion, text)
                if hit is not None:
                    self.last = f"{e.name} (önbellek)"
                    return hit
        for e in self.order():
            if self.down.get(e.name, 0) > now:
                errors.append(f"{e.name}: bekletiliyor")
                continue
            try:
                audio = e.synth(text, emotion)
                self.last = e.name
                if self.cache is not None:
                    self.cache.put(e.name, emotion, text, audio)
                return audio
            except Exception as ex:  # noqa: BLE001 - CUDA OOM, bozuk ses verisi: sıradaki motora geç, bu motoru beklet
                if not isinstance(ex, TTSError):
                    ex = TTSError(f"{type(ex).__name__}: {ex}")
                wait = self.cooldown
                if isinstance(ex, QuotaError):
                    wait = 3600 if ex.daily else (ex.retry_after or self.cooldown)
                self.down[e.name] = now + wait
                errors.append(f"{e.name}: {ex}")
        raise TTSError("; ".join(errors) or "ses motoru yok")


def make_cloud(cfg: Config, voice: str | None = None) -> GeminiTTS | None:
    v = cfg.data.get("voice", {})
    ref = voice or v.get("tts_cloud", "gemini:Aoede")
    provider, _, name = ref.partition(":")
    if provider != "gemini":
        return None
    key = os.environ.get(cfg.data.get("providers", {}).get("gemini", {}).get("api_key_env", "GEMINI_API_KEY"), "")
    if not key:
        return None
    models = list(v.get("gemini_tts_models") or DEFAULT_GEMINI_MODELS)
    if v.get("gemini_tts_model"):  # eski tek model ayarı: onu öne al
        models = [v["gemini_tts_model"]] + [m for m in models if m != v["gemini_tts_model"]]
    return GeminiTTS(key, name or "Aoede", models)


def make_local(cfg: Config, name: str | None = None) -> TTS | None:
    name = name if name is not None else cfg.data.get("voice", {}).get("tts_local", "")
    allow = lambda: core_policy().get("local_gpu", True) is not False  # noqa: E731
    if name == "xtts":
        return XTTS(allow_gpu=allow)
    if name == "chatterbox":
        from alice.voice.stt import make_stt
        from alice.voice.trim import make_trimmer

        v = cfg.data.get("voice", {})
        trim = make_trimmer(make_stt(cfg) if v.get("local_trim_whisper", True) else None)
        return Chatterbox(v.get("local_python", ""), int(v.get("local_port", 8771)), allow_gpu=allow, trim=trim,
                          temperature=float(v.get("local_temperature", 0.6)),
                          verify=bool(v.get("local_verify", True)),
                          whisper_model=str(v.get("local_whisper_model", "small")))
    return None


def make_tts(cfg: Config) -> TTSRouter:
    v = cfg.data.get("voice", {})
    edge = str(v.get("tts_edge", "tr-TR-EmelNeural") or "")
    return TTSRouter(make_cloud(cfg), make_local(cfg), v.get("prefer", "cloud"), cache=SpeechCache(),
                     backup=EdgeTTS(edge) if edge else None)
