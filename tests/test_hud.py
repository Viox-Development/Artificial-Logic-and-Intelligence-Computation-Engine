from __future__ import annotations

import math
import queue
import socket
import time

import pytest

from alice.hud import client, window
from alice.hud.model import MAX_BYTES, HudModel, clock_of, parse
from alice.hud.supervisor import BURST, HudSupervisor


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- protocol
def test_parse_accepts_known_kinds_and_drops_garbage():
    assert parse(b'{"kind": "arrow", "clock": 4, "ttl": 3}') == {"kind": "arrow", "clock": 4, "ttl": 3}
    assert parse(b'{"kind": "clear"}') == {"kind": "clear"}
    for raw in (b"", b"not json", b"[1, 2]", b'{"kind": "explode"}', b"\xff\xfe", b'{"x": 1}',
                b'{"kind": "label", "text": "' + b"a" * MAX_BYTES + b'"}'):
        assert parse(raw) is None


def test_clock_normalization():
    assert [clock_of(v) for v in (0, 12, 3, "4", 6.4, 13, -1, None, "x")] == [12, 12, 3, 4, 6, None, None, None, None]


def test_invalid_items_are_ignored():
    m = HudModel()
    for msg in ({"kind": "arrow"}, {"kind": "edge", "clock": 15}, {"kind": "box", "x": 1, "y": 2, "w": 3},
                {"kind": "progress", "value": "x"}, {"kind": "label", "text": ""}, {"kind": "card"}):
        assert not m.apply(msg, 0.0)
    assert m.items == [] and m.version == 0


def test_ttl_expiry_and_clamp():
    m = HudModel()
    m.apply({"kind": "arrow", "clock": 4, "ttl": 2}, 10.0)
    m.apply({"kind": "label", "text": "uzun", "ttl": 9999}, 10.0)   # at most 60 s
    m.apply({"kind": "card", "title": "varsayılan"}, 10.0)          # no ttl: 3 s
    assert not m.expire(11.9)
    assert m.expire(12.1) and [i.kind for i in m.items] == ["label", "card"]
    assert m.expire(13.1) and [i.kind for i in m.items] == ["label"]
    assert m.items[0].until == 70.0
    assert m.expire(70.0) and m.items == []


def test_same_id_replaces_and_max_items_drops_oldest_calm_first():
    m = HudModel(max_items=3)
    m.apply({"kind": "arrow", "clock": 2, "id": "six", "ttl": 5}, 0.0)
    m.apply({"kind": "arrow", "clock": 6, "id": "six", "ttl": 5}, 1.0)
    assert len(m.items) == 1 and m.items[0].data["clock"] == 6
    m.apply({"kind": "edge", "clock": 6, "severity": "critical"}, 2.0)
    m.apply({"kind": "label", "text": "a"}, 3.0)
    m.apply({"kind": "label", "text": "b"}, 4.0)
    kinds = [(i.kind, i.data.get("text")) for i in m.items]
    assert kinds == [("edge", None), ("label", "a"), ("label", "b")]  # the oldest calm item (arrow) was dropped
    assert m.items[0].data["severity"] == "critical" and m.items[1].data["severity"] == "info"


def test_clear_and_toggle():
    m = HudModel()
    m.apply({"kind": "arrow", "clock": 1, "id": "a"}, 0.0)
    m.apply({"kind": "arrow", "clock": 2, "id": "b"}, 0.0)
    assert m.apply({"kind": "clear", "id": "a"}, 0.0) and [i.id for i in m.items] == ["b"]
    assert not m.apply({"kind": "clear", "id": "yok"}, 0.0)
    m.apply({"kind": "toggle"}, 0.0)
    assert m.hidden and m.visible() == [] and len(m.items) == 1
    m.apply({"kind": "toggle"}, 0.0)
    assert len(m.visible()) == 1
    assert m.apply({"kind": "clear"}, 0.0) and m.items == []


def test_client_reaches_receiver_over_socket():
    port = free_port()
    sock = window.bind(port)
    assert sock is not None
    assert window.bind(port) is None  # a second HUD cannot take the same port
    inbox: queue.Queue = queue.Queue()
    rx = window.Receiver(sock, inbox).start()
    try:
        client.configure(port)
        assert client.show("arrow", clock=4, text="SAM · 12 km", severity="warn", id="sam")
        client.send({"kind": "bilinmeyen"})  # the receiver ignores this
        assert client.clear("sam")
        got = [inbox.get(timeout=2), inbox.get(timeout=2)]
        assert got[0] == {"kind": "arrow", "ttl": 3.0, "clock": 4, "text": "SAM · 12 km", "severity": "warn",
                          "id": "sam"}
        assert got[1] == {"kind": "clear", "id": "sam"}
        assert inbox.empty()
    finally:
        rx.stop()
        client.configure(window.PORT, on=False)


