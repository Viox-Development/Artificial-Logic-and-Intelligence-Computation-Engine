"""Action log: everything ALICE sends to someone else on your behalf (message, DM, e-mail).

The answer to "Bugün kime ne yazdın?" (who did you write to today, and what?) lives here. Sent, rejected and
failed actions are all recorded: for trust you need to see not only what was done, but also what was attempted
and stopped.

The log is `~/.alice/actions.jsonl` (one JSON per line); once it exceeds 1 MB the old file becomes `.1`. It never
leaves your computer.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from alice.config import alice_home

MAX_BYTES = 1_000_000
OUTCOMES = {"sent": "gönderildi", "failed": "başarısız", "rejected": "reddedildi", "blocked": "engellendi"}  # (sent, failed, rejected, blocked)
_lock = threading.Lock()


def path() -> Path:
    return alice_home() / "actions.jsonl"


def _short(v: Any, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def log(tool: str, summary: str, outcome: str, args: dict[str, Any] | None = None, detail: str = "") -> None:
    row = {"ts": time.time(), "tool": tool, "summary": _short(summary, 200), "outcome": outcome,
           "args": _short(args or {}, 600), "detail": _short(detail, 300)}
    p = path()
    try:
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > MAX_BYTES:
                p.replace(p.with_suffix(".jsonl.1"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the action must not stop just because the log could not be written


def _read_lines(p: Path) -> list[str]:
    try:
        # A corrupt byte in a half-written line (crash/power cut) used to make the whole log unreadable with
        # UnicodeDecodeError (the "bugün kime ne yazdın" question returned an error).
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def recent(limit: int = 50, since: float = 0.0) -> list[dict[str, Any]]:
    """Newest to oldest. If the file has rotated, the requested window is completed from `.1`: right after a
    rotation, earlier entries from today looked like they had vanished. Both files are read under the lock so
    that no rotation happens in between."""
    p = path()
    out: list[dict[str, Any]] = []
    with _lock:
        for f in (p, p.with_suffix(".jsonl.1")):
            for line in reversed(_read_lines(f)):
                try:
                    row = json.loads(line)
                    ts = float(row.get("ts") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if ts < since or len(out) >= limit:
                    return out
                out.append(row)
    return out


def format_row(row: dict[str, Any]) -> str:
    t = time.strftime("%d.%m %H:%M", time.localtime(float(row.get("ts") or 0)))
    status = OUTCOMES.get(str(row.get("outcome")), str(row.get("outcome")))
    detail = f" — {row['detail']}" if row.get("detail") and row.get("outcome") != "sent" else ""
    return f"{t}  [{status}] {row.get('summary', '')}{detail}"
