"""ALICE local voice server (Chatterbox multilingual).

Runs in a separate virtual environment (installed by `alice voice install-local`) so that Chatterbox's PyTorch/numpy
versions don't break ALICE's environment, and the model is loaded onto the GPU once and shared by the core, the voice
assistant and the quick bar. This file does not import the ALICE package.

Binds only to 127.0.0.1.
  GET  /health                 -> {"ok": true, "device": "cuda", "loaded": true}
  POST /release                -> unload the models from memory (to free the GPU for a game during flight)
  POST /synth  (JSON)          -> audio/wav
       {"text", "language": "tr", "ref": "C:/.../calm.wav" | "", "exaggeration", "cfg_weight", "temperature",
        "device": "cuda"|"cpu", "verify": true, "whisper_model": "small"}
       verify: the generated audio is listened to with local Whisper; anything after the sentence's last word
       is cut (made-up speech, crackle); if what is heard differs a lot from the written text, it is generated
       once more and the better take is kept.
"""

from __future__ import annotations

import os
import sys

# This file runs as a script from inside alice/voice: remove that folder from the import path so the modules in it
# (e.g. audio.py) don't clash with the standard library or with what numpy/torch import.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import traceback  # noqa: E402
import wave  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
STATE = {"model": None, "device": None, "ref": None}


def load(device: str):
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    if STATE["model"] is not None and STATE["device"] == device:
        return STATE["model"]
    if STATE["model"] is not None:
        # Device changed (e.g. GPU released in flight mode): unload the old one and reload (weights are on disk).
        STATE["model"] = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    cap_tokens(model)
    STATE["model"] = model
    STATE["device"] = device
    STATE["ref"] = None
    return STATE["model"]


# Chatterbox caps each sentence at 1000 speech tokens (~40 s of audio). The model sometimes fails to find the end
# of a sentence, loops, and generates up to that cap ("forcing EOS"): a single sentence took 40-120 s.
# Set the cap based on text length: Turkish ~14 letters/s, model ~25 tokens/s;
# 2x headroom + a floor for short sentences.
TOKENS = {"max": 1000}


def max_tokens_for(text: str) -> int:
    return int(min(1000, max(120, len(text) / 14 * 25 * 2 + 60)))


def cap_tokens(model) -> None:
    t3 = getattr(model, "t3", None)
    inner = getattr(t3, "inference", None)
    if inner is None or getattr(inner, "_alice_capped", False):
        return

    def inference(*args, **kwargs):
        if "max_new_tokens" in kwargs:
            kwargs["max_new_tokens"] = min(int(kwargs["max_new_tokens"]), TOKENS["max"])
        else:
            kwargs["max_new_tokens"] = TOKENS["max"]
        return inner(*args, **kwargs)

    inference._alice_capped = True
    t3.inference = inference


def chunks(text: str, limit: int = 160) -> list:
    """Split long text into sentences, and split a sentence at commas if still too long: the model loops less
    on short pieces."""
    import re

    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    out = []
    for p in parts:
        while len(p) > limit:
            cut = max(p.rfind(", ", 0, limit), p.rfind("; ", 0, limit))
            if cut < limit // 3:
                cut = p.rfind(" ", 0, limit)
            if cut <= 0:
                break
            out.append(p[: cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    # Merge very short pieces into the previous one ("Tamam.", Turkish for "OK", alone gets an odd intonation).
    merged = []
    for p in out:
        if merged and (len(p) < 25 or len(merged[-1]) < 25) and len(merged[-1]) + len(p) < limit:
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged or [text]


WHISPER = {"model": None, "key": None}


def release() -> None:
    """Unload the voice and Whisper models and give GPU memory back to the OS. The next sentence reloads them
    (on the requested device; CPU during flight)."""
    import gc

    STATE.update(model=None, device=None, ref=None)
    WHISPER.update(model=None, key=None)
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass  # no torch: no CUDA cache to free


def load_whisper(name: str, device: str):
    """OpenAI Whisper (local, free). If it is not installed, returns None: verification is skipped."""
    try:
        import whisper
    except ImportError:
        return None
    key = (name, device)
    if WHISPER["key"] != key:
        WHISPER["model"] = None
        WHISPER["model"] = whisper.load_model(name, device=device)
        WHISPER["key"] = key
    return WHISPER["model"]


def fold(s: str) -> list:
    import re
    import unicodedata

    s = s.lower().replace("ı", "i").replace("İ", "i")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", s)


def same(a: str, b: str) -> bool:
    return a == b or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4])


