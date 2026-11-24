"""Computer tab of the phone app: metric history (small charts), open windows and bringing them to the front."""

from __future__ import annotations

import time

import pytest

from alice.app import remote
from alice.core.store import Store


def test_series_buckets_and_carries_last_value(tmp_path):
    with Store(tmp_path / "s.db") as st:
        now = time.time()
        st.add("pc.cpu.percent", 10, ts=now - 590)
        st.add("pc.cpu.percent", 30, ts=now - 300)
        st.add("pc.cpu.percent", 50, ts=now - 5)
        out = remote.series(st, minutes=10, points=10)
    cpu = out["cpu"]
    assert len(cpu) == 10
    assert cpu[0] == 10 and cpu[-1] == 50
    assert 30 in cpu  # the middle sample falls in its own bucket
    assert all(a is not None for a in cpu)
    assert out["ram"] == []  # empty if there are no samples at all


def test_windows_without_window_tools_is_empty(monkeypatch):
    monkeypatch.setattr(remote, "IS_WIN", False)
    import alice.tools.desktop as desktop

    def boom():
        raise RuntimeError("wmctrl yok")

    monkeypatch.setattr(desktop, "list_windows", boom)
    assert remote.windows() == []


def test_windows_lists_titles_with_app_name(monkeypatch):
    monkeypatch.setattr(remote, "IS_WIN", False)
    import alice.tools.desktop as desktop

    monkeypatch.setattr(desktop, "list_windows", lambda: ["notlar.txt - Not Defteri", "Discord"])
    rows = remote.windows()
    assert [r["app"] for r in rows] == ["Not Defteri", "Discord"]


def test_focus_action(monkeypatch):
    import alice.tools.desktop as desktop

    seen = {}
    monkeypatch.setattr(desktop, "focus_window", lambda t: seen.setdefault("t", t) or t)
    assert remote.action({"action": "focus", "title": "Discord"}) == "Discord brought to the front"
    with pytest.raises(ValueError):
        remote.action({"action": "focus", "title": ""})


class FakePG:
    def __init__(self):
        self.calls = []

    def click(self, x, y, clicks=1, button="left"):
        self.calls.append(("click", x, y, clicks, button))

    def moveTo(self, x, y):  # noqa: N802 - pyautogui's name
        self.calls.append(("move", x, y))

    def scroll(self, amount):
        self.calls.append(("scroll", amount))


class FakeMSS:
    monitors = [{}, {"left": -1920, "top": 0, "width": 1920, "height": 1080}]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def pg(monkeypatch):
    import sys
    import types

    import alice.tools.computer as computer

    fake = FakePG()
    monkeypatch.setattr(computer, "_pyautogui", lambda: fake)
    monkeypatch.setitem(sys.modules, "mss", types.SimpleNamespace(mss=FakeMSS))
    return fake


def test_tap_on_phone_screen_clicks_same_spot_on_pc(pg):
    """Tapping the live phone screen: the image fraction is converted to main-screen pixels (also at negative coordinates)."""
    assert remote.action({"action": "click", "x": 0.5, "y": 0.25}) == "tamam"
    assert remote.action({"action": "click", "x": 1.0, "y": 0.0, "button": "right"}) == "tamam"
    assert pg.calls == [("click", -1920 + 960, 270, 1, "left"), ("click", -1, 0, 1, "right")]
    with pytest.raises(ValueError):
        remote.action({"action": "click", "x": 1.5, "y": 0.2})


def test_two_finger_scroll(pg):
    remote.action({"action": "scroll", "x": 0.5, "y": 0.5, "dy": 2})
    assert pg.calls[-1] == ("scroll", -240)  # two notches down


def test_typing_says_where_it_went_and_refuses_without_window(monkeypatch):
    import alice.tools.computer as computer

    typed, keys = [], []
    monkeypatch.setattr(computer, "type_text", typed.append)
    monkeypatch.setattr(computer, "press_keys", keys.append)
    monkeypatch.setattr(remote, "_foreground_title", lambda: "Discord")
    assert remote.action({"action": "type", "text": "selam", "enter": True}) == "typed → Discord"
    assert typed == ["selam"] and keys == ["enter"]
    monkeypatch.setattr(remote, "_foreground_title", lambda: "")
    with pytest.raises(ValueError, match="no window has focus"):
        remote.action({"action": "type", "text": "selam"})


def test_flight_warnings_are_not_sent_to_phone():
    """In-game warnings (missile, lock, SAM) were not supposed to go to the phone as notifications, but they did:
    the phone turned each one into a notification. Even the dozens of warnings in a fight must not push other events
    out of the list."""
    with Store() as st:  # ALICE_HOME is a temporary folder in tests
        st.add_event("core", "Çekirdek başladı", delivered=True)
        for i in range(60):
            st.add_event("flight", "Uçuş", f"Füze atıldı {i}", "critical", delivered=True)
    evs = remote.status()["events"]
    assert [e["title"] for e in evs] == ["Çekirdek başladı"]
