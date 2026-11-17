from __future__ import annotations

import json
import socket
import time
import zipfile

import httpx
import pytest

from alice.sims import checklists, kneeboard
from alice.sims.dcs import DCSListener, install, parse_lua_table, read_mission, to_state, uninstall
from alice.sims.flight import KT, NM, FlightCompanion, FlightState, TouchdownTracker, profile_for, save_debrief
from alice.sims.warthunder import WarThunderPoller


class Recorder:
    def __init__(self):
        self.said: list[tuple[str, str, str]] = []
        self.debriefs = []

    def say(self, text, severity, emotion):
        self.said.append((text, severity, emotion))

    def texts(self):
        return [t for t, _, _ in self.said]


def make(hr=None):
    r = Recorder()
    c = FlightCompanion(r.say, hr_stats=lambda a, b: {"avg": 110, "max": 142, "min": 80},
                        hr_now=lambda: hr, on_debrief=r.debriefs.append)
    return c, TouchdownTracker(c), r


def st(t, **kw):
    base = dict(source="dcs", aircraft="F-16C_50", ias=150.0, tas=160.0, alt=3000.0, agl=2900.0, vv=0.0, aoa=3.0,
                g=1.0, fuel_kg=3000.0, burn_kgs=0.8, chaff=60, flare=30, gear=0.0, x=0.0, z=0.0, wall=1_000_000 + t)
    base.update(kw)
    return FlightState(t=t, **base)


def test_full_flight():
    c, tr, r = make()
    t = 0.0
    tr.feed(st(t, ias=0, agl=0.5, alt=50))                     # on the ground
    t += 1
    tr.feed(st(t, ias=80, agl=30))                              # takeoff
    assert c.phase == "air" and c.home == (0.0, 0.0)
    fuel = 3000.0
    # fuel burns while flying 100 km out: an "eve dön" warning should come at some point
    x = 0.0
    while fuel > 700:
        t += 1
        fuel -= 1.0
        x += 200
        tr.feed(st(t, fuel_kg=fuel, burn_kgs=1.0, x=x))
    texts = " ".join(r.texts())
    assert "Joker. Bingo'ya" in texts and "Bingo. Şimdi eve dön" in texts and "tüketim" not in texts
    assert texts.index("Joker") < texts.index("Bingo.")
    bingo = next(s for s in r.said if s[0].startswith("Bingo"))
    assert bingo[1] == "critical" and bingo[2] == "urgent"
    n = len(r.said)
    for _ in range(5):
        t += 1
        tr.feed(st(t, fuel_kg=fuel, burn_kgs=1.0, x=x))
    assert len(r.said) == n                                     # does not repeat

    # sustained 8 G
    for _ in range(10):
        t += 1
        tr.feed(st(t, g=8.2, fuel_kg=fuel, x=x))
    assert any("G üstündesin" in s for s in r.texts())

    # flare 30 -> 8 -> 0
    tr.feed(st(t + 0.1, flare=8, fuel_kg=fuel, x=x))
    tr.feed(st(t + 0.2, flare=0, fuel_kg=fuel, x=x))
    assert "Flare azaldı, 8 kaldı." in r.texts() and "Flare bitti." in r.texts()

    # approach: first 'fast' call, then 'on speed', then gear down
    r.said.clear()
    for agl, aoa in [(500, 7), (450, 7), (400, 13), (350, 13), (250, 13), (150, 13), (80, 13),
                     (40, 12.5), (10, 13), (2, 13), (0.3, 13)]:
        t += 2
        tr.feed(st(t, gear=1.0, ias=88 if aoa < 10 else 75, agl=agl, aoa=aoa, vv=-3.5 if agl > 2 else -1.2,
                   fuel_kg=fuel, x=0, flare=0))
    assert r.texts()[:2] == ["Hızlısın.", "On speed, iyi."]
    # coming to a stop
    for _ in range(7):
        t += 1
        tr.feed(st(t, gear=1.0, ias=10, agl=0.2, vv=0, fuel_kg=fuel, x=0, flare=0))
    assert len(r.debriefs) == 1 and c.phase == "ground"
    d = r.debriefs[0]
    assert d.max_g >= 8.2 and d.flare_used == 30 and d.approach_on_speed_pct == 100
    assert d.touchdown_vv_ms == pytest.approx(-1.2)
    summary = d.summary()
    assert "8.2 G" in summary and "on speed" in summary and "yumuşaktı" in summary and "Nabzın ortalama 110" in summary
    assert r.said[-1][0] == summary


def test_high_heart_rate_calms_voice():
    c, tr, r = make(hr=150)
    tr.feed(st(0, ias=80, agl=30))
    fuel = 900.0
    for i in range(1, 80):
        fuel -= 1
        tr.feed(st(i, fuel_kg=fuel, burn_kgs=1.0, x=i * 2000))
    assert r.said and all(e == "calm" for _, _, e in r.said)


def test_restricted_and_session_end():
    c, tr, r = make()
    c.restricted()
    c.restricted()
    assert len(r.said) == 1 and "kontrol listeleri" in r.said[0][0]
    tr.feed(st(0, ias=80, agl=30))
    for i in range(1, 90):
        tr.feed(st(i, fuel_kg=3000 - i))
    c.session_end()
    assert len(r.debriefs) == 1 and r.debriefs[0].touchdown_vv_ms is None


def test_save_debrief_html(tmp_path):
    c, tr, r = make()
    tr.feed(st(0, ias=80, agl=30))
    for i in range(1, 120):
        tr.feed(st(i, alt=3000 + i * 10, g=1 + (i % 5)))
    c.session_end()
    page = save_debrief(r.debriefs[0], [(1_000_000 + i, 90 + i % 20) for i in range(0, 120, 5)], out=tmp_path)
    html = page.read_text(encoding="utf-8")
    assert "<svg" in html and "Nabız" in html and "İrtifa" in html
    assert json.loads(page.with_suffix(".json").read_text(encoding="utf-8"))["aircraft"] == "F-16C"


# ------------------------------------------------------------------- DCS
def test_to_state_fuel_fraction_and_kg():
    s = to_state({"aircraft": "F-16C_50", "fuel_int": 0.5, "fuel_ext": 0, "ias": 100, "chaff": 60.0})
    assert s.fuel_kg == pytest.approx(3249 * 0.5) and s.chaff == 60
    assert to_state({"aircraft": "X", "fuel_int": 2500.0}).fuel_kg == 2500


