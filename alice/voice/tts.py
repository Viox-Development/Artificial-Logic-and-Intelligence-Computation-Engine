"""Voice engines.

Cloud: Gemini TTS. The voice itself (e.g. "Aoede") stays fixed; the tone changes with a style instruction.
Local: XTTS v2 and Chatterbox. Both clone the voice from a reference recording. The "emotion library"
is used as the reference: one recording per emotion of the chosen cloud voice
(~/.alice/voice/library/<emotion>.wav). This way the same ALICE is heard whether it speaks from the
cloud while DCS is open or locally when it is closed.

Last resort: Microsoft Edge's neural voices (edge-tts). No key or quota, ready in ~1 s; it speaks when
the cloud and local voices fail (or when the local voice hangs and times out).

Last resort: Microsoft Edge's neural voices (edge-tts). No key or quota, ready in ~1 s; it speaks when
the cloud and local voices fail (or when the local voice hangs and times out).

Selection (TTSRouter): while DCS is open (mode policy local_gpu = false) the local engine does not
touch the GPU; either the cloud is used or the local engine runs on the CPU. An engine that
errors is skipped for a while.
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
from alice.i18n import edge_voice, speech_language
from alice.voice.audio import Audio
from alice.voice.tone import CHATTERBOX, DIRECTION, EMOTIONS, NAMES_TR

# Gemini's female voices (per Google's descriptions). Chosen from these in the listening test.
GEMINI_VOICES = {
    "Aoede": "ferah", "Kore": "kararlı", "Leda": "genç", "Sulafat": "sıcak", "Despina": "akıcı",
    "Achernar": "yumuşak", "Vindemiatrix": "nazik", "Callirrhoe": "rahat", "Autonoe": "parlak",
}
# All of Gemini's ready-made voices (shared by TTS and Live) and Google's descriptions;
    # listened to and chosen under Sistem > Ses.
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
    """Quota exhausted. daily: daily quota (none until tomorrow); retry_after: seconds until a retry is allowed."""

    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily


def gemini_quota_error(body: str) -> QuotaError:
    """Extract the quota type and wait time from Gemini's 429 response."""
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
    """One reference recording per emotion. Local engines clone the voice from here."""

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


