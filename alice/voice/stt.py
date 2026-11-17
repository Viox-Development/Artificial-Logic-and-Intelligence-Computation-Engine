"""Speech-to-text transcription (Whisper).

Cloud: whisper-large-v3-turbo on Groq (fast, free tier, does not touch the GPU).
Local: faster-whisper (a fast, lightweight version of the same model). It kicks in when there is no internet
or quota; in flight it does not use the GPU and runs on the CPU with a small model.
"""

from __future__ import annotations

import io
import os
import re
import threading
import time
from typing import Any, Callable, Protocol

import httpx

from alice.config import Config
from alice.voice.audio import Audio


class STTError(Exception):
    pass


class STT(Protocol):
    name: str

    def transcribe(self, audio: Audio) -> str: ...


_DLLS_DONE = False


def add_cuda_dlls() -> None:
    """Windows: faster-whisper (ctranslate2) cannot find cublas64_12.dll/cudnn; add the ones inside the PyTorch
    and nvidia-* pip packages to the search path. On other systems it does nothing."""
    global _DLLS_DONE
    if _DLLS_DONE or os.name != "nt":
        return
    _DLLS_DONE = True
    import importlib.util
    from pathlib import Path

    dirs: list[Path] = []
    spec = importlib.util.find_spec("torch")
    if spec and spec.origin:
        dirs.append(Path(spec.origin).parent / "lib")
    spec = importlib.util.find_spec("nvidia")
    for root in (spec.submodule_search_locations or []) if spec else []:
        dirs += list(Path(root).glob("*/bin"))
    for d in dirs:
        if d.is_dir():
            try:
                os.add_dll_directory(str(d))  # type: ignore[attr-defined]
            except OSError:
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


# Whisper weights on the graphics card are int8, computation is float16: memory halves (small ~0.5 -> ~0.25 GB),
# accuracy and speed are almost the same as with float16. [voice] gpu_compute = "float16" is the old behavior.
GPU_COMPUTE = "int8_float16"


def _free_cuda() -> None:
    """The GPU memory of a deleted model is released when its Python object is collected; collect it right away."""
    import gc

    gc.collect()


def _cuda_ok(model) -> bool:
    """Don't assume CUDA works just because the model loaded: a DLL error only shows up on the first
    transcription. Test with half a second of silence (the first CUDA call takes ~2.5 s, which also warms it up)."""
    try:
        segs, _ = model.transcribe(io.BytesIO(Audio.silence(0.5, 16000).to_wav()), beam_size=1)
        list(segs)
        return True
    except Exception:  # noqa: BLE001
        return False


