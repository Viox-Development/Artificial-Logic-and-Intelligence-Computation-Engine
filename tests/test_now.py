from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from alice import now as nowmod
from alice.now import Anticipator, Snapshot, advise, describe, line, predict_next, session_starts, snapshot


def ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


class Store:
    def __init__(self, last=None, series=None, events=None):
        self._last = last or {}
        self._series = series or {}
        self._events = events or []
        self.added = []

    def last(self, key):
        return self._last.get(key)

    def series(self, key, since=None, until=None, mode=None):
        return [(t, v) for t, v in self._series.get(key, []) if t >= (since or 0)]

    def events(self, since=None, kind=None, limit=50):
        return [e for e in self._events if e.ts >= (since or 0) and (kind is None or e.kind == kind)]

    def add_event(self, *a, **k):
        self.added.append(a)


def test_sessions_and_prediction_from_habit():
    times = []
    for day in range(1, 8):  # every day between 21:00-22:00, one sample every 15 s
        times += [ts(day, 21) + i * 15 for i in range(240)]
    starts = session_starts(times)
    assert len(starts) == 7 and all(datetime.fromtimestamp(s).hour == 21 for s in starts)
    at = predict_next(starts, ts(9, 18))
    assert datetime.fromtimestamp(at).strftime("%d %H:%M") == "09 21:00"
    assert datetime.fromtimestamp(predict_next(starts, ts(9, 23))).strftime("%d %H:%M") == "10 21:00"
    assert predict_next(starts[:2], ts(9, 18)) is None  # little history: no prediction


def test_snapshot_collects_state_and_shot_from_six():
    now = ts(20, 22)
    flights = [{"start_wall": now - 3600 + i * 600, "end_wall": now - 3600 + i * 600 + 150, "duration_s": 150,
                "touchdown_vv_ms": None} for i in range(5)]
    flights += [{"start_wall": ts(10, 20) + i * 600, "end_wall": ts(10, 20) + i * 600 + 300, "duration_s": 300,
                 "touchdown_vv_ms": None} for i in range(12)]
    sixes = [SimpleNamespace(ts=flights[i]["end_wall"] - 20, kind="flight", body="Arkanda düşman, saat 6, 300 metre!")
             for i in (0, 2, 4)]
    st = Store(last={"pc.foreground": (now - 10, None, "aces.exe"), nowmod.GAME_KEY: (now - 5, 3.0, None),
                     "watch.hr": (now - 30, 131.0, None)}, events=sixes)
    s = snapshot(st, now=now, flights=flights, state={"mode": "flight"})
    assert (s.mode, s.app, s.in_game, s.flights_today, s.recent, s.shot_from_six, s.hr) == \
        ("flight", "War Thunder", True, 5, 5, 3, 131.0)
    assert round(s.avg_min_today, 1) == 2.5 and round(s.avg_min_usual, 1) == 5.0
    assert line(s) == "mode flight, in War Thunder, 5 flights today, avg 2.5 min, hit from behind in 3 of the last 5 flights, heart rate 131"
    text = describe(s)
    assert "In 3 of your last 5 flights an enemy was behind you before you went down" in text
    assert "Your heart rate is 131." in text and "Sitting out a match might help." in text


def test_snapshot_is_quiet_with_no_data():
    s = snapshot(Store(), now=ts(20, 12), flights=[], state={})
    assert line(s) == "" and describe(s) == "Nothing notable right now." and s.advice == []


def test_advice_needs_enough_evidence():
    assert advise(Snapshot(at=0, recent=2, shot_from_six=2)) == []
    assert advise(Snapshot(at=0, recent=5, shot_from_six=2)) == []
    assert advise(Snapshot(at=0, hr=125.0)) == ["Your heart rate is 125."]


def test_anticipator_prepares_once_before_usual_session():
    times = []
    for day in range(1, 8):
        times += [ts(day, 21) + i * 15 for i in range(60)]
    st = Store(series={nowmod.GAME_KEY: [(t, 3.0) for t in times]})
    ran = []
    a = Anticipator(st, hooks=[lambda: ran.append(1)])
    assert not a.check(ts(9, 20, 0))      # 1 hour left: too early
    assert a.check(ts(9, 20, 45))         # 15 min left: prepare
    assert not a.check(ts(9, 20, 50))     # once per day
    assert ran == [1] and st.added[0][1] == "Anticipation: getting ready for a flight"
    assert not Anticipator(st, hooks=[]).check(ts(10, 20, 45))  # no hooks: does nothing


def test_anticipator_skips_when_already_playing():
    times = [ts(day, 21) + i * 15 for day in range(1, 8) for i in range(60)] + [ts(9, 20, 30)]
    st = Store(series={nowmod.GAME_KEY: [(t, 3.0) for t in times]})
    a = Anticipator(st, hooks=[lambda: None])
    assert not a.check(ts(9, 20, 45))


def test_voice_line_never_blocks(monkeypatch):
    calls = []
    monkeypatch.setattr(nowmod, "_cache", {"at": -1e9, "line": "", "busy": False})
    monkeypatch.setattr(nowmod, "_refresh", lambda now: calls.append(now) or nowmod._cache.update(
        at=now, line="mod uçuş", busy=False))
    assert nowmod.voice_line(now=100.0, wait=True) == "mod uçuş"
    assert nowmod.voice_line(now=110.0) == "mod uçuş" and calls == [100.0]  # not recomputed within 30 s


def test_with_clock_adds_state(monkeypatch):
    from alice.voice import assistant

    monkeypatch.setattr(nowmod, "voice_line", lambda: "mod uçuş, War Thunder'da")
    assert assistant.with_clock("nasılım").endswith("; status: mod uçuş, War Thunder'da]")
    monkeypatch.setattr(nowmod, "voice_line", lambda: "")
    assert "durum:" not in assistant.with_clock("nasılım")


def test_now_tool_in_group():
    from alice.tools.groups import GROUPS, groups_for_text

    assert "now" in GROUPS["now"][0] and "now" in groups_for_text("Alice nasılım bugün?")
