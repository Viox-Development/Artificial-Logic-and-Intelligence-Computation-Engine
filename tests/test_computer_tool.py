"""Unit tests for alice/tools/computer.py: pyautogui/mss/pyperclip are replaced with fake modules,
no real screen, mouse or keyboard is used."""

from __future__ import annotations

import base64
import io
import sys
import threading
import types
from types import SimpleNamespace

import pytest
from PIL import Image

from alice.tools import computer
from alice.tools.base import COMPUTER, READ, ToolContext, ToolError
from tests.conftest import make_config


class FakePyAutoGUI(types.ModuleType):
    def __init__(self):
        super().__init__("pyautogui")
        self.calls: list[tuple] = []
        self.FAILSAFE = False
        self.PAUSE = 1.0
        self.pos = (0, 0)

    def write(self, text, interval=0.0):
        self.calls.append(("write", text, interval))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", *keys))

    def press(self, key):
        self.calls.append(("press", key))

    def click(self, x, y, clicks=1, button="left", interval=0.0):
        self.calls.append(("click", x, y, clicks, button))

    def moveTo(self, x, y, duration=0.0):
        self.calls.append(("moveTo", x, y))

    def dragTo(self, x, y, duration=0.0, button="left"):
        self.calls.append(("dragTo", x, y, button))

    def scroll(self, n):
        self.calls.append(("scroll", n))

    def position(self):
        return self.pos


class FakeClipboard(types.ModuleType):
    def __init__(self, content="eski", fail_paste=False):
        super().__init__("pyperclip")
        self.content = content
        self.fail_paste = fail_paste
        self.copies: list[str] = []

    def paste(self):
        if self.fail_paste:
            raise RuntimeError("pano yok")
        return self.content

    def copy(self, text):
        self.copies.append(text)
        self.content = text


def make_fake_mss(width=200, height=100, monitors=None):
    mons = monitors or [{"left": 0, "top": 0, "width": width, "height": height},
                        {"left": 10, "top": 20, "width": width, "height": height}]

    class Shot:
        def __init__(self, mon):
            self.size = (mon["width"], mon["height"])
            self.bgra = bytes([10, 20, 30, 255]) * (mon["width"] * mon["height"])

    class Sct:
        monitors = mons

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def grab(self, mon):
            return Shot(mon)

    mod = types.ModuleType("mss")
    mod.mss = Sct
    return mod


@pytest.fixture
def pg(monkeypatch):
    fake = FakePyAutoGUI()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(computer, "time", SimpleNamespace(sleep=slept.append))
    return slept


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, screenshot_max_width=100))


def set_platform(monkeypatch, plat):
    monkeypatch.setattr(computer, "sys", SimpleNamespace(platform=plat))


# ------------------------------------------------------------------ helpers


def test_pyautogui_missing_raises_tool_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    with pytest.raises(ToolError, match="pyautogui"):
        computer._pyautogui()


def test_pyautogui_enables_failsafe(pg):
    assert computer._pyautogui() is pg
    assert pg.FAILSAFE is True and pg.PAUSE == 0.05


def test_dpi_aware_noop_off_windows(monkeypatch):
    set_platform(monkeypatch, "linux")
    monkeypatch.setattr(computer, "_DPI_SET", False)
    computer._dpi_aware()
    assert computer._DPI_SET is False


def test_dpi_aware_windows_falls_back_and_runs_once(monkeypatch):
    set_platform(monkeypatch, "win32")
    monkeypatch.setattr(computer, "_DPI_SET", False)
    calls = []

    def shcore_fail(_):
        raise OSError("eski windows")

    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = SimpleNamespace(shcore=SimpleNamespace(SetProcessDpiAwareness=shcore_fail),
                                         user32=SimpleNamespace(SetProcessDPIAware=lambda: calls.append("u32")))
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    computer._dpi_aware()
    computer._dpi_aware()  # the second call should do nothing
    assert calls == ["u32"]
    assert computer._DPI_SET is True


