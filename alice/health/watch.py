"""Importing from the Gadgetbridge database, live data intake, and measuring data frequency."""

from __future__ import annotations

import json
import shutil
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.core.store import Store

# Gadgetbridge column -> ALICE key and valid range
COLUMNS = {
    "HEART_RATE": ("watch.hr", 30, 230),
    "STEPS": ("watch.steps", 0, 10000),
    "STRESS": ("watch.stress", 1, 100),
    "SPO2": ("watch.spo2", 50, 100),
}

LIVE_KEYS = {"hr": ("watch.hr", 30, 230), "heart_rate": ("watch.hr", 30, 230), "steps": ("watch.steps", 0, 100000),
             "stress": ("watch.stress", 1, 100), "spo2": ("watch.spo2", 50, 100)}


def state_path() -> Path:
    return alice_home() / "watch-state.json"


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict[str, Any]) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(st), encoding="utf-8")


def _match(col: str) -> str | None:
    c = col.upper()
    if c in COLUMNS:
        return c
    if "STRESS" in c and "TIME" not in c:
        return "STRESS"
    if c in ("SPO", "SPO2_VALUE", "OXYGEN"):
        return "SPO2"
    return None


def import_gadgetbridge(db: Path, store: Store) -> dict[str, int]:
    """Write the samples that arrived since the last import to the store. Returns the count added per key."""
    st = _load_state()
    marks: dict[str, float] = st.get("marks", {})
    added: dict[str, int] = {}
    # Read from a copy so the phone's file is not opened while it is being written.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "gb.db"
        shutil.copy2(db, copy)
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
                if "TIMESTAMP" not in (c.upper() for c in cols):
                    continue
                ts_col = next(c for c in cols if c.upper() == "TIMESTAMP")
                for col in cols:
                    kind = _match(col)
                    if kind is None:
                        continue
                    key, lo, hi = COLUMNS[kind]
                    mark_key = f"{table}.{col}"
                    since = marks.get(mark_key, 0)
                    rows = con.execute(f'SELECT "{ts_col}", "{col}" FROM "{table}" WHERE "{ts_col}" > ? '
                                       f'ORDER BY "{ts_col}"', (since,)).fetchall()
                    batch: list[tuple[float, float]] = []
                    last = since
                    for ts, v in rows:
                        last = max(last, ts)
                        if v is None or not lo <= v <= hi or (kind == "STEPS" and v == 0):
                            continue
                        sec = ts / 1000 if ts > 1e11 else ts  # some tables use milliseconds
                        batch.append((float(sec), float(v)))
                    for sec, v in batch:
                        store.add(key, v, ts=sec)
                    if batch:
                        added[key] = added.get(key, 0) + len(batch)
                    marks[mark_key] = last
        finally:
            con.close()
    st["marks"] = marks
    st["last_import"] = time.time()
    _save_state(st)
    return added


def ingest(store: Store, payload: dict[str, Any]) -> int:
    """Live data: {"hr": 88, "ts": 1700000000} or {"samples": [{...}, ...]}. Returns the number added."""
    items = payload.get("samples") if isinstance(payload.get("samples"), list) else [payload]
    n = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = item.get("ts") or item.get("timestamp") or time.time()
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            ts = time.time()
        if ts > 1e11:
            ts /= 1000
        for name, (key, lo, hi) in LIVE_KEYS.items():
            v = item.get(name)
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if lo <= v <= hi:
                store.add(key, v, ts=ts)
                n += 1
    return n


def cadence(store: Store, key: str = "watch.hr", since_hours: float = 24) -> dict[str, Any]:
    """How often is data arriving? The answer to the plan's 'Gadgetbridge testi' (Gadgetbridge test) question."""
    pts = store.series(key, since=time.time() - since_hours * 3600)
    last = store.last(key)
    out: dict[str, Any] = {"key": key, "count": len(pts), "last_age_s": time.time() - last[0] if last else None}
    if len(pts) >= 3:
        gaps = [b[0] - a[0] for a, b in zip(pts, pts[1:]) if b[0] > a[0]]
        med = statistics.median(gaps) if gaps else None
        out["median_gap_s"] = med
        if med is not None:
            out["verdict"] = ("canlı (birkaç saniyede bir): uçuşta anlık tepki mümkün" if med <= 15 else
                              "birkaç dakikada bir: gün içi takip, uyarılar ve uçuş grafiği mümkün" if med <= 900 else
                              "seyrek: sadece günlük özetler için yeterli")
    else:
        out["verdict"] = "veri yok ya da çok az"
    return out


def find_export(path: str) -> Path | None:
    """If the path in settings is a file, that file; if it is a folder, the newest .db inside it."""
    if not path:
        return None
    p = Path(path).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        dbs = list(p.glob("*.db")) + list(p.glob("*.sqlite"))
        return max(dbs, key=lambda x: x.stat().st_mtime) if dbs else None
    return None


class WatchMonitor:
    """Core observer: imports whenever the exported database changes."""

    name = "watch"

    def __init__(self, store: Store, export_path: str):
        self.store = store
        self.export_path = export_path
        self.processes: set[str] = set()
        self._mtime = 0.0

    def collect(self) -> list:
        db = find_export(self.export_path)
        if db is None:
            return []
        mt = db.stat().st_mtime
        if mt <= self._mtime:
            return []
        # Let the sync finish writing.
        if time.time() - mt < 10:
            return []
        self._mtime = mt
        import_gadgetbridge(db, self.store)
        return []
