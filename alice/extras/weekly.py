"""Weekly "ne yaptın" (what did you do) summary: once on Sunday evening, short and spoken.

Data: the week's flights (~/.alice/flights), the programs most used on the computer (pc.foreground), time spent in
flight and development mode, conversation summaries and completed reminders. The fast model writes the text in the
personality; without a model, template sentences. Which week was already announced is kept in the file, so it is not
repeated even if ALICE restarts.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home
from alice.i18n import _

WEEKDAY = 6        # Sunday
HOUR = 19          # from this hour on
GAP_CAP = 120.0    # a gap between two samples longer than this means the computer was unattended

# On screen but not counted as "ne yaptın" (what you did)
IGNORED_APPS = {"explorer.exe", "searchhost.exe", "lockapp.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
                "applicationframehost.exe", "textinputhost.exe", "pythonw.exe", "python.exe", ""}  # ALICE itself
APP_NAMES = {"aces.exe": "War Thunder", "dcs.exe": "DCS", "opera.exe": "Opera", "chrome.exe": "Chrome",
             "msedge.exe": "Edge", "discord.exe": "Discord", "code.exe": "VS Code", "claude.exe": "Claude",
             "spotify.exe": "Spotify", "steam.exe": "Steam", "blender.exe": "Blender", "windowsterminal.exe": "Terminal",
             "robloxplayerbeta.exe": "Roblox"}


def state_path() -> Path:
    return alice_home() / "weekly-state.json"


def app_minutes(samples: list[tuple[float, str]]) -> dict[str, float]:
    """Minutes per program from the foreground-program samples (gaps between samples are capped at GAP_CAP)."""
    out: dict[str, float] = {}
    for (t0, app), (t1, _app) in zip(samples, samples[1:]):
        name = (app or "").lower()
        if name in IGNORED_APPS:
            continue
        out[name] = out.get(name, 0.0) + min(t1 - t0, GAP_CAP) / 60
    return out


def gather(store, mem=None, now: float | None = None, flights: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    since = now - 7 * 86400
    data: dict[str, Any] = {}
    if flights is None:
        from alice.sims.flight import load_history

        flights = load_history(limit=200)
    week = [f for f in flights if float(f.get("start_wall") or 0) >= since]
    if week:
        by_ac: dict[str, int] = {}
        for f in week:
            by_ac[f.get("aircraft") or "?"] = by_ac.get(f.get("aircraft") or "?", 0) + 1
        lands = [abs(f["touchdown_vv_ms"]) for f in week if f.get("touchdown_vv_ms") is not None]
        data["flights"] = (f"{len(week)} flights, {sum(f.get('duration_s') or 0 for f in week) / 60:.0f} min in total; "
                           + ", ".join(f"{a} {n}" for a, n in sorted(by_ac.items(), key=lambda x: -x[1])[:4])
                           + (f"; softest landing {min(lands):.1f} m/s" if lands else ""))
    if store is not None:
        apps = app_minutes(store.texts("pc.foreground", since, now))
        top = sorted(apps.items(), key=lambda x: -x[1])[:5]
        if top:
            data["most used programs"] = ", ".join(
                f"{APP_NAMES.get(a, a.removesuffix('.exe'))} {m / 60:.1f} h" for a, m in top if m >= 20)
        from alice.core.watchdogs import outages

        n, mins = outages(store, since)
        if n:
            data["internet outages"] = f"{n} times, {mins:.0f} min in total"
        for mode, label in (("flight", "game/flight mode"), ("dev", "development mode")):
            mins = sum(b - a for a, b in store.mode_spans(mode, since, now)) / 60
            if mins >= 30:
                data[label] = f"{mins / 60:.1f} h"
    if mem is not None:
        try:
            sums = [s.text for s in mem.summaries(30, within_days=7)]
            if sums:
                data["conversations"] = " | ".join(x[:160] for x in sums[:12])
            done = [f.text for f in mem.followups(include_done=True)
                    if f.done and f.kind == "remind" and float(f.due or 0) >= since]
            if done:
                data["completed reminders"] = "; ".join(done[:6])
        except Exception:  # noqa: BLE001 - produce a summary even without memory
            pass
    return data


PROMPT = """Give the user a short spoken summary of their week. Your personality is below; speak as that \
character. Rules: in the user's language, at most 5 short sentences, no lists/markdown/emoji, round the numbers
("about 6 hours"). \
First the overall mood of the week, then the highlights (flights, what took the most time, one or two themes from what \
you talked about), last a single light suggestion for next week. Don't judge, just point things out. Don't make \
anything up: use only what is below.

Personality (summary):
{persona}

This week:
{data}"""


def compose(data: dict[str, Any], router=None) -> str:
    lines = "\n".join(f"- {k}: {v}" for k, v in data.items() if v)
    if router is not None:
        try:
            from alice.config import personality_text

            res = router.chat("fast", [{"role": "user", "content": PROMPT.format(
                persona=personality_text()[:1500] or "(calm, brief, warm)", data=lines)}], temperature=0.7,
                max_tokens=400)
            text = (res.message.get("content") or "").strip()
            if text:
                return text
        except Exception:  # noqa: BLE001 - no model: template text
            pass
    parts = [_("Your week in summary.")]
    for k, v in data.items():
        if k != "conversations" and v:
            label = _(k)
            parts.append(f"{label[0].upper() + label[1:]}: {v}.")
    return " ".join(parts)


def maybe_send(store, mem, router, send: Callable[[str, str], bool], now: float | None = None,
               path: Path | None = None, **kw) -> str | None:
    now = time.time() if now is None else now
    path = path or state_path()
    dt = datetime.fromtimestamp(now)
    if dt.weekday() != WEEKDAY or dt.hour < HOUR:
        return None
    week = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if st.get("week") == week:
        return None
    data = gather(store, mem, now, **kw)
    text = compose(data, router) if data else None
    if text and not send(_("Your week in summary"), text):
        return None  # the mode muted it (you are flying): retry in the next round
    st["week"] = week
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass  # unsaved state only means the summary may be offered again
    return text
