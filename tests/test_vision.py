from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")  # missile vision from the screen needs numpy; not installed in CI

from alice.sims import vision  # noqa: E402
from alice.sims.vision import Confirmer, Detection, MissileWatcher, detect, region_of  # noqa: E402


def frame(clock: int | None = None, color=(255, 40, 30), size=120, blob=6, extra=None):
    """Dark RWR circle; optionally a red marker at the given clock position."""
    a = np.full((size, size, 3), 20, dtype=np.uint8)
    if clock is not None:
        ang = math.radians(clock * 30)
        cx = size / 2 + math.sin(ang) * size * 0.35
        cy = size / 2 - math.cos(ang) * size * 0.35
        a[int(cy) - blob:int(cy) + blob, int(cx) - blob:int(cx) + blob] = color
    if extra is not None:
        extra(a)
    return a


@pytest.mark.parametrize("clock", [12, 2, 4, 6, 9, 11])
def test_detect_clock_from_red_marker(clock):
    d = detect(frame(clock))
    assert d is not None and d.clock == clock and d.pixels >= 100


def test_detect_ignores_white_center_and_specks():
    assert detect(frame()) is None
    assert detect(frame(3, color=(255, 255, 255))) is None  # a white/gray marker is not a missile
    assert detect(frame(extra=lambda a: a.__setitem__((slice(55, 65), slice(55, 65)), (255, 30, 30)))) is None
    assert detect(frame(5, blob=1)) is None  # 4 pixels: noise
    assert detect(np.zeros((2, 2, 3))) is None


def test_detect_orange_counts_and_thresholds_are_configurable():
    assert detect(frame(8, color=(255, 140, 20))) .clock == 8
    assert detect(frame(8, color=(255, 140, 20)), {"red_margin": 150}) is None


def test_confirmer_needs_several_frames_and_holds_through_blinks():
    c = Confirmer(confirm=3, window=5, hold=1.0)
    hit = Detection(4, 50)
    assert c.feed(hit, 0.0) is None and c.feed(None, 0.1) is None and c.feed(hit, 0.2) is None
    assert c.feed(hit, 0.3) == hit  # 3 of 4 frames: confirmed
    assert c.feed(None, 0.4) == hit and c.feed(None, 0.5) == hit  # still held across the blink gap
    for t in (0.6, 0.7, 0.8, 0.9, 1.0):
        c.feed(None, t)
    assert c.feed(None, 1.5) is None  # not seen for more than 1 s since the last confirmation (0.4): over


def test_single_frame_false_alarm_is_ignored():
    c = Confirmer()
    assert all(c.feed(d, i / 10) is None for i, d in enumerate([None, Detection(6, 30), None, None, None, None]))


def test_watcher_reports_confirmed_missile_as_contact():
    frames = iter([frame(4), frame(4), frame(None), frame(4)])
    w = MissileWatcher((0, 0, 120, 120), {"method": "red", "confirm": 3}, grab=lambda region: next(frames))
    for i in range(4):
        w.step(i / 10)
    assert w.contacts() == [{"id": "vision", "name": "", "kind": "radar", "clock": 4, "lock": True,
                             "missile": True, "dist_m": None, "vision": True}]


def test_watcher_survives_capture_errors():
    def boom(region):
        raise OSError("ekran kilitli")

    w = MissileWatcher((0, 0, 50, 50), grab=boom)
    assert w.step(0.0) is None and w.errors == 1 and w.contacts() == []


def test_region_parsing():
    assert region_of({"vision_region": [10, 20, 200, 200]}) == (10, 20, 200, 200)
    for bad in (None, [1, 2, 3], [1, 2, 3, 4], ["a", 1, 2, 3]):
        assert region_of({"vision_region": bad}) is None


def test_vision_missile_callout_has_no_type_and_reaches_hud():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    tr = TouchdownTracker(FlightCompanion(r.say, show=lambda kind, **kw: shown.append((kind, kw.get("text")))))
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    seen = {"id": "vision", "name": "", "kind": "radar", "clock": 4, "lock": True, "missile": True,
            "dist_m": None, "vision": True}
    tr.feed(st(2, extra={"contacts": [seen]}))
    assert r.texts()[-1] == "Füze! Saat 4. Chaff at, dön!"
    assert ("arrow", "FÜZE") in shown


def test_hub_adds_vision_contacts_to_war_thunder_feed(monkeypatch):
    from tests.test_sims_hub import FakeNotifier, FakePart, FakeStore

    from alice.sims import dcs, warthunder
    from alice.sims.hub import SimHub

    FakePart.instances = []
    monkeypatch.setattr(dcs, "ensure_installed", lambda port=0: [])
    monkeypatch.setattr(dcs, "DCSListener", FakePart)
    monkeypatch.setattr(warthunder, "WarThunderPoller", FakePart)

    class Fake:
        def __init__(self, region, cfg):
            self.region, self.poked = region, 0

        def poke(self):
            self.poked += 1

        def contacts(self):
            return [{"id": "vision", "kind": "radar", "clock": 7, "missile": True, "vision": True}]

        def stop(self):
            pass

    monkeypatch.setattr(vision, "MissileWatcher", Fake)
    hb = SimHub({"dcs": False, "missile_vision": True, "vision_region": [0, 0, 100, 100]}, FakeStore(),
                FakeNotifier())
    hb.start()
    try:
        feed = FakePart.instances[0].args[0]
        got = []
        monkeypatch.setattr(hb.companions["warthunder"], "update", lambda s: got.append(s))
        from tests.test_sims import st

        mine = [{"kind": "aircraft", "clock": 1}]
        s = st(0, source="warthunder", extra={"contacts": mine})
        feed(s)
        assert [c["clock"] for c in s.extra["contacts"]] == [1, 7]
        assert mine == [{"kind": "aircraft", "clock": 1}]  # the poller's list was not changed
    finally:
        hb.stop()


