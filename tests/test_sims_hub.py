from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from alice.sims import dcs, hub, warthunder
from alice.sims.flight import KT, Debrief, FlightState
from alice.sims.hub import SimHub

FT = 0.3048


class FakeStore:
    def __init__(self):
        self.events: list[tuple] = []
        self.rows: list = []
        self.stats_result = SimpleNamespace(count=0, avg=None, max=None, min=None)
        self.last_result = None
        self.series_result: list = []
        self.calls: list = []

    def stats(self, key, a=None, b=None):
        self.calls.append(("stats", key, a, b))
        return self.stats_result

    def last(self, key):
        self.calls.append(("last", key))
        return self.last_result

    def series(self, key, a=None, b=None):
        self.calls.append(("series", key, a, b))
        return self.series_result

    def add_event(self, kind, title, body="", severity="info", topic=""):
        self.events.append((kind, title, body, severity, topic))

    def add_many(self, rows):
        self.rows.extend(rows)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, n):
        self.sent.append(n)


@pytest.fixture
def h():
    hb = SimHub({}, FakeStore(), FakeNotifier())
    yield hb
    hb.stop()


@pytest.fixture(autouse=True)
def reset_active(monkeypatch):
    monkeypatch.setattr(hub, "ACTIVE", None)


def state(**kw):
    base = dict(t=100.0, source="dcs", aircraft="F-16C_50", ias=150.0, tas=160.0, alt=3000.0, agl=2900.0,
                vv=0.0, g=1.2, fuel_kg=2000.0, x=0.0, z=0.0, heading=90.0)
    base.update(kw)
    return FlightState(**base)


def airborne(hb, src="dcs", **kw):
    c = hb.companion(src)
    c.phase = "air"
    c.last_state = state(source=src, **kw)
    return c


# ------------------------------------------------------------ simple helpers
def test_say_sends_flight_notification(h):
    h._say("Bingo", "warn", "urgent")
    n = h.notifier.sent[0]
    assert (n.title, n.body, n.severity, n.topic, n.kind, n.emotion) == ("Uçuş", "Bingo", "warn", "flight", "flight",
                                                                          "urgent")


def test_hr_stats_needs_three_samples(h):
    h.store.stats_result = SimpleNamespace(count=2, avg=90, max=100, min=80)
    assert h._hr_stats(1.0, 2.0) is None
    h.store.stats_result = SimpleNamespace(count=3, avg=90, max=100, min=80)
    assert h._hr_stats(1.0, 2.0) == {"avg": 90, "max": 100, "min": 80}
    assert h.store.calls[-1] == ("stats", "watch.hr", 1.0, 2.0)


def test_hr_now_only_fresh(h):
    assert h._hr_now() is None
    h.store.last_result = (time.time() - 10, 88.0, None)
    assert h._hr_now() == 88.0
    h.store.last_result = (time.time() - 120, 88.0, None)
    assert h._hr_now() is None


def test_companion_is_cached_per_source(h):
    a = h.companion("dcs")
    assert h.companion("dcs") is a
    assert h.companion("warthunder") is not a
    assert set(h.companions) == {"dcs", "warthunder"}


def test_companion_wired_to_hub_callbacks(h):
    c = h.companion("dcs")
    c.say("merhaba", "info", "calm")
    assert h.notifier.sent[-1].body == "merhaba"


def test_debrief_saves_page_and_logs_event(h, monkeypatch, tmp_path):
    saved = {}

    def fake_save(d, series):
        saved.update(d=d, series=series)
        return tmp_path / "f.html"

    monkeypatch.setattr(hub, "save_debrief", fake_save)
    h.store.series_result = [(1.0, 90.0)]
    d = Debrief("F-16C", "dcs", 10.0, 20.0, 600, 5.0, 400, 20000, 500, 1500, None, None, -1.0, 2, 1)
    h._debrief(d)
    assert saved == {"d": d, "series": [(1.0, 90.0)]}
    assert h.store.calls[-1] == ("series", "watch.hr", 10.0, 20.0)
    assert h.last_debrief == (d, str(tmp_path / "f.html"))
    kind, title, body, sev, topic = h.store.events[-1]
    assert (kind, title, sev, topic) == ("flight", "Uçuş değerlendirmesi: F-16C", "info", "flight")
    assert d.summary() in body and str(tmp_path / "f.html") in body


def test_debrief_real_save_writes_under_alice_home(h, isolated_home):
    d = Debrief("F-16C", "dcs", 10.0, 20.0, 600, 5.0, 400, 20000, None, None, None, None, None, None, None,
                series={"t": [0, 1], "wall": [10, 11], "alt_ft": [0, 100], "g": [1, 1.5], "ias_kt": [0, 150],
                        "fuel_kg": [3000, 2990]})
    h._debrief(d)
    page = Path(h.last_debrief[1])
    assert page.exists() and isolated_home in page.parents