class ElevenScribe:
    """ElevenLabs Scribe (ELEVENLABS_API_KEY). Clearly better than Groq Whisper in Turkish: Whisper wrote
    "Alice naber" (Turkish for "what's up") as "Aysenabek" and "Alessandraş". With keyterms, "Alice" and flight
    terms are spelled correctly. If credits run out it raises STTError: the router switches to Groq."""

    URL = "https://api.elevenlabs.io/v1/speech-to-text"
    KEYTERMS = ["Alice", "DCS", "War Thunder", "bingo", "joker", "waypoint", "kneeboard", "chaff", "flare"]

    def __init__(self, api_key: str, model: str = "scribe_v2", language: str = "tr",
                 client: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.language = language
        self.name = f"eleven:{model}"
        self.client = client or httpx.Client(timeout=httpx.Timeout(20, connect=8))

    def transcribe(self, audio: Audio) -> str:
        data = {"model_id": self.model, "language_code": self.language, "tag_audio_events": "false",
                "keyterms": self.KEYTERMS}
        try:
            r = self.client.post(self.URL, headers={"xi-api-key": self.api_key}, data=data,
                                 files={"file": ("speech.wav", audio.resample(16000).to_wav(), "audio/wav")})
        except httpx.HTTPError as e:
            raise STTError(f"elevenlabs bağlantı hatası: {e}") from e
        if r.status_code != 200:
            raise STTError(f"elevenlabs {r.status_code}: {r.text[:200]}")
        return str(r.json().get("text", "")).strip()


class GroqWhisper:
    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo", language: str = "tr",
                 client: httpx.Client | None = None, prompt: str = ""):
        self.api_key = api_key
        self.model = model
        self.language = language
        self.prompt = prompt
        self.name = f"groq:{model}"
        self.client = client or httpx.Client(timeout=30)

    def transcribe(self, audio: Audio) -> str:
        data = {"model": self.model, "language": self.language, "response_format": "json", "temperature": "0"}
        if self.prompt:
            data["prompt"] = self.prompt
        try:
            r = self.client.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"}, data=data,
                                 files={"file": ("speech.wav", audio.resample(16000).to_wav(), "audio/wav")})
        except httpx.HTTPError as e:
            raise STTError(f"groq bağlantı hatası: {e}") from e
        if r.status_code != 200:
            raise STTError(f"groq {r.status_code}: {r.text[:200]}")
        return str(r.json().get("text", "")).strip()

    def words(self, audio: Audio) -> list[tuple[str, float, float]]:
        """Transcribe with word timings: [(word, start in seconds, end in seconds)]."""
        data = {"model": self.model, "language": self.language, "response_format": "verbose_json",
                "temperature": "0", "timestamp_granularities[]": ["word"]}
        try:
            r = self.client.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"}, data=data,
                                 files={"file": ("speech.wav", audio.resample(16000).to_wav(), "audio/wav")})
        except httpx.HTTPError as e:
            raise STTError(f"groq bağlantı hatası: {e}") from e
        if r.status_code != 200:
            raise STTError(f"groq {r.status_code}: {r.text[:200]}")
        return [(str(w.get("word", "")), float(w.get("start", 0)), float(w.get("end", 0)))
                for w in r.json().get("words", []) or []]

    def transcribe_file(self, data: bytes, filename: str) -> str:
        """Recordings from the browser, such as webm/ogg (Groq decodes them itself)."""
        try:
            r = self.client.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"},
                                 data={"model": self.model, "language": self.language, "response_format": "json",
                                       "temperature": "0", **({"prompt": self.prompt} if self.prompt else {})},
                                 files={"file": (filename, data, "application/octet-stream")})
        except httpx.HTTPError as e:
            raise STTError(f"groq bağlantı hatası: {e}") from e
        if r.status_code != 200:
            raise STTError(f"groq {r.status_code}: {r.text[:200]}")
        return str(r.json().get("text", "")).strip()


