"""Linux UI elements through AT-SPI (alice/tools/atspi.py), with a fake Atspi module (#236)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alice.tools import atspi, desktop
from alice.tools.base import ToolContext, ToolError

STATES = SimpleNamespace(ACTIVE="active", SHOWING="showing", SENSITIVE="sensitive", ENABLED="enabled")


class Rect(SimpleNamespace):
    pass


class Action:
    def __init__(self, names, log):
        self.names, self.log = names, log

    def get_n_actions(self):
        return len(self.names)

    def get_action_name(self, i):
        return self.names[i]

    def do_action(self, i):
        self.log.append(("action", self.names[i]))
        return True


class Component:
    def __init__(self, rect, log):
        self.rect, self.log = rect, log

    def get_extents(self, coord):
        assert coord == "screen"
        return Rect(x=self.rect[0], y=self.rect[1], width=self.rect[2], height=self.rect[3])

    def grab_focus(self):
        self.log.append(("focus",))
        return True


class Text:
    def __init__(self, acc):
        self.acc = acc

    def get_text(self, start, end):
        return self.acc.text


class Editable:
    def __init__(self, acc):
        self.acc = acc

    def set_text_contents(self, text):
        self.acc.text = text
        return True


class Acc:
    def __init__(self, role, name="", children=(), states=("showing", "sensitive"), rect=(10, 20, 100, 30),
                 actions=(), text=None, log=None):
        self.role, self.name, self.children, self.states = role, name, list(children), set(states)
        self.rect, self.actions, self.text = rect, list(actions), text
        self.log = log if log is not None else []

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_state_set(self):
        return SimpleNamespace(contains=lambda s: s in self.states)

    def get_component_iface(self):
        return Component(self.rect, self.log) if self.rect else None

    def get_action_iface(self):
        return Action(self.actions, self.log) if self.actions else None

    def get_text_iface(self):
        return Text(self) if self.text is not None else None

    def get_editable_text_iface(self):
        return Editable(self) if self.text is not None else None


def fake_atspi(windows):
    desktop_acc = Acc("desktop", children=[Acc("application", "app", children=windows)])
    return SimpleNamespace(StateType=STATES, CoordType=SimpleNamespace(SCREEN="screen"),
                           get_desktop=lambda i: desktop_acc)


def window(log):
    return Acc("frame", "Belge - Metin Düzenleyici", states=("active", "showing"), children=[
        Acc("panel", children=[
            Acc("push button", "Kaydet", actions=["click"], log=log),
            Acc("push button", "Gizli", states=("sensitive",), actions=["click"]),  # not showing
            Acc("check box", "Satır numaraları", actions=["toggle"], log=log),
            Acc("text", "", text="merhaba", log=log),
            Acc("label", "Sadece yazı"),  # not interactive
            Acc("push button", "", actions=["click"]),  # nameless button: skipped
            Acc("push button", "Sıfır", rect=(0, 0, 1, 1)),  # too small
            Acc("push button", "Pasif", states=("showing",), actions=["click"]),
        ]),
    ])


def test_collect_lists_interactive_visible_elements_in_uia_format():
    log = []
    title, items, wrappers = atspi.collect(atspi=fake_atspi([Acc("frame", "Arka", states=("showing",)), window(log)]))
    assert title == "Belge - Metin Düzenleyici"
    assert [(i["id"], i["role"], i["name"], i["value"], i["enabled"]) for i in items] == [
        (1, "Button", "Kaydet", "", True),
        (2, "CheckBox", "Satır numaraları", "", True),
        (3, "Edit", "", "merhaba", True),
        (4, "Button", "Pasif", "", False),
    ]
    assert items[0]["rect"] == (10, 20, 110, 50) and items[0]["center"] == (60, 35)
    assert set(wrappers) == {1, 2, 3, 4}


def test_window_by_title_and_errors():
    log = []
    at = fake_atspi([Acc("frame", "Arka", states=("showing",)), window(log)])
    assert atspi.find_window(at, "metin düzen").get_name() == "Belge - Metin Düzenleyici"
    with pytest.raises(RuntimeError, match="no window titled .* found"):
        atspi.find_window(at, "Discord")
    with pytest.raises(RuntimeError, match="no foreground window"):
        atspi.find_window(fake_atspi([Acc("frame", "Arka", states=("showing",))]))


def test_elements_act_like_pywinauto_wrappers_for_ui_act(monkeypatch):
    log = []
    _, items, wrappers = atspi.collect(atspi=fake_atspi([window(log)]))
    ctx = ToolContext(cwd=".", config=None)
    ctx.state["ui_elems"], ctx.state["ui_items"] = wrappers, {i["id"]: i for i in items}
    assert desktop.ui_act(ctx, 1, "click") == "click -> [1] Button 'Kaydet'"
    assert ("action", "click") in log
    desktop.ui_act(ctx, 2, "toggle")
    assert ("action", "toggle") in log
    monkeypatch.setattr(desktop, "SET_TEXT_SETTLE", 0)
    desktop.ui_act(ctx, 3, "set_text", "yeni metin")
    assert wrappers[3].get_value() == "yeni metin"
    desktop.ui_act(ctx, 3, "focus")
    assert ("focus",) in log


def test_click_falls_back_to_coordinates_without_an_action(monkeypatch):
    done = []
    monkeypatch.setitem(__import__("sys").modules, "pyautogui",
                        SimpleNamespace(click=lambda x, y, clicks=1, button="left": done.append((x, y, button))))
    el = atspi.AtspiElement(fake_atspi([]), Acc("push button", "X"), (0, 0, 40, 20))
    with pytest.raises(RuntimeError):
        el.invoke()
    el.click_input()
    el.right_click_input()
    assert done == [(20, 10, "left"), (20, 10, "right")]


def test_collect_ui_elements_uses_atspi_on_linux(monkeypatch):
    log = []
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setattr(atspi, "load", lambda: fake_atspi([window(log)]))
    ctx = ToolContext(cwd=".", config=None)
    title, items = desktop.collect_ui_elements(ctx)
    assert title.startswith("Belge") and ctx.state["ui_items"][1]["name"] == "Kaydet"
    assert isinstance(ctx.state["ui_elems"][1], atspi.AtspiElement)
    assert "[1] Button 'Kaydet'" in desktop.format_ui(title, items)


def test_missing_pygobject_gives_install_hint(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setitem(__import__("sys").modules, "gi", None)
    with pytest.raises(ToolError, match="gir1.2-atspi-2.0"):
        desktop.collect_ui_elements(ToolContext(cwd=".", config=None))
