"""Screen capture supports both current and older mss releases."""

import sys
from types import SimpleNamespace

import pytest

from alice.voice.live import monitor_layout


@pytest.mark.parametrize("modern", [False, True])
def test_monitor_layout_uses_supported_mss_factory(monkeypatch, modern):
    calls = []

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

    assert monitor_layout() == "tek monitör (1)"
    assert calls == ["opened", "closed"]
