from __future__ import annotations

from types import SimpleNamespace

from alice import mood
from alice.sims import coach


def flight(end: float, low=True, turning=True, landed=False, dur=200.0):
    n = 30
    wall = [end - n + i + 1 for i in range(n)] + [end + 1, end + 2]  # last two samples: frozen after death
    return {"start_wall": end - dur, "end_wall": end + 2, "duration_s": dur, "aircraft": "su_30mkk",
            "touchdown_vv_ms": -2.0 if landed else None,
            "series": {"wall": wall, "alt_ft": [1500 if low else 9000] * n + [0, 0],
                       "g": [5.5 if turning else 1.2] * n + [0, 0], "ias_kt": [300] * n + [0, 0]}}


def ev(ts, body):
    return SimpleNamespace(ts=ts, kind="flight", title="Uçuş", body=body)


def test_death_context_reads_last_alive_moment():
    f = flight(1000.0)
    events = [ev(970.0, "Arkanda düşman, saat 6, 400 metre!"), ev(950.0, "SAM menzilindesin: saat 2, 9 km."),
              ev(995.0, "Yanıyorsun! F-16C vurdu.")]
    c = coach.death_context(f, events)
    # You were in range, but a plane hit you: the death is not blamed on SAM.
    assert (c["low"], c["turning"], c["six"], c["sam"], c["killer"]) == (True, True, True, False, "F-16C")
    assert coach.death_context(flight(1000.0, landed=True), events) is None
    assert coach.death_context({"touchdown_vv_ms": None, "series": {}}, events) is None


def test_death_blamed_on_real_killer():
    f = flight(1000.0)
    sam_only = [ev(950.0, "SAM menzilindesin: saat 2, 9 km.")]
    assert coach.death_context(f, sam_only)["sam"] is True  # killer unknown: the range hint stays
    late_plane = sam_only + [ev(1015.0, "Ağır hasar aldın, F/A-18E vurdu!")]  # the text came in after the flight ended
    c = coach.death_context(f, late_plane)
    assert (c["killer"], c["sam"]) == ("F/A-18E", False)
    assert coach.death_context(f, [ev(995.0, "Yanıyorsun! Pantsir-S1 vurdu.")])["sam"] is True
    # One hit you for damage, another brought you down: the death is blamed on whoever brought you down.
    both = [ev(990.0, "Ağır hasar aldın, F-16C vurdu!"), ev(1010.0, "Seni FSAF SAMP/T düşürdü.")]
    assert (coach.death_context(f, both)["killer"], coach.death_context(f, both)["sam"]) == ("FSAF SAMP/T", True)
    assert not coach.is_air_defense("Tornado IDS") and not coach.is_air_defense("F-16C")


def test_patterns_need_share_and_count():
    deaths, events = [], []
    for i in range(10):
        end = 1000.0 * (i + 1)
        deaths.append(flight(end, low=i < 7, turning=i < 7))
        if i < 7:
            events.append(ev(end - 20, "Arkanda düşman, saat 5, 300 metre!"))
    p = coach.patterns(coach.contexts(deaths, events))
    assert p == {"low_turn_six": "Son 10 ölümünün 7'si alçakta dönerken arkadan geldi. Alçakta uzun dönme; enerjini "
                                 "koru, irtifa kazanıp dön."}
    assert coach.patterns(coach.contexts(deaths[:2], events)) == {}  # too few deaths: no pattern
    few = coach.contexts(deaths, events[:2])
    assert "six" not in coach.patterns(few) and "low_turn_six" not in coach.patterns(few)


def test_turkish_number_suffix():
    assert [coach.ek(n) for n in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 40)] == [
        "0'ı", "1'i", "2'si", "3'ü", "4'ü", "5'i", "6'sı", "7'si", "8'i", "9'u", "10'u", "20'si", "40'ı"]
    assert mood.read([], ["lan bu ne saçma", "ne biçim harita"], None, 0.0).swears == 0  # everyday slang


def test_mood_levels():
    now = 10_000.0
    quick = [{"end_wall": now - 60 * i, "duration_s": 70, "touchdown_vv_ms": None} for i in range(3)]
    assert mood.read([], [], None, now).level == "sakin"
    assert mood.read(quick, [], None, now).level == "gergin"
    m = mood.read(quick, ["amk ya yine mi", "siktir", "kahretsin"], 125.0, now)
    assert m.level == "tilt" and m.swears == 3 and "nabız 125" in m.reasons
    old = [{"end_wall": now - 3600, "duration_s": 70, "touchdown_vv_ms": None}] * 5
    assert mood.read(old, [], None, now).quick_deaths == 0  # older than 15 minutes


def test_heard_lines_only_recent(tmp_path):
    p = tmp_path / "voice.log"
    p.write_text("2026-11-02 03:00:00 uyandırma yok [base 1sn]: eski laf\n"
                 "2026-11-02 03:20:00 live [m] duyulan: amk yine düştüm | cevap: üzgünüm\n"
                 "2026-11-02 03:21:00 uyandırma yok [base 1sn]: lan ne biçim oyun\n", encoding="utf-8")
    from datetime import datetime

    since = datetime(2026, 11, 2, 3, 10).timestamp()
    assert [x.strip() for x in mood.heard_lines(p, since)] == ["amk yine düştüm", "lan ne biçim oyun"]


def test_hint_once_per_session_and_calm_mode():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r = Recorder()
    c = FlightCompanion(r.say)
    c.coach_flags = {"sam"}
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    sam = {"id": "", "clock": 2, "dist_m": 9000.0, "kind": "sam", "name": "SAM", "range_m": 18000.0}
    tr.feed(st(2, extra={"contacts": [sam]}))
    assert r.texts()[-1] == ("SAM menzilindesin: saat 2, 9,0 kilometre. Geçen ölümlerinin çoğu hava savunmasındandı, "
                             "alçal ya da çık.")
    c.calm = True
    assert c._may_say(10.0, "x", 0) is False  # tilt: 24 sec gap instead of 12
    assert c._may_say(27.0, "x", 0) is True


def test_hub_after_death_coaches_and_suggests_break(monkeypatch):
    from tests.test_sims_hub import FakeNotifier, FakeStore

    from alice.sims.flight import FlightCompanion
    from alice.sims.hub import SimHub

    store = FakeStore()
    store.events = lambda since=None, kind=None, limit=50: []
    hb = SimHub({}, store, FakeNotifier())
    comp = FlightCompanion(hb._say)
    monkeypatch.setattr(coach, "load_flights", lambda limit=40: [])
    monkeypatch.setattr(coach, "patterns", lambda ctxs: {"sam": "Son 10 ölümünün 5'i SAM menzilindeyken oldu."})
    monkeypatch.setattr(mood, "current", lambda st, now, flights=None: mood.Mood())
    hb._after_death(comp, now=10_000.0)
    hb._after_death(comp, now=10_100.0)  # same day and 20 min not yet passed: no repeat
    assert [n.body for n in hb.notifier.sent] == ["Son 10 ölümünün 5'i SAM menzilindeyken oldu."]
    assert comp.coach_flags == {"sam"} and not comp.calm
    monkeypatch.setattr(mood, "current", lambda st, now, flights=None: mood.Mood(level="tilt"))
    hb._after_death(comp, now=12_000.0)
    assert hb.notifier.sent[-1].body == "Üst üste düştün. Bir maç ara vermek iyi gelebilir." and comp.calm
