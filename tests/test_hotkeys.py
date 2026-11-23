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

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")  # an X11 desktop session
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
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


# ---------------------------------------------------------------- Linux/macOS: pynput, Wayland (#235)
def _session(monkeypatch, display=":0", wayland=False):
    from alice import hotkeys

    monkeypatch.setattr(hotkeys.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", display)
    else:
        monkeypatch.delenv("DISPLAY", raising=False)
    if wayland:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    return hotkeys


def test_pynput_problem_explains_wayland_missing_display_and_missing_pynput(monkeypatch):
    hk = _session(monkeypatch, wayland=True)
    assert hk.wayland_session() and "alice quick --open" in hk.pynput_problem()
    hk = _session(monkeypatch, display="")
    assert "DISPLAY" in hk.pynput_problem()
    hk = _session(monkeypatch)
    monkeypatch.setitem(sys.modules, "pynput", None)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", None)
    assert "alice-harness[voice]" in hk.pynput_problem()


def test_pynput_hotkeys_start_stop_and_report_errors(monkeypatch):
    hk = _session(monkeypatch)
    made = []

    class GHK:
        def __init__(self, mapping):
            if "bozuk" in mapping:
                raise ValueError("bozuk kısayol")
            self.mapping, self.stopped = mapping, False
            made.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    fake = type(sys)("pynput")
    fake.keyboard = type(sys)("keyboard")
    fake.keyboard.GlobalHotKeys = GHK
    monkeypatch.setitem(sys.modules, "pynput", fake)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake.keyboard)

    keys = hk.PynputHotkeys({"<ctrl>+<alt>+a": lambda: None, "": lambda: None})
    assert keys.start() and list(made[0].mapping) == ["<ctrl>+<alt>+a"] and made[0].daemon
    keys.stop()
    assert made[0].stopped
    bad = hk.PynputHotkeys({"bozuk": lambda: None})
    assert not bad.start() and "bozuk kısayol" in bad.problem
    _session(monkeypatch, wayland=True)
    w = hk.PynputHotkeys({"<ctrl>+<alt>+a": lambda: None})
    assert not w.start() and "Wayland" in w.problem and len(made) == 1  # nothing is started on Wayland


def test_open_running_reuses_the_quick_bar_of_a_running_process(monkeypatch, tmp_path):
    from alice.app import quick

    monkeypatch.setattr(quick, "url_file", lambda: tmp_path / "quick-url")
    launched = []
    monkeypatch.setattr(quick.QuickBar, "_launch", staticmethod(launched.append))
    assert not quick.open_running()  # nothing is running

    import http.server
    import threading

    class OK(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), OK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/quick?t=abc"
        (tmp_path / "quick-url").write_text(url, encoding="utf-8")
        assert quick.open_running() and launched == [url]
    finally:
        srv.shutdown()
    assert not quick.open_running()  # stale file: the process is gone
    (tmp_path / "quick-url").write_text("https://evil.example/", encoding="utf-8")
    assert not quick.open_running()  # only our own local server


def test_quick_cmd_open_uses_the_running_quick_bar(monkeypatch):
    from alice.app import quick

    monkeypatch.setattr(quick, "open_running", lambda: True)
    monkeypatch.setattr(quick, "QuickBar", lambda *a, **k: (_ for _ in ()).throw(AssertionError("new server")))
    assert quick.quick_cmd(["--open"]) == 0


def test_core_keeps_the_quick_bar_on_wayland_for_a_desktop_shortcut(monkeypatch):
    from alice.app import quick
    from alice.core import cli as core_cli

    _session(monkeypatch, wayland=True)
    monkeypatch.setattr("alice.hotkeys.IS_WIN", False)

    class QB:
        stopped = False

        def __init__(self, cfg, state=None):
            pass

        def start_hotkeys(self):
            return False

        def stop(self):
            QB.stopped = True

    monkeypatch.setattr(quick, "QuickBar", QB)
    qb = core_cli._start_quick(object())
    assert isinstance(qb, QB) and not QB.stopped  # kept running: `alice quick --open` finds it
    _session(monkeypatch, display="")  # no desktop session at all: skipped
    assert core_cli._start_quick(object()) is None