# Reference sentences: local engines clone well from 6-15 s of clean speech.
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
    """Generate a reference recording for each emotion. Existing ones are skipped (unless redo), and if the
    quota runs out the rest are left for the next run. Returns (folder, emotions still missing)."""
    out = out or library_dir()
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if meta.get("source") and meta["source"] != engine.name:
        redo = True  # don't mix in a library made with a different voice
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
    """Gemini voice models. Quota counts per model, so when one runs out it moves to the next;
    the voice (e.g. Aoede) stays the same.

    The tone instruction comes in three formats: notes (separate headings), tag (a [bracketed] label at the
    start of the text), plain (no instruction, so no tone). Some models read the instruction aloud too; if
    verify is given (a function that transcribes with Whisper and compares), the format read aloud is marked
    'bad' for that model, the next format is tried, and the result is saved to disk. Everyday speech uses the
    saved good format."""

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    TOTAL_SECONDS = 20.0

    def __init__(self, api_key: str, voice: str = "Aoede", models: list[str] | str | None = None,
                 client: httpx.Client | None = None, verify: Callable[[Audio, str], bool] | None = None,
                 formats_file: Path | None = None):
        self.api_key = api_key
        self.voice = voice
        self.models = [models] if isinstance(models, str) else list(models or DEFAULT_GEMINI_MODELS)
        self.name = f"gemini:{voice}"
        # Long sentences can make speech generation slow; if the connection can't be made, give up quickly.
        self.client = client or httpx.Client(timeout=httpx.Timeout(45, connect=10))
        self.verify = verify
        self.formats_file = formats_file or formats_path()
        self.down: dict[str, float] = {}  # model -> skip until this time
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
            pass  # remembered in memory for this run only

    def formats_for(self, model: str) -> list[str]:
        bad = self.bad.get(model, [])
        return [f for f in FORMATS if f not in bad] or ["plain"]

    @staticmethod
    def prompt(text: str, emotion: str, fmt: str = "notes") -> str:
        if fmt == "plain":
            return text
        if fmt == "tag":
            return f"[{TAGS.get(emotion, TAGS['calm'])}] {text}"
        # The spoken text goes under its own heading, in the structure Google recommends.
        return (
            "Synthesize speech for the performance defined below. The audio profile and director's notes are "
            "direction only. Do NOT speak them. Speak ONLY the lines under #### TRANSCRIPT, exactly as written, "
            "in Turkish.\n\n"
            "# AUDIO PROFILE\nALICE: a young adult female AI assistant with a calm, warm voice, native Turkish "
            "speaker.\n\n"
            f"### DIRECTOR'S NOTES\nStyle: {DIRECTION.get(emotion, DIRECTION['calm'])}\n\n"
            f"#### TRANSCRIPT\n{text}")

    def _request(self, model: str, prompt: str):
        """("ok", Audio) | ("quota", QuotaError) | ("error", message)."""
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
        }
        try:
            r = self.client.post(self.URL.format(model=model), json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.TimeoutException:
            # The model is slow or stuck right now: move on to the next model for this sentence.
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
                pass  # malformed rate: keep the default
        return "ok", Audio(base64.b64decode(part["data"]), rate).polish()

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        now = time.monotonic()
        quota: list[QuotaError] = []
        errors: list[str] = []
        status: dict[str, str] = {}
        for model in self.models:
            # Total time limit: if each model stalled for 45 s, falling back to Edge across 5 models took minutes.
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
                    # Daily quota: that model is out for today. Per-minute: that model waits briefly,
                    # move on to another.
                    self.down[model] = now + (6 * 3600 if res.daily else (res.retry_after or 60))
                    self.reason[model] = "daily" if res.daily else "minute"
                    quota.append(res)
                    status[model] = "günlük kota" if res.daily else "dakikalık kota"
                    break
                if kind == "missing":
                    # Model missing, not enabled on this account, or voice/language unsupported: skip it for a day.
                    self.down[model] = now + 86400
                    self.reason[model] = "error"
                    errors.append(res)
                    status[model] = "bulunamadı/desteklenmiyor (" + res.rsplit(" ", 1)[-1] + ")"
                    break
                if kind == "slow":
                    self.down[model] = now + 120  # two minutes: continue with the other models
                    self.reason[model] = "error"
                    errors.append(res)
                    status[model] = "zaman aşımı"
                    break
                if kind == "error":
                    self.down[model] = now + 60  # a model returning 500/503 should not be tried first on every sentence
                    self.reason[model] = "error"
                    errors.append(res)
                    status[model] = res.split(": ", 1)[-1][:60]
                    break
                if fmt != "plain" and (too_long(res, text) or (self.verify is not None and not self.verify(res, text))):
                    # the model read the instruction aloud too: never use this format with this model again
                    self._mark_bad(model, fmt)
                    if not too_long(res, text):  # audio that reads the instruction aloud (too long) is never played
                        fallback = res
                    continue
                self.last_model, self.last_format = model, fmt
                return res
            if fallback is not None:  # if no format passed verification, use the last audio
                self.last_model, self.last_format = model, "?"
                return fallback
        # No model produced audio. QuotaError only if the quota is the cause (the listening test
        # waits or stops based on it).
        skipped = [m for m in self.models if self.down.get(m, 0) > now and self.reason.get(m) != "error"]
        if quota or skipped:  # errors from missing models don't matter; the quota is decisive
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
    """Speech (TTS) models accessible with this key. Listing models does not use up quota."""
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
    """Does what Whisper heard contain more than the requested text (was the instruction read aloud)?"""
    want = set(_fold(expected))
    got = _fold(heard)
    if any(w in LEAK_WORDS and w not in want for w in got):
        return True
    extra = [w for w in got if w not in want and not any(w[:4] == x[:4] for x in want if len(w) > 3)]
    return len(extra) >= 3 and len(extra) > 0.3 * max(1, len(want))


def too_long(audio: Audio, text: str) -> bool:
    """Is the audio too long for the text? If the instruction was read aloud, the audio runs several seconds
    longer (Turkish ~12-15 letters/s). It needs no Whisper, so it runs for free on every sentence."""
    return audio.seconds > 2.5 + len(text) / 8


def make_verifier(stt) -> Callable[[Audio, str], bool]:
    def verify(audio: Audio, text: str) -> bool:
        try:
            return not leaked(stt.transcribe(audio), text)
        except Exception:
            return True  # if we can't verify, accept the audio
    return verify


# -------------------------------------------------------------- local engines
def _device(allow_gpu: bool) -> str:
    if not allow_gpu:
        return "cpu"
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


class XTTS:
    """Coqui XTTS v2 (pip install coqui-tts). Supports Turkish; clones the voice from a reference recording."""

    def __init__(self, library: EmotionLibrary | None = None, allow_gpu: Callable[[], bool] = lambda: True,
                 language: str = "tr"):
        self.library = library or EmotionLibrary()
        self.language = language
        self.allow_gpu = allow_gpu
        self.name = "xtts"
        self._model = None
        self._device = ""
        self._lock = threading.Lock()

    def _load(self):
        dev = _device(self.allow_gpu())
        if self._model is None or dev != self._device:
            # The license (CPML) prompt waits for y/n in the console; in a background process that hangs forever.
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
            wav = self._load().tts(text=text, speaker_wav=str(ref), language=self.language)
        return Audio.from_floats(wav, 24000).polish()

    def release_gpu(self) -> None:
        """In flight, unload the model from the GPU (the next sentence loads it on the CPU)."""
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
                pass  # no torch: no CUDA cache to free
        finally:
            self._lock.release()


class Chatterbox:
    """Resemble Chatterbox, a multilingual local voice server in a separate virtualenv (tts_server.py).

    If the server isn't running, it is started in the background on the first request (the first launch
    downloads a model of ~2 GB). It imitates the voice from the recording in the emotion library; without the
    library it speaks with Chatterbox's own voice. In flight (local_gpu = false) the model moves to the CPU,
    and the GPU stays with the game."""

    def __init__(self, python: str = "", port: int = 8771, library: EmotionLibrary | None = None,
                 allow_gpu: Callable[[], bool] = lambda: True, client: httpx.Client | None = None,
                 start_timeout: float = 600, trim: Callable[[Audio, str], Audio] | None = None,
                 temperature: float = 0.6, verify: bool = True, whisper_model: str = "small",
                 synth_timeout: float = 30, language: str = "tr"):
        from alice.voice.trim import by_length

        self.language = language

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
        """In flight, if the model on the voice server is on the GPU (~3 GB), unload it.
        Does nothing if the server is off."""
        try:
            r = self.client.get(self.base + "/health", timeout=1)
            if r.status_code == 200 and r.json().get("device") == "cuda":
                self.client.post(self.base + "/release", timeout=10)
        except (httpx.HTTPError, ValueError):
            pass  # server not reachable: nothing to release

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
            with open(log, "a", encoding="utf-8") as out:  # the child gets its own copy; ours must not leak into it
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
        body = {"text": text, "language": self.language, "ref": str(ref) if ref else "", "exaggeration": exaggeration,
                "cfg_weight": cfg_weight, "temperature": self.temperature,
                "device": "cuda" if self.allow_gpu() else "cpu", "verify": self.verify,
                "whisper_model": self.whisper_model}
        # The first request loads/downloads the model and can take a long time. After that a sentence should take
        # seconds; if it hangs, don't wait: move on to the next voice (Edge) so the user is not left without an answer.
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
            # If the server has no Whisper, trim the tail here (Groq Whisper, or by length).
            try:
                audio = self.trim(audio, text)
            except Exception:
                pass  # keep the untrimmed audio
        return audio.polish()


# Speech rate/pitch by emotion (the Edge voice takes no style instruction).
EDGE_PROSODY = {"excited": ("+10%", "+4Hz"), "happy": ("+5%", "+2Hz"), "serious": ("-5%", "-2Hz"),
                "sad": ("-10%", "-4Hz"), "urgent": ("+15%", "+0Hz")}


class EdgeTTS:
    """Microsoft Edge voices (pip install edge-tts). MP3 comes back and is converted to 24 kHz mono PCM with av."""

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
        except Exception as e:  # network/service errors come back as one type
            raise TTSError(f"edge sesi alınamadı: {type(e).__name__}: {e}") from e

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        data = self._mp3(text, emotion)
        if not data:
            raise TTSError("edge sesi boş döndü")
        return Audio(decode_mp3(data, 24000), 24000)


# Tone -> ElevenLabs voice setting (low stability = livelier/more variable, high style = more emphatic)
ELEVEN_SETTINGS = {"calm": (0.6, 0.15), "happy": (0.45, 0.35), "playful": (0.4, 0.45), "serious": (0.6, 0.2),
                   "urgent": (0.35, 0.5), "excited": (0.35, 0.5)}
# Model "auto": the first one found in the account's list (newest fast models that support Turkish first)
ELEVEN_PREFER = ("eleven_v4_turbo", "eleven_turbo_v4", "eleven_v4", "eleven_turbo_v2_5", "eleven_flash_v2_5",
                 "eleven_multilingual_v2")


class ElevenLabsTTS:
    """ElevenLabs (ELEVENLABS_API_KEY): asks for raw 24 kHz PCM, so no decoding is needed. If credits run out,
    QuotaError: the router switches to Gemini and ALICE doesn't go silent."""

    URL = "https://api.elevenlabs.io/v1"
    RATE = 24000

    def __init__(self, api_key: str, voice: str, model: str = "auto", client: httpx.Client | None = None,
                 language: str = "tr"):
        self.api_key = api_key
        self.language = language
        self.voice = voice
        self.model = model
        self.name = f"eleven:{voice}"
        self.client = client or httpx.Client(timeout=httpx.Timeout(20, connect=8))
        self._lang_ok = True  # don't send language_code again to a model that doesn't accept it

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.api_key}

    def model_id(self) -> str:
        if self.model and self.model != "auto":
            return self.model
        try:
            r = self.client.get(f"{self.URL}/models", headers=self._headers())
            models = r.json() if r.status_code == 200 else []
            ids = [m.get("model_id", "") for m in models if m.get("can_do_text_to_speech", True)]
        except (httpx.HTTPError, ValueError):
            ids = []
        named = {i.lower(): i for i in ids}
        pick = next((named[k] for k in ELEVEN_PREFER if k in named), None)
        if pick is None:  # a new name not in our list: the first model with v4 and turbo in it
            pick = next((i for i in ids if "v4" in i and "turbo" in i), None) or "eleven_turbo_v2_5"
        self.model = pick
        return pick

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        stability, style = ELEVEN_SETTINGS.get(emotion, ELEVEN_SETTINGS["calm"])
        body: dict[str, Any] = {"text": text, "model_id": self.model_id(),
                                "voice_settings": {"stability": stability, "similarity_boost": 0.8, "style": style,
                                                   "use_speaker_boost": True}}
        if self._lang_ok:
            body["language_code"] = self.language
        url = f"{self.URL}/text-to-speech/{self.voice}"
        try:
            r = self.client.post(url, params={"output_format": f"pcm_{self.RATE}"}, headers=self._headers(),
                                 json=body)
            if r.status_code in (400, 422) and "language" in r.text.lower() and self._lang_ok:
                self._lang_ok = False  # this model takes no language code: it infers the language from the text
                body.pop("language_code", None)
                r = self.client.post(url, params={"output_format": f"pcm_{self.RATE}"}, headers=self._headers(),
                                     json=body)
        except httpx.HTTPError as e:
            raise TTSError(f"elevenlabs'e ulaşılamadı: {type(e).__name__}: {e}") from e
        if r.status_code == 200 and r.content:
            return Audio(r.content, self.RATE)
        low = r.text.lower()
        if r.status_code == 429 or "quota" in low or "credits" in low:
            # Monthly credits used up or rate limit: if credits ran out, retry after an hour (they may have renewed)
            raise QuotaError(f"elevenlabs kredisi/kotası: {r.text[:160]}", None if r.status_code == 429 else 3600,
                             daily=r.status_code != 429)
        raise TTSError(f"elevenlabs HTTP {r.status_code}: {r.text[:200]}")