def test_client_never_blocks_when_hud_is_closed():
    client.configure(free_port())  # nobody is listening
    try:
        t0 = time.monotonic()
        for i in range(200):
            client.show("arrow", clock=i % 12 + 1, ttl=1)
        assert time.monotonic() - t0 < 1.0
        assert client.send({"kind": "arrow", "clock": 1, "bad": object()}) is False  # not JSON-serializable: silent
    finally:
        client.configure(window.PORT, on=False)


def test_client_swallows_socket_errors(monkeypatch):
    class Broken:
        def sendto(self, *a):
            raise ConnectionResetError(10054, "kapalı porta önceki paket")

    monkeypatch.setattr(client, "_sock", Broken())
    assert client.show("label", text="x") is False


# ------------------------------------------------------------------ drawing
class FakeCanvas:
    def __init__(self):
        self.ops: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def rec(*a, **kw):
            self.ops.append((name, a, kw))

        return rec


ALL_KINDS = ({"kind": "arrow", "clock": 3, "text": "SAM", "severity": "warn"},
             {"kind": "edge", "clock": 6, "severity": "critical"},
             {"kind": "label", "text": "merhaba"}, {"kind": "card", "title": "ALICE", "text": "deneme"},
             {"kind": "box", "x": 100, "y": 100, "w": 50, "h": 40, "text": "düğme"},
             {"kind": "progress", "value": 0.5, "text": "yükleniyor"})


def test_everything_goes_into_one_island_by_priority():
    """Apple's single-surface rule: threats, fuel, gauges and cards all go in the Island; the most important
    one leads, the rest sit in a small circle. Outside the Island only the edge light and focus ring are drawn."""
    from alice.hud import style

    m = HudModel(max_items=10)
    for msg in ALL_KINDS:
        m.apply(msg, 0.0)
    ents = style.entries(m.visible())
    assert ents[0].kind == "card"  # a new card expands the Island (in a live trial a small circle stayed beside it)
    assert [e.severity for e in ents[1:3]] == ["warn", "info"]  # then by severity
    crit = style.entries(m.visible() + [_item({"kind": "arrow", "clock": 6, "severity": "critical"}, 99)])
    assert crit[0].severity == "critical"  # a critical threat comes before the card too
    assert [s.item.kind for s in style.layout(m.visible(), 1920, 1080)] == ["edge", "box"]
    sam = next(e for e in ents if e.glyph == "arrow")
    assert sam.title == "SAM" and sam.angle == 90  # 3 o'clock
    six = style.entries([_item({"kind": "arrow", "clock": 7, "text": "ARKANDA · 900 m", "severity": "critical"})])
    assert (six[0].title, six[0].value, six[0].angle) == ("Arkanda", "900 m", 210)  # does not shout in capitals
    fuel = style.entries([_item({"kind": "label", "text": "YAKIT ~4 dk", "severity": "warn"})])[0]
    assert (fuel.glyph, fuel.title, fuel.value) == ("drop", "Yakıt", "~4 dk")
    assert style.tr_sentence("İLERİ GİDİYOR") == "İleri gidiyor" and style.tr_sentence("SAM") == "SAM"


def _item(d, seq=1):
    from alice.hud.model import Item

    return Item(d["kind"], {**d, "severity": d.get("severity", "info")}, 0.0, 9.0, d.get("id", ""), seq)


def test_arrow_glyph_points_its_clock_and_edge_glows_in_corner():
    from PIL import Image

    from alice.hud import style

    def tip_angle(angle):
        img = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
        style.glyph(img, "arrow", 40, 40, 60, (0, 145, 255), angle)
        px = img.load()
        pts = [(x, y) for y in range(80) for x in range(80) if px[x, y][3] > 200]
        fx, fy = max(pts, key=lambda p: (p[0] - 40) ** 2 + (p[1] - 40) ** 2)
        return math.degrees(math.atan2(fx - 40, -(fy - 40))) % 360

    for clock in (1, 3, 4, 7, 10, 12):
        diff = (tip_angle(clock % 12 * 30) - clock * 30 + 180) % 360 - 180
        assert abs(diff) < 10, clock  # the arrow really points at its clock
    m = HudModel()
    m.apply({"kind": "edge", "clock": 4, "severity": "warn"}, 0.0)
    sp = style.layout(m.visible(), 1920, 1080)[0]
    assert sp.w == sp.h and sp.x + sp.w == 1920 and sp.y + sp.h == 1080  # 4 o'clock: light in the bottom-right corner


def test_island_springs_to_content_and_fades():
    from alice.hud import style

    isl = style.Island(1920, 1080)
    status = style.entries([_item({"kind": "label", "text": "Yakıt ~4 dk", "severity": "warn", "id": "f"})])
    assert isl.update(status, 0.0)
    img, moving = isl.frame(0.0)
    assert moving and img.size == (isl.cw, isl.ch)
    for i in range(1, 90):
        img, moving = isl.frame(i / 60)
    assert not moving and isl.h.x == pytest.approx(38) and isl.shown == 1.0  # compact capsule settled
    assert img.getchannel("A").getbbox() is not None
    card = style.entries([_item({"kind": "card", "title": "Hatırlatma", "text": "20:00'de maç var.", "id": "c"}, 2)])
    isl.update(card, 2.0)
    for i in range(1, 90):
        isl.frame(2 + i / 60)
    assert isl.h.x > 60 and isl.w.x > 200  # expanded for the card
    isl.update([], 4.0)
    for i in range(1, 60):
        isl.frame(4 + i / 60)
    assert not isl.update([], 5.0)  # faded out


