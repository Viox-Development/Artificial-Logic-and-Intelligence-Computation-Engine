from __future__ import annotations

from datetime import datetime

from alice.core.store import Store
from alice.extras import weekly


def test_app_minutes_caps_gaps_and_skips_shell():
    s = [(0, "aces.exe"), (60, "aces.exe"), (120, "explorer.exe"), (180, "opera.exe"), (3780, "opera.exe")]
    m = weekly.app_minutes(s)
    assert m == {"aces.exe": 2.0, "opera.exe": 2.0}  # a one-hour gap is capped at 2 minutes


def test_weekly_summary_sunday_evening_once(tmp_path):
    store = Store(":memory:")
    now = datetime(2026, 11, 1, 20).timestamp()  # Sunday
    for i in range(0, 4 * 3600, 60):
        store.add("pc.foreground", text="aces.exe", ts=now - 86400 + i)
    flights = [{"aircraft": "F-16C", "start_wall": now - 3600, "duration_s": 1800, "touchdown_vv_ms": -1.4},
               {"aircraft": "F-16C", "start_wall": now - 10 * 86400, "duration_s": 600}]  # last week, not counted
    sent = []
    kw = dict(flights=flights, path=tmp_path / "w.json")
    send = lambda t, b: sent.append(b) or True  # noqa: E731
    assert weekly.maybe_send(store, None, None, send, now=now - 3 * 3600, **kw) is None  # not before 19 o'clock
    text = weekly.maybe_send(store, None, None, send, now=now, **kw)
    assert "1 uçuş, toplam 30 dk; F-16C 1; en yumuşak iniş 1.4" in text and "War Thunder 4.0 saat" in text
    assert weekly.maybe_send(store, None, None, send, now=now + 600, **kw) is None and len(sent) == 1
    assert weekly.maybe_send(store, None, None, send, now=datetime(2026, 11, 2, 20).timestamp(), **kw) is None