# ------------------------------------------------------------------ start/stop
class FakePart:
    instances: list = []

    def __init__(self, *args, **kw):
        self.args, self.kw = args, kw
        self.started = self.stopped = False
        FakePart.instances.append(self)

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_sims(monkeypatch):
    FakePart.instances = []
    installs = []

    def ensure(port=dcs.PORT, dirs=None, **kw):
        installs.append(port)
        return [Path("Saved Games/DCS/Scripts")]

    monkeypatch.setattr(dcs, "ensure_installed", ensure)
    monkeypatch.setattr(dcs, "DCSListener", FakePart)
    monkeypatch.setattr(warthunder, "WarThunderPoller", FakePart)
    return SimpleNamespace(installs=installs)


def test_start_both_sources_and_stop(fake_sims):
    procs = {"explorer.exe"}
    hb = SimHub({"dcs_port": "5555"}, FakeStore(), FakeNotifier(), processes=lambda: procs)
    assert hb.start() is hb
    assert hub.ACTIVE is hb
    assert fake_sims.installs == [5555]
    assert len(FakePart.instances) == 2 and all(p.started for p in FakePart.instances)
    dcs_part, wt_part = FakePart.instances
    assert dcs_part.kw["port"] == 5555
    assert hb.store.events[0][1] == "DCS data script installed"
    assert set(hb.companions) == {"dcs", "warthunder"}

    # the add_many callback writes to the store
    dcs_part.args[3]([("dcs.alt", 1.0, None)])
    assert hb.store.rows == [("dcs.alt", 1.0, None)]

    # The War Thunder "çalışıyor mu" (is it running?) check looks for aces.exe (case-insensitive)
    running = wt_part.kw["running"]
    assert running() is False
    procs.add("ACES.EXE")
    assert running() is True

    hb.stop()
    assert all(p.stopped for p in FakePart.instances)
    assert hub.ACTIVE is None


def test_start_respects_disabled_sources(fake_sims):
    hb = SimHub({"dcs": False, "warthunder": False}, FakeStore(), FakeNotifier())
    hb.start()
    assert FakePart.instances == [] and fake_sims.installs == []
    assert hub.ACTIVE is hb
    hb.stop()


def test_start_logs_install_and_listener_errors(monkeypatch, fake_sims):
    def bad_install(port=dcs.PORT, dirs=None, **kw):
        raise PermissionError("erişim yok")

    class BadListener(FakePart):
        def start(self):
            raise OSError("port kullanımda")

    monkeypatch.setattr(dcs, "ensure_installed", bad_install)
    monkeypatch.setattr(dcs, "DCSListener", BadListener)
    hb = SimHub({"warthunder": False}, FakeStore(), FakeNotifier())
    hb.start()
    titles = [(e[0], e[1], e[3]) for e in hb.store.events]
    assert ("error", "Could not install the DCS data script", "warn") in titles
    assert ("error", "The DCS listener did not start", "warn") in titles
    assert hb._parts == []
    assert "dcs" in hb.companions
    hb.stop()


def test_stop_does_not_clear_other_active(h):
    other = SimHub({}, FakeStore(), FakeNotifier())
    hub.ACTIVE = other
    h.stop()
    assert hub.ACTIVE is other


# ------------------------------------------------------------------ alert_home
def test_alert_home_not_flying(h):
    assert h.alert_home(10) == "not set: you are not flying right now"
    c = h.companion("dcs")
    c.last_state = state()  # yerde
    assert h.alert_home(10) == "not set: you are not flying right now"


def test_alert_home_unknown_home(h):
    c = airborne(h)
    c.home = None
    assert "the base position is unknown" in h.alert_home(10)
    assert c.home_alert_nm is None


def test_alert_home_sets_threshold(h):
    c = airborne(h)
    c.home = (0.0, 0.0)
    assert h.alert_home(12.4) == "set: will be said aloud at 12 miles from base"
    assert c.home_alert_nm == 12.4
    assert isinstance(h.alert_home(7), str) and c.home_alert_nm == 7.0 and isinstance(c.home_alert_nm, float)


def test_alert_home_accepts_text_number(h):
    c = airborne(h)
    c.home = (0.0, 0.0)
    assert h.alert_home("15") == "set: will be said aloud at 15 miles from base" and c.home_alert_nm == 15.0


# ---------------------------------------------------------------------- status
def test_status_no_data_dcs_hint(h):
    h.companion("dcs")
    assert "No data came from DCS in this session" in h.status()


def test_status_no_companions(h):
    assert h.status() == "you are not flying right now"


def test_status_dcs_disabled_no_hint(h):
    h.cfg["dcs"] = False
    h.companion("dcs")
    assert h.status() == "you are not flying right now"


def test_status_on_ground(h):
    c = h.companion("dcs")
    c.last_state = state(ias=0)
    c.profile = SimpleNamespace(name="F-16C")
    assert h.status() == "dcs: F-16C, on the ground"


