"""What and how things appear in the HUD: notification cards, written summaries of conversation, target frames before clicking,
frames of items worked on in the browser or shown to the user.

Callers (notifications, voice, computer use, browser) only call these. If the HUD is not active, none of them do
anything or wait.
"""

from __future__ import annotations

import re
import time

from alice.hud import client
from alice.i18n import _

CARD_TTL = {"critical": 12.0, "warn": 9.0, "info": 7.0}
CLICK_PREVIEW = 0.4  # sec: ALICE's cursor slides and settles on the target, then actual click (preview action)
_LIST = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$")


def notification(n) -> bool:
    """ALICE's card instead of Windows balloon. Flight announcements already come via voice and arrow: no card opened."""
    if not client.active or n.kind == "flight" or n.topic == "flight":
        return False
    sev = n.severity if n.severity in CARD_TTL else "info"
    body = " ".join((n.body or "").split())
    return client.show("card", title=n.title[:60], text=body[:240], severity=sev, ttl=CARD_TTL[sev],
                       id=f"note:{n.title[:40]}")


def summarize(text: str, limit: int = 260) -> str | None:
    """Short written version of the voice response shown in the corner; short one-sentence answers don't need a card (None).
    List items and numbers are preserved: voice response does not read long lists, it shows them."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    items = [m.group(1) for ln in lines if (m := _LIST.match(ln))]
    if len(items) >= 2:
        out = " · ".join(items)
    else:
        flat = " ".join(lines)
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", flat) if x]
        if len(flat) < 120 and len(sentences) <= 1 and not re.search(r"\d", flat):
            return None
        out = flat
    out = re.sub(r"[*_`#]+", "", out)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def speech(text: str) -> bool:
    if not client.active:
        return False
    short = summarize(text)
    if short is None:
        return False
    return client.show("card", title="ALICE", text=short, severity="info", ttl=min(15.0, max(5.0, len(short) / 18)),
                       id="speech")


def click_target(x: float, y: float, action: str = "click", wait: bool = True) -> bool:
    """During computer use, ALICE's cursor slides to the click target, ring spreads on click (physical pixel)."""
    if not client.active:
        return False
    label = {"double_click": _("double-clicking"), "right_click": _("right-clicking"), "drag": _("dragging"),
             "move": _("moving here"), "scroll": _("scrolling"), "type": _("typing")}.get(action) or _("clicking")
    ok = client.show("cursor", x=round(x), y=round(y), action=action, text=label, severity="info", ttl=1.5,
                     id="computer")
    if ok and wait:
        time.sleep(CLICK_PREVIEW)
    return ok


TASK_TTL = 45.0  # sec: screen task refreshes on each step; if task hangs and dies, status clears automatically


def task_step(step: int) -> bool:
    """Status in Island while screen task is running: model thinks 5-15s between steps, don't leave HUD empty."""
    if not client.active:
        return False
    text = _("Screen task · step {n}", n=step) if step else _("Screen task · starting")
    return client.show("label", text=text, icon="cursor", severity="info", ttl=TASK_TTL, id="task")


def task_end() -> bool:
    return client.clear("task") if client.active else False


# Labels are English keys, shown in the interface language.
VOICE = {"listen": ("I'm listening", "wave"), "think": ("I'm thinking", "sparkle"), "speak": ("I'm speaking", "wave"),
         "work": ("I'm working", "sparkle")}
VOICE_TTL, VOICE_REFRESH = 30.0, 20.0  # fades out automatically if stuck; refreshed on long listening
_voice: dict = {"state": None, "detail": "", "at": 0.0}


def voice_state(state: str | None, detail: str = "") -> bool:
    """State of ALICE's voice session in Island: listening (wave + blue glow), thinking, speaking, doing work.
    Does not resend if the same state comes again (can be called every loop)."""
    if not client.active:
        return False
    now = time.monotonic()
    if state == _voice["state"] and detail == _voice["detail"] and (state is None or now - _voice["at"] < VOICE_REFRESH):
        return False
    _voice.update(state=state, detail=detail, at=now)
    if state not in VOICE:
        return client.clear("voice")
    text, icon = VOICE[state]
    text = _(text)
    return client.show("label", text=f"{text} · {detail}" if detail else text, icon=icon, severity="info",
                       ttl=VOICE_TTL, id="voice")


MUSIC_TTL = 5.0


def now_playing(n: dict) -> bool:
    """When track changes, short song · artist in Island for a short time (invisible when in-game due to game info)."""
    if not client.active or not n.get("title"):
        return False
    text = f"{n['title'][:40]} · {n['artist'][:30]}" if n.get("artist") else n["title"][:60]
    return client.show("label", text=text, icon="music", severity="info", ttl=MUSIC_TTL, id="music")


def game_context(in_game: bool) -> bool:
    """When in-game, HUD shows only threats and warnings (setting: [hud] mode)."""
    return client.send({"kind": "context", "game": bool(in_game)}) if client.active else False


def system_gauge(name: str, value: float, limit: float, unit: str, warn: float | None = None,
                 ttl: float = 25.0) -> bool:
    """Computer value approaching critical limit shown as small corner bar: "GPU 89°C". Sends nothing below limit (default
    90%); stops sending when value normalizes and bar disappears automatically with short ttl.
    Same name always goes with same ID: HUD doesn't open new bar every turn, updates existing one."""
    if not client.active or limit <= 0:
        return False
    warn = 0.9 * limit if warn is None else warn
    if value < warn:
        return False
    sev = "critical" if value >= limit else "warn"
    text = f"{name} %{value:.0f}" if unit == "%" else f"{name} {value:.0f}{unit}"
    return client.show("progress", value=round(min(1.0, value / limit), 3), text=text, severity=sev, ttl=ttl,
                       id=f"sys:{name}")


PAGE_TTL = 1.5  # sec: frame of clicked/typed element in browser
SHOW_TTL = 6.0  # sec: element shown to user (browser action=highlight) stays longer


def page_target(x: float, y: float, w: float, h: float, label: str = "", ttl: float = PAGE_TTL,
                wait: bool = False) -> bool:
    """Frame an element on the page on screen (physical screen pixel, x/y top-left corner)."""
    if not client.active or w < 2 or h < 2:
        return False
    ok = client.show("box", x=x, y=y, w=w, h=h, text=label[:40], severity="info", ttl=ttl, id="browser")
    if ok and wait:
        time.sleep(CLICK_PREVIEW)
    return ok
