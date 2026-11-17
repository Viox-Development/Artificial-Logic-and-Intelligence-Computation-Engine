"""Clipboard history: "open the link I just copied", "translate the text I copied 10 minutes ago".

The core reads clipboard every second; keeps changed text for the last 25 entries, 12h (~/.alice/clipboard-history.json).
Items resembling passwords (no space, mixed letters+digits+symbols, 8-64 chars), API key/token patterns, and password
manager entries are not saved. The clipboard tool reads via action=history.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from alice.config import alice_home

KEEP = 25
TTL = 12 * 3600
MAX_LEN = 5000

_TOKEN = re.compile(r"^(sk-|ghp_|gho_|github_pat_|xox[bap]-|AIza|ya29\.|eyJ)[\w\-.]{10,}$")


def history_path() -> Path:
    return alice_home() / "clipboard-history.json"


def looks_secret(text: str) -> bool:
    t = text.strip()
    if _TOKEN.match(t):
        return True
    if " " in t or not (8 <= len(t) <= 64) or t.startswith(("http://", "https://", "www.")):
        return False
    classes = sum(bool(re.search(rx, t)) for rx in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    return classes >= 3


def load(path: Path | None = None, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    try:
        items = json.loads((path or history_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in items if now - float(i.get("ts") or 0) < TTL]


def add(text: str, path: Path | None = None, now: float | None = None) -> bool:
    text = (text or "").strip()
    if not text or looks_secret(text):
        return False
    now = time.time() if now is None else now
    items = [i for i in load(path, now) if i.get("text") != text[:MAX_LEN]]
    items.append({"ts": now, "text": text[:MAX_LEN]})
    p = path or history_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items[-KEEP:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True


class ClipWatch:
    def __init__(self, read: Callable[[], str] | None = None, every: float = 1.0):
        self.read = read or self._paste
        self.every = every
        self.last = None
        self._stop = threading.Event()

    @staticmethod
    def _paste() -> str:
        import pyperclip

        return pyperclip.paste() or ""

    def step(self) -> bool:
        try:
            text = self.read()
        except Exception:  # noqa: BLE001 - clipboard might be locked in another program
            return False
        if self.last is None:  # what was on clipboard at startup is not saved to history (may be old)
            self.last = text
            return False
        if text == self.last:
            return False
        self.last = text
        return add(text)

    def start(self) -> "ClipWatch":
        def loop():
            while not self._stop.wait(self.every):
                self.step()

        threading.Thread(target=loop, daemon=True, name="alice-clipboard").start()
        return self

    def stop(self) -> None:
        self._stop.set()
