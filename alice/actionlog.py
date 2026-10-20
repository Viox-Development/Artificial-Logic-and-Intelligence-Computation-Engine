"""Eylem kaydı: ALICE'in senin adına başkasına gönderdiği her şey (mesaj, DM, e-posta).

"Bugün kime ne yazdın?" sorusunun cevabı burada. Gönderilen de, reddedilen de, başarısız olan da yazılır:
güven için sadece yapılanı değil, yapılmak istenip durdurulanı da görmek gerekiyor.

Kayıt `~/.alice/actions.jsonl` (satır başına bir JSON), 1 MB'ı geçince eskisi `.1` olur. Bilgisayarından
çıkmaz.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from alice.config import alice_home

MAX_BYTES = 1_000_000
OUTCOMES = {"sent": "gönderildi", "failed": "başarısız", "rejected": "reddedildi", "blocked": "engellendi"}
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
        pass  # kayıt yazılamadı diye eylem durmasın


def recent(limit: int = 50, since: float = 0.0) -> list[dict[str, Any]]:
    """En yeniden eskiye."""
    try:
        lines = path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if float(row.get("ts") or 0) < since:
            break
        out.append(row)
        if len(out) >= limit:
            break
    return out


def format_row(row: dict[str, Any]) -> str:
    t = time.strftime("%d.%m %H:%M", time.localtime(float(row.get("ts") or 0)))
    status = OUTCOMES.get(str(row.get("outcome")), str(row.get("outcome")))
    detail = f" — {row['detail']}" if row.get("detail") and row.get("outcome") != "sent" else ""
    return f"{t}  [{status}] {row.get('summary', '')}{detail}"