def test_critical_edge_fades_out_and_arrow_turns():
    """Live trial finding: critical edge light never faded (pulse); the arrow jumped on direction change."""
    from alice.hud import style

    m = HudModel()
    m.apply({"kind": "edge", "clock": 7, "severity": "critical", "id": "e"}, 0.0)
    sp = style.layout(m.visible(), 1920, 1080)[0]
    an = style.Anim(0.0)
    assert an.frame(1.0, sp)[3]  # pulses while visible
    an.gone_at = 2.0
    alpha, _, _, moving = an.frame(2.5, sp)
    assert alpha == 0 and not moving  # fades out and ends once cleared

    isl = style.Island(1920, 1080)
    arrow = {"kind": "arrow", "text": "Arkanda · 900 m", "severity": "critical", "id": "six"}
    isl.update(style.entries([_item({**arrow, "clock": 7})]), 0.0)
    for i in range(60):
        isl.frame(i / 60)
    isl.update(style.entries([_item({**arrow, "clock": 5, "text": "Arkanda · 700 m"})]), 1.0)
    isl.frame(1.05)
    assert 150 < isl.angle < 210  # on its way from 7 to 5 o'clock (no jump)
    for i in range(30):
        isl.frame(1.1 + i / 60)
    assert isl.angle == pytest.approx(150)


def test_focus_ring_and_island_render_fast():
    import time as _t

    from alice.hud import style

    m = HudModel()
    m.apply({"kind": "box", "x": 300, "y": 300, "w": 120, "h": 40, "text": "tıklıyorum"}, 0.0)
    m.apply({"kind": "arrow", "clock": 7, "text": "Arkanda · 900 m", "severity": "critical"}, 0.0)
    sp = style.layout(m.visible(), 1920, 1080)[0]
    assert style.render(sp, 1080).getbbox() is not None
    an = style.Anim(0.0)
    assert an.frame(0.0, sp)[1] > 1 and an.frame(1.0, sp)[1] == 1  # the focus ring settles in from outside
    isl = style.Island(1920, 1080)
    isl.update(style.entries(m.visible()), 0.0)
    t0 = _t.perf_counter()
    for i in range(60):
        isl.frame(i / 60)
    assert (_t.perf_counter() - t0) / 60 < 0.03  # under 30 ms per frame (alongside the game)


def test_edge_point_and_direction():
    assert window.edge_point(12, 1000, 500)[:2] == pytest.approx((500, 0))
    x, y, side = window.edge_point(3, 1000, 500)
    assert (round(x), round(y), side) == (1000, 250, True)
    x, y, side = window.edge_point(6, 1000, 500)
    assert (round(x), round(y), side) == (500, 500, False)
    assert window.direction(9) == pytest.approx((-1, 0), abs=1e-9)


class FakeWin32:
    def __init__(self):
        self.calls: list[tuple] = []

    def style(self, hwnd):
        self.calls.append(("style", hwnd))

    def exclude_from_capture(self, hwnd):
        self.calls.append(("capture", hwnd))
        return True

    def show(self, hwnd, visible):
        self.calls.append(("show", visible))

    def topmost(self, hwnd):
        self.calls.append(("topmost",))


class FakeTop:
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def test_window_tick_shows_only_while_items_exist():
    """Empty means no window (for the game's FPS). An arriving item shows the Island's window without taking focus;
    when its ttl runs out the Island fades and its window closes."""
    hwnds = iter(range(100, 200))
    w = window.HudWindow(HudModel(), queue.Queue(), win32=FakeWin32(), root=object(), rect=(0, 0, 1920, 1080),
                         make_slot=lambda root: (FakeTop(), next(hwnds)))
    w.win32.update_layered = lambda hwnd, img, x, y: w.win32.calls.append(("ulw", hwnd, img.size))
    w.tick()
    assert w.win32.calls == [] and not w.shown
    w.inbox.put({"kind": "arrow", "clock": 4, "ttl": 0.2, "id": "sam"})
    w.tick()
    assert ("style", 100) in w.win32.calls and ("capture", 100) in w.win32.calls  # click-through, not in the picture
    assert ("show", True) in w.win32.calls and any(c[0] == "ulw" for c in w.win32.calls) and w.shown
    assert window.ISLAND_KEY in w.slots
    time.sleep(0.25)
    for _ in range(40):  # time's up: the Island fades out
        w.tick()
        time.sleep(0.01)
    assert w.win32.calls[-1] == ("show", False) and not w.shown and not w.slots