class LocalWhisper:
    """faster-whisper. large-v3-turbo if the GPU is allowed, otherwise a small model on the CPU."""

    def __init__(self, gpu_model: str = "large-v3-turbo", cpu_model: str = "small", language: str = "tr",
                 allow_gpu: Callable[[], bool] = lambda: True, prompt: str = "", compute: str = GPU_COMPUTE,
                 idle_unload: float = 600):
        self.gpu_model = gpu_model
        self.cpu_model = cpu_model
        self.compute = compute
        self.idle_unload = idle_unload  # fallback engine: unload from memory if unused for this many seconds
        self.last_used = 0.0
        self.language = language
        self.allow_gpu = allow_gpu
        self.prompt = prompt
        self.name = "local-whisper"
        self._models: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _model(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise STTError("faster-whisper kurulu değil: pip install faster-whisper") from e
        gpu = False
        if self.allow_gpu():
            try:
                import ctranslate2

                gpu = ctranslate2.get_cuda_device_count() > 0
            except (ImportError, RuntimeError):
                gpu = False
        if gpu and "cuda" not in self._models and not self._models.get("cuda_failed"):
            add_cuda_dlls()
            try:
                m = WhisperModel(self.gpu_model, device="cuda", compute_type=self.compute)
                if _cuda_ok(m):
                    self._models["cuda"] = m
                else:
                    self._models["cuda_failed"] = True
            except Exception:  # noqa: BLE001 - no CUDA: fall back to the CPU
                self._models["cuda_failed"] = True
        if gpu and "cuda" in self._models:
            return self._models["cuda"]
        if "cpu" not in self._models:
            self._models["cpu"] = WhisperModel(self.cpu_model, device="cpu", compute_type="int8")
        return self._models["cpu"]

    def transcribe(self, audio: Audio) -> str:
        return self.transcribe_file(audio.resample(16000).to_wav(), "speech.wav")

    def transcribe_file(self, data: bytes, filename: str) -> str:
        # faster-whisper decodes wav/webm/ogg, all of them, through PyAV.
        with self._lock:
            self.last_used = time.monotonic()
            segments, _ = self._model().transcribe(io.BytesIO(data), language=self.language, vad_filter=True,
                                                   beam_size=1, initial_prompt=self.prompt or None)
            return " ".join(s.text.strip() for s in segments).strip()

    def trim(self, gpu_ok: bool) -> None:
        """In flight (GPU not allowed) release the model on the graphics card; also drop backup models that have
        been unused for a long time. While the cloud (Groq) works this engine is never used, but once it has been
        loaded, a ~1 GB model kept taking up space permanently."""
        if not self._lock.acquire(blocking=False):
            return  # it is transcribing right now
        try:
            idle = self.last_used and time.monotonic() - self.last_used > self.idle_unload
            drop = [k for k in ("cuda", "cpu") if k in self._models and (idle or (k == "cuda" and not gpu_ok))]
            for k in drop:
                del self._models[k]
            if drop:
                _free_cuda()
        finally:
            self._lock.release()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-zçğıöşü0-9 ]", " ", text.lower())).strip()


def speech_segments(segments, prompt: str = "Hey Alice.") -> list:
    """Drop Whisper segments that are not real speech. Because initial_prompt is "Hey Alice.", Whisper can echo
    the hint back word for word on game/engine noise or music: ALICE woke up with nobody speaking. Also dropped:
    looping repetitions (high compression ratio) and segments with a high "no speech" probability plus low
    confidence."""
    hint = _norm(prompt)
    out = []
    for s in segments:
        nsp = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
        lp = float(getattr(s, "avg_logprob", 0.0) or 0.0)
        cr = float(getattr(s, "compression_ratio", 1.0) or 1.0)
        if cr > 2.4 or (nsp > 0.6 and lp < -0.7):
            continue
        t = _norm(getattr(s, "text", ""))
        echo = t in {"hey alice", "alice", "hey alis", "hey"} or (len(t) > 3 and t in hint)
        if echo and (nsp > 0.35 or lp < -0.9):
            continue
        out.append(s)
    return out


# Words often said in flight and misheard ("yakıtım" (my fuel) -> "yakıştın", "cannon" -> "Canon").
FLIGHT_HINT = "Yakıtım ne kadar kaldı? Bingo, cannon, waypoint, tanker, AoA, stall, iniş takımı, flap."