def test_dpi_aware_windows_swallows_all_errors(monkeypatch):
    set_platform(monkeypatch, "win32")
    monkeypatch.setattr(computer, "_DPI_SET", False)
    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = SimpleNamespace()  # every attribute access raises AttributeError
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    computer._dpi_aware()  # must not raise an exception
    assert computer._DPI_SET is True


def test_to_real_requires_screenshot(ctx):
    with pytest.raises(ToolError, match="screenshot"):
        computer.to_real(ctx, 1, 2)


def test_to_real_applies_scale_and_offset(ctx):
    ctx.state["screen"] = {"scale": 2.0, "left": 100, "top": 50, "w": 10, "h": 10}
    assert computer.to_real(ctx, 10, 20) == (120, 90)
    assert computer.to_real(ctx, 0.4, 0.4) == (100, 50)  # int() rounds down


def test_type_text_ascii_uses_write(pg):
    computer.type_text("hello")
    assert pg.calls == [("write", "hello", 0.01)]


def test_type_text_non_ascii_pastes_and_restores(pg, monkeypatch, sleeps):
    clip = FakeClipboard("eski")
    monkeypatch.setitem(sys.modules, "pyperclip", clip)
    set_platform(monkeypatch, "linux")
    computer.type_text("şığ")
    assert ("hotkey", "ctrl", "v") in pg.calls
    assert clip.copies == ["şığ", "eski"]
    assert clip.content == "eski"


def test_type_text_non_ascii_mac_uses_command(pg, monkeypatch, sleeps):
    clip = FakeClipboard("x")
    monkeypatch.setitem(sys.modules, "pyperclip", clip)
    set_platform(monkeypatch, "darwin")
    computer.type_text("ü")
    assert ("hotkey", "command", "v") in pg.calls


def test_type_text_paste_failure_does_not_restore(pg, monkeypatch, sleeps):
    clip = FakeClipboard(fail_paste=True)
    monkeypatch.setitem(sys.modules, "pyperclip", clip)
    computer.type_text("ö")
    assert clip.copies == ["ö"]