def test_dcs_listener_udp():
    got, restricted, ended, metrics = [], [], [], []
    lis = DCSListener(got.append, lambda: restricted.append(1), lambda: ended.append(1), metrics.extend, port=0).start()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        port = lis.bound_port
        for msg in ({"event": "start"}, {"t": 1, "aircraft": "F-16C_50", "ias": 120, "fuel_int": 0.4},
                    {"t": 2, "restricted": True}, b"bozuk", {"event": "stop"}):
            tx.sendto(msg if isinstance(msg, bytes) else json.dumps(msg).encode(), ("127.0.0.1", port))
        for _ in range(100):
            if ended:
                break
            time.sleep(0.02)
    finally:
        tx.close()
        lis.stop()
    assert len(got) == 1 and got[0].ias == 120 and restricted and ended
    assert ("dcs.ias_kt", pytest.approx(120 / KT), None) in metrics


def test_install_is_idempotent_and_keeps_other_exports(tmp_path):
    d = tmp_path / "DCS"
    (d / "Scripts").mkdir(parents=True)
    (d / "Scripts" / "Export.lua").write_text("dofile(lfs.writedir()..'Scripts/TacviewGameExport.lua')\n", encoding="utf-8")
    install([d], port=45000)
    install([d], port=45000)
    text = (d / "Scripts" / "Export.lua").read_text(encoding="utf-8")
    assert text.count("alice_export.lua") == 1 and "Tacview" in text
    assert '"127.0.0.1", 45000, 10' in (d / "Scripts" / "alice_export.lua").read_text(encoding="utf-8")
    uninstall([d])
    assert "alice" not in (d / "Scripts" / "Export.lua").read_text(encoding="utf-8") and "Tacview" in (d / "Scripts" / "Export.lua").read_text(encoding="utf-8")


MISSION = r'''
mission =
{
    ["theatre"] = "Caucasus",
    ["descriptionText"] = "DictKey_descriptionText_1",
    ["coalition"] =
    {
        ["blue"] =
        {
            ["country"] =
            {
                [1] =
                {
                    ["plane"] =
                    {
                        ["group"] =
                        {
                            [1] =
                            {
                                ["name"] = "Viper-1",
                                ["frequency"] = 305.5,
                                ["route"] = {
                                    ["points"] = {
                                        [1] = { ["x"] = 0, ["y"] = 0, ["alt"] = 15.24, ["name"] = "", ["type"] = "TakeOff", },
                                        [2] = { ["x"] = 18520, ["y"] = 0, ["alt"] = 6096, ["name"] = "DictKey_WptName_2", ["speed"] = 257.2, },
                                        [3] = { ["x"] = 18520, ["y"] = 18520, ["alt"] = 7620, ["name"] = "HEDEF", }, -- end of [3]
                                    },
                                },
                                ["units"] = {
                                    [1] = { ["type"] = "F-16C_50", ["skill"] = "Client", ["name"] = "Viper-1-1", },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
} -- end of mission
'''
DICT = 'dictionary = { ["DictKey_descriptionText_1"] = "Köprüyü vur.\\nSAM tehdidi var.", ["DictKey_WptName_2"] = "IP", }'


@pytest.fixture
def miz(tmp_path):
    p = tmp_path / "Gorev.miz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mission", MISSION)
        z.writestr("l10n/DEFAULT/dictionary", DICT)
    return p


def test_lua_parser_basics():
    t = parse_lua_table('x = { [1] = "a\\"b", ["k"] = -1.5e2, flag = true, nested = { 1, 2, }, ["n"] = nil, }')
    assert t == {1: 'a"b', "k": -150.0, "flag": True, "nested": {1: 1, 2: 2}, "n": None}


def test_read_mission(miz):
    m = read_mission(miz)
    assert (m.aircraft, m.group, m.frequency_mhz) == ("F-16C_50", "Viper-1", 305.5)
    assert [w.name for w in m.waypoints] == ["Kalkış", "IP", "HEDEF"]
    legs = m.legs()
    assert legs[1][1] == pytest.approx(10.0) and legs[1][2] == pytest.approx(0.0)
    assert legs[2][2] == pytest.approx(90.0) and "Köprüyü vur" in m.description
    assert m.waypoints[2].alt_ft == pytest.approx(25000)


def test_kneeboard_pages(miz, tmp_path, isolated_home):
    kneeboard.add_note("Tanker 251.0, TACAN 50X")
    pages = kneeboard.render(read_mission(miz), kneeboard.load_notes())
    assert len(pages) == 3 and pages[0].size == (768, 1024)
    out = kneeboard.write(pages, [tmp_path / "Kneeboard"])
    assert [p.name for p in out] == ["ALICE_01.png", "ALICE_02.png", "ALICE_03.png"]
    out = kneeboard.write(pages[:1], [tmp_path / "Kneeboard"])
    assert sorted(p.name for p in (tmp_path / "Kneeboard").iterdir()) == ["ALICE_01.png"]


# --------------------------------------------------------------- checklists
def test_checklists(isolated_home):
    assert checklists.find("iniş kontrol listesi").key == "inis"
    assert checklists.find("iniş sonrası").key == "inis-sonrasi"
    assert checklists.find("kalkış").key == "kalkis"
    run = checklists.Runner(checklists.find("iniş"))
    assert run.current().startswith("Adım 1/8: Master Arm")
    for _ in range(8):
        run.next()
    assert run.done and "tamam" in run.current()
    (isolated_home / "checklists").mkdir(parents=True)
    (isolated_home / "checklists" / "inis.md").write_text("# Benim inişim\n- Bir\n- İki\n", encoding="utf-8")
    assert checklists.find("iniş").steps == ["Bir", "İki"]


def test_checklist_tool(tmp_path):
    from alice.config import Config, default_config_dict
    from alice.tools import ToolContext
    from alice.tools.flight import Flight

    ctx = ToolContext(cwd=tmp_path, config=Config(default_config_dict()))
    tool = Flight()
    assert "Adım 1/" in tool.run(ctx, action="checklist_start", name="kalkış").text
    assert "Adım 2/" in tool.run(ctx, action="checklist_next").text
    assert "Adım 1/" in tool.run(ctx, action="checklist_prev").text
    assert tool.risk_for({"action": "kneeboard_note"}) == "write" and tool.risk_for({"action": "status"}) == "read"
    assert "çalışmıyor" in tool.run(ctx, action="status").text