def decode_mp3(data: bytes, rate: int) -> bytes:
    """MP3 -> int16 mono PCM (av comes with faster-whisper)."""
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


# --------------------------------------------------------------------- selection
def core_policy() -> dict[str, Any]:
    from alice.core.service import read_state

    s = read_state()
    return s.get("policy", {}) if s.get("running") else {}


class SpeechCache:
    """Audio of short, repeated sentences kept on disk ("Hemen bakıyorum", "On speed, iyi"...):
    spends no quota, plays instantly."""

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
        except Exception:  # noqa: BLE001 - half-written/corrupt file (wave.Error included):
            # delete it so it is regenerated
            try:
                f.unlink()
            except OSError:
                pass  # already gone or locked; it is overwritten next time
            return None

    def put(self, engine: str, emotion: str, text: str, audio: Audio) -> None:
        if len(text) > self.max_chars:
            return
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            f = self._file(engine, emotion, text)
            tmp = f.with_suffix(".tmp")
            audio.save(tmp)
            os.replace(tmp, f)  # if interrupted midway, a corrupt .wav must not stay in the cache
            self._writes += 1
            if self._writes % 100 == 0:
                files = sorted(self.path.glob("*.wav"), key=lambda p: p.stat().st_mtime)
                for old in files[: max(0, len(files) - self.max_files)]:
                    old.unlink()
        except OSError:
            pass  # cache cleanup is retried on a later write


