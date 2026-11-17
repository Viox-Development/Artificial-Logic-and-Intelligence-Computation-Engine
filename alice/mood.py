"""Emotional intelligence: reads pulse, conversation and game events together; notices when you are angry (tilt).

Signs (last 15 minutes): flights that end quickly one after another (drops), swearing or angry words in voice
speech (voice.log, only lines recognized as your voice), high pulse (if a watch is connected).
Level: sakin (calm) | gergin (tense) | tilt.

At tilt ALICE talks less (the gap between threat sounds is doubled, unimportant announcements are silenced) and
once between matches says "üst üste düştün, bir maç ara ver" ("you keep crashing, take a match break"). No advice,
no repeats.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

WINDOW = 15 * 60.0
QUICK_DEATH = 120.0      # a flight ending sooner than this is "çabuk düştü" (quick drop)
HR_HIGH = 120.0
# "lan", "saçma", "ne biçim" (dude, nonsense, what kind of) also come up in casual chat: alone they
# don't count as anger.
SWEAR = re.compile(r"\b(amk|aq|siktir\w*|sik\w*|of+ be|kahretsin|lanet|yeter ya|bıktım|salak|aptal oyun|"
                   r"hile|hileci)\b", re.I)


@dataclass
class Mood:
    level: str = "sakin"            # sakin (calm) | gergin (tense) | tilt
    quick_deaths: int = 0
    swears: int = 0
    hr: float | None = None
    reasons: list[str] = field(default_factory=list)


def heard_lines(path: Path, since: float) -> list[str]:
    """Sentences heard within the last WINDOW from voice.log (recognized as your voice)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            ts = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if ts < since:
            continue
        if "duyulan:" in ln:
            out.append(ln.split("duyulan:", 1)[1].split("|", 1)[0])
        elif "uyandırma yok" in ln or " uyandı " in ln:
            out.append(ln.split("]:", 1)[-1])
    return out


def read(flights: list[dict[str, Any]], heard: list[str], hr: float | None, now: float) -> Mood:
    m = Mood(hr=hr)
    recent = [f for f in flights if float(f.get("end_wall") or 0) >= now - WINDOW]
    m.quick_deaths = sum(1 for f in recent if f.get("touchdown_vv_ms") is None
                         and float(f.get("duration_s") or 1e9) < QUICK_DEATH)
    m.swears = sum(len(SWEAR.findall(t)) for t in heard)
    score = 0
    if m.quick_deaths >= 3:
        score += 2
        m.reasons.append(f"{m.quick_deaths} uçuş üst üste çabuk bitti")
    elif m.quick_deaths == 2:
        score += 1
    if m.swears >= 3:
        score += 2
        m.reasons.append("sesin gergin")
    elif m.swears >= 1:
        score += 1
    if hr is not None and hr >= HR_HIGH:
        score += 1
        m.reasons.append(f"nabız {hr:.0f}")
    m.level = "tilt" if score >= 3 else "gergin" if score >= 2 else "sakin"
    return m


def current(store: Any, now: float | None = None, flights: list[dict[str, Any]] | None = None,
            voice_log: Path | None = None) -> Mood:
    from alice.config import alice_home

    now = time.time() if now is None else now
    if flights is None:
        from alice.sims.flight import load_history

        flights = load_history(limit=30)
    hr = None
    last = store.last("watch.hr") if store is not None else None
    if last and now - last[0] < 120 and last[1]:
        hr = float(last[1])
    return read(flights, heard_lines(voice_log or alice_home() / "voice.log", now - WINDOW), hr, now)