# ------------------------------------------------------------- War Thunder
def test_warthunder_poller():
    def handler(req):
        if req.url.path == "/state":
            return httpx.Response(200, json={"valid": True, "IAS, km/h": 540, "TAS, km/h": 600, "H, m": 2000,
                                             "Ny": 3.5, "AoA, deg": 6.0, "Vy, m/s": -2, "Mfuel, kg": 900,
                                             "gear, %": 0})
        return httpx.Response(200, json={"valid": True, "type": "f_16a"})

    got, metrics = [], []
    p = WarThunderPoller(got.append, on_metrics=metrics.extend,
                         client=httpx.Client(base_url="http://wt", transport=httpx.MockTransport(handler)))
    s = p.poll_once()
    assert s.ias == pytest.approx(150) and s.fuel_kg == 900 and s.aircraft == "f_16a" and s.gear == 0
    assert any(k == "wt.g" for k, _, _ in metrics)

    ended = []
    down = WarThunderPoller(got.append, on_end=lambda: ended.append(1), client=httpx.Client(
        base_url="http://wt", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"valid": False}))))
    down.active = True
    assert down.poll_once() is None and ended


def test_export_lua_runs_in_luajit():
    """Run the script in LuaJIT, the runtime DCS uses, with fake DCS functions (if lupa is installed)."""
    lupa = pytest.importorskip("lupa.luajit21")
    from alice.sims.dcs import SCRIPT

    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    sent = []
    lua.globals().py_send = sent.append
    lua.execute('''
      package.preload["socket"] = function() return { udp = function() return {
        settimeout = function() end, setpeername = function() end, close = function() end,
        send = function(self, s) py_send(s) end } end } end
      T = 0
      function LoGetModelTime() return T end
      SELF = { Name = "F-16C_50", LatLongAlt = {Alt = 1500}, Position = {x = 1, z = 2} }
      function LoGetSelfData() return SELF end
      function LoGetIndicatedAirSpeed() return 150 end
      function LoGetEngineInfo() return {fuel_internal = 0.5, FuelConsumption = {left = 0.8, right = 0}} end
      function LoGetSnares() return {chaff = 60, flare = 30} end
      LoGetPilotName = function() error("yok") end
      CALLS = 0
      LuaExportAfterNextFrame = function() CALLS = CALLS + 1 end
    ''')
    lua.execute(SCRIPT.read_text(encoding="utf-8"))
    g = lua.globals()
    g.LuaExportStart()
    for i in range(10):
        g.T = i * 0.05
        g.LuaExportAfterNextFrame()
    lua.execute("SELF = nil")
    g.T = 5
    g.LuaExportAfterNextFrame()
    g.LuaExportStop()
    msgs = [json.loads(s) for s in sent]
    assert msgs[0] == {"event": "start"} and msgs[-1] == {"event": "stop"}
    assert g.CALLS == 11  # the previous export script keeps running too
    data = [m for m in msgs if "aircraft" in m]
    assert len(data) == 5 and data[0]["burn"] == 0.8 and data[0]["chaff"] == 60
    assert msgs[-2] == {"t": 5, "restricted": True}


def test_dcs_ensure_installed_only_when_missing_or_changed(tmp_path):
    from alice.sims import dcs

    d = tmp_path / "DCS"
    d.mkdir()
    assert dcs.ensure_installed([d]) == [d]
    assert "alice_export.lua" in (d / "Scripts" / "Export.lua").read_text(encoding="utf-8")
    assert dcs.ensure_installed([d]) == []  # already installed
    (d / "Scripts" / "alice_export.lua").write_text("eski", encoding="utf-8")
    assert dcs.ensure_installed([d]) == [d]  # script changed: reinstall
    assert (d / "Scripts" / "Export.lua").read_text(encoding="utf-8").count("alice_export.lua") == 1


def test_checklist_after_takeoff_exists():
    from alice.sims.checklists import find

    cl = find("kalkış sonrası")
    assert cl is not None and cl.key == "kalkis-sonrasi"


def test_home_nav_bearing_turn_and_alert():
    from alice.sims.flight import NM, FlightCompanion, FlightState

    said = []
    c = FlightCompanion(lambda text, sev="info", emo=None: said.append(text), lambda a, b: None, lambda: None,
                        lambda d: None)
    c.home = (0.0, 0.0)
    # we are 20 nm southwest of home (x north, z east), nose at 212°: home is northeast (~045°)
    s = FlightState(t=1, x=-20 * NM * 0.7071, z=-20 * NM * 0.7071, heading=212)
    nav = c.home_nav(s)
    assert round(nav["bearing"]) == 45 and round(nav["nm"]) == 20
    assert round(nav["turn"]) == -167  # 167° to the left (shortest turn)
    c.home_alert_nm = 10
    c._home_alert(s)
    assert said == []
    c._home_alert(FlightState(t=2, x=-9 * NM, z=0, heading=0))
    assert said == ["Üsse 9 mil kaldı."] and c.home_alert_nm is None


def test_dcs_extra_fields_described():
    import math

    from alice.sims.dcs import to_state
    from alice.sims.flight import NM, describe_extra

    s = to_state({"t": 1, "aircraft": "F-16C_50", "ias": 150, "alt": 3000, "x": 0, "z": 0, "heading": 0,
                  "mach": 0.62, "pitch": math.radians(5), "bank": math.radians(-20), "mag_heading": math.radians(6),
                  "radalt": 914.4, "rpm_l": 92, "egt_l": 640, "wind_x": -10, "wind_z": 0, "wp_x": 10 * NM, "wp_z": 0,
                  "wp_num": 3, "weapons": "AIM-120C x2, AIM-9X x2", "gun": 510, "warnings": "LeftEngineFailure",
                  "gear": 0, "flaps": 0})
    text = "\n".join(describe_extra(s))
    assert "Mach 0.62" in text and "manyetik yön 006°" in text and "yatış -20°" in text
    assert "radar irtifa 3,000 ft" in text and "devir %92" in text
    assert "rüzgar 000° yönünden 19 kt" in text  # wind from the north pushes south
    assert "sıradaki waypoint 3: 000° (gerçek), 10.0 nm" in text
    assert "AIM-120C x2" in text and "top mermisi 510" in text and "UYARI / ARIZA: LeftEngineFailure" in text


