from __future__ import annotations

import sys

import pytest

from alice.hotkeys import key_vk, parse_combo


def test_parse_combo_pynput_style():
    assert parse_combo("<ctrl>+<alt>+a") == (0x2 | 0x1, ord("A"))
    assert parse_combo("<ctrl>+<shift>+<f13>") == (0x2 | 0x4, 0x7C)
    assert parse_combo("<cmd>+space") == (0x8, 0x20)
    assert parse_combo("<ctrl>+<alt>") is None  # no main key
    assert parse_combo("<ctrl>+a+b") is None  # two main keys
    assert parse_combo("<ctrl>+<bilinmeyen>") is None


def test_key_vk():
    assert key_vk("<scroll_lock>") == 0x91
    assert key_vk("<pause>") == 0x13
    assert key_vk("5") == ord("5")
    assert key_vk("ş") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows'ta kancasız yol kullanılır")
def test_quickbar_uses_pynput_off_windows(monkeypatch):
    from alice.app import quick

    started = {}

    class FakeGHK:
        def __init__(self, mapping):
            started["keys"] = sorted(mapping)
            self.daemon = False

        def start(self):
            started["ok"] = True

    fake = type(sys)("pynput")
    fake.keyboard = type(sys)("keyboard")
    fake.keyboard.GlobalHotKeys = FakeGHK
    monkeypatch.setitem(sys.modules, "pynput", fake)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake.keyboard)
    qb = quick.QuickBar.__new__(quick.QuickBar)
    qb.hotkeys = {"open": "<ctrl>+<alt>+a", "region": "<ctrl>+<alt>+s", "clipboard": ""}
    qb._listener = None
    assert qb.start_hotkeys() and started == {"keys": ["<ctrl>+<alt>+a", "<ctrl>+<alt>+s"], "ok": True}