def assess(words: list, text: str) -> tuple:
    """(quality 0..1, seconds to cut or None). Quality: how much of the requested words were heard,
    and how many extra (made-up) words there are."""
    want = fold(text)
    if not want:
        return 1.0, None
    heard = []
    for w, start, end in words:
        for x in fold(w):
            heard.append((x, start, end))
    matched = {y for y in want if any(same(x, y) for x, _, _ in heard)}
    extra = [x for x, _, _ in heard if not any(same(x, y) for y in want)]
    quality = len(matched) / len(set(want)) - 0.5 * max(0, len(extra) - 1) / len(want)
    seen = set()
    end_at = None
    for x, _start, end in heard:
        if any(same(x, y) for y in want):
            seen.add(x)
        # If the last word comes after most of the sentence has been spoken, that is the real end.
        if same(x, want[-1]) and len(seen) >= 0.6 * len(set(want)):
            end_at = end
            break
    return max(0.0, quality), end_at


def transcribe_words(samples, rate: int, name: str, device: str):
    import numpy as np

    model = load_whisper(name, device)
    if model is None:
        return None
    n16 = int(len(samples) * 16000 / rate)
    audio16 = np.interp(np.linspace(0, len(samples) - 1, n16), np.arange(len(samples)), samples).astype("float32")
    res = model.transcribe(audio16, language="tr", word_timestamps=True, fp16=(device == "cuda"), temperature=0.0,
                           condition_on_previous_text=False)
    return [(w.get("word", ""), float(w.get("start", 0)), float(w.get("end", 0)))
            for seg in res.get("segments", []) for w in seg.get("words", []) or []]


def synth(d: dict) -> tuple:
    """(wav bytes, extra headers). Long text is split into pieces, each generated separately, then joined."""
    import numpy as np

    text = str(d["text"]).strip()
    pieces = chunks(text)
    if len(pieces) == 1:
        return synth_one(d)
    rate, out, headers = 24000, [], {}
    for p in pieces:
        wav, h = synth_one({**d, "text": p})
        with wave.open(io.BytesIO(wav), "rb") as w:
            rate = w.getframerate()
            out.append(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"))
        out.append(np.zeros(int(rate * 0.12), dtype="<i2"))  # short breath between sentences
        headers = h
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.concatenate(out[:-1]).tobytes())
    return buf.getvalue(), headers


def synth_one(d: dict) -> tuple:
    import inspect

    import numpy as np

    text = str(d["text"]).strip()
    if text and text[-1] not in ".!?…":
        text += "."  # a sentence ending without punctuation makes the model more likely to invent an ending
    device = d.get("device") or "cuda"
    verify = bool(d.get("verify", True))
    headers = {"X-Alice-Verified": "0"}
    with LOCK:
        model = load(device)
        device = STATE["device"]
        rate = int(getattr(model, "sr", 24000))
        wanted = {"language_id": d.get("language") or "tr"}
        TOKENS["max"] = max_tokens_for(text)
        # Don't reprocess the reference audio on every request: if the same recording and exaggeration level are
        # used, the model's prepared conditions are reused.
        ref_key = (d.get("ref") or "", d.get("exaggeration"))
        if d.get("ref") and (STATE.get("ref") != ref_key or getattr(model, "conds", None) is None):
            wanted["audio_prompt_path"] = d["ref"]
        STATE["ref"] = ref_key
        for k in ("exaggeration", "cfg_weight", "temperature", "repetition_penalty", "min_p", "top_p"):
            if d.get(k) is not None:
                wanted[k] = float(d[k])
        # Chatterbox parameters differ between versions: pass only the supported ones.
        try:
            params = inspect.signature(model.generate).parameters
            accepts_any = any(p.kind is p.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            params, accepts_any = {}, True
        best = None
        for attempt in range(2 if verify else 1):
            kw = dict(wanted)
            if attempt and "temperature" in kw:
                kw["temperature"] = max(0.3, kw["temperature"] * 0.75)  # the second attempt is more cautious
            kwargs = {k: v for k, v in kw.items() if accepts_any or k in params}
            wav = model.generate(text, **kwargs)
            samples = wav.squeeze().detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav).squeeze()
            samples = samples.astype("float32")
            if not verify:
                best = (samples, None, None)
                break
            try:
                words = transcribe_words(samples, rate, d.get("whisper_model") or "small", device)
            except Exception:  # noqa: BLE001 - verification should not block the audio
                traceback.print_exc()
                words = None
            if words is None:
                best = (samples, None, None)
                break
            quality, end_at = assess(words, text)
            if best is None or best[1] is None or quality > best[1]:
                best = (samples, quality, end_at)
            if quality >= 0.85:
                break
        samples, quality, end_at = best
        if quality is not None:
            headers = {"X-Alice-Verified": "1", "X-Alice-Quality": f"{quality:.2f}"}
            if end_at is not None and end_at + 0.15 < len(samples) / rate:
                samples = samples[: int((end_at + 0.15) * rate)]
                headers["X-Alice-Trimmed"] = "1"
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue(), headers


