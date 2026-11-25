"""Wingman coaching: coaching, not announcements. Death patterns are extracted from flight logs.

For each flight that ends without a landing, the 15 seconds up to its last live moment (the last sample before speed
drops) are checked: were you low, were you turning hard, was "Arkanda düşman" (enemy behind you) said before, were you
in SAM range, who shot you. If a pattern is frequent enough in recent deaths, it is said: "Son 10 ölümünün 7'si
alçakta dönerken arkadan geldi."

During a match it is also reminded once: a short tip matching the pattern is added to the SAM range announcement or the
enemy-behind announcement. SimHub calculates the coach flags at the start of the match; the flight code only reads them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from alice.i18n import speech_language, spoken

LOW_FT = 2000.0
TURN_G = 4.0
WINDOW = 15.0          # this many seconds before the last live moment
SIX_BEFORE = 45.0
SAM_BEFORE = 90.0
HIT_BEFORE = 30.0
HIT_AFTER = 30.0       # War Thunder's "seni X vurdu" (X hit you) can arrive after the speed has dropped
ALIVE_KT = 50.0
MIN_SHARE, MIN_COUNT = 0.4, 3   # pattern: at least 40% of the recent deaths, and at least 3 of them

_LAST = {1: "i", 2: "si", 3: "ü", 4: "ü", 5: "i", 6: "sı", 7: "si", 8: "i", 9: "u"}
_TENS = {10: "u", 20: "si", 30: "u", 40: "ı", 50: "si", 60: "ı", 70: "i", 80: "i", 90: "ı"}


def ek(n: int) -> str:
    """Possessive suffix of a number: 7 -> "7'si", 3 -> "3'ü", 10 -> "10'u". For the locative add "nde": "7'sinde".
    Only in Turkish speech; other languages get the number as is."""
    if speech_language() != "tr":
        return str(n)
    tail = "ı" if n == 0 else _LAST.get(n % 10) or _TENS.get(n % 100) or "ü"
    return f"{n}'{tail}"


def load_flights(folder: Path | None = None, limit: int = 40) -> list[dict[str, Any]]:
    """Recent flights with their series data (oldest to newest)."""
    from alice.sims.flight import flights_dir

    out = []
    for f in sorted((folder or flights_dir()).glob("2*.json"))[-limit:]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


# Air defense vehicles in War Thunder (SPAA/SAM). If the killer is not one of these, the death isn't logged as SAM.
AIR_DEFENSE_RX = re.compile(
    r"\b(?:SAM|SAMP/T|TEL|SPAA|pantsir|tor|osa|strela|tunguska|zsu|shilka|buk|kub|2S6|gepard|ozelot|flarakpz|roland|adats|"
    r"chaparral|avenger|vulcan|m163|m247|sergeant york|crotale|rapier|stormer|lvkv|ito|veak|sidam|otomatic|"
    r"skyguard|tan-sam|type 81|type 87|type 93|pgz|hq-?\d+|fk-?\d+|lav-ad|wiesel 2|iris-t|machbet|spyder|"
    r"ystervark|bosvark|marksman)\b", re.I)


def is_air_defense(name: str) -> bool:
    return bool(AIR_DEFENSE_RX.search(name or ""))


def death_context(f: dict[str, Any], events: list[Any]) -> dict[str, Any] | None:
    """The last moment of a flight that ended without landing. None if it landed or there is no series data."""
    if f.get("touchdown_vv_ms") is not None:
        return None
    s = f.get("series") or {}
    wall, alt, g, ias = s.get("wall") or [], s.get("alt_ft") or [], s.get("g") or [], s.get("ias_kt") or []
    n = min(len(wall), len(alt), len(g), len(ias))
    alive = [i for i in range(n) if (ias[i] or 0) > ALIVE_KT]
    if not alive:
        return None
    end_i = alive[-1]
    end = float(wall[end_i])
    win = [i for i in range(n) if end - WINDOW <= float(wall[i]) <= end]
    ctx = {"end": end, "aircraft": f.get("aircraft") or "",
           "low": min(float(alt[i] or 0) for i in win) < LOW_FT,
           "turning": max(float(g[i] or 0) for i in win) >= TURN_G,
           "six": False, "sam": False, "killer": ""}
    downed = ""
    for e in events:
        dt = end - e.ts
        body = e.body or ""
        if 0 <= dt <= SIX_BEFORE and body.startswith(("Arkanda düşman", "Enemy behind you")):
            ctx["six"] = True
        if 0 <= dt <= SAM_BEFORE and ("menzilindesin" in body or (body.startswith("You're in ") and " range:" in body)):
            ctx["sam"] = True
        # The callouts in either speech language: who shot you down / set you on fire / damaged you.
        m = re.match(r"^(?:Seni (.+) düşürdü|Yanıyorsun! (.+) vurdu|Ağır hasar aldın, (.+) vurdu|"
                     r"(.+) shot you down|You're on fire! (.+) hit you|Heavy damage, (.+) hit you)", body)
        if m and -HIT_AFTER <= dt <= HIT_BEFORE:
            who = next(g for g in m.groups() if g).strip(" .!")
            if m.group(1) or m.group(4):
                downed = who  # the one who downed you is more certain than the one who damaged you
            ctx["killer"] = who
    if downed:
        ctx["killer"] = downed
    # Being in range isn't the cause: an aircraft kill isn't SAM, an air defense kill is even if the range was unknown.
    if ctx["killer"]:
        ctx["sam"] = is_air_defense(ctx["killer"])
    return ctx


def contexts(flights: list[dict[str, Any]], events: list[Any], last: int = 10) -> list[dict[str, Any]]:
    out = [c for f in flights if (c := death_context(f, events)) is not None]
    return out[-last:]


def patterns(ctxs: list[dict[str, Any]]) -> dict[str, str]:
    """Pattern key -> sentence to say (only the frequent ones)."""
    n = len(ctxs)
    if n < MIN_COUNT:
        return {}

    def often(k: int) -> bool:
        return k >= MIN_COUNT and k / n >= MIN_SHARE

    out: dict[str, str] = {}
    low_turn_six = sum(1 for c in ctxs if c["low"] and c["turning"] and c["six"])
    six = sum(1 for c in ctxs if c["six"])
    if often(low_turn_six):
        out["low_turn_six"] = spoken("{k} of your last {n} deaths came from behind while you turned low. Don't make long "
                                     "turns down low; keep your energy, gain altitude, then turn.", n=n, k=ek(low_turn_six))
    elif often(six):
        out["six"] = spoken("In {k} of your last {n} deaths an enemy was behind you before you went down. Check your six "
                            "more often.", n=n, k=ek(six))
    sam = sum(1 for c in ctxs if c["sam"])
    if often(sam):
        # If the killer wasn't seen, only "menzildeydin" (you were in range) is known: don't speak with certainty.
        sure = sum(1 for c in ctxs if c["sam"] and c["killer"])
        where = spoken("were to air defences") if sure == sam else spoken("happened inside air defence range")
        out["sam"] = spoken("{k} of your last {n} deaths {where}. When you enter the range, go low or get out at once.",
                            n=n, k=ek(sam), where=where)
    killers = Counter(c["killer"] for c in ctxs if c["killer"])
    if killers:
        who, k = killers.most_common(1)[0]
        if often(k):
            out["killer"] = spoken("In {k} of your last {n} deaths {who} shot you.", n=n, k=ek(k), who=who)
    return out


# Short tip added to an announcement during a match (pattern -> addition). The flight code uses it once.
HINTS = {"sam": "Most of your recent deaths were to air defences, go low or climb.",
         "low_turn_six": "Don't make long turns down low, gain altitude.",
         "six": "Check your six."}  # spoken in the speech language (spoken())
