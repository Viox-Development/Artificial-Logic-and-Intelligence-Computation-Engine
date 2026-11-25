"""Screen capture supports both current and older mss releases."""

import sys
from types import SimpleNamespace

import pytest

from alice.tools.imaging import screen_capture
from alice.voice.live import monitor_layout


def fake_mss(monkeypatch, modern, calls):
    class Capture:
        monitors = [{}, {"left": 0, "width": 1920, "height": 1080}]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

    def factory():
        calls.append("opened")
        return Capture()

    def deprecated():
        raise AssertionError("modern mss should not use the deprecated factory")

    module = SimpleNamespace(mss=deprecated if modern else factory)
    if modern:
        module.MSS = factory
    monkeypatch.setitem(sys.modules, "mss", module)
    return Capture


@pytest.mark.parametrize("modern", [False, True])
def test_screen_capture_uses_supported_mss_factory(monkeypatch, modern):
    calls = []
    capture = fake_mss(monkeypatch, modern, calls)

    sct = screen_capture()
    assert isinstance(sct, capture)
    assert calls == ["opened"]  # the caller owns it: nothing is closed for them


def test_screen_capture_without_mss_raises_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", None)
    with pytest.raises(ImportError):
        screen_capture()


@pytest.mark.parametrize("modern", [False, True])
def test_monitor_layout_uses_supported_mss_factory(monkeypatch, modern):
    calls = []
    fake_mss(monkeypatch, modern, calls)

    assert monitor_layout() == "one monitor (1)"
    assert calls == ["opened", "closed"]