def test_warthunder_extra_fields():
    from alice.sims.flight import describe_extra
    from alice.sims.warthunder import to_state

    s = to_state({"valid": True, "IAS, km/h": 500, "M": 0.45, "throttle 1, %": 100, "RPM throttle 1, %": 95,
                  "oil temp 1, C": 80, "Mfuel, kg": 300, "Mfuel0, kg": 600, "flaps, %": 20},
                 {"type": "spitfire", "compass": 270, "aviahorizon_roll": 10, "aviahorizon_pitch": 2}, 1.0)
    text = "\n".join(describe_extra(s))
    assert s.heading == 270 and "Mach 0.45" in text and "gaz %100" in text and "yağ 80°C" in text
    assert "yakıt %50" in text and "flap %20" in text


def test_proactive_callouts():
    from alice.sims.flight import NM, FlightCompanion, FlightState, describe_extra

    said = []
    c = FlightCompanion(lambda text, sev="info", emo=None: said.append(text))
    c.phase = "air"

    def st(t, **kw):
        base = dict(t=t, ias=120, agl=2000, gear=0, x=0, z=0)
        base.update(kw)
        return FlightState(**base)

    c._warnings(st(1, extra={"warnings": "LeftEngineFailure"}))
    c._warnings(st(2, extra={"warnings": "LeftEngineFailure"}))  # same light is not called out again
    assert said == ["Uyarı: sol motor arızası."]
    c._engine_heat(st(3, extra={"oil_temp": 108}))
    assert "Yağ sıcaklığı 108" in said[-1]
    c._gear_check(st(4, ias=70, agl=150, vv=-4, gear=0))
    assert said[-1] == "Teker kapalı! Alçak ve yavaşsın."
    c._waypoint(st(5, extra={"wp_x": 1.5 * NM, "wp_z": 0, "wp_num": 3}))
    c._waypoint(st(6, extra={"wp_x": 1.4 * NM, "wp_z": 0, "wp_num": 3}))
    assert said[-1] == "3 numaralı waypoint'e 2 mil." and said.count("3 numaralı waypoint'e 2 mil.") == 1
    c._weapons(st(30, extra={"weapons": "AIM-120C x2, AIM-9X x2", "gun": 500}))
    c._weapons(st(31, extra={"weapons": "AIM-120C x1, AIM-9X x2", "gun": 500}))
    c._weapons(st(50, extra={"weapons": "AIM-120C x1, AIM-9X x2", "gun": 90}))
    assert "Son AIM-120C." in said and "Top mermisi azaldı, 90 kaldı." in said
    text = "\n".join(describe_extra(st(9, extra={"radios": "251.000,127.500"})))
    assert "UHF 251.000" in text and "VHF 127.500" in text


def test_approach_calls_are_calm_and_not_contradictory():
    c, tr, r = make()
    c.phase = "air"
    c.profile = c.profile.__class__("F-16C", aoa_on_speed=(11.0, 15.0), approach_ias_max=95)
    t = 0.0
    # oscillating at the edge of the band (10-16 degrees): nothing is said
    for aoa in [10, 16, 10.5, 15.8, 10.2, 16.5] * 3:
        t += 0.5
        c._approach(st(t, gear=1.0, ias=75, agl=400, aoa=aoa, vv=-3))
    assert r.texts() == []
    # sustained approach to stall (18 degrees): "yavaşsın" once, no repeat before 10 s
    for _ in range(12):
        t += 0.5
        c._approach(st(t, gear=1.0, ias=60, agl=400, aoa=18, vv=-3))
    assert r.texts() == ["Yavaşsın, stall'a yaklaşıyorsun, güç ver."]
    # corrected: "on speed" once
    for _ in range(8):
        t += 0.5
        c._approach(st(t, gear=1.0, ias=70, agl=400, aoa=13, vv=-3))
    assert r.texts()[-1] == "On speed, iyi." and r.texts().count("On speed, iyi.") == 1
    # low AoA but speed far from the approach limit: no "hızlısın"
    for _ in range(8):
        t += 0.5
        c._approach(st(t, gear=1.0, ias=70, agl=400, aoa=7, vv=-3))
    assert "Hızlısın." not in r.texts()


def test_pilot_alerts_altitude_and_fuel():
    c, tr, r = make()
    c.phase = "air"
    c.alt_alert_ft = 10000
    c._pilot_alerts(st(1, alt=8000 * 0.3048))
    c._pilot_alerts(st(2, alt=9990 * 0.3048))
    assert r.texts() == []
    c._pilot_alerts(st(3, alt=10010 * 0.3048))
    c._pilot_alerts(st(4, alt=9000 * 0.3048))
    c._pilot_alerts(st(5, alt=11000 * 0.3048))
    assert r.texts() == ["10.000 feet."]  # once
    c.fuel_alert_kg = 1500
    c._pilot_alerts(st(6, fuel_kg=1600))
    c._pilot_alerts(st(7, fuel_kg=1499))
    c._pilot_alerts(st(8, fuel_kg=1400))
    assert r.texts()[1:] == ["Yakıt 1500 kiloya indi."]


def test_hub_alert_setup_and_clear():
    from alice.sims.hub import SimHub

    hub = SimHub({}, None, None)
    assert "uçuşta değilsin" in hub.alert_altitude(5000)
    c = hub.companion("dcs")
    c.phase, c.last_state = "air", st(1, fuel_kg=2000)
    assert hub.alert_altitude(5000).startswith("kuruldu: 5.000")
    assert "zaten" in hub.alert_fuel(2500)
    assert hub.alert_fuel(1000).startswith("kuruldu")
    assert hub.clear_alerts() == "2 uyarı kaldırıldı"
    assert c.alt_alert_ft is None and c.fuel_alert_kg is None


def test_ground_proximity_only_without_own_gpws():
    c, tr, r = make()
    from alice.sims.flight import profile_for

    c.phase, c.profile = "air", profile_for("F-16C_50")
    c._ground(st(1, agl=200, vv=-50))  # F-16: has its own warning
    assert r.texts() == []
    c.profile = c.profile.__class__("uçak")
    c._ground(st(2, aircraft="P-51D", agl=200, vv=-50))
    c._ground(st(3, aircraft="P-51D", agl=150, vv=-50))  # too early to repeat
    assert r.texts() == ["Yere yaklaşıyorsun, burnu kaldır!"]
    c._ground(st(20, aircraft="P-51D", agl=500, vv=-20))  # 25 s left: silent
    c._ground(st(30, aircraft="P-51D", agl=100, vv=-10, gear=1.0, ias=70))  # approaching landing: silent
    assert len(r.texts()) == 1