def test_type_text_non_ascii_without_pyperclip(pg, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyperclip", None)
    with pytest.raises(ToolError, match="pyperclip"):
        computer.type_text("ç")


@pytest.mark.parametrize("keys,expected", [
    ("enter", ("press", "enter")),
    ("Return", ("press", "enter")),
    ("esc", ("press", "escape")),
    ("ctrl+s", ("hotkey", "ctrl", "s")),
    ("Control + Shift + Esc", ("hotkey", "ctrl", "shift", "escape")),
    ("cmd+pgdn", ("hotkey", "command", "pagedown")),
    ("alt++tab", ("hotkey", "alt", "tab")),  # empty parts are dropped
])
def test_press_keys_aliases(pg, keys, expected):
    computer.press_keys(keys)
    assert pg.calls == [expected]


# ------------------------------------------------------------------ screenshot


def _decode(b64):
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def test_grab_screen_missing_mss(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", None)
    with pytest.raises(ToolError, match="mss"):
        computer.grab_screen(ctx)


def test_grab_screen_scales_and_stores_state(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", make_fake_mss(200, 100))
    b64, desc = computer.grab_screen(ctx)
    img = _decode(b64)
    assert img.size == (100, 50)
    assert ctx.state["screen"] == {"scale": 2.0, "left": 10, "top": 20, "w": 100, "h": 50}
    assert "100x50" in desc and "200x100" in desc and "monitor 1" in desc
    # coordinate conversion should work now
    assert computer.to_real(ctx, 50, 25) == (110, 70)


def test_grab_screen_small_image_not_scaled_with_grid(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", make_fake_mss(80, 250))
    b64, _ = computer.grab_screen(ctx, grid=True)
    img = _decode(b64).convert("RGB")
    assert img.size == (80, 250)
    assert ctx.state["screen"]["scale"] == 1.0
    # there should be magenta grid lines at y=100 and y=200
    assert img.getpixel((40, 100)) == (255, 0, 255)
    assert img.getpixel((40, 200)) == (255, 0, 255)
    assert img.getpixel((40, 150)) != (255, 0, 255)


def test_grab_screen_grid_vertical_lines(workdir, monkeypatch):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, screenshot_max_width=1000))
    monkeypatch.setitem(sys.modules, "mss", make_fake_mss(250, 40))
    b64, _ = computer.grab_screen(ctx, grid=True)
    img = _decode(b64).convert("RGB")
    assert img.size == (250, 40)
    assert img.getpixel((100, 30)) == (255, 0, 255)
    assert img.getpixel((200, 30)) == (255, 0, 255)
    assert img.getpixel((150, 30)) != (255, 0, 255)


def test_type_text_restore_failure_is_ignored(pg, monkeypatch, sleeps):
    class Clip(FakeClipboard):
        def copy(self, text):
            if self.copies:
                raise RuntimeError("pano kilitli")
            super().copy(text)

    clip = Clip("eski")
    monkeypatch.setitem(sys.modules, "pyperclip", clip)
    computer.type_text("ğ")  # restore errors are swallowed
    assert clip.copies == ["ğ"]


def test_grab_screen_bad_monitor(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", make_fake_mss())
    with pytest.raises(ToolError, match="there is no monitor 5; there are 1 monitors"):
        computer.grab_screen(ctx, monitor=5)


def test_grab_screen_monitor_zero_uses_all(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", make_fake_mss(60, 40))
    _, desc = computer.grab_screen(ctx, monitor=0)
    assert ctx.state["screen"]["left"] == 0 and "monitor 0" in desc


# ------------------------------------------------------------------ Computer tool


def test_risk_for():
    t = computer.Computer()
    for a in ("screenshot", "cursor_position", "wait"):
        assert t.risk_for({"action": a}) == READ
    for a in ("click", "type", "key", "drag"):
        assert t.risk_for({"action": a}) == COMPUTER
    assert t.risk_for({}) == COMPUTER


def test_run_screenshot_returns_image(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", make_fake_mss())
    r = computer.Computer().run(ctx, action="screenshot")
    assert len(r.images) == 1 and "screenshot" in r.text


@pytest.mark.parametrize("seconds,expected", [(2, 2.0), (-3, 0.0), (999, 30.0)])
def test_run_wait_is_clamped(ctx, sleeps, seconds, expected):
    r = computer.Computer().run(ctx, action="wait", seconds=seconds)
    assert sleeps == [expected]
    assert "beklendi" in r.text and not r.images


def test_run_cursor_position_without_and_with_screen(ctx, pg):
    pg.pos = (300, 140)
    t = computer.Computer()
    assert "(300, 140)" in t.run(ctx, action="cursor_position").text
    ctx.state["screen"] = {"scale": 2.0, "left": 100, "top": 40}
    assert "(100, 50)" in t.run(ctx, action="cursor_position").text


def test_run_type_and_key(ctx, pg):
    t = computer.Computer()
    assert t.run(ctx, action="type", text="abc").text == "typed 3 characters"
    assert t.run(ctx, action="key", keys="ctrl+c").text == "key: ctrl+c"
    assert pg.calls == [("write", "abc", 0.01), ("hotkey", "ctrl", "c")]


def test_run_type_and_key_validation(ctx, pg):
    t = computer.Computer()
    with pytest.raises(ToolError, match="text"):
        t.run(ctx, action="type")
    with pytest.raises(ToolError, match="keys"):
        t.run(ctx, action="key", keys="")
    # empty text is valid (not None)
    assert t.run(ctx, action="type", text="").text == "typed 0 characters"


def test_run_pointer_actions_need_xy_and_screenshot(ctx, pg):
    t = computer.Computer()
    with pytest.raises(ToolError, match="needs x and y"):
        t.run(ctx, action="click", x=5)
    with pytest.raises(ToolError, match="screenshot"):
        t.run(ctx, action="click", x=5, y=5)


@pytest.mark.parametrize("action,expected", [
    ("click", ("click", 120, 90, 1, "left")),
    ("double_click", ("click", 120, 90, 2, "left")),
    ("right_click", ("click", 120, 90, 1, "right")),
    ("middle_click", ("click", 120, 90, 1, "middle")),
    ("move", ("moveTo", 120, 90)),
])
def test_run_click_variants(ctx, pg, action, expected):
    ctx.state["screen"] = {"scale": 2.0, "left": 100, "top": 50}
    r = computer.Computer().run(ctx, action=action, x=10, y=20)
    assert pg.calls == [expected]
    assert r.text == f"{action} (10,20)"


def test_run_drag(ctx, pg):
    ctx.state["screen"] = {"scale": 1.0, "left": 0, "top": 0}
    t = computer.Computer()
    with pytest.raises(ToolError, match="to_x"):
        t.run(ctx, action="drag", x=1, y=2)
    t.run(ctx, action="drag", x=1, y=2, to_x=30, to_y=40)
    assert pg.calls == [("moveTo", 1, 2), ("dragTo", 30, 40, "left")]


@pytest.mark.parametrize("plat,unit", [("win32", 120), ("darwin", 5), ("linux", 1)])
def test_run_scroll_units_per_platform(ctx, pg, monkeypatch, plat, unit):
    set_platform(monkeypatch, plat)
    ctx.state["screen"] = {"scale": 1.0, "left": 0, "top": 0}
    computer.Computer().run(ctx, action="scroll", x=5, y=6, amount=-3)
    assert pg.calls == [("moveTo", 5, 6), ("scroll", -3 * unit)]


def test_run_screenshot_after(ctx, pg, monkeypatch, sleeps):
    monkeypatch.setitem(sys.modules, "mss", make_fake_mss())
    ctx.state["screen"] = {"scale": 1.0, "left": 0, "top": 0}
    r = computer.Computer().run(ctx, action="click", x=1, y=1, screenshot_after=True)
    assert r.text.startswith("click (1,1)\nscreenshot")
    assert len(r.images) == 1 and sleeps == [0.4]


# ------------------------------------------------------------------ ComputerTask tool


class _FakeRes:
    def __init__(self, status):
        self.status = status
        self.summary = "özet"

    def report(self):
        return f"rapor:{self.status}"


@pytest.fixture
def fake_agent_mod(monkeypatch):
    import alice.computer_agent as ca

    rec: dict = {"logs": [], "status": "done"}

    class FakeAgent:
        def __init__(self, screen, ask, emit, stop):
            rec["screen"], rec["ask"], rec["emit"], rec["stop"] = screen, ask, emit, stop

        def run(self, goal, max_steps):
            rec["goal"], rec["max_steps"] = goal, max_steps
            rec["emit"]("adım 1")
            if rec["status"] == "raise":
                raise RuntimeError("çöktü")
            return _FakeRes(rec["status"])

    monkeypatch.setattr(ca, "ComputerAgent", FakeAgent)
    monkeypatch.setattr(ca, "Screen", lambda ctx, monitor=0: ("screen", monitor))
    monkeypatch.setattr(ca, "router_asker", lambda router, role: ("asker", role))
    monkeypatch.setattr(ca, "log_line", rec["logs"].append)
    monkeypatch.setattr(ca, "_RUNNING", threading.Lock())
    return rec


def test_computer_task_requires_router(ctx):
    with pytest.raises(ToolError, match="no model router"):
        computer.ComputerTask().run(ctx, goal="x")


def test_computer_task_runs_agent(ctx, fake_agent_mod):
    import alice.computer_agent as ca

    emitted = []
    ctx.router = object()
    ctx.emit = emitted.append
    r = computer.ComputerTask().run(ctx, goal="not defteri aç", max_steps=500, monitor=2)
    assert r.text == "rapor:done" and not r.is_error
    assert fake_agent_mod["max_steps"] == 60  # upper bound
    assert fake_agent_mod["screen"] == ("screen", 2)
    assert fake_agent_mod["ask"] == ("asker", "vision")
    assert emitted == ["adım 1"]
    assert fake_agent_mod["logs"][0] == "started: not defteri aç"
    assert fake_agent_mod["logs"][-1] == "bitti: done: özet"
    assert not ca._RUNNING.locked()


def test_computer_task_shows_progress_on_hud(ctx, fake_agent_mod, monkeypatch):
    """Live progress: while a screen task was running, the HUD showed nothing (5-20 s of blank between steps)."""
    from alice.hud import client

    sent = []
    monkeypatch.setattr(client, "active", True)
    monkeypatch.setattr(client, "send", lambda m: sent.append(m) or True)

    class StepAgent:
        def __init__(self, screen, ask, emit, stop):
            self.emit = emit

        def run(self, goal, max_steps):
            self.emit("1. [{\"action\": \"click\"}] -> click (10,10)")
            return _FakeRes("done")

    import alice.computer_agent as ca

    monkeypatch.setattr(ca, "ComputerAgent", StepAgent)
    ctx.router = object()
    computer.ComputerTask().run(ctx, goal="g")
    texts = [m.get("text") for m in sent if m.get("id") == "task"]
    assert texts[:2] == ["Ekran görevi · 1. adım", "Ekran görevi · 2. adım"]
    assert sent[-1] == {"kind": "clear", "id": "task"}  # removed from the HUD ("Ada") when finished


def test_screen_act_frames_click_on_hud(monkeypatch):
    import alice.computer_agent as ca
    from alice.hud import views
    from alice.tools import computer as comp

    seen, clicks = [], []
    monkeypatch.setattr(views, "click_target", lambda x, y, action="click", wait=True: seen.append((x, y, action)))
    monkeypatch.setattr(comp, "_pyautogui", lambda: type("PG", (), {"click": staticmethod(
        lambda *a, **k: clicks.append(a))})())
    shot = ca.Shot("", 0, 0, 1000, 1000, [], "")
    ca.Screen(None).act({"action": "right_click", "point": [500, 250]}, shot)
    assert seen == [(250, 500, "right_click")] and clicks == [(250, 500)]


@pytest.mark.parametrize("given,expected", [(0, 30), (-4, 1), (None, 30), (12, 12)])
def test_computer_task_max_steps_clamp(ctx, fake_agent_mod, given, expected):
    ctx.router = object()
    computer.ComputerTask().run(ctx, goal="g", max_steps=given)
    assert fake_agent_mod["max_steps"] == expected


def test_computer_task_error_status(ctx, fake_agent_mod):
    fake_agent_mod["status"] = "error"
    ctx.router = object()
    assert computer.ComputerTask().run(ctx, goal="g").is_error


def test_computer_task_releases_lock_on_exception(ctx, fake_agent_mod):
    import alice.computer_agent as ca

    fake_agent_mod["status"] = "raise"
    ctx.router = object()
    with pytest.raises(RuntimeError):
        computer.ComputerTask().run(ctx, goal="g")
    assert not ca._RUNNING.locked()


def test_computer_task_refuses_when_busy(ctx, fake_agent_mod):
    import alice.computer_agent as ca

    ctx.router = object()
    ca._RUNNING.acquire()
    try:
        r = computer.ComputerTask().run(ctx, goal="g")
    finally:
        ca._RUNNING.release()
    assert r.is_error and "already running" in r.text
    assert "goal" not in fake_agent_mod


def test_computer_task_uses_configured_role(workdir, fake_agent_mod):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, computer_role="eyes"), router=object())
    computer.ComputerTask().run(ctx, goal="g")
    assert fake_agent_mod["ask"] == ("asker", "eyes")
