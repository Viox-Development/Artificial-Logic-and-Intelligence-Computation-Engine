"""Voice mode latency: the time elapsed from when the user stops speaking until ALICE's first audio, for each turn.

There used to be "live response latency" lines in voice.log, but they were scattered; where the slowness came from
(tool, model, or which path) was found by guesswork. Each turn writes a row to ~/.alice/voice/latency.jsonl;
`alice voice latency` and the core's weekly notification summarize from here.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Any

KEEP_DAYS = 30
_lock = threading.Lock()


def log_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "voice" / "latency.jsonl"


def record(kind: str, seconds: float, **extra: Any) -> None:
    """Latency of a turn. kind: "live" (Gemini Live) or "classic" (transcribe + model + audio)."""
    row = {"ts": round(time.time(), 1), "kind": kind, "s": round(float(seconds), 2)}
    row.update({k: v for k, v in extra.items() if v not in (None, "", False)})
    path = log_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if path.stat().st_size > 512_000:  # ~5000 turns: drop older ones
                rows = load(KEEP_DAYS)
                path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    except OSError:
        pass  # latency stats are optional; never break a turn


def load(days: float = 7) -> list[dict[str, Any]]:
    cutoff = time.time() - days * 86400
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("ts", 0) >= cutoff and isinstance(r.get("s"), (int, float)):
            rows.append(r)
    return rows


def _p(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]


def summary(days: float = 7) -> str:
    """Turn count per path, median, worst 10%, and tool/no-tool difference. Returns "" if no records."""
    rows = load(days)
    if not rows:
        return ""
    lines = []
    for kind in dict.fromkeys(r["kind"] for r in rows):
        vals = sorted(r["s"] for r in rows if r["kind"] == kind)
        line = (f"{kind}: {len(vals)} turns, median {statistics.median(vals):.1f} s, slowest 10% "
                f"above {_p(vals, 90):.1f} s, longest {vals[-1]:.1f} s")
        tool = [r["s"] for r in rows if r["kind"] == kind and r.get("tools")]
        plain = [r["s"] for r in rows if r["kind"] == kind and not r.get("tools")]
        if tool and plain:
            line += f" (turns with tools median {statistics.median(tool):.1f}, without {statistics.median(plain):.1f} s)"
        lines.append(line)
    return "\n".join(lines)


def weekly_report(now: float | None = None) -> str:
    """Weekly summary (for the core's notification); returns "" if not time yet or no records."""
    now = now or time.time()
    marker = log_path().with_name("latency-report.json")
    try:
        last = float(json.loads(marker.read_text(encoding="utf-8")).get("last", 0))
    except (OSError, ValueError, AttributeError):
        last = 0.0
    if not last:  # first startup: let a week accumulate
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"last": now}), encoding="utf-8")
        except OSError:
            pass  # the marker is retried on the next start
        return ""
    if now - last < 7 * 86400:
        return ""
    text = summary(7)
    try:
        marker.write_text(json.dumps({"last": now}), encoding="utf-8")
    except OSError:
        pass  # report again next time
    return text