def test_ground_warning_rearms_only_after_recovery():
    """Not repeated back to back while dogfighting low; repeats after returning to safety and diving again."""
    c, tr, r = make()
    c.phase, c.profile = "air", profile_for("P-51D")
    c.profile = c.profile.__class__("uçak")  # no GPWS of its own
    warn = "Yere yaklaşıyorsun, burnu kaldır!"
    c._ground(st(0, agl=200, vv=-50))
    for t in range(1, 60):  # a minute low the whole time, diving and climbing while fighting: brief recoveries
        dive = t % 4 != 0
        c._ground(st(t, agl=150 if dive else 250, vv=-40 if dive else 5))
    assert r.texts() == [warn]
    for t in range(60, 64):  # safe for more than 3 s: climbed
        c._ground(st(t, agl=500, vv=10))
    c._ground(st(64, agl=200, vv=-50))
    assert r.texts() == [warn, warn]
    # even with a safe return, no repeat before GROUND_REPEAT has passed
    for t in (65, 66, 67, 68):
        c._ground(st(t, agl=200, vv=0))
    c._ground(st(69, agl=200, vv=-50))
    assert r.texts() == [warn, warn]
    c._ground(st(72.5, agl=200, vv=-50))
    assert r.texts() == [warn, warn, warn]


def test_flaps_forgotten_and_external_tanks():
    c, tr, r = make()
    c.phase = "air"
    c.profile = c.profile.__class__("uçak")
    for t in range(0, 30, 2):
        c._flaps(st(t, extra={"flaps": 0.5}, ias=180, agl=2000))
    assert r.texts() == ["Flaplar hâlâ açık, toplamayı unutma."]
    for kg in (300, 100, 40, 10, 0):
        c._ext_tanks(st(40, extra={"fuel_ext_kg": kg}))
    c._ext_tanks(st(41, extra={"fuel_ext_kg": 0}))
    assert r.texts()[1:] == ["Dış tanklar boşaldı."]
    # F-16: automatic flaps, no reminder
    c2, _, r2 = make()
    c2.phase, c2.profile = "air", c2.profile.__class__("F-16C", auto_flaps=True)
    for t in range(0, 30, 2):
        c2._flaps(st(t, extra={"flaps": 0.5}, ias=180, agl=2000))
    assert r2.texts() == []


def test_new_checklists_found():
    for q, key in (("savaş girişi", "savas-girisi"), ("fence out", "savas-cikisi"),
                   ("motor çalıştırma", "motor-calistirma"), ("motor arızası", "motor-arizasi")):
        assert checklists.find(q).key == key


def test_flight_tool_alert_actions(monkeypatch):
    from alice.sims import hub as hubmod
    from alice.sims.hub import SimHub
    from alice.tools.flight import Flight

    h = SimHub({}, None, None)
    c = h.companion("dcs")
    c.phase, c.last_state = "air", st(1)
    monkeypatch.setattr(hubmod, "ACTIVE", h)
    tool = Flight()
    assert tool.run(None, "alert_alt", ft=20000).text.startswith("kuruldu")
    assert c.alt_alert_ft == 20000
    assert tool.run(None, "alerts_clear").text == "1 uyarı kaldırıldı"


def test_afterburner_does_not_trigger_fuel_calls():
    """Full fuel with afterburner: no joker/bingo/"X dakika" calls, even though the game's instant burn is 12 kg/s."""
    c, tr, r = make()
    tr.feed(st(0, ias=80, agl=30))
    fuel = 2500.0
    for i in range(1, 200):
        fuel -= 3.0
        tr.feed(st(i, fuel_kg=fuel, burn_kgs=12.0, x=20 * NM))
    assert r.texts() == []


def test_tank_transfer_jumps_do_not_repeat_joker():
    c, tr, r = make()
    tr.feed(st(0, ias=80, agl=30))
    fuel, t = 1300.0, 0  # 80 nm: bingo 770, joker 1257 kg
    for _ in range(70):
        t += 1
        fuel -= 1
        tr.feed(st(t, fuel_kg=fuel, x=80 * NM))
    assert [x for x in r.texts() if x.startswith("Joker")] and len(r.said) == 1
    for jump in (200, 0, 180, 0):  # transfer from external tanks makes the gauge jump
        for _ in range(15):
            t += 1
            fuel -= 1
            tr.feed(st(t, fuel_kg=fuel + jump, x=80 * NM))
    assert len(r.said) == 1


def test_low_fuel_load_warthunder_style():
    """Takeoff point unknown (War Thunder) and took off with 12 minutes of fuel: no 10 min warning, 5 min once."""
    c, tr, r = make()
    tr.feed(st(0, ias=80, agl=30, x=None, z=None))
    fuel = 720.0  # 12 minutes at 1 kg/s
    for i in range(1, 700):
        fuel -= 1.0
        tr.feed(st(i, fuel_kg=fuel, burn_kgs=None, x=None, z=None))
    texts = r.texts()
    assert len(texts) == 1 and texts[0].startswith("Yakıt yaklaşık 5 dakika") and "tüketim" not in texts[0]
    assert r.said[0][1] == "critical"


def test_endurance_warning_holds_before_speaking():
    """On a long flight the 10 and 5 minute warnings each come once, after the threshold has held for 20 s."""
    c, tr, r = make()
    tr.feed(st(0, ias=80, agl=30, x=None, z=None))
    fuel = 2400.0
    for i in range(1, 2400):
        fuel -= 1.0
        tr.feed(st(i, fuel_kg=fuel, burn_kgs=None, x=None, z=None))
        if fuel <= 150:
            break
    assert r.texts() == ["Yakıt yaklaşık 10 dakika yeter.", "Yakıt yaklaşık 5 dakika yeter."]


def test_noisy_lights_and_info_gap():
    from alice.sims.flight import FlightCompanion, FlightState

    said = []
    c = FlightCompanion(lambda text, sev="info", emo=None: said.append(text))
    c.phase = "air"
    for t, lights in ((1, "AutopilotOn"), (2, "StallSignalization"), (3, ""), (4, "HydraulicsFailure,MasterWarning"),
                      (5, ""), (6, "HydraulicsFailure"), (200, ""), (201, "HydraulicsFailure")):
        c._warnings(FlightState(t=t, extra={"warnings": lights}))
    # autopilot is not a warning; the master warning is not called out together with a failure;
    # a flashing light is not repeated before 2 min have passed
    assert said == ["Uyarı: stall.", "Uyarı: hidrolik arızası.", "Uyarı: hidrolik arızası."]
    said.clear()
    c.profile = profile_for("F-16C_50")
    c.warn_seen = set()  # F-16 has its own stall warning
    c._warnings(FlightState(t=300, extra={"warnings": "StallSignalization"}))
    assert said == []
    c._ext_tanks(FlightState(t=400, extra={"fuel_ext_kg": 30}))
    c._waypoint(FlightState(t=401, x=0, z=0, extra={"wp_x": NM, "wp_z": 0, "wp_num": 2}))
    c._ext_tanks(FlightState(t=402, extra={"fuel_ext_kg": 0}))
    assert said == ["2 numaralı waypoint'e 1 mil."]  # the tank call did not overlap