# --------------------------------------------------------------- supervisor
class FakeProc:
    def __init__(self):
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 1

    def wait(self, timeout=None):
        return self.code


def test_supervisor_restarts_crashed_hud_and_backs_off():
    spawned: list[FakeProc] = []

    def spawn(argv):
        assert argv[1:4] == ["-m", "alice", "hud"] and "--parent" in argv
        spawned.append(FakeProc())
        return spawned[-1]

    sv = HudSupervisor(41240, spawn=spawn)
    sv.check(0.0)
    sv.check(1.0)
    assert len(spawned) == 1  # running: leave it alone
    for i in range(1, BURST):
        spawned[-1].code = 1  # crashed
        sv.check(float(i))
    assert len(spawned) == BURST and sv.restarts == BURST - 1
    spawned[-1].code = 1
    sv.check(10.0)  # 5 crashes in a minute: wait
    sv.check(20.0)
    assert len(spawned) == BURST
    sv.check(41.0)
    assert len(spawned) == BURST + 1
    sv.stop()
    assert spawned[-1].terminated


# ---------------------------------------------------------- flight -> HUD
def test_flight_threats_also_show_on_hud():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    c = FlightCompanion(r.say, show=lambda kind, **kw: shown.append((kind, kw)))
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    radar = {"id": "7", "clock": 2, "name": "SA-6", "lock": True, "missile": False, "dist_m": None, "kind": "radar"}
    tr.feed(st(2, extra={"contacts": [radar]}))
    tr.feed(st(3, extra={"contacts": [{**radar, "missile": True}]}))
    sam = {"id": "", "clock": 10, "dist_m": 12000.0, "kind": "sam", "name": "SAM", "range_m": 18000.0}
    tr.feed(st(4, extra={"contacts": [sam]}))
    bandit = {"id": "", "clock": 6, "dist_m": 1200.0, "kind": "aircraft", "name": "Fighter"}
    tr.feed(st(20, extra={"contacts": [bandit]}))  # 12 s after the SAM: the voice's turn came
    hot = {"id": "", "clock": 1, "dist_m": 8000.0, "kind": "aircraft", "name": "Fighter", "aspect": 5.0}
    for t in (40, 41, 43):
        tr.feed(st(t, extra={"contacts": [hot]}))
    got = [(k, kw["clock"], kw.get("text"), kw["severity"], kw["id"]) for k, kw in shown]
    assert got == [
        ("arrow", 2, "KİLİT · SA-6", "warn", "lock"),
        ("arrow", 2, "FÜZE · SA-6", "critical", "missile"),
        ("edge", 2, None, "critical", "missile.edge"),
        ("arrow", 10, "SAM · 12,0 km", "warn", "sam:SAM"),
        ("arrow", 6, "ARKANDA · 1,2 km", "critical", "six"),
        ("edge", 6, None, "critical", "six.edge"),
        ("arrow", 1, "BURNU SANA · 8,0 km", "warn", "nose"),
    ]
    assert all(kw["ttl"] > 0 for _, kw in shown)
    assert any(x.startswith("Arkanda düşman") for x in r.texts())  # the voice carries on as before


def test_flight_fuel_and_kill_show_on_hud():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    c = FlightCompanion(r.say, show=lambda kind, **kw: shown.append((kind, kw)))
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=80, agl=30, x=None, z=None))
    fuel = 2400.0
    for i in range(1, 2400):
        fuel -= 1.0
        tr.feed(st(i, fuel_kg=fuel, burn_kgs=None, x=None, z=None))
        if fuel <= 150:
            break
    tr.feed(st(2400, x=None, z=None, extra={"hud": [("kill", "MiG-29SMT")]}))
    fuel_lbl = [(kw["text"], kw["severity"], kw["ttl"]) for k, kw in shown if k == "label" and kw["id"] == "fuel"]
    assert fuel_lbl == [("YAKIT ~10 dk", "warn", 6.0), ("YAKIT ~5 dk", "critical", 6.0)]
    cards = [kw for k, kw in shown if k == "card"]
    assert cards == [{"title": "Düşürdün", "text": "MiG-29SMT", "severity": "info", "ttl": 4.0, "id": "kill"}]


def test_flight_fuel_and_kill_hud_failure_keeps_speech():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion

    def boom(kind, **kw):
        raise RuntimeError("HUD bozuk")

    r = Recorder()
    c = FlightCompanion(r.say, show=boom)
    c._hud(st(1, extra={"hud": [("kill", "Bf 109")]}))
    c._label("YAKIT ~5 dk", "critical", "fuel", 6.0)
    assert r.texts() == ["Düşürdün: Bf 109."]


