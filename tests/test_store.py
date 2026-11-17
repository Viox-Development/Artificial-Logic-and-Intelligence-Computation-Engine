from __future__ import annotations

from alice.core.store import Store


def test_old_samples_thinned_to_one_per_minute():
    s = Store(":memory:")
    day = 86400.0
    now = 100 * day
    old = now - 5 * day
    for i in range(120):  # two minutes, one per second
        s.add_many([("wt.g", float(i % 2), None), ("core.mode", None, "flight" if i < 90 else "normal")], ts=old + i)
    s.add("wt.g", 7.0, ts=now - 60)  # new measurement is left untouched
    removed = s.thin(now)
    assert removed == 240 - 4
    assert [v for _, v in s.series("wt.g")] == [0.5, 0.5, 7.0]
    assert [t for _, t in s.texts("core.mode")] == ["flight", "normal"]  # last value of the minute
    assert s.thin(now) == 0  # the same range is never processed again
    s.add("wt.g", 1.0, ts=old + 10)  # a row added later to an already-processed range stays as is
    assert s.thin(now) == 0


def test_thin_failure_midway_does_not_lose_samples():
    """If the summaries fail to be written back, the deletion must not be made permanent by the next commit."""
    import sqlite3

    import pytest

    s = Store(":memory:")
    day = 86400.0
    now = 100 * day
    for i in range(120):
        s.add("wt.g", float(i), ts=now - 5 * day + i)
    real = s._db

    class Flaky:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO samples SELECT"):
                raise sqlite3.OperationalError("disk dolu")
            return real.execute(sql, *args)

    s._db = Flaky()
    with pytest.raises(sqlite3.OperationalError):
        s.thin(now)
    s._db = real
    s.add("x", 1.0, ts=now)  # the next write's commit must not make the half-done deletion permanent
    assert len(s.series("wt.g")) == 120
