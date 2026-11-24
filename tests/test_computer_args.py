"""Loose JSON arguments: the computer / computer_task tools and the screen agent's actions handle numbers given as
text, "false"-style flags and missing fields without crashing, returning a recoverable error."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from alice import computer_agent as ca
from alice.computer_agent import Screen, Shot
from alice.tools import computer
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config


class PG(types.ModuleType):
    def __init__(self):
        super().__init__("pyautogui")
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        if name.startswith("_") or name in ("FAILSAFE", "PAUSE"):
            raise AttributeError(name)
        return lambda *a, **k: self.calls.append((name, *a))


@pytest.fixture
def pg(monkeypatch):
    fake = PG()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    return fake


@pytest.fixture
def ctx(workdir):
    c = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    c.state["screen"] = {"scale": 2.0, "left": 10, "top": 0, "w": 100, "h": 100}
    return c


@pytest.fixture
def sleeps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(computer, "time", SimpleNamespace(sleep=slept.append))
    return slept


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (None, False), ("false", False),
                                            ("False", False), ("0", False), ("", False), ("true", True),
                                            ("evet", True), (1, True), (0, False), (0.0, False)])
def test_as_bool(value, expected):
    assert computer.as_bool(value) is expected


@pytest.mark.parametrize("value", ["belki", [1], {"a": 1}])
def test_as_bool_rejects_garbage(value):
    with pytest.raises(ToolError, match="true or false"):
        computer.as_bool(value, "submit")


@pytest.mark.parametrize("value,kind,expected", [("12", int, 12), (" 3.7 ", float, 3.7), (4.9, int, 4),
                                                 ("-2", int, -2), (7, float, 7.0)])
def test_as_number(value, kind, expected):
    assert computer.as_number(value, "n", kind) == expected


@pytest.mark.parametrize("value", ["on iki", None, [1], True, "nan", "inf"])
def test_as_number_rejects_garbage(value):
    with pytest.raises(ToolError, match="n "):
        computer.as_number(value, "n")


def test_click_with_string_coordinates(ctx, pg):
    r = computer.Computer().run(ctx, action="click", x="50", y="20.5")  # previously "50" * 2.0 raised TypeError
    assert pg.calls[-1] == ("click", 110, 41)
    assert r.text == "click (50,20)"
    with pytest.raises(ToolError, match="x must be a number"):
        computer.Computer().run(ctx, action="click", x="sol", y=1)


def test_scroll_and_drag_with_strings(ctx, pg):
    computer.Computer().run(ctx, action="scroll", x=1, y=1, amount="3")
    assert pg.calls[-1][0] == "scroll"
    computer.Computer().run(ctx, action="drag", x="1", y="1", to_x="5", to_y="5")
    assert pg.calls[-1][:3] == ("dragTo", 20, 10)
    computer.Computer().run(ctx, action="scroll", x=1, y=1, amount=None)


def test_wait_with_string_or_null_seconds(ctx, sleeps):
    computer.Computer().run(ctx, action="wait", seconds="2.5")
    computer.Computer().run(ctx, action="wait", seconds=None)  # used to raise TypeError from float(None)
    assert sleeps == [2.5, 1.0]
    with pytest.raises(ToolError, match="seconds must be a number"):
        computer.Computer().run(ctx, action="wait", seconds="biraz")


def test_type_and_key_non_string(ctx, pg, monkeypatch):
    typed: list[str] = []
    monkeypatch.setattr(computer, "type_text", typed.append)
    assert computer.Computer().run(ctx, action="type", text=2024).text == "typed 4 characters"
    assert typed == ["2024"]
    computer.Computer().run(ctx, action="key", keys=5)
    assert pg.calls[-1] == ("press", "5")


def test_screenshot_after_string_false_and_bad_monitor(ctx, pg, monkeypatch):
    shots: list = []
    monkeypatch.setattr(computer, "grab_screen", lambda c, grid=False, monitor=1: shots.append((grid, monitor))
                        or ("b64", "d"))
    r = computer.Computer().run(ctx, action="move", x=1, y=1, screenshot_after="false")
    assert not r.images and shots == []  # the "false" text used to trigger an extra screenshot
    computer.Computer().run(ctx, action="screenshot", grid="false", monitor="2")
    assert shots == [(False, 2)]
    with pytest.raises(ToolError, match="monitor must be 0 or positive"):
        computer.Computer().run(ctx, action="screenshot", monitor=-1)
    with pytest.raises(ToolError, match="monitor must be a number"):
        computer.Computer().run(ctx, action="screenshot", monitor="ikinci")


def test_computer_task_bad_numbers_release_lock(ctx, monkeypatch):
    ctx.router = object()
    monkeypatch.setattr(ca, "log_line", lambda s: None)
    monkeypatch.setattr(ca, "router_asker", lambda r, role: (lambda m: ""))
    with pytest.raises(ToolError, match="max_steps must be a number"):
        computer.ComputerTask().run(ctx, goal="x", max_steps="çok")
    assert ca._RUNNING.acquire(blocking=False)
    ca._RUNNING.release()


# ------------------------------------------------------------------ screen agent


SHOT = Shot("", 100, 50, 2000, 1000, [])


def test_agent_open_rejects_empty_target(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("alice.tools.desktop.open_app", opened.append)
    for target in ("", "   ", None):
        with pytest.raises(ValueError, match="target is required"):
            Screen(None).act({"action": "open", "target": target}, SHOT)
    assert opened == []  # On Windows, Path("") is "." and was opening the working folder


def test_agent_open_refuses_script_with_trailing_dot(monkeypatch):
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: pytest.fail("açılmamalı"))
    with pytest.raises(ValueError, match="betik"):
        Screen(None).act({"action": "open", "target": "C:/indirilen/kur.bat."}, SHOT)


def test_point_is_clamped_to_screen():
    # if the model sends pixels instead of 0-1000, don't click off-screen or on another monitor
    assert Screen.point({"point": [1500, -20]}, SHOT) == (100, 1050)
    with pytest.raises(ValueError, match="id ya da point"):
        Screen.point({"point": "500,500"}, SHOT)