def test_status_in_air_full(h):
    c = airborne(h, alt=3048.0, ias=300 * KT, g=2.0, fuel_kg=2000.0, x=0.0, z=-18520.0, heading=0.0,
                 chaff=30, flare=15, extra={"weapons": "AIM-120 x2"})
    c.profile = SimpleNamespace(name="F-16C", reserve_kg=450, cruise_kg_nm=4.0)
    c.home = (0.0, 0.0)
    for i in range(5):
        c.burn_hist.append((float(i), 1.0))
    c.home_alert_nm = 5.0
    out = h.status()
    lines = out.splitlines()
    assert lines[0] == "dcs: F-16C, 10,000 ft, 300 kt, 2.0 G"
    assert "  fuel 2000 kg, burn 3600 kg/h, 33 min left, base 10 nm, bingo 490 kg (+1510 kg margin)" in lines
    # home is to the east (z+), nose points north: turn 90° right
    assert "  base (takeoff point): bearing 090° (true north), 10 nm, your nose is now 000°: turn 90° right" in lines
    assert "  weapons: AIM-120 x2" in lines
    assert not any("BİLİNMİYOR" in ln for ln in lines)
    assert "  alert set: will be said at 5 miles from base" in lines
    assert lines[-1] == "  chaff 30, flare 15"


def test_status_nose_already_on_home_and_left_turn(h):
    c = airborne(h, x=-18520.0, z=0.0, heading=5.0)
    c.home = (0.0, 0.0)
    assert "(your nose already points at the base)" in h.status()
    c.last_state = state(x=-18520.0, z=0.0, heading=90.0)
    assert "turn 90° left" in h.status()


def test_status_unknown_home_dcs_and_unknown_weapons(h):
    c = airborne(h, fuel_kg=None, chaff=None)
    c.home = None
    out = h.status()
    assert "  direction to base unknown (takeoff point not recorded)" in out
    assert "weapons/ammunition: the game didn't give it for this aircraft, UNKNOWN" in out
    assert "yakıt" not in out and "chaff" not in out


def test_status_warthunder_unknown_weapons_no_home_hint(h):
    airborne(h, src="warthunder", x=None, z=None, alt=None, ias=None, g=None)
    out = h.status()
    assert out.splitlines()[0].startswith("warthunder: uçak, 0 ft, 0 kt, 0.0 G")
    assert "War Thunder doesn't publish this" in out
    assert "direction to base unknown" not in out


def test_status_skips_companion_without_state(h):
    h.companion("dcs")
    airborne(h, src="warthunder")
    out = h.status()
    assert out.startswith("warthunder:") and "dcs:" not in out


def test_briefing_from_current_mission_log(tmp_path, monkeypatch, h):
    from alice.sims.dcs import MissionInfo, Waypoint, briefing_text, current_mission

    miz = tmp_path / "tempMission.miz"
    miz.write_bytes(b"x")
    (tmp_path / "Logs").mkdir()
    (tmp_path / "Logs" / "dcs.log").write_text(
        f"2026 INFO Dispatcher (Main): loadMission C:/eski.miz\n2026 INFO Dispatcher (Main): loadMission {miz}\n",
        encoding="utf-8")
    assert current_mission([tmp_path]) == miz
    m = MissionInfo("Kırmızı Bayrak", "F-16C_50", "Viper 1", 251.0,
                    [Waypoint("Kalkış", 0, 0, 0), Waypoint("WP1", 18520, 0, 15000), Waypoint("Hedef", 18520, 18520, 20000)],
                    "SA-6 bataryasını vur. Sonra eve dön.")
    text = briefing_text(m)
    assert text.startswith("Görev: Kırmızı Bayrak. Viper 1 grubundasın. Rotada 2 nokta var, toplam 20 mil.")
    assert "WP1: 000 derece, 10 mil, 15.000 feet." in text and "Hedef: 090 derece" in text
    assert "Grup frekansı 251." in text and text.endswith("Görev açıklaması: SA-6 bataryasını vur.")
    monkeypatch.setattr(dcs, "read_mission", lambda p: m)
    monkeypatch.setattr(dcs, "saved_games_dirs", lambda: [])
    assert h.brief_mission(miz) == text and h.notifier.sent[-1].body == text
    assert h.brief_mission(miz) == ""  # same mission, only once


def test_cockpit_commands_safety_and_ack(monkeypatch, h):
    sent = []
    c = h.companion("dcs")

    def fake_send(kind, arg, port=dcs.PORT):
        sent.append((kind, arg))
        c.last_state.extra["ack"] = f"{len(sent)}|ok|{arg}"
        return len(sent)

    monkeypatch.setattr(dcs, "send_command", fake_send)
    assert "no live data" in h.cockpit("flare")
    c.last_state = FlightState(t=1, source="dcs", gear=1.0, wall=time.time())
    c.phase = "ground"
    assert h.cockpit("gear_up") == "not done: the gear isn't raised on the ground"
    assert h.cockpit("gear_down") == "not needed: the gear is already down" and sent == []
    assert h.cockpit("flare") == "done: flare" and sent == [("cmd", "flare")]
    assert h.cockpit("radio", 251) == "done: 251.000" and sent[-1] == ("freq", "251.000")
    c.phase = "air"
    assert h.cockpit("gear_up") == "done: gear"