class WakeGate:
    """Uses a small local Whisper to look only at the start of the sentence and check whether "Hey Alice" is there.

    This way not every conversation in the room is sent to Groq (quota and latency); if there is no wake word,
    the audio never goes to the cloud. If faster-whisper is not installed, available() returns False and the old
    behavior continues."""

    HEAD_SECONDS = 2.5

    def __init__(self, model: str = "base", language: str = "tr", gpu_model: str = "",
                 allow_gpu: Callable[[], bool] = lambda: False, compute: str = GPU_COMPUTE):
        self.model_name = model
        self.cpu_model = model
        self.compute = compute
        self.load_seconds = 0.0
        self.gpu_model = gpu_model  # model that hears better if there is a graphics card (e.g. small): ~0.1-0.2 s
        self.allow_gpu = allow_gpu
        self.device = ""
        self.last_seconds = 0.0
        self.last_wait = 0.0  # time spent waiting because the model was busy with another transcription
        self.last_conf = 0.0  # confidence of the last head transcription (average log probability)
        self.language = language
        self._model: Any = None
        self._failed = False
        self._lock = threading.Lock()

    def available(self) -> bool:
        if self._failed:
            return False
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            self._failed = True
            return False
        return True

    def _load(self):
        from faster_whisper import WhisperModel

        t0 = time.monotonic()
        try:
            if self.gpu_model and self.allow_gpu():
                add_cuda_dlls()
                try:
                    m = WhisperModel(self.gpu_model, device="cuda", compute_type=self.compute)
                    if _cuda_ok(m):
                        self.device, self.model_name = "cuda", self.gpu_model
                        return m
                except Exception:  # noqa: BLE001 - no CUDA: small model on the CPU
                    pass
                self._cuda_failed = True  # don't try moving to the graphics card again
            self.device, self.model_name = "cpu", self.cpu_model
            # In flight the CPU is needed by DCS: Whisper should use 4 threads, not all the cores.
            return WhisperModel(self.cpu_model, device="cpu", compute_type="int8", cpu_threads=4)
        finally:
            self.load_seconds = time.monotonic() - t0

    def warm_up(self, log: Callable[[str], None] | None = None) -> None:
        """Load the model in the background so the first "Hey Alice" does not wait for the model to load."""
        def run() -> None:
            if self.available():
                with self._lock:
                    if self._model is None:
                        try:
                            self._model = self._load()
                            if log:
                                log(f"uyandırma modeli hazır: {self.model_name} ({self.device}), "
                                    f"{self.load_seconds:.1f} sn")
                        except Exception:  # noqa: BLE001
                            self._failed = True

        threading.Thread(target=run, daemon=True, name="alice-wake-warm").start()

    def sync(self, gpu_ok: bool) -> None:
        """Move the model to the right device when the mode changes: when DCS starts, leave the graphics card and
        switch to the CPU (leaving memory for the game); when it closes, return to the graphics card. The new model
        loads outside the lock; a "Hey Alice" that arrives meanwhile is answered with the old model and
        does not wait."""
        want = "cuda" if gpu_ok and self.gpu_model else "cpu"
        if (self._model is None or self.device == want or self._failed or getattr(self, "_moving", False)
                or (want == "cuda" and getattr(self, "_cuda_failed", False))):
            return
        self._moving = True

        def run() -> None:
            try:
                m = self._load()
                with self._lock:
                    self._model = m
                _free_cuda()
            except Exception:  # noqa: BLE001 - if it cannot be moved, keep the old one
                pass
            finally:
                self._moving = False

        threading.Thread(target=run, daemon=True, name="alice-wake-move").start()

    def flying(self) -> bool:
        """Flight mode: there is a graphics card but its use is turned off (local_gpu = false)."""
        if not self.gpu_model or getattr(self, "_cuda_failed", False):
            return False
        try:
            return not self.allow_gpu()
        except Exception:  # noqa: BLE001
            return False

    def heard(self, audio: Audio) -> str | None:
        """Local transcription of the start of the sentence; None if the local model is unavailable."""
        if not self.available():
            return None
        head = Audio(audio.pcm[: int(self.HEAD_SECONDS * audio.rate) * 2], audio.rate).resample(16000)
        t0 = time.monotonic()
        try:
            with self._lock:
                # Waiting and computing are timed separately: median 1.3 s in game, 0.4 s idle
                # (Nov 7 measurement); which one is growing?
                self.last_wait = time.monotonic() - t0
                if self._model is None:
                    self._model = self._load()
                segments, _ = self._model.transcribe(io.BytesIO(head.to_wav()), language=self.language,
                                                     beam_size=1, initial_prompt=None,
                                                     condition_on_previous_text=False)
                segs = speech_segments(segments)
                self.last_seconds = time.monotonic() - t0
                self._errors = 0
                self.last_conf = min((s.avg_logprob for s in segs), default=-9.0)
                return " ".join(s.text.strip() for s in segs).strip()
        except Exception:  # noqa: BLE001 - if the local model is broken, fall back to the cloud
            # A single failure (a game filled GPU memory, CUDA OOM) must not shut the gate for good: drop the model,
            # let it reload on the next sentence; give up only if it fails several times in a row.
            self._errors = getattr(self, "_errors", 0) + 1
            self._model = None
            if self._errors >= 3:
                self._failed = True
            return None

    def transcribe(self, audio: Audio, prompt: str = "Hey Alice.") -> tuple[str, float] | None:
        """Local transcription of the whole sentence and its confidence (average log probability; closer to 0
        means more certain). None if there is no model."""
        if not self.available():
            return None
        wav = audio.resample(16000).to_wav()
        try:
            with self._lock:
                if self._model is None:
                    self._model = self._load()
                if self.flying():
                    prompt = f"{prompt} {FLIGHT_HINT}".strip()
                segments, _ = self._model.transcribe(io.BytesIO(wav), language=self.language, beam_size=1,
                                                     initial_prompt=prompt, condition_on_previous_text=False)
                segs = speech_segments(segments, prompt)
        except Exception:  # noqa: BLE001
            return None
        text = " ".join(s.text.strip() for s in segs).strip()
        if not segs:
            return text, -9.0
        return text, min(s.avg_logprob for s in segs)