def test_hub_without_region_uses_war_thunder_default(monkeypatch):
    from tests.test_sims_hub import FakeNotifier, FakeStore

    from alice.sims import vision
    from alice.sims.hub import SimHub

    hb = SimHub({"missile_vision": True}, FakeStore(), FakeNotifier())
    w = hb._missile_vision()
    try:
        assert w is not None and w.region[2:] == tuple(round(v * w.region[3] / 285) for v in (255, 285))
        assert w.region[0] == round(60 * w.region[3] / 285)  # left edge, scaled to the screen height
    finally:
        hb.stop()
    assert SimHub({}, FakeStore(), FakeNotifier())._missile_vision() is None
    assert vision.default_region(1440) == (80, 507, 340, 380)


# ------------------------------------------------- War Thunder's cyan RWR text (from Medal clips)
def test_read_rwr_launch_track_and_direction_from_words():
    from alice.sims.vision import WT_CENTER, read_rwr

    img = np.zeros((285, 255, 3), dtype=np.uint8)
    cx, cy = WT_CENTER[0] * 255, WT_CENTER[1] * 285
    track = read_rwr(img, words=[("INSTRUCTOR", (0, 0, 90, 14)), ("TRACK", (92, 14, 40, 14))])
    assert track.track and not track.launch and track.clock is None
    # missile on the right (clock 3) and the OCR engine's Turkish letters ("MSÜ", "LAUNŞH")
    d = read_rwr(img, words=[("LAUNŞH", (92, 14, 50, 14)), ("MSÜ", (cx + 60, cy - 7, 24, 14))])
    assert d.launch and d.clock == 3
    up = read_rwr(img, words=[("MSL", (cx - 12, cy - 80, 24, 14))])  # MSL means missile even if LAUNCH isn't read
    assert up.launch and up.clock == 12
    merged = read_rwr(img, words=[("LAUNCH", (92, 14, 50, 14)), ("M29MSL", (cx + 40, cy + 40, 50, 14))])
    assert merged.launch and merged.clock is None  # a merged word's position doesn't count as a direction
    assert read_rwr(img, words=[("F15", (cx, cy, 20, 14)), ("ENGN1", (0, 0, 40, 14))]) is None


def test_hud_mask_keeps_only_cyan_text():
    from alice.sims.vision import hud_mask

    img = np.zeros((10, 20, 3), dtype=np.uint8)
    img[:, :10] = (40, 230, 200)   # cyan HUD
    img[:, 10:] = (60, 140, 50)    # tree
    m = np.asarray(hud_mask(__import__("PIL.Image", fromlist=["Image"]).fromarray(img)))
    assert (m[:, :10] == 0).all() and (m[:, 10:] == 255).all()


def test_watcher_text_mode_reports_directionless_missile_and_lock(monkeypatch):
    from alice.sims import vision

    seq = iter([vision.TextDetection(None, False, True), vision.TextDetection(None, False, True),
                vision.TextDetection(None, True, False), None, vision.TextDetection(None, True, False)])
    monkeypatch.setattr(vision, "read_rwr", lambda frame, cfg=None: next(seq))
    w = MissileWatcher((0, 0, 50, 50), grab=lambda r: np.zeros((50, 50, 3), dtype=np.uint8))
    w.step(0.0)
    w.step(0.1)
    assert w.contacts()[0]["missile"] is False and w.contacts()[0]["lock"] is True  # lock
    for i in range(3):
        w.step(0.2 + i / 10)
    c = w.contacts()[0]
    assert c["missile"] is True and c["clock"] is None  # a blinking LAUNCH: 2 of 5 is enough, no direction


def test_directionless_vision_missile_callout():
    from alice.sims.flight import clock_from

    assert [clock_from(c) for c in (3, 6, 12)] == ["3'ten", "6'dan", "12'den"]


def test_test_files_crops_full_screenshots(tmp_path):
    from PIL import Image

    full = np.full((400, 600, 3), 30, dtype=np.uint8)
    full[100:220, 300:420] = frame(2)
    Image.fromarray(full).save(tmp_path / "missile-1.png")
    Image.fromarray(np.full((400, 600, 3), 30, dtype=np.uint8)).save(tmp_path / "clear-1.png")
    lines = vision.test_files(sorted(tmp_path.glob("*.png")), (300, 100, 120, 120), {})
    assert lines == ["clear-1.png: no mark", lines[1]]
    assert lines[1].startswith("missile-1.png: mark found, 2 o'clock")
