"""Workflow views (Agents panel on the desktop, Workflows page on the phone): static checks on the scripts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import alice

STATIC = Path(alice.__file__).parent / "app" / "static"


def _section(name: str, title: str) -> str:
    """The part of a script between its `/* ==== title` heading and the next heading."""
    src = (STATIC / name).read_text(encoding="utf-8")
    m = re.search(rf"/\* =+ {title}\n(.*?)(?=\n/\* =====|\Z)", src, re.S)
    assert m, f"{name}: no section {title!r}"
    return m.group(1)


@pytest.mark.parametrize("name, title", [("ui.js", "Workflows"), ("m.js", "workflows")])
def test_text_from_runs_never_becomes_html(name, title):
    """Labels, logs and results come from models and the pages they read."""
    code = _section(name, title)
    assert "textContent" in code
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "setMd", "marked", "md("):
        assert sink not in code, f"{name}: {sink}"


@pytest.mark.parametrize("name", ["ui.js", "m.js"])
def test_views_use_the_server_contract(name):
    src = (STATIC / name).read_text(encoding="utf-8")
    assert '"/api/workflows' in src and "/api/workflows/" in src
    assert re.search(r"handlers\.workflow = onWorkflowEvent|workflow: \(ev\) => onWorkflow\(ev\)", src)


def test_chat_switch_reloads_the_runs():
    """app.js calls into ui.js; a rename there would break every status update."""
    app, ui = ((STATIC / n).read_text(encoding="utf-8") for n in ("app.js", "ui.js"))
    for fn in ("onWorkflowSession", "loadWorkflows"):
        assert f"{fn}();" in app and re.search(rf"function {fn}\(", ui)
