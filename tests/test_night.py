from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from alice import night


def ev(ts, kind="flight", title="Uçuş", body="", severity="info"):
    return SimpleNamespace(ts=ts, kind=kind, title=title, body=body, severity=severity, topic="")


class Store:
    def __init__(self, events):
        self._events = events
        self.added = []

    def events(self, since=None, kind=None, limit=50):
        return [e for e in self._events if e.ts >= (since or 0)]

    def add_event(self, *a, **k):
        self.added.append(a)


def ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def test_noisy_callouts_found_by_pattern_not_exact_text():
    evs = [ev(1000 + i * 30, body=f"Arkanda düşman, saat {4 + i % 3}, {300 + i * 10} metre!") for i in range(6)]
    evs += [ev(1000 + i * 400, body="Düşman saat 2, 3,0 kilometre.") for i in range(4)]  # sparse: not a problem
    found = night.noisy_callouts(evs)
    assert len(found) == 1 and found[0].count == 6 and "Arkanda düşman" in found[0].title


def test_repeated_errors_ignore_critical_flight_callouts():
    evs = [ev(i, kind="error", title=f"takvim takibi hata verdi {i}") for i in range(3)]
    evs += [ev(i, severity="critical", body="Yakıt 3 dakika yeter.") for i in range(5)]
    found = night.repeated_errors(evs)
    assert [(f.kind, f.count) for f in found] == [("error", 3)]


def test_false_wakes_from_voice_log():
    lines = [f"2026-11-02 03:0{i}:00 uyandı [base 0.5sn]: Eşek eşek {i}" for i in range(5)]
    lines += ["2026-11-02 03:09:00 uyandı [base 0.5sn]: Alice saat kaç", "2026-11-01 23:00:00 uyandı: dün"]
    f = night.false_wakes(lines, "2026-11-02")
    assert f is not None and f.count == 5 and "Eşek eşek" in f.detail
    assert night.false_wakes(lines[:4], "2026-11-02") is None


def test_crashes_group_traceback_last_lines():
    tb = "Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n    f()\nValueError: kötü\n"
    found = night.crashes(tb * 3 + tb.replace("ValueError: kötü", "KeyError: 'a'"))
    assert [(f.title, f.count) for f in found] == [("Tekrarlayan çökme: ValueError: kötü", 3)]


def test_insights_short_flights_after_late_nights():
    flights = []
    for day in range(1, 21):
        late = day % 2 == 0
        if late:  # played that night at 02:00 (counted toward the previous day)
            flights.append({"start_wall": ts(day + 1 if day < 20 else day, 2), "duration_s": 300})
        for k in range(3):
            # short flights on the evening after a late night
            prev_late = (day - 1) % 2 == 0 and day > 1
            flights.append({"start_wall": ts(day, 19, k * 10), "duration_s": 120 if prev_late else 360})
    out = night.insights(flights)
    assert out and out[0].startswith("Gece 1'den sonra oynadığın günlerin ertesinde uçuşların ortalama")
    assert night.insights(flights[:5]) == []  # little data: no connection is invented


def test_run_writes_report_and_morning_flow(tmp_path, monkeypatch):
    now = ts(30, 4)
    evs = [ev(now - 3000 + i * 20, body=f"Yere yaklaşıyorsun, burnu kaldır! {i}") for i in range(5)]
    st = Store(evs)
    kw = dict(voice_log=tmp_path / "yok.log", core_log=tmp_path / "yok.log")
    assert night.maybe_run(st, None, now=ts(30, 2), flights=[], **kw) is None  # not before 3 a.m.
    r = night.maybe_run(st, None, now=now, flights=[], **kw)
    assert r is not None and r["findings"][0]["kind"] == "noisy"
    assert night.maybe_run(st, None, now=now + 600, flights=[], **kw) is None  # once per night
    md = (night.reports_dir() / "2026-10-30.md").read_text(encoding="utf-8")
    assert "# Gece raporu: 2026-10-30" in md and "Öneri:" in md
    assert st.added[0][1] == "Gece raporu hazır"

    said = []
    assert not night.maybe_tell(lambda t, b: said.append(b) or True, now=ts(30, 7))  # not before 8 a.m.
    assert not night.maybe_tell(lambda t, b: False, now=ts(30, 9))  # silenced in flight mode: does not count as told
    assert night.maybe_tell(lambda t, b: said.append(b) or True, now=ts(30, 9, 10))
    assert not night.maybe_tell(lambda t, b: said.append(b) or True, now=ts(30, 10))
    assert len(said) == 1 and said[0].startswith("Gece 1 şey buldum") and "Düzeltmeyi deneyeyim mi?" in said[0]
    assert json.loads((night.reports_dir() / "2026-10-30.json").read_text(encoding="utf-8"))["date"] == "2026-10-30"