class TTSRouter:
    """Tries the next suitable engine. An engine that errors is skipped for `cooldown` seconds."""

    def __init__(self, cloud: TTS | None, local: TTS | None, prefer: str = "cloud",
                 policy: Callable[[], dict[str, Any]] = core_policy, cooldown: float = 300,
                 cache: SpeechCache | None = None, backup: TTS | None = None, premium: TTS | None = None):
        # ElevenLabs: if present, it goes first everywhere (in flight too; it doesn't use the GPU)
        self.premium = premium
        # If the key is entered later (Bağlantılar → Anahtarlar), it takes effect without a restart
        self.premium_factory: Callable[[], TTS | None] | None = None
        self._premium_checked = 0.0
        self.cloud = cloud
        self.local = local
        self.backup = backup  # fast last resort (Edge): when the cloud and local voices fail
        self.prefer = prefer
        self.policy = policy
        self.cooldown = cooldown
        self.down: dict[str, float] = {}
        self.last = ""
        self.cache = cache
        # Sticky engine: once switched to an engine, stay on it for a while. Before, each sentence came from the
        # first suitable engine in line (or from another engine's cache), so ALICE's voice changed sentence by sentence.
        self.sticky = ""
        self.sticky_until = 0.0

    @property
    def name(self) -> str:
        return self.last or (self.order()[0].name if self.order() else "yok")

    def release_gpu(self) -> None:
        """When the core switches to flight mode: release the local voice model if it is on the GPU.
        In flight the cloud voice is used first, so the local engine was never called and its model
        stayed on the GPU for the whole of DCS."""
        fn = getattr(self.local, "release_gpu", None)
        if fn is not None:
            fn()

    def order(self) -> list[TTS]:
        # Backup (Edge) goes last: ALICE's own voice first (the cloud, or the local engine that imitates it).
        engines = ([self.local, self.cloud, self.backup] if self.prefer == "local"
                   else [self.cloud, self.local, self.backup])
        if self.policy().get("local_gpu") is False and self.local is not None:
            # In flight the GPU stays with the game: cloud first; local only on the CPU, as a last resort.
            engines = [self.cloud, self.backup, self.local]
        if self.premium is None and self.premium_factory is not None and time.monotonic() - self._premium_checked > 20:
            self._premium_checked = time.monotonic()
            self.premium = self.premium_factory()
        engines = [e for e in [self.premium, *engines] if e is not None]
        now = time.monotonic()
        stick = next((e for e in engines if e.name == self.sticky), None) if now < self.sticky_until else None
        if stick is not None and self.down.get(stick.name, 0) <= now:
            engines = [stick] + [e for e in engines if e is not stick]
        return engines

    def warm_up(self) -> None:
        """Load the local voice model onto the GPU in the background so the first answer doesn't wait 20-25 s."""
        load = getattr(self.local, "_load", None)
        if load is None or self.policy().get("local_gpu") is False:
            return

        def run() -> None:
            try:
                with self.local._lock:
                    load()
            except Exception:  # noqa: BLE001 - if warm-up fails, it loads on the first sentence
                pass

        threading.Thread(target=run, daemon=True, name="alice-tts-warm").start()

    def cached(self, text: str, emotion: str = "calm") -> bool:
        """Is this sentence's audio in the cache (from any engine)?"""
        from alice.voice.spoken_numbers import normalize

        if self.cache is None:
            return False
        text = normalize(text)
        return any(self.cache._file(e.name, emotion, text).exists() for e in self.order())

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        from alice.voice.spoken_numbers import normalize

        text = normalize(text)
        errors = []
        now = time.monotonic()
        order = self.order()
        usable = [e for e in order if self.down.get(e.name, 0) <= now]
        if self.cache is not None and usable:
            # Only the cache of the engine speaking now: another engine's recording would be a different voice.
            hit = self.cache.get(usable[0].name, emotion, text)
            if hit is not None:
                self.last = f"{usable[0].name} (önbellek)"
                return hit
        for e in order:
            if self.down.get(e.name, 0) > now:
                errors.append(f"{e.name}: bekletiliyor")
                continue
            try:
                audio = e.synth(text, emotion)
                self.last = e.name
                if e is not order[0] or self.sticky == e.name:
                    self.sticky, self.sticky_until = e.name, time.monotonic() + 600
                if self.cache is not None:
                    self.cache.put(e.name, emotion, text, audio)
                return audio
            except Exception as ex:  # noqa: BLE001 - CUDA OOM, bad audio data: next engine, hold this one
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
    if v.get("gemini_tts_model"):  # old single-model setting: move it to the front
        models = [v["gemini_tts_model"]] + [m for m in models if m != v["gemini_tts_model"]]
    return GeminiTTS(key, name or "Aoede", models)


