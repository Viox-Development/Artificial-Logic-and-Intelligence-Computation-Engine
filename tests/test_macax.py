"""macOS UI elements: parsing System Events' output and wiring into ui_elements."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alice.tools import macax

SEP = macax.SEP
OUT = "\n".join([
    "Untitled",
    SEP.join(["AXButton", "Save", "", "100", "200", "80", "24"]),
    SEP.join(["AXStaticText", "Label", "", "10", "10", "50", "20"]),          # not interactive: skipped
    SEP.join(["AXTextField", "", "hello", "300", "40", "200", "22"]),
    SEP.join(["AXButton", "", "", "0", "0", "20", "20"]),                       # no name: skipped
    SEP.join(["AXCheckBox", "Wrap", "1", "5", "5", "1", "1"]),                  # too small: skipped
    "garbage line",
]) + "\n"


def test_parse():
    title, items = macax.parse(OUT)
    assert title == "Untitled"
    assert [(i["role"], i["name"], i["value"]) for i in items] == [("Button", "Save", ""), ("Edit", "", "hello")]
    assert items[0]["center"] == (140, 212) and items[0]["rect"] == (100, 200, 180, 224) and items[1]["id"] == 2


def test_collect_and_permission_error(monkeypatch):
    monkeypatch.setattr(macax.shutil, "which", lambda name: "/usr/bin/osascript")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout=OUT, stderr="")

    win, items, wrappers = macax.collect("Untitled", run=run)
    assert win == "Untitled" and len(items) == 2 and set(wrappers) == {1, 2} and seen[0][-1] == "Untitled"
    denied = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="osascript is not allowed assistive access. (-1719)")  # noqa: E731
    with pytest.raises(RuntimeError, match="Accessibility permission"):
        macax.collect(run=denied)


def test_ui_elements_uses_macax_on_macos(monkeypatch):
    from alice.tools import desktop

    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop, "IS_MAC", True)
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    title, items = macax.parse(OUT)
    monkeypatch.setattr(macax, "collect", lambda t, limit, budget: (title, items, {i["id"]: macax.MacElement(i["rect"]) for i in items}))
    ctx = SimpleNamespace(state={})
    assert desktop.collect_ui_elements(ctx)[0] == "Untitled" and isinstance(ctx.state["ui_elems"][1], macax.MacElement)
