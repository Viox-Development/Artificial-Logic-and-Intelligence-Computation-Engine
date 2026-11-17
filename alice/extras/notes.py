"""Notes: "Alice, take a note…" and later "what notes did I take last week?".

~/.alice/notes.jsonl, one note per line: {"ts": epoch, "text": "..."}.
"""

from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from alice.config import alice_home


@dataclass
class Note:
    ts: float
    text: str

    def line(self) -> str:
        return f"{datetime.fromtimestamp(self.ts):%d.%m.%Y %H:%M}  {self.text}"


def path() -> Path:
    return alice_home() / "notes.jsonl"


def add(text: str, ts: float | None = None) -> Note:
    n = Note(time.time() if ts is None else ts, text.strip())
    path().parent.mkdir(parents=True, exist_ok=True)
    with open(path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": n.ts, "text": n.text}, ensure_ascii=False) + "\n")
    return n


def all_notes() -> list[Note]:
    if not path().exists():
        return []
    out = []
    for line in path().read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            out.append(Note(float(d["ts"]), str(d["text"])))
        except (ValueError, KeyError):
            continue
    return out


def _fold(s: str) -> str:
    s = s.lower().replace("ı", "i")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def search(query: str = "", since: float | None = None, until: float | None = None) -> list[Note]:
    words = _fold(query).split()
    return [n for n in all_notes()
            if (since is None or n.ts >= since) and (until is None or n.ts <= until)
            and all(w in _fold(n.text) for w in words)]


def remove_last() -> Note | None:
    notes = all_notes()
    if not notes:
        return None
    last = notes.pop()
    path().write_text("".join(json.dumps({"ts": n.ts, "text": n.text}, ensure_ascii=False) + "\n" for n in notes),
                      encoding="utf-8")
    return last


def remove(ts: float) -> Note | None:
    """Delete a single note by timestamp (Delete in the app)."""
    notes = all_notes()
    hit = next((n for n in notes if abs(n.ts - ts) < 1e-6), None)
    if hit is None:
        return None
    notes.remove(hit)
    path().write_text("".join(json.dumps({"ts": n.ts, "text": n.text}, ensure_ascii=False) + "\n" for n in notes),
                      encoding="utf-8")
    return hit