def test_flight_hud_failure_does_not_break_callouts():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    def boom(kind, **kw):
        raise RuntimeError("HUD bozuk")

    r = Recorder()
    tr = TouchdownTracker(FlightCompanion(r.say, show=boom))
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    radar = {"id": "7", "clock": 2, "name": "SA-6", "lock": False, "missile": True, "dist_m": None, "kind": "radar"}
    tr.feed(st(2, extra={"contacts": [radar]}))
    assert "Füze! Saat 2, SA-6. Chaff at, dön!" in r.texts()


def test_hub_passes_show_to_companions():
    from alice.sims.hub import SimHub

    marker = []
    hb = SimHub({}, store=None, notifier=None, show=lambda kind, **kw: marker.append(kind))
    hb.companion("warthunder").show("arrow", clock=1)
    assert marker == ["arrow"]


def test_hud_arrow_follows_threat_while_voice_stays_quiet():
    """While turning, an enemy moves from 6 to 4 o'clock: the voice stays sparse and the arrow shows the new side
    (even if the voice is muted)."""
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    c = FlightCompanion(r.say, show=lambda kind, **kw: shown.append((kind, kw)))
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    bandit = {"id": "", "clock": 6, "dist_m": 1200.0, "kind": "aircraft", "name": "Fighter"}
    tr.feed(st(2, extra={"contacts": [bandit]}))
    tr.feed(st(2.2, extra={"contacts": [{**bandit, "clock": 5}]}))   # too frequent: not sent
    tr.feed(st(3, extra={"contacts": [{**bandit, "clock": 4, "dist_m": 900.0}]}))
    for t in range(5, 24, 2):  # constantly behind: after 20 s the arrow goes quiet
        tr.feed(st(t, extra={"contacts": [{**bandit, "clock": 5}]}))
    tr.feed(st(30, extra={"contacts": [{**bandit, "clock": 8}]}))    # vanished, came back: no voice, arrow shows
    tr.feed(st(48, extra={"contacts": [{**bandit, "clock": 7}]}))    # 45 s passed: said again
    arrows = [(kw["clock"], kw["ttl"]) for k, kw in shown if k == "arrow"]
    assert arrows[:3] == [(6, 4.0), (4, 2.5), (5, 2.5)]
    assert arrows[3:-2] == [(5, 2.5)] * 8  # 5..21: the first 20 s of being seen
    assert arrows[-2:] == [(8, 2.5), (7, 4.0)]
    assert [x for x in r.texts() if x.startswith("Arkanda")] == ["Arkanda düşman, saat 6, 1,2 kilometre!",
                                                                 "Arkanda düşman, saat 7, 1,2 kilometre!"]


def test_hud_shows_threats_the_voice_skipped():
    """In a ship group only the nearest is spoken; the others appear as arrows on the HUD, not by voice."""
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    c = FlightCompanion(r.say, show=lambda kind, **kw: shown.append(kw["id"]))
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    ships = [{"id": "", "clock": 11, "dist_m": 7200.0, "kind": "sam", "name": "füzeli muhrip", "range_m": 9000.0},
             {"id": "", "clock": 10, "dist_m": 8200.0, "kind": "sam", "name": "fırkateyn", "range_m": 9000.0}]
    tr.feed(st(2, extra={"contacts": ships}))
    tr.feed(st(3, extra={"contacts": ships}))
    assert [x for x in r.texts() if "menzilindesin" in x] == ["Füzeli muhrip menzilindesin: saat 11, 7,2 kilometre."]
    assert shown == ["sam:füzeli muhrip", "sam:füzeli muhrip", "sam:fırkateyn"]


def test_exclusive_fullscreen_said_once_only_with_hud(monkeypatch):
    from alice.hud import screen
    from alice.sims.hub import SimHub

    monkeypatch.setattr(screen, "exclusive_fullscreen", lambda: True)
    said = []
    quiet = SimHub({}, store=None, notifier=None)
    monkeypatch.setattr(quiet, "_say", lambda *a: said.append(a))
    quiet._fullscreen_check("War Thunder")
    assert said == []  # HUD off: nothing to say
    hb = SimHub({}, store=None, notifier=None, show=lambda kind, **kw: None)
    monkeypatch.setattr(hb, "_say", lambda *a: said.append(a))
    hb._fullscreen_check("War Thunder")
    hb._fullscreen_check("War Thunder")
    assert len(said) == 1 and said[0][0].startswith("War Thunder özel tam ekranda")


def test_exclusive_fullscreen_reads_notification_state(monkeypatch):
    import sys
    from types import SimpleNamespace

    from alice.hud import screen

    if sys.platform != "win32":
        assert screen.exclusive_fullscreen() is False
        return

    def fake(value, ret=0):
        def q(ref):
            ref._obj.value = value
            return ret

        return SimpleNamespace(shell32=SimpleNamespace(SHQueryUserNotificationState=q))

    import ctypes

    for value, ret, want in ((3, 0, True), (2, 0, False), (3, 1, False)):
        monkeypatch.setattr(ctypes, "windll", fake(value, ret))
        assert screen.exclusive_fullscreen() is want


