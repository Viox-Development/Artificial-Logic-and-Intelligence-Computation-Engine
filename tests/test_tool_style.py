"""Do tool definitions follow the core rules in docs/TOOL-STYLE.md (the writing style of Claude Code tools).

The model learns when and how to use a tool only from its description: one-sentence tools with undescribed
parameters were used wrongly or not at all."""

from __future__ import annotations

from typing import Any

import pytest

from alice.tools import builtin_tools

MIN_DESC = 150


def _props(schema: dict[str, Any]) -> dict[str, Any]:
    return schema.get("properties") or {}


def _walk(schema: dict[str, Any], where: str):
    for name, prop in _props(schema).items():
        yield f"{where}.{name}", prop
        if prop.get("type") == "object":
            yield from _walk(prop, f"{where}.{name}")
        items = prop.get("items")
        if isinstance(items, dict) and items.get("type") == "object":
            yield from _walk(items, f"{where}.{name}[]")


TOOLS = builtin_tools()


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_description_says_what_and_when(tool):
    desc = tool.description.strip()
    assert len(desc) >= MIN_DESC, f"{tool.name}: açıklama {len(desc)} karakter; ne yapar, ne döner, ne zaman kullanılmaz"
    assert desc[0].isupper(), f"{tool.name}: açıklama büyük harfle, ne yaptığını söyleyen bir cümleyle başlamalı"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_every_parameter_is_described(tool):
    schema = tool.parameters
    for path, prop in _walk(schema, tool.name):
        assert len(str(prop.get("description") or "")) >= 8, f"{path}: parametre açıklaması yok ya da çok kısa"
    for req in schema.get("required") or []:
        assert req in _props(schema), f"{tool.name}: required '{req}' tanımlı değil"


def test_tool_names_are_unique_snake_case():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names))
    for n in names:
        assert n == n.lower() and " " not in n and "-" not in n, n


def test_claude_code_counterparts_exist():
    """The mapping in docs/TOOL-STYLE.md: every core Claude Code tool has an ALICE counterpart."""
    names = {t.name for t in TOOLS}
    for n in ("read_file", "write_file", "edit_file", "multi_edit", "glob", "grep", "notebook_edit", "run_command",
              "process_output", "process_stop", "process_wait", "web_fetch", "web_search", "task", "ask_user",
              "todo_write", "skill", "load_tools", "exit_plan_mode", "send_file", "schedule"):
        assert n in names, n
