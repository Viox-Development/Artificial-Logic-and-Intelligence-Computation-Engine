"""Store for streaming data.

Each value is saved as a timestamped number (or text) under a key ("pc.gpu.temp", "dcs.fuel.kg", "watch.hr"). Mode
changes are also written under the "core.mode" key, so "how hot was the GPU yesterday while DCS was open?" can be
answered by looking at the flight-mode spans. Notifications and task results are kept in the `events` table.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from alice.config import alice_home

MODE_KEY = "core.mode"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (ts REAL NOT NULL, key TEXT NOT NULL, value REAL, text TEXT);
CREATE INDEX IF NOT EXISTS ix_samples_key_ts ON samples(key, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL DEFAULT 'info', topic TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
"""


@dataclass
class Stats:
    key: str
    count: int
    min: float | None
    max: float | None
    avg: float | None
    last: float | None
    max_ts: float | None = None
    total: float | None = None

    def line(self) -> str:
        if not self.count:
            return f"{self.key}: veri yok"
        when = f" ({datetime.fromtimestamp(self.max_ts):%d.%m %H:%M})" if self.max_ts else ""
        total = f", toplam {self.total:.0f}" if self.key.endswith("steps") and self.total is not None else ""
        return (f"{self.key}: ort {self.avg:.1f}, en düşük {self.min:.1f}, en yüksek {self.max:.1f}{when}, "
                f"son {self.last:.1f} ({self.count} ölçüm{total})")


@dataclass
class Event:
    id: int
    ts: float
    kind: str
    title: str
    body: str
    severity: str
    topic: str
    delivered: bool

    def line(self) -> str:
        mark = "" if self.delivered else " [susturuldu]"
        body = f": {self.body}" if self.body else ""
        return f"{datetime.fromtimestamp(self.ts):%d.%m %H:%M} {self.kind}/{self.severity}{mark} {self.title}{body}"


_REL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sn|m|dk|h|sa|d|g|w|hf)\s*$", re.I)
_UNITS = {"s": 1, "sn": 1, "m": 60, "dk": 60, "h": 3600, "sa": 3600, "d": 86400, "g": 86400, "w": 604800, "hf": 604800}