# ------------------------------------------------------- notification, speech, click
@pytest.fixture
def hud_on(monkeypatch):
    sent = []
    monkeypatch.setattr(client, "active", True)
    monkeypatch.setattr(client, "send", lambda msg: sent.append(msg) or True)
    return sent


def test_views_do_nothing_when_hud_is_off(monkeypatch):
    from types import SimpleNamespace

    from alice.hud import views

    monkeypatch.setattr(client, "active", False)
    monkeypatch.setattr(client, "send", lambda msg: pytest.fail("HUD kapalıyken gönderilmemeli"))
    t0 = time.monotonic()
    assert not views.click_target(10, 10)
    assert not views.speech("1. bir\n2. iki")
    assert not views.notification(SimpleNamespace(kind="alert", topic="pc", severity="warn", title="t", body="b"))
    assert time.monotonic() - t0 < 0.1  # the click preview does not wait


def test_notification_card_skips_flight(hud_on):
    from alice.core.notify import Notification
    from alice.hud import views

    assert views.notification(Notification("ALICE", "Ekran kartı  88°C,\nçok sıcak.", "critical", "pc"))
    assert not views.notification(Notification("Uçuş", "Arkanda düşman", "warn", "flight", kind="flight"))
    assert hud_on == [{"kind": "card", "ttl": 12.0, "title": "ALICE", "text": "Ekran kartı 88°C, çok sıcak.",
                       "severity": "critical", "id": "note:ALICE"}]


def test_speech_summary_shows_lists_and_numbers_only():
    from alice.hud.views import summarize

    assert summarize("Tamam, açıyorum.") is None
    assert summarize("Bugün üç işin var:\n- **Okul** 9'da\n- Staj 14'te\n- Spor") == "Okul 9'da · Staj 14'te · Spor"
    assert summarize("Ekran kartı 72 derece.") == "Ekran kartı 72 derece."
    long = summarize("Uzun bir cümle. " * 40)
    assert long.endswith("…") and len(long) <= 260


def test_click_target_previews_before_click(hud_on, monkeypatch):
    from alice.hud import views

    waited = []
    monkeypatch.setattr(views.time, "sleep", waited.append)
    assert views.click_target(500, 300, "double_click")
    assert hud_on[0]["kind"] == "cursor" and (hud_on[0]["x"], hud_on[0]["y"]) == (500, 300)  # ALICE's pointer
    assert hud_on[0]["action"] == "double_click"
    assert hud_on[0]["text"] == "çift tıklıyorum" and waited == [views.CLICK_PREVIEW]


def test_voice_ui_sends_speech_card(hud_on):
    from alice.voice.assistant import VoiceUI

    class Spk:
        speaking = False

        def say(self, *a, **kw):
            return True

        def stop(self):
            pass

    ui = VoiceUI(Spk())
    ui.begin_turn()
    ui.stream_end("Yarın:\n1. Okul\n2. Staj", "model")
    assert [m["text"] for m in hud_on if m.get("id") == "speech"] == ["Okul · Staj"]


def test_start_hud_replaces_windows_toast_with_card(monkeypatch):
    from types import SimpleNamespace

    from alice.core import cli as core_cli
    from alice.core.notify import Notifier
    from alice.hud import supervisor

    monkeypatch.setattr(supervisor.HudSupervisor, "start", lambda self: self)
    nt = Notifier(store=None, policy=lambda: None)
    core = SimpleNamespace(notifier=nt)
    cfg = SimpleNamespace(data={"hud": {"enabled": True, "port": 41277}})
    try:
        sv = core_cli._start_hud(cfg, core)
        assert sv.port == 41277 and client.active
        assert nt._desktop not in nt.channels and len(nt.channels) == 1
        from alice.core import notify
        from alice.core.notify import Notification

        toasts, sent = [], []
        monkeypatch.setattr(notify, "desktop_notify", lambda n: toasts.append(n.body) or True)
        monkeypatch.setattr(client, "send", lambda msg: sent.append(msg) or True)
        sv.proc = SimpleNamespace(poll=lambda: 1)  # HUD crashed: a Windows balloon instead of a card
        nt.channels[0](Notification("ALICE", "GPU 95°C", "critical", "pc"), None)
        assert sent == [] and toasts == ["GPU 95°C"]
        sv.proc = SimpleNamespace(poll=lambda: None)  # HUD running: card
        nt.channels[0](Notification("ALICE", "RAM %96", "critical", "pc"), None)
        assert sent[0]["kind"] == "card" and toasts == ["GPU 95°C"]
    finally:
        client.configure(window.PORT, on=False)


# ------------------------------------------------------- system gauge
def test_system_gauge_below_and_above_limit(hud_on):
    from alice.hud import views

    assert not views.system_gauge("GPU", 80, 93, "°C")  # below 90%: silent
    assert views.system_gauge("GPU", 87, 93, "°C", ttl=20)
    assert views.system_gauge("RAM", 96, 95, "%")
    assert not views.system_gauge("GPU", 87, 93, "°C", warn=88)  # custom starting value
    assert hud_on == [
        {"kind": "progress", "ttl": 20, "value": 0.935, "text": "GPU 87°C", "severity": "warn", "id": "sys:GPU"},
        {"kind": "progress", "ttl": 25.0, "value": 1.0, "text": "RAM %96", "severity": "critical", "id": "sys:RAM"},
    ]