MAX_BODY = 256 * 1024  # bytes: request body
MAX_TEXT = 4000  # characters: longer text would lock up the GPU for minutes
NUMERIC = ("exaggeration", "cfg_weight", "temperature", "repetition_penalty", "min_p", "top_p")


class BadRequest(ValueError):
    pass


def validate(data) -> dict:
    """Validate the /synth request so a malformed one gets a clear 400 instead of a 500 (stack dump)."""
    if not isinstance(data, dict):
        raise BadRequest("JSON nesnesi bekleniyor")
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("text bos")
    if len(text) > MAX_TEXT:
        raise BadRequest(f"text cok uzun ({len(text)} > {MAX_TEXT})")
    out = dict(data)
    for k in NUMERIC:
        v = data.get(k)
        if v is None:
            continue
        if isinstance(v, bool):
            raise BadRequest(f"{k} sayi olmali")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise BadRequest(f"{k} sayi olmali") from None
        if f != f or f in (float("inf"), float("-inf")):
            raise BadRequest(f"{k} sonlu olmali")
        out[k] = f
    device = data.get("device") or "cuda"
    if device not in ("cuda", "cpu"):
        raise BadRequest("device cuda ya da cpu olmali")
    out["device"] = device
    for k in ("ref", "language", "whisper_model"):
        if data.get(k) is not None and not isinstance(data[k], str):
            raise BadRequest(f"{k} metin olmali")
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):  # noqa: N802
        if not self._host_ok() or self.path != "/health":
            return self._send(404, b"{}", "application/json")
        body = {"ok": True, "device": STATE["device"], "loaded": STATE["model"] is not None}
        self._send(200, json.dumps(body).encode(), "application/json")
        return None

    def _discard_body(self, n: int) -> None:
        """Read and drop a request body we won't use before answering. Closing the socket with unread bytes makes
        the OS send a TCP reset, and on Windows the reset discards our response before the client reads it
        (WinError 10053, flaky test_tts_server_limits_body_and_accepts_good_request). A length we refuse (negative
        or over MAX_BODY) may not match what was sent, so only what has already arrived is read, briefly."""
        try:
            if 0 < n <= MAX_BODY:
                self.rfile.read(n)
                return
            self.connection.settimeout(0.2)
            got = 0
            while got < MAX_BODY:
                chunk = self.rfile.read1(65536)
                if not chunk:
                    break
                got += len(chunk)
        except OSError:  # includes the timeout: nothing more has arrived
            pass

    def do_POST(self):  # noqa: N802
        if self._host_ok() and self.path == "/release":
            with LOCK:
                release()
            return self._send(200, b'{"ok": true}', "application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not self._host_ok() or self.path != "/synth":
            self._discard_body(n)
            return self._send(404, b"{}", "application/json")
        # A negative length made read(-1) wait until the connection closed; a huge body fills memory.
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            self._discard_body(n)
            return self._send(413 if n > MAX_BODY else 400, b'{"error": "gecersiz Content-Length"}',
                              "application/json")
        try:
            data = validate(json.loads(self.rfile.read(n) or b"{}"))
        except (ValueError, UnicodeDecodeError) as e:  # broken JSON also raises ValueError
            msg = str(e) if isinstance(e, BadRequest) else "gecersiz JSON"
            return self._send(400, json.dumps({"error": msg}).encode(), "application/json")
        try:
            wav, headers = synth(data)
            self._send(200, wav, "audio/wav", headers)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--preload", action="store_true", help="modeli açılışta yükle")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    if a.preload:
        def preload():
            with LOCK:
                load(a.device)

        threading.Thread(target=preload, daemon=True).start()
    print(f"ALICE ses sunucusu 127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass  # Ctrl+C: shut down quietly
    return 0


if __name__ == "__main__":
    sys.exit(main())