def test_dcs_fraction_fuel_unknown_aircraft_is_guess():
    assert to_state({"aircraft": "Su-25T", "fuel_int": 0.5}).extra.get("fuel_guess") is True
    assert "fuel_guess" not in to_state({"aircraft": "F-16C_50", "fuel_int": 0.5}).extra
    assert "fuel_guess" not in to_state({"aircraft": "Su-25T", "fuel_int": 2500.0}).extra


def test_cruise_fuel_is_learned_from_flights(tmp_path, monkeypatch):
    import json

    from alice.sims import flight

    def series(n, burn, ias=400, alt=20000, g=1.0):
        return {"t": [float(i) for i in range(n)], "alt_ft": [alt] * n, "g": [g] * n, "ias_kt": [ias] * n,
                "fuel_kg": [4000 - burn * i for i in range(n)]}

    s = series(200, 1.0)
    # 400 kt IAS at 20000 ft is ~560 kt TAS: 1 kg per second -> ~6.4 kg/nm
    assert [round(x, 1) for x in flight.cruise_parts(s)] == [6.4]
    dog = series(200, 1.0, g=4.0)  # dogfight segment is not counted
    assert flight.cruise_parts(dog) == []
    jump = series(200, 1.0)
    jump["fuel_kg"][100] -= 900  # dropping a tank splits the segment, does not skew the value
    assert all(6 < x < 7 for x in flight.cruise_parts(jump))
    for i in range(3):
        (tmp_path / f"2026{i}.json").write_text(json.dumps({"aircraft": "F-16C", "series": s}), encoding="utf-8")
    monkeypatch.setattr(flight, "flights_dir", lambda: tmp_path)
    assert flight.learn_cruise()["F-16C"]["parts"] == 3
    assert round(flight.profile_for("F-16C_50").cruise_kg_nm, 1) == 6.4  # instead of the manually given 4.0
    assert flight.profile_for("FA-18C_hornet").cruise_kg_nm == 6.0  # an unlearned aircraft stays the same


def test_coach_compares_with_previous_flights(tmp_path):
    import json

    from alice.sims.flight import Debrief, coach, load_history

    def deb(**k):
        base = dict(aircraft="F-16C", source="dcs", start_wall=1000.0, end_wall=2000.0, duration_s=1200.0, max_g=5.0,
                    max_ias_kt=500.0, max_alt_ft=20000.0, fuel_used_kg=2400.0, min_fuel_kg=900.0,
                    approach_on_speed_pct=60.0, max_sink_final_ms=None, touchdown_vv_ms=-2.0, chaff_used=None,
                    flare_used=None)
        return Debrief(**{**base, **k})

    hist = [dict(aircraft="F-16C", duration_s=1200.0, fuel_used_kg=2400.0, touchdown_vv_ms=-v,
                 approach_on_speed_pct=60.0) for v in (2.0, 2.4, 1.9, 2.1)]
    assert coach(deb(touchdown_vv_ms=-1.2), hist).startswith("Son 4 inişinin en yumuşağıydı")
    assert "alçalmayı" in coach(deb(touchdown_vv_ms=-3.4), hist)
    assert "yüzde 60 yerine 30" in coach(deb(approach_on_speed_pct=30.0), hist)
    assert "en uzun uçuşundu" in coach(deb(duration_s=3000.0), hist)
    assert coach(deb(), hist[:2]) == ""  # little history: stay silent
    (tmp_path / "20261001-1000.json").write_text(json.dumps({"start_wall": 1000.0, "series": {"t": [1]}}), "utf-8")
    (tmp_path / "20261002-1000.json").write_text(json.dumps({"start_wall": 5.0, "series": {"t": [1]}}), "utf-8")
    assert load_history(tmp_path, exclude=1000.0) == [{"start_wall": 5.0}]  # excluding the flight recorded just before


def test_rwr_and_minimap_contacts_parse():
    from alice.sims.dcs import clock_of, parse_rwr
    from alice.sims.warthunder import contacts

    assert [clock_of(a) for a in (0, 29, 91, 180, -60, 350)] == [12, 1, 3, 6, 10, 12]
    cs = parse_rwr("101|1.571|lock|SA-6 Straight Flush|0.8;102|-3.1416|scan|MiG-29|0.2;bad")
    assert [(c["clock"], c["name"], c["lock"], c["missile"]) for c in cs] == [
        (3, "SA-6 Straight Flush", True, False), (6, "MiG-29", False, False)]
    info = {"map_min": [0, 0], "map_max": [10000, 10000], "valid": True}
    objs = [{"type": "aircraft", "icon": "Player", "color": "#faC81E", "x": 0.5, "y": 0.5, "dx": 0.0, "dy": -1.0},
            {"type": "aircraft", "icon": "Fighter", "color": "#f40C00", "x": 0.5, "y": 0.6},   # 1 km south: clock 6
            {"type": "aircraft", "icon": "Fighter", "color": "#174DFF", "x": 0.6, "y": 0.5},   # friendly
            {"type": "aircraft", "icon": "Bomber", "color": "#f40C00", "x": 0.8, "y": 0.5},    # 3 km east: clock 3
            {"type": "ground_model", "icon": "Tank", "color": "#f40C00", "x": 0.5, "y": 0.4}]
    got = [(c["clock"], round(c["dist_m"]), c["name"]) for c in contacts(objs, info)]
    assert got == [(6, 1000, "Fighter"), (3, 3000, "Bomber")]


def test_radar_without_type_or_direction_says_nothing_empty():
    # Radar seen on screen: it said "Yeni radar: radar, saat None." (6 times in two days).
    c, tr, r = make()
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    base = {"lock": False, "missile": False, "dist_m": None, "kind": "radar"}
    tr.feed(st(2, extra={"contacts": [{**base, "id": "v", "clock": None, "name": "radar"}]}))
    assert not any("Yeni radar" in t for t in r.texts())
    tr.feed(st(40, extra={"contacts": [{**base, "id": "s", "clock": None, "name": "SA-6"}]}))
    assert "Yeni radar: SA-6." in r.texts() and not any("None" in t for t in r.texts())