def test_system_gauge_silent_when_hud_is_off(monkeypatch):
    from alice.core.watchdogs import SystemGauges
    from alice.hud import views

    monkeypatch.setattr(client, "active", False)
    monkeypatch.setattr(client, "send", lambda msg: pytest.fail("HUD kapalıyken gönderilmemeli"))

    class NoStore:
        def last(self, key):
            pytest.fail("HUD kapalıyken depoya bakılmamalı")

    assert not views.system_gauge("GPU", 99, 93, "°C")
    assert SystemGauges(NoStore())(None) == []


class GaugeStore:
    def __init__(self, values, ts=1000.0):
        self.values, self.ts = values, ts

    def last(self, key):
        return (self.ts, self.values[key], None) if key in self.values else None


def test_system_gauges_hook_refreshes_same_id_and_fades(hud_on):
    from alice.core.watchdogs import SystemGauges

    store = GaugeStore({"pc.gpu.temp": 89.0, "pc.mem.percent": 85.0, "pc.cpu.temp": 70.0})
    hook = SystemGauges(store, interval=15, clock=lambda: 1005.0)
    assert hook(None) == ["GPU"] and hook(None) == ["GPU"]  # a new packet every turn, same id
    assert [m["id"] for m in hud_on] == ["sys:GPU", "sys:GPU"]
    assert hud_on[0]["ttl"] == 27.5 and hud_on[0]["severity"] == "warn"
    store.values["pc.mem.percent"] = 96.0
    assert hook(None) == ["GPU", "RAM"] and hud_on[-1]["severity"] == "critical"
    store.values.update({"pc.gpu.temp": 75.0, "pc.mem.percent": 60.0})
    n = len(hud_on)
    assert hook(None) == [] and len(hud_on) == n  # back to normal: stop sending, bar fades by ttl
    store.values["pc.gpu.temp"] = 99.0
    store.ts = 800.0  # a stale reading does not open the bar
    assert hook(None) == []


def test_start_hud_adds_system_gauge_hook(monkeypatch):
    from types import SimpleNamespace

    from alice.core import cli as core_cli
    from alice.core.notify import Notifier
    from alice.core.watchdogs import SystemGauges
    from alice.hud import supervisor

    monkeypatch.setattr(supervisor.HudSupervisor, "start", lambda self: self)
    core = SimpleNamespace(notifier=Notifier(store=None, policy=lambda: None), hooks=[], store=GaugeStore({}),
                           interval=10.0)
    try:
        core_cli._start_hud(SimpleNamespace(data={"hud": {"enabled": True, "port": 41278}}), core)
        assert len(core.hooks) == 1 and isinstance(core.hooks[0], SystemGauges) and core.hooks[0].ttl == 20.0
    finally:
        client.configure(window.PORT, on=False)


# ------------------------------------------------- pointer, voice state, density, history, directionless threat
def test_pointer_glides_instead_of_teleporting_and_ripples_on_click():
    from alice.hud import style

    p = style.Pointer(1080)
    p.go(100, 100, "click", 0.0)  # first time: appears at the target
    assert p.at(0.0) == (100, 100)
    p.go(900, 500, "click", 1.0)  # second target: glides from where it is
    mid = p.at(1.0 + p.dur / 2)
    assert 100 < mid[0] < 900 and 100 < mid[1] < 500  # on the way (not teleported)
    assert style.GLIDE_MIN <= p.dur <= style.GLIDE_MAX < 0.4  # settles before the click preview ends
    assert p.at(1.0 + p.dur) == pytest.approx((900, 500))
    img, x, y, moving = p.frame(1.0 + p.dur + 0.1)  # settled, the ring is spreading
    assert img is not None and moving and (x + p.size / 2, y + p.size / 2) == pytest.approx((900, 500))
    _, _, _, moving = p.frame(1.0 + p.dur + style.RIPPLE + 0.3)
    assert not moving  # the wave is over: no redraw
    p.leave(5.0)
    assert p.frame(5.0 + style.FADE_OUT + 0.01)[0] is None and not p.active  # faded out


def test_pointer_starts_from_real_cursor_when_first_shown():
    from alice.hud import style

    p = style.Pointer(1080)
    p.go(800, 400, "click", 0.0, origin=(10, 10))
    assert p.at(0.0) == (10, 10) and p.at(p.dur) == pytest.approx((800, 400))