class STTRouter:
    def __init__(self, engines: list[STT], cooldown: float = 300):
        self.wake_gate: WakeGate | None = None
        self.engines = engines
        self.cooldown = cooldown
        self.down: dict[str, float] = {}
        self.last = ""

    def transcribe(self, audio: Audio) -> str:
        return self._try(lambda e: e.transcribe(audio))

    def transcribe_file(self, data: bytes, filename: str) -> str:
        return self._try(lambda e: e.transcribe_file(data, filename))

    def trim(self, gpu_ok: bool) -> None:
        """On each turn of the core: if the graphics card permission changed, move the models; drop idle backups."""
        if self.wake_gate is not None:
            self.wake_gate.sync(gpu_ok)
        for e in self.engines:
            if hasattr(e, "trim"):
                e.trim(gpu_ok)

    def _try(self, fn) -> str:
        errors = []
        now = time.monotonic()
        for e in self.engines:
            if self.down.get(e.name, 0) > now:
                continue
            try:
                text = fn(e)
                self.last = e.name
                return text
            except Exception as ex:  # noqa: BLE001 - CUDA memory, model download, broken response:
                # move on to the next engine
                self.down[e.name] = now + self.cooldown
                errors.append(f"{e.name}: {ex}" if isinstance(ex, STTError) else f"{e.name}: {type(ex).__name__}: {ex}")
        raise STTError("; ".join(errors) or "konuşma tanıma motoru yok")


# Whisper makes up sentences it memorized from YouTube subtitles when there is silence or noise
# (such as "Altyazı M.K.", a Turkish subtitle credit).
HALLUCINATIONS = ("altyazı", "alt yazı", "m.k.", "abone ol", "izlediğiniz için", "izlediğin için", "teşekkürler",
                  "teşekkür ederim", "bir sonraki videoda", "videoyu beğen", "kanalıma", "hoşça kalın",
                  "amara.org", "subtitles by", "thanks for watching", "www.", "sesli betimleme")


