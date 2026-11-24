"""The HUD's drawing-independent state: validate incoming commands, keep items, remove expired ones.

The window (window.py) only reads this, so the protocol and lifetime rules can be tested without tkinter.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from alice.i18n import _

KINDS = ("arrow", "edge", "label", "card", "box", "progress", "cursor")
CONTROL = ("clear", "toggle", "mode", "context", "history")
# Density: auto is "game" in a game and "all" on the desktop. game: only threats, warnings and game-related;
# quiet: only critical. A user setting like in game HUDs (reduce, not remove).
MODES = ("auto", "all", "game", "quiet")
MODE_NAMES = {"auto": "automatic", "all": "all", "game": "game", "quiet": "quiet"}  # shown via _()
GAME_KEEP = ("kill", "voice", "__mode__")  # still shown in game mode even at info level (id prefixes)
HISTORY_MAX, HISTORY_SHOW = 8, 3
MAX_ITEMS = 6          # at most this many items at once: the HUD informs, it does not cover the screen
MAX_BYTES = 4096
TTL_DEFAULT, TTL_MIN, TTL_MAX = 3.0, 0.2, 60.0
SEVERITIES = ("info", "warn", "critical")


@dataclass
class Item:
    kind: str
    data: dict[str, Any]
    born: float
    until: float
    id: str = ""
    seq: int = field(default=0, compare=False)


def parse(raw: bytes) -> dict[str, Any] | None:
    """One datagram from the network -> command. Broken, oversized, or unknown kind gives None (silently ignored)."""
    if not raw or len(raw) > MAX_BYTES:
        return None
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or msg.get("kind") not in KINDS + CONTROL:
        return None
    return msg


def clock_of(value: Any) -> int | None:
    """Clock direction 1-12 (0 and 12 are the same spot). None if not understood."""
    try:
        c = round(float(value))
    except (TypeError, ValueError):
        return None
    if not 0 <= c <= 12:
        return None
    return c or 12


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _valid(msg: dict[str, Any]) -> bool:
    kind = msg["kind"]
    if kind == "edge":
        return clock_of(msg.get("clock")) is not None
    if kind == "arrow":  # unknown-direction threat (directionless lock/missile warning) shown too: arrowless, pulsing
        if msg.get("clock") in (None, ""):
            return bool(str(msg.get("text") or ""))  # directionless: at least its text must say what it is
        return clock_of(msg.get("clock")) is not None
    if kind == "cursor":
        return all(_num(msg.get(k)) is not None for k in ("x", "y"))
    if kind == "box":
        return all(_num(msg.get(k)) is not None for k in ("x", "y", "w", "h"))
    if kind == "progress":
        return _num(msg.get("value")) is not None
    return bool(str(msg.get("text") or msg.get("title") or ""))  # label, card: empty text is not drawn


class HudModel:
    def __init__(self, max_items: int = MAX_ITEMS, mode: str = "auto"):
        self.max_items = max_items
        self.items: list[Item] = []
        self.hidden = False
        self.mode = mode if mode in MODES else "auto"
        self.in_game = False  # the core reports this (flight/game mode)
        self.history: deque[tuple[str, str, str]] = deque(maxlen=HISTORY_MAX)  # (time, title, text)
        self.version = 0  # increases on every change: the window only redraws when it changes
        self._seq = 0

    def apply(self, msg: dict[str, Any], now: float) -> bool:
        """Apply the command. True if something changed."""
        kind = msg.get("kind")
        if kind == "toggle":
            self.hidden = not self.hidden
            self.version += 1
            return True
        if kind == "mode":
            value = str(msg.get("value") or "")
            self.mode = value if value in MODES else MODES[(MODES.index(self.mode) + 1) % len(MODES)]
            self.version += 1
            self._put("label", {"text": f"HUD · {_(MODE_NAMES[self.mode])}", "icon": "sparkle"}, now, 2.0, "__mode__")
            return True
        if kind == "context":
            game = bool(msg.get("game"))
            changed = game != self.in_game
            self.in_game = game
            self.version += changed
            return changed
        if kind == "history":
            return self._show_history(now)
        if kind == "clear":
            key = str(msg.get("id") or "")
            keep = [i for i in self.items if key and i.id != key]
            changed = len(keep) != len(self.items)
            self.items = keep
            self.version += changed
            return changed
        if kind not in KINDS or not _valid(msg):
            return False
        ttl = _num(msg.get("ttl"))
        ttl = TTL_DEFAULT if ttl is None else min(TTL_MAX, max(TTL_MIN, ttl))
        key = str(msg.get("id") or "")
        data = {k: v for k, v in msg.items() if k not in ("kind", "ttl", "id")}
        if data.get("severity") not in SEVERITIES:
            data["severity"] = "info"
        if kind == "card" and not key.startswith(("__", "kill")):
            self.history.append((time.strftime("%H:%M"), str(data.get("title") or "ALICE"),
                                 " ".join(str(data.get("text") or "").split())))
        self._put(kind, data, now, ttl, key)
        return True

    def _put(self, kind: str, data: dict[str, Any], now: float, ttl: float, key: str) -> None:
        data.setdefault("severity", "info")
        if key:  # the same id is replaced by the new one: a repeated threat does not pile up on screen
            self.items = [i for i in self.items if i.id != key]
        self._seq += 1
        self.items.append(Item(kind, data, now, now + ttl, key, self._seq))
        while len(self.items) > self.max_items:  # drop the oldest non-critical item first
            calm = [i for i in self.items if i.data["severity"] != "critical"]
            self.items.remove(min(calm or self.items, key=lambda i: i.seq))
        self.version += 1

    def _show_history(self, now: float) -> bool:
        """Recently missed notifications on one card (Ada is not clicked: it is opened with a shortcut)."""
        last = list(self.history)[-HISTORY_SHOW:][::-1]
        body = " · ".join(f"{t} {title}: {text[:60]}" if text else f"{t} {title}" for t, title, text in last)
        self._put("card", {"title": _("Recent notifications"), "text": body or _("No notifications yet."), "icon": "clock"},
                  now, 10.0, "__history__")
        return True

    def effective_mode(self) -> str:
        if self.mode == "auto":
            return "game" if self.in_game else "all"
        return self.mode

    def allows(self, it: Item) -> bool:
        mode = self.effective_mode()
        if mode == "all" or it.id.startswith("__mode__"):
            return True
        sev = it.data.get("severity")
        if mode == "quiet":
            return sev == "critical"
        return (it.kind in ("arrow", "edge") or sev in ("warn", "critical") or it.id.startswith(GAME_KEEP)
                or it.id == "__history__")

    def expire(self, now: float) -> bool:
        keep = [i for i in self.items if i.until > now]
        if len(keep) == len(self.items):
            return False
        self.items = keep
        self.version += 1
        return True

    def visible(self) -> list[Item]:
        return [] if self.hidden else [i for i in self.items if self.allows(i)]