def test_failed_night_is_retried_three_times(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise ValueError("bozuk kayıt")

    monkeypatch.setattr(night, "run", boom)
    for i in range(4):
        try:
            night.maybe_run(Store([]), None, now=ts(29, 4, i * 10))
        except ValueError:
            pass
    assert len(calls) == 3  # after 3 attempts that night is abandoned


def test_render_when_all_is_well():
    assert "Her şey yolunda" in night.render({"date": "x", "findings": [], "insights": [], "tomorrow": []})
    assert night.morning_text({"findings": [], "insights": [], "tomorrow": []}) == ""


def test_core_runs_night_step(monkeypatch):
    from alice.core import service

    calls = []
    monkeypatch.setattr(night, "maybe_run", lambda store, mem: calls.append("run"))
    monkeypatch.setattr(night, "maybe_tell", lambda send: calls.append("tell"))
    core = SimpleNamespace(cfg=SimpleNamespace(data={}), store=None, notifier=None)
    service.Core._night(core, None)
    assert calls == ["run", "tell"]
    core.cfg.data = {"night": {"enabled": False}}
    service.Core._night(core, None)
    assert calls == ["run", "tell"]


def test_single_thread_death_is_reported():
    """Live log: voice thread died once, ALICE was deaf for hours; "en az 2 kez" (at least 2 times) rule missed it."""
    from alice import night

    log = ("Exception in thread alice-voice:\nTraceback (most recent call last):\n  File \"x.py\", line 1, in run\n"
           "    self._live_q.put(f)\n    ^^^^^^^^\nAttributeError: 'NoneType' object has no attribute 'put'\n")
    found = night.thread_deaths(log)
    assert len(found) == 1 and "alice-voice" in found[0].title and "AttributeError" in found[0].title


def test_struggles_find_loops_repeated_keys_interrupts_and_frustration(tmp_path):
    import json

    from alice import night

    call = {"id": "c", "type": "function", "function": {"name": "reflex", "arguments": "{\"goal\": \"kur\"}"}}
    msgs = [{"role": "user", "content": "sonorayı kur"},
            *[{"role": "assistant", "content": "", "tool_calls": [call]} for _ in range(3)],
            {"role": "tool", "content": "adımlar: 1. pressed enter (ekran değişti) 2. pressed enter 3. pressed enter "
                                        "4. pressed enter"},
            {"role": "tool", "content": "durduruldu (kullanıcı kesti)"},
            {"role": "user", "content": "yine saçmaladın ya"},
            {"role": "user", "content": "napayım boş boş takılmaca aq"}]  # everyday chat: not counted
    (tmp_path / "s.json").write_text(json.dumps({"messages": msgs}), encoding="utf-8")
    titles = [f.title for f in night.struggles(tmp_path, 0)]
    assert "Aynı adımı tekrarladı: reflex" in titles
    assert "Ekran işinde aynı adım 4 kez: pressed enter" in titles
    assert "Kullanıcı işi yarıda kesti" in titles
    assert any(t.startswith("Kullanıcı memnun kalmadı: \"yine saçmaladın") for t in titles)
    assert not any("takılmaca" in t for t in titles)


def test_tomorrow_does_not_close_followups_that_are_still_current():
    from alice.memory import LongMemory

    m = LongMemory(":memory:")
    now = datetime(2026, 10, 20, 4, 0).timestamp()
    # It was due 3.5 days ago: still valid today (4-day rule); looking 36 hours ahead makes it look "eski" (old).
    fid = m.add_followup("Sınavın nasıl geçtiğini sor.", now - 3.5 * 86400, "ask")
    m.add_followup("Dişçi randevusunu hatırlat.", now + 20 * 3600, "remind")
    assert night.tomorrow(m, now) == ["Dişçi randevusunu hatırlat."]
    assert fid in [f.id for f in m.due_followups(now)]  # the night report did not close the follow-up


def test_state_is_written_atomically_and_retries_locked_target(monkeypatch):
    import os
    import pytest

    night._save_state(ran="2026-11-15")
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("kilitli")
        real(src, dst)

    monkeypatch.setattr(night.os, "replace", flaky)
    monkeypatch.setattr(night.time, "sleep", lambda s: None)
    night._save_state(told="2026-11-15")
    assert len(calls) == 2
    assert json.loads(night.state_path().read_text(encoding="utf-8")) == {"ran": "2026-11-15", "told": "2026-11-15"}

    def broken(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(night.os, "replace", broken)
    with pytest.raises(OSError):
        night._save_state(told="x")
    assert json.loads(night.state_path().read_text(encoding="utf-8"))["told"] == "2026-11-15"  # the old version is intact
    assert [p.name for p in night.state_path().parent.iterdir() if p.name.endswith(".tmp")] == []