def is_hallucination(text: str) -> bool:
    t = text.lower().strip(" .,!?…\"'")
    if not t:
        return True
    # It returned several terms from the hint text verbatim (separated by commas/periods): "Hey Alice. ALICE, DCS".
    # A single term ("bingo", "Hey Alice") can be real speech, so leave it alone.
    if len(t) > 3 and any(c in t for c in ",.") and (t in PROMPT.lower() or t in LEGACY_PROMPT.lower()):
        return True
    # A short echo of the old hint: uppercase "ALICE," followed by one garbled word ("ALICE, DIGITI.",
    # "ALICE, D'AMO."). In real speech Whisper writes the name as "Alice"; this format is the hint itself.
    if re.fullmatch(r"\W*(HEY\s+)?ALICE[,.]\s*[\w'’-]{1,12}[.?!]?\W*", text.strip()):
        return True
    # It returned a garbled hint: "ALICE, DIGITAL, Bingo, waypoint, kneeboard." If most of the words
    # come from the hint, it is noise; since it started with "ALICE", ALICE kept waking itself up.
    words = re.findall(r"\w+", t)
    hits = sum(1 for w in words if w in _PROMPT_WORDS)
    tail = sum(1 for w in words if w in ("bingo", "waypoint", "kneeboard"))
    if hits >= 3 and hits / len(words) > 0.75 and tail >= 2:
        return True
    if "alice" in t:
        return False
    return len(t) < 60 and any(h in t for h in HALLUCINATIONS)


# Hint for Whisper so names are spelled correctly. "Alice" is deliberately NOT here: in noise Whisper returned the
# hint garbled ("ALICE, DIGITI.", "ALICE, D'AMO.", "ALICE, DELİK." (Turkish for "hole")), and since it began with
# ALICE the assistant kept waking itself up (voice.log, dozens of times on the night of Oct 24). strip_wake already
# recognizes the other spellings of the name.
PROMPT = "DCS, F-16, Unity, War Thunder, bingo, waypoint, kneeboard."
LEGACY_PROMPT = "Hey Alice. ALICE, DCS, F-16, Unity, War Thunder, bingo, waypoint, kneeboard."
# "alice" stays in the list so that echoes of the old hint ("Hey Alice. ALICE, DCS, ...") are recognized too.
_PROMPT_WORDS = set(re.findall(r"\w+", PROMPT.lower())) | {"alice"}


def make_stt(cfg: Config) -> STTRouter:
    from alice.voice.tts import core_policy

    v = cfg.data.get("voice", {})
    lang = v.get("language", "tr")
    compute = str(v.get("gpu_compute", GPU_COMPUTE) or GPU_COMPUTE)
    engines: list[STT] = []
    eleven = os.environ.get(str(v.get("eleven_api_key_env", "ELEVENLABS_API_KEY")), "")
    # Scribe is only the "second ear": sending every sentence to it used up ElevenLabs credits within minutes.
    second = ElevenScribe(eleven, str(v.get("stt_eleven", "scribe_v2")), lang) if eleven and v.get("stt_eleven",
                                                                                                    "scribe_v2") else None
    for ref in v.get("stt", ["groq:whisper-large-v3-turbo", "local:large-v3-turbo"]):
        kind, _, model = ref.partition(":")
        if kind == "groq":
            env = cfg.data.get("providers", {}).get("groq", {}).get("api_key_env", "GROQ_API_KEY")
            key = os.environ.get(env, "")
            if key:
                engines.append(GroqWhisper(key, model or "whisper-large-v3-turbo", lang, prompt=PROMPT))
        elif kind == "local":
            engines.append(LocalWhisper(model or "large-v3-turbo", v.get("stt_cpu_model", "small"), lang,
                                        allow_gpu=lambda: core_policy().get("local_gpu", True) is not False,
                                        prompt=PROMPT, compute=compute))
    router = STTRouter(engines)
    router.second_ear = second
    gate_model = v.get("wake_local", "base")
    if gate_model:
        router.wake_gate = WakeGate(gate_model, lang, gpu_model=str(v.get("wake_local_gpu", "small") or ""),
                                    allow_gpu=lambda: core_policy().get("local_gpu", True) is not False,
                                    compute=compute)
    return router