def make_local(cfg: Config, name: str | None = None) -> TTS | None:
    name = name if name is not None else cfg.data.get("voice", {}).get("tts_local", "")
    allow = lambda: core_policy().get("local_gpu", True) is not False  # noqa: E731
    lang = speech_language(cfg.data.get("voice", {}).get("language", "auto"))
    if name == "xtts":
        return XTTS(allow_gpu=allow, language=lang)
    if name == "chatterbox":
        from alice.voice.stt import make_stt
        from alice.voice.trim import make_trimmer

        v = cfg.data.get("voice", {})
        trim = make_trimmer(make_stt(cfg) if v.get("local_trim_whisper", True) else None)
        return Chatterbox(v.get("local_python", ""), int(v.get("local_port", 8771)), allow_gpu=allow, trim=trim,
                          temperature=float(v.get("local_temperature", 0.6)),
                          verify=bool(v.get("local_verify", True)),
                          whisper_model=str(v.get("local_whisper_model", "small")), language=lang)
    return None


def make_eleven(cfg: Config) -> ElevenLabsTTS | None:
    v = cfg.data.get("voice", {})
    voice = str(v.get("tts_eleven", "") or "")
    key = os.environ.get(str(v.get("eleven_api_key_env", "ELEVENLABS_API_KEY")), "")
    if not voice or not key:
        return None
    return ElevenLabsTTS(key, voice, str(v.get("tts_eleven_model", "auto") or "auto"),
                         language=speech_language(v.get("language", "auto")))


def make_tts(cfg: Config) -> TTSRouter:
    v = cfg.data.get("voice", {})
    edge = edge_voice(v.get("tts_edge", "auto"), v.get("language", "auto"))
    r = TTSRouter(make_cloud(cfg), make_local(cfg), v.get("prefer", "cloud"), cache=SpeechCache(),
                  backup=EdgeTTS(edge) if edge else None, premium=make_eleven(cfg))
    r.premium_factory = lambda: make_eleven(cfg)
    return r