def parse_time(value: str | float | None, now: float | None = None) -> float | None:
    """'15m', '2h', '3d' (counting back from now), 'dün' (yesterday), 'bugün' (today), an ISO date, or epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    now = time.time() if now is None else now
    s = value.strip().lower()
    m = _REL.match(s)
    if m:
        return now - float(m.group(1)) * _UNITS[m.group(2)]
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("bugün", "today"):
        return today.timestamp()
    if s in ("dün", "yesterday"):
        return (today - timedelta(days=1)).timestamp()
    if s in ("now", "şimdi"):
        return now
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.strip()).timestamp()
    except ValueError as e:
        raise ValueError(f"zaman anlaşılamadı: {value!r} (ör. 2h, 3d, dün, 2026-10-08T20:00)") from e


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else alice_home() / "core.db"
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.executescript(_SCHEMA)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            # Once the WAL file grew, it stayed at that size (Nov 1: core.db 70 MB, core.db-wal 70 MB):
            # after a checkpoint, trim it to 16 MB
            self._db.execute("PRAGMA journal_size_limit=16777216")
        except sqlite3.DatabaseError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ write
    def add(self, key: str, value: float | None = None, text: str | None = None, ts: float | None = None) -> None:
        self.add_many([(key, value, text)], ts)

    def add_many(self, rows: Iterable[tuple[str, float | None, str | None]], ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._db.executemany("INSERT INTO samples(ts, key, value, text) VALUES (?, ?, ?, ?)",
                                 [(ts, k, None if v is None else float(v), t) for k, v, t in rows])
            self._db.commit()

    def add_event(self, kind: str, title: str, body: str = "", severity: str = "info", topic: str = "",
                  delivered: bool = True, ts: float | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(ts, kind, title, body, severity, topic, delivered) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time() if ts is None else ts, kind, title, body, severity, topic, int(delivered)))
            self._db.commit()
            return int(cur.lastrowid or 0)

    def prune(self, retention_days: float, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - retention_days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db.commit()
        self.thin(now)
        return n

    THIN_AFTER_DAYS = 2.0   # older samples thinned to one per minute (1.7M rows in 20 days -> core.db 130 MB)
    THIN_STEP_DAYS = 3.0    # at most this many days per call, so the hourly tick doesn't hold the lock too long

    def thin(self, now: float | None = None) -> int:
        """Thin old samples to one row per key per minute: numbers are averaged, the last value stays for text.

        Where it left off is stored in the `meta` table; the same range is never processed twice."""
        now = time.time() if now is None else now
        upto = now - self.THIN_AFTER_DAYS * 86400
        with self._lock:
            db = self._db
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            row = db.execute("SELECT value FROM meta WHERE key = 'thinned_until'").fetchone()
            start = float(row[0]) if row else (db.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or upto)
            start = start // 60 * 60  # minute boundary: buckets must not be split
            end = min(upto, start + self.THIN_STEP_DAYS * 86400) // 60 * 60
            if end <= start:
                return 0
            before = db.execute("SELECT COUNT(*) FROM samples WHERE ts >= ? AND ts < ?", (start, end)).fetchone()[0]
            try:
                # In a query with a single MAX(), SQLite takes the bare columns (text) from the last row.
                db.execute("CREATE TEMP TABLE IF NOT EXISTS _thin (ts REAL, key TEXT, value REAL, text TEXT)")
                db.execute("DELETE FROM _thin")
                db.execute("INSERT INTO _thin SELECT MAX(ts), key, AVG(value), text FROM samples WHERE ts >= ? "
                           "AND ts < ? GROUP BY key, CAST(ts / 60 AS INTEGER)", (start, end))
                db.execute("DELETE FROM samples WHERE ts >= ? AND ts < ?", (start, end))
                db.execute("INSERT INTO samples SELECT * FROM _thin")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('thinned_until', ?)", (str(end),))
                db.commit()
            except sqlite3.Error:
                # If it stopped halfway, the open transaction was committed by the next add_many: samples would be
                # deleted and their summaries never inserted (and the open lock keeps other processes waiting).
                db.rollback()
                raise
            after = db.execute("SELECT COUNT(*) FROM _thin").fetchone()[0]
            # VACUUM is not run: on a 130 MB file it held the store lock for tens of seconds, and even flight
            # announcements were waiting. New samples reuse the freed pages, so the file never grows again.
        return before - after

    # ----------------------------------------------------------------- read
    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def keys(self, pattern: str = "*", since: float | None = None) -> list[str]:
        rows = self._q("SELECT DISTINCT key FROM samples WHERE ts >= ?", (since or 0,))
        return sorted(k for (k,) in rows if fnmatch.fnmatchcase(k, pattern))

    def last(self, key: str) -> tuple[float, float | None, str | None] | None:
        rows = self._q("SELECT ts, value, text FROM samples WHERE key = ? ORDER BY ts DESC LIMIT 1", (key,))
        return rows[0] if rows else None

    def series(self, key: str, since: float | None = None, until: float | None = None,
               mode: str | None = None) -> list[tuple[float, float]]:
        rows = self._q("SELECT ts, value FROM samples WHERE key = ? AND ts >= ? AND ts <= ? AND value IS NOT NULL "
                       "ORDER BY ts", (key, since or 0, until if until is not None else 1e18))
        if mode:
            spans = self.mode_spans(mode, since, until)
            rows = [r for r in rows if any(a <= r[0] < b for a, b in spans)]
        return [(float(t), float(v)) for t, v in rows]

    def texts(self, key: str, since: float | None = None, until: float | None = None) -> list[tuple[float, str]]:
        rows = self._q("SELECT ts, text FROM samples WHERE key = ? AND ts >= ? AND ts <= ? AND text IS NOT NULL "
                       "ORDER BY ts", (key, since or 0, until if until is not None else 1e18))
        return [(float(t), str(x)) for t, x in rows]

    def stats(self, key: str, since: float | None = None, until: float | None = None, mode: str | None = None) -> Stats:
        pts = self.series(key, since, until, mode)
        if not pts:
            return Stats(key, 0, None, None, None, None)
        vals = [v for _, v in pts]
        mx = max(pts, key=lambda p: p[1])
        return Stats(key, len(vals), min(vals), mx[1], sum(vals) / len(vals), vals[-1], mx[0], sum(vals))

    def mode_spans(self, mode: str, since: float | None = None, until: float | None = None) -> list[tuple[float, float]]:
        """Spans [start, end) during which the given mode was on."""
        until = time.time() if until is None else until
        rows = self._q("SELECT ts, text FROM samples WHERE key = ? AND ts <= ? ORDER BY ts", (MODE_KEY, until))
        spans: list[tuple[float, float]] = []
        start = None
        for ts, text in rows:
            if text == mode and start is None:
                start = ts
            elif text != mode and start is not None:
                spans.append((start, ts))
                start = None
        if start is not None:
            spans.append((start, until))
        lo = since or 0
        return [(max(a, lo), b) for a, b in spans if b > lo]

    def events(self, since: float | None = None, kind: str | None = None, limit: int = 50) -> list[Event]:
        sql = "SELECT id, ts, kind, title, body, severity, topic, delivered FROM events WHERE ts >= ?"
        args: list = [since or 0]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = self._q(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit))
        return [Event(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7])) for r in rows]