def test_threat_callouts_once_and_six_warning():
    c, tr, r = make()
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    radar = {"id": "7", "clock": 2, "name": "SA-6", "lock": False, "missile": False, "dist_m": None, "kind": "radar"}
    tr.feed(st(2, extra={"contacts": [radar]}))
    tr.feed(st(3, extra={"contacts": [radar]}))
    tr.feed(st(4, extra={"contacts": [{**radar, "lock": True}]}))
    tr.feed(st(5, extra={"contacts": [{**radar, "lock": True}]}))
    tr.feed(st(6, extra={"contacts": [{**radar, "lock": True, "missile": True}]}))
    said = r.texts()
    assert said.count("Yeni radar: SA-6, saat 2.") == 1 and said.count("Saat 2'den kilit: SA-6.") == 1
    assert "Füze! Saat 2, SA-6. Chaff at, dön!" in said
    bandit = {"id": "", "clock": 6, "dist_m": 1200.0, "kind": "aircraft", "name": "Fighter", "lock": False,
              "missile": False}
    for t in (30, 31, 45, 52, 76):
        tr.feed(st(t, extra={"contacts": [bandit]}))
    six = [x for x in r.texts() if x.startswith("Arkanda")]
    assert six == ["Arkanda düşman, saat 6, 1,2 kilometre!"] * 2  # once every 45 s
    far = {**bandit, "clock": 1, "dist_m": 4000.0}
    tr.feed(st(100, extra={"contacts": [{**far, "dist_m": 9000.0}]}))
    tr.feed(st(101, extra={"contacts": [far]}))
    tr.feed(st(119, extra={"contacts": [far]}))
    assert r.texts().count("Düşman saat 1, 4,0 kilometre.") == 1


def test_warthunder_quiet_g_and_no_debrief_speech_on_death():
    c, tr, r = make()
    wt = dict(source="warthunder", aircraft="su_30mkk", x=None, z=None)
    tr.feed(st(0, ias=0, agl=0.5, alt=50, **wt))
    tr.feed(st(1, ias=80, agl=30, **wt))
    for t in range(2, 200):
        tr.feed(st(t, g=9.5 if t < 150 else (48.0 if t == 160 else 1.0), **wt))  # long pull + spike on death
    assert not any("G üstündesin" in x for x in r.texts())
    c.session_end()  # dead: data cut off
    assert r.debriefs and r.debriefs[0].max_g == 9.5  # the single-sample 48 G spike is not counted
    assert not any(x.startswith("Uçuş ") for x in r.texts())  # 3-minute flight with no landing: no spoken assessment


def test_endurance_warning_waits_until_tank_is_low():
    c, tr, r = make()
    wt = dict(source="warthunder", aircraft="su_30mkk", x=None, z=None, burn_kgs=None)
    tr.feed(st(0, ias=0, agl=0.5, alt=50, fuel_kg=9000, extra={"fuel_pct": 100}, **wt))
    tr.feed(st(1, ias=80, agl=30, fuel_kg=9000, extra={"fuel_pct": 100}, **wt))
    fuel = 9000.0
    for t in range(2, 600):
        fuel -= 12  # afterburner: 720 kg per minute
        tr.feed(st(t, fuel_kg=fuel, extra={"fuel_pct": 100 * fuel / 9400}, **wt))
        if fuel > 0.35 * 9400:
            assert not any("dakika yeter" in x for x in r.texts())  # does not speak while above 35 percent
    assert any("dakika yeter" in x for x in r.texts())  # speaks once the tank runs low


def test_warthunder_ground_threats_and_nose_aspect():
    from alice.sims.warthunder import contacts

    info = {"map_min": [0, 0], "map_max": [100000, 100000], "valid": True}
    objs = [{"type": "aircraft", "icon": "Player", "color": "#faC81E", "x": 0.5, "y": 0.5, "dx": 0.0, "dy": -1.0},
            # 8 km north, nose pointing south (toward us)
            {"type": "aircraft", "icon": "Fighter", "color": "#f00C00", "x": 0.5, "y": 0.42, "dx": 0.0, "dy": 1.0},
            # 5 km east, flying north (from our side)
            {"type": "aircraft", "icon": "Fighter", "color": "#f00C00", "x": 0.55, "y": 0.5, "dx": 0.0, "dy": -1.0},
            {"type": "ground_model", "icon": "SAM", "color": "#fa0C00", "x": 0.5, "y": 0.6},       # 10 km south
            {"type": "ground_model", "icon": "MediumTank", "color": "#fa0C00", "x": 0.5, "y": 0.51},
            {"type": "ground_model", "icon": "SPAA", "color": "#fa0C00", "x": 0.9, "y": 0.5}]      # 40 km: far
    cs = contacts(objs, info)
    assert [(c["kind"], c["clock"], round(c["dist_m"]), c["name"]) for c in cs] == [
        ("aircraft", 3, 5000, "Fighter"), ("aircraft", 12, 8000, "Fighter"), ("sam", 6, 10000, "SAM")]
    assert round(cs[1]["aspect"]) == 0 and round(cs[0]["aspect"]) == 90


def test_warthunder_threat_callouts():
    c, tr, r = make()
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    sam = {"id": "", "clock": 2, "dist_m": 20000.0, "kind": "sam", "name": "SAM", "range_m": 18000.0}
    tr.feed(st(2, extra={"contacts": [sam]}))
    tr.feed(st(3, extra={"contacts": [{**sam, "dist_m": 17000.0}]}))
    tr.feed(st(4, extra={"contacts": [{**sam, "dist_m": 16000.0}]}))
    assert r.texts().count("SAM menzilindesin: saat 2, 17,0 kilometre.") == 1
    hot = {"id": "", "clock": 1, "dist_m": 8000.0, "kind": "aircraft", "name": "Fighter", "aspect": 5.0}
    for t in (10, 11, 12.5, 13, 20):
        tr.feed(st(t, extra={"contacts": [hot]}))
    nose = [x for x in r.texts() if "burnunu" in x]
    assert nose == ["Saat 1'deki düşman burnunu sana çevirdi, 8,0 kilometre. Füze gelebilir!"]
    tr.feed(st(21, extra={"contacts": [], "hud": [("kill", "MiG-29SMT"), ("fire", "Su-30MK")]}))
    assert "Düşürdün: MiG-29SMT." in r.texts() and "Yanıyorsun! Su-30MK vurdu." in r.texts()


