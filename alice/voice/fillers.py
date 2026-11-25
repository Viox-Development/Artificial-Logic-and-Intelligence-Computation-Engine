"""Short canned sentences spoken by Live while waiting ("One second.", "Let me think.").

When the model calls a tool, it's requested to say something first, but most of the time it doesn't:
there used to be 8-16 seconds of silence until the tool finished (voice.log Oct 28: weather 10s,
Spotify 16s). If the model didn't produce any sound in that turn, one of these phrases is played
instantly. The audio is synthesized once using Live's own voice (Korean...) via Gemini TTS and
stored on disk; even if the voice changes, ALICE's voice remains the same, and playing doesn't wait
for the network.

The phrase matches the user's tone: a calm "Let me think." felt awkward for someone in a rush.
Urgent and happy phrases are generated when first requested (to avoid wasting quota); until then,
the calm one is played.
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
from pathlib import Path
from typing import Callable

from alice.i18n import spoken
from alice.voice.audio import Audio

# English keys, spoken in the speech language (spoken()).
TOOL = ("One second.", "Checking now.", "Okay, on it.")
THINK = ("Let me think.", "Hmm, let me see.")
PHRASES = {
    "calm": {"tool": TOOL, "think": THINK},
    "urgent": {"tool": ("Right away.", "Okay, checking right away."), "think": ("Thinking right away.",)},
    "happy": {"tool": ("Sure, let's look!", "Great, one second!"), "think": ("Good question, let me think!",)},
}
RATE = 24000  # Live's output sample rate (AudioOut)

_URGENT = re.compile(r"\b(acil|hemen|çabuk|cabuk|acele|hızlı|hizli|koş|kos|yetiş|yetis|imdat|tehlike)\w*", re.I)
_HAPPY = re.compile(r"\b(harika|süper|super|mükemmel|mukemmel|yaşasın|yasasin|oley|helal|kazandı\w*|"
                    r"ha(ha)+)\b|!{2,}|[😄😂🎉]", re.I)


def user_tone(text: str) -> str:
    """Tone in the user's utterance: "urgent", "happy", or "calm"."""
    if _URGENT.search(text or ""):
        return "urgent"
    if _HAPPY.search(text or ""):
        return "happy"
    return "calm"


def filler_dir(voice: str) -> Path:
    from alice.config import alice_home

    return alice_home() / "voice" / "fillers" / voice


class Fillers:
    """Audio of canned phrases. get() does not wait for the network: if audio is missing, returns None and starts background generation."""

    def __init__(self, voice: str, synth: Callable[[str], Audio] | None, path: Path | None = None):
        self.voice = voice
        self.synth = synth  # (text, tone) -> Audio (in Live's voice); None = do not generate, only play what's on disk
        self.path = path or filler_dir(voice)
        self._mem: dict[str, bytes] = {}
        self._last = ""
        self._lock = threading.Lock()
        self._warming = False

    def _file(self, text: str, tone: str = "calm") -> Path:
        key = text if tone == "calm" else f"{tone}|{text}"  # keep calm voices with their old names
        return self.path / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".wav")

    def _load(self, text: str, tone: str = "calm") -> bytes | None:
        if (tone, text) in self._mem:
            return self._mem[(tone, text)]
        f = self._file(text, tone)
        try:
            pcm = Audio.from_wav(f.read_bytes()).resample(RATE).pcm if f.exists() else None
        except Exception:  # noqa: BLE001 - corrupted file: delete, regenerate
            f.unlink(missing_ok=True)
            pcm = None
        if pcm:
            self._mem[(tone, text)] = pcm
        return pcm

    def get(self, kind: str = "tool", tone: str = "calm") -> bytes | None:
        """PCM to be played (24 kHz). The same sentence does not repeat consecutively. If the voice for that tone does not exist yet, plays the calm one."""
        kind = "think" if kind == "think" else "tool"
        for t in dict.fromkeys((tone if tone in PHRASES else "calm", "calm")):
            texts = [spoken(x) for x in PHRASES[t][kind] if spoken(x) != self._last]
            random.shuffle(texts)
            for text in texts:
                pcm = self._load(text, t)
                if pcm:
                    self._last = text
                    if t != tone:
                        self.warm(tone)
                    return pcm
        self.warm(tone)
        return None

    def warm(self, tone: str = "calm") -> None:
        """Generate missing audio for a tone in the background (a few seconds, once)."""
        tone = tone if tone in PHRASES else "calm"
        with self._lock:
            if self._warming or self.synth is None:
                return
            missing = [spoken(t) for t in PHRASES[tone]["tool"] + PHRASES[tone]["think"]
                       if not self._file(spoken(t), tone).exists()]
            if not missing:
                return
            self._warming = True

        def run() -> None:
            try:
                self.path.mkdir(parents=True, exist_ok=True)
                for text in missing:
                    try:
                        audio = self.synth(text, tone)
                    except Exception:  # noqa: BLE001 - quota/network: retry in the next session
                        return
                    tmp = self._file(text, tone).with_suffix(".tmp")
                    audio.save(tmp)
                    tmp.replace(self._file(text, tone))
            finally:
                self._warming = False

        threading.Thread(target=run, daemon=True, name="alice-live-fillers").start()


def gemini_fillers(cfg, voice: str, api_key: str) -> Fillers:
    """Filler audio generated from Gemini TTS in Live's voice."""
    from alice.voice.tts import GeminiTTS

    models = cfg.data.get("voice", {}).get("gemini_tts_models") or None
    tts = GeminiTTS(api_key, voice=voice, models=models)
    return Fillers(voice, lambda text, tone="calm": tts.synth(text, tone))