def test_window_draws_pointer_and_drops_it_when_done():
    hwnds = iter(range(100, 200))
    w = window.HudWindow(HudModel(), queue.Queue(), win32=FakeWin32(), root=object(), rect=(0, 0, 1920, 1080),
                         make_slot=lambda root: (FakeTop(), next(hwnds)))
    pos = []
    w.win32.update_layered = lambda hwnd, img, x, y: pos.append((hwnd, x, y))
    w.win32.cursor_pos = lambda: (5, 5)
    # A long ttl, so a slow runner (macOS CI) can't let the pointer expire before the first checks; then tick until
    # it is gone, with a deadline instead of a fixed number of ticks.
    w.inbox.put({"kind": "cursor", "x": 600, "y": 400, "action": "click", "text": "tıklıyorum", "ttl": 3.0,
                 "id": "computer"})
    for _ in range(30):
        w.tick()
        time.sleep(0.01)
    assert window.POINTER_KEY in w.slots and window.ISLAND_KEY in w.slots  # pointer + "tıklıyorum" on the Island
    ph = w.slots[window.POINTER_KEY].hwnd
    xs = [x for h, x, _ in pos if h == ph]
    assert len(set(xs)) > 3  # the window glided step by step
    deadline = time.monotonic() + 10
    while window.POINTER_KEY in w.slots and time.monotonic() < deadline:
        w.tick()
        time.sleep(0.02)
    assert window.POINTER_KEY not in w.slots


def test_directionless_threat_shows_radar_not_a_wrong_arrow():
    from alice.hud import style

    m = HudModel()
    assert m.apply({"kind": "arrow", "text": "FÜZE", "severity": "critical", "id": "missile"}, 0.0)
    assert not m.apply({"kind": "arrow"}, 0.0)  # it has neither a direction nor text
    e = style.entries(m.visible())[0]
    assert e.glyph == "radar" and e.severity == "critical"
    style.preview([{"kind": "arrow", "text": "FÜZE", "severity": "critical"}])  # it can be drawn


def test_flash_rate_stays_under_three_per_second():
    from alice.hud import style

    assert 1 / style.PULSE < 3 and 1 / style.LISTEN_PULSE < 3


def test_modes_filter_by_density():
    m = HudModel()
    m.apply({"kind": "card", "title": "Takvim", "text": "20:00 maç"}, 0.0)
    m.apply({"kind": "arrow", "clock": 6, "text": "Arkanda", "severity": "critical", "id": "six"}, 0.0)
    m.apply({"kind": "label", "text": "Yakıt ~4 dk", "severity": "warn", "id": "fuel"}, 0.0)
    m.apply({"kind": "card", "title": "Düşürdün", "text": "Bf 109", "id": "kill"}, 0.0)
    m.apply({"kind": "label", "text": "Dinliyorum", "icon": "wave", "id": "voice"}, 0.0)
    assert len(m.visible()) == 5  # desktop: everything
    m.apply({"kind": "context", "game": True}, 0.0)  # in a game: info cards are hidden
    assert {i.id for i in m.visible()} == {"six", "fuel", "kill", "voice"}
    m.apply({"kind": "mode", "value": "quiet"}, 0.0)
    assert [i.id for i in m.visible()] == ["six"] or {i.id for i in m.visible()} == {"six", "__mode__"}
    m.apply({"kind": "mode"}, 0.0)  # shortcut: switches to the next mode and says so briefly
    assert m.mode == "auto" and any(i.id == "__mode__" and "otomatik" in i.data["text"] for i in m.visible())


def test_history_hotkey_shows_missed_cards():
    m = HudModel()
    m.apply({"kind": "history"}, 0.0)
    assert "Henüz" in m.items[-1].data["text"]
    for t in ("Bir", "İki", "Üç", "Dört"):
        m.apply({"kind": "card", "title": t, "text": f"{t} yazısı", "ttl": 1}, 0.0)
    m.expire(5.0)
    m.apply({"kind": "history"}, 5.0)
    h = m.items[-1]
    assert h.id == "__history__" and h.data["title"] == "Son bildirimler"
    assert h.data["text"].index("Dört") < h.data["text"].index("Üç") and "Bir" not in h.data["text"]


def test_voice_state_dedupes_and_clears(hud_on, monkeypatch):
    from alice.hud import views

    monkeypatch.setitem(views._voice, "state", None)
    monkeypatch.setitem(views._voice, "detail", "")
    assert views.voice_state("listen")
    assert not views.voice_state("listen")  # same state: not sent again
    assert views.voice_state("work", "ekranda")
    assert views.voice_state(None)
    assert [m.get("text") for m in hud_on] == ["Dinliyorum", "Çalışıyorum · ekranda", None]
    assert hud_on[0]["icon"] == "wave" and hud_on[-1] == {"kind": "clear", "id": "voice"}


def test_listening_island_animates_wave():
    from alice.hud import style

    isl = style.Island(1920, 1080)
    isl.update(style.entries([_item({"kind": "label", "text": "Dinliyorum", "icon": "wave", "id": "voice"})]), 0.0)
    for i in range(80):
        _, moving = isl.frame(i / 60)
    assert moving and isl.calm  # the wave is moving but can be drawn sparsely
