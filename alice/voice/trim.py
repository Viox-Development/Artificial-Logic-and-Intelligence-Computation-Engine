"""Cutting overflows at the end of sentences from the local voice engine (Chatterbox).

Chatterbox sometimes continues speaking unwritten things after the sentence ends or adds static at the end.
Two ways:
  - by_words: with Whisper's word timestamps, cut from where the last word of the sentence ends (most accurate)
  - by_length: if Whisper is not available, drop audio segments starting with silence after the expected time from text length
"""

from __future__ import annotations

import array
import math
import re
import unicodedata
from typing import Callable

from alice.voice.audio import Audio


def fold(s: str) -> list[str]:
    s = s.lower().replace("ı", "i").replace("İ", "i")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", s)


def _same(a: str, b: str) -> bool:
    return a == b or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4])


def cut(audio: Audio, seconds: float) -> Audio:
    n = max(0, int(seconds * audio.rate)) * 2
    return Audio(audio.pcm[:n], audio.rate)


def by_words(audio: Audio, words: list[tuple[str, float, float]], text: str, pad: float = 0.15) -> Audio | None:
    """Cut after the last word of the sentence. None if no match found."""
    want = fold(text)
    if not want or not words:
        return None
    last = want[-1]
    seen: set[str] = set()
    for w, _start, end in words:
        f = fold(w)
        if not f:
            continue
        for x in f:
            if any(_same(x, y) for y in want):
                seen.add(x)
        # If the last word comes after most of the sentence has been spoken, that is the real end
        # (subsequent repeated last words might be part of the fabricated tail).
        if _same(f[-1], last) and len(seen) >= 0.6 * len(set(want)):
            if end + pad < audio.seconds:
                return cut(audio, end + pad)
            return audio
    return None


def _segments(audio: Audio, win: float = 0.02, rel: float = 0.06, gap: float = 0.3) -> list[tuple[float, float]]:
    a = array.array("h", audio.pcm[: len(audio.pcm) // 2 * 2])
    n = max(1, int(audio.rate * win))
    levels = [math.sqrt(sum(x * x for x in a[i:i + n]) / max(1, len(a[i:i + n]))) for i in range(0, len(a), n)]
    if not levels:
        return []
    thr = max(0.004 * 32768, max(levels) * rel)
    segs: list[list[float]] = []
    quiet = 0
    for i, lv in enumerate(levels):
        t = i * win
        if lv >= thr:
            if segs and quiet * win < gap:
                segs[-1][1] = t + win
            else:
                segs.append([t, t + win])
            quiet = 0
        else:
            quiet += 1
    return [(s, e) for s, e in segs]


def by_length(audio: Audio, text: str, chars_per_sec: float = 11.0, slack: float = 0.8) -> Audio:
    """Drop segments starting with silence after the expected duration."""
    expected = len(text.strip()) / chars_per_sec + slack
    segs = _segments(audio)
    if len(segs) < 2:
        return audio
    keep = [s for s in segs if s[0] <= expected] or segs[:1]
    end = keep[-1][1] + 0.08
    return cut(audio, end) if end < audio.seconds else audio


def make_trimmer(stt=None) -> Callable[[Audio, str], Audio]:
    """Cut with Whisper (Groq, word-timed) if available, otherwise based on length."""
    def trim(audio: Audio, text: str) -> Audio:
        engine = next((e for e in getattr(stt, "engines", []) if hasattr(e, "words")), None) if stt else None
        if engine is not None:
            try:
                out = by_words(audio, engine.words(audio), text)
                if out is not None:
                    return out
            except Exception:
                pass
        return by_length(audio, text)
    return trim