def test_warthunder_hud_parse_and_nick_learning():
    from alice.sims.warthunder import hud_events, learn_nick

    msgs = [{"msg": "=TAG= Pilot_One (␗Su-30MKK) destroyed weapons/su_r_77_default/short"},
            {"msg": "=ENM= Enemy_One (▄Su-30MK) set afire =TAG= Pilot_One (␗Su-30MKK)"},
            {"msg": "=ENM= Enemy_One (▄Su-30MK) shot down =TAG= Pilot_One (␗Su-30MKK)"},
            {"msg": "FSAF SAMP/T (TEL) shot down [SQD] Enemy_Two (☢MiG-29SMT)"}]
    assert learn_nick(msgs, "su_30mkk") == "=TAG= Pilot_One"
    assert learn_nick(msgs + [{"msg": "a (F-16C) shot down b (␗Su-30MKK)"}], "su_30mkk") is None  # iki aday
    kill = [{"msg": "=TAG= Pilot_One (␗Su-30MKK) shot down [SQD] Enemy_Two (☢MiG-29SMT)"}]
    assert hud_events(msgs + kill, "=TAG= Pilot_One") == [("fire", "Su-30MK"), ("killed", "Su-30MK"),
                                                            ("kill", "MiG-29SMT")]
    # Yapay zekâ SAM'inde tip "TEL": düşüren olarak sistemin adı söylenir.
    sam = [{"msg": "FSAF SAMP/T (TEL) shot down =TAG= Pilot_One (␗Su-30MKK)"}]
    assert hud_events(sam, "=TAG= Pilot_One") == [("killed", "FSAF SAMP/T")]
    assert hud_events(kill, "") == []


def test_warthunder_crash_is_not_a_landing_and_takeoff_is_not_approach():
    c, tr, r = make()
    wt = dict(source="warthunder", aircraft="su_30mkk", x=None, z=None, agl=None)
    tr.feed(st(0, ias=0, alt=50, gear=1.0, **wt))
    for t in range(1, 20):  # takeoff roll: gear down, accelerating, climbing
        tr.feed(st(t, ias=60 + 3 * t, alt=50 + 10 * t, vv=8.0, aoa=1.0, gear=1.0, **wt))
    assert "Hızlısın." not in r.texts()
    for t in range(20, 200):
        tr.feed(st(t, ias=250, alt=2000, **wt))
    for t in range(200, 210):  # dead: wreckage on the ground, gear up
        tr.feed(st(t, ias=0, alt=40, vv=0.0, **wt))
    assert r.debriefs and not any(x.startswith("Uçuş ") for x in r.texts())


def test_threat_voices_share_a_gap_and_survive_respawn():
    """In a dogfight ALICE kept talking: threat voices share a gap, a ship group is one call, respawns don't recount."""
    c, tr, r = make()
    wt = dict(source="warthunder", aircraft="su_30mkk", x=None, z=None)
    tr.feed(st(0, ias=0, agl=0.5, alt=50, **wt))
    tr.feed(st(1, ias=80, agl=30, **wt))
    ships = [{"id": "", "clock": 11, "dist_m": 7200.0, "kind": "sam", "name": "füzeli muhrip", "range_m": 9000.0},
             {"id": "", "clock": 11, "dist_m": 8200.0, "kind": "sam", "name": "fırkateyn", "range_m": 9000.0}]
    bandit = {"id": "", "clock": 2, "dist_m": 3600.0, "kind": "aircraft", "name": "Fighter"}
    tr.feed(st(2, extra={"contacts": ships + [bandit]}, **wt))
    tr.feed(st(5, extra={"contacts": ships + [{**bandit, "clock": 5, "dist_m": 900.0}]}, **wt))
    said = r.texts()
    assert said == ["Füzeli muhrip menzilindesin: saat 11, 7,2 kilometre."]  # frigate, enemy and the one behind: silent
    tr.feed(st(15, extra={"contacts": [{**bandit, "clock": 6, "dist_m": 900.0}]}, **wt))
    assert r.texts()[-1] == "Arkanda düşman, saat 6, 900 metre!"  # after the 12 s gap
    c.session_end()  # dead, respawned: the same ships are not counted again
    tr.feed(st(60, ias=0, agl=0.5, alt=50, **wt))
    tr.feed(st(61, ias=80, agl=30, **wt))
    n = len(r.texts())
    tr.feed(st(80, extra={"contacts": ships}, **wt))
    assert len(r.texts()) == n


def test_clock_suffix_follows_turkish_vowel_harmony():
    """Live recording: the callouts said "Saat 3'deki", "Saat 9'deki"."""
    from alice.sims.flight import clock_loc

    assert [clock_loc(c) for c in (3, 6, 9, 10, 12)] == ["3'teki", "6'daki", "9'daki", "10'daki", "12'deki"]


def test_flapping_lock_and_missile_are_not_repeated_aloud():
    """In War Thunder a threat read off the screen drops out and returns: don't shout on every return (16 missiles
    in 30 min, 13 lock warnings, 7 November). Voice is limited; the HUD readout continues."""
    from alice.sims.flight import FlightCompanion, FlightState

    said = []
    c = FlightCompanion(lambda text, sev="info", emo=None: said.append(text))
    c.phase = "air"

    def contact(t, **kw):
        c._contacts(FlightState(t=t, ias=200, agl=3000, gear=0, x=0, z=0,
                                extra={"contacts": [{"kind": "radar", "id": 1, "name": "radar", **kw}]}))

    for t in (0, 11, 22):  # a lock counts as "yeni" every 11 s (LOST_AFTER 10 s exceeded)
        contact(t, lock=True)
    assert said.count("Kilitlendin!") == 1
    contact(40, lock=True)
    assert said.count("Kilitlendin!") == 2  # said again 30 s later
    for t in (100, 111, 122):
        contact(t, missile=True, vision=True)
    assert said.count("Füze atıldı! Chaff at, dön!") == 3  # 11 s apart: outside the 8 s limit, all are said
    said.clear()
    for t in (200, 211):
        contact(t, missile=True, vision=True)
    contact(215, missile=True, vision=True, id=2)  # another missile 4 s later: not said
    assert said.count("Füze atıldı! Chaff at, dön!") == 2
