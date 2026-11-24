"""Quality of tool definitions: as in Claude Code / Codex, every parameter is described, the description fits
the Live limit, and a bad call comes back with a message the model can fix."""

from __future__ import annotations

import pytest

from alice.tools import builtin_tools
from alice.tools.base import check_args, unknown_params
from alice.voice.live import declarations

TOOLS = builtin_tools()


def _props(schema: dict) -> dict:
    return schema.get("properties") or {}


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_description_fits_live_limit(tool):
    # Gemini Live cuts the description at 1000 characters (live.declarations): keep its end from being lost.
    assert 40 <= len(tool.description) <= 1000


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_every_parameter_is_described(tool):
    def walk(schema: dict, path: str) -> None:
        for name, p in _props(schema).items():
            assert (p.get("description") or "").strip(), f"{tool.name}: {path}{name} açıklamasız"
            if p.get("type") == "array" and isinstance(p.get("items"), dict):
                walk(p["items"], f"{path}{name}[].")

    walk(tool.parameters, "")
    assert set(tool.parameters.get("required") or []) <= set(_props(tool.parameters))


def test_tool_names_unique_and_live_declarations_build():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names))
    decls = declarations([t.spec() for t in TOOLS])
    assert all(d["description"] == t.description for d, t in zip(decls, TOOLS))


def _tool(name):
    return next(t for t in TOOLS if t.name == name)


def test_check_args_reports_missing_required_with_hint():
    msg = check_args(_tool("edit_file"), {"path": "a.py", "new_string": "x"})
    assert "old_string" in msg and "eksik" in msg and "read_file" in msg


def test_check_args_reports_bad_enum_with_valid_values():
    msg = check_args(_tool("windows"), {"action": "launch", "target": "notepad"})
    assert "launch" in msg and "open" in msg and "navigate" in msg


def test_check_args_accepts_valid_call():
    assert check_args(_tool("windows"), {"action": "open", "target": "notepad"}) == ""
    assert check_args(_tool("process_list"), {}) == ""


def test_unknown_params_lists_valid_ones():
    msg = unknown_params(_tool("web_search"), {"query": "x", "lang": "tr"})
    assert "lang" in msg and "max_results" in msg
    assert unknown_params(_tool("web_search"), {"query": "x"}) == ""


def test_empty_string_is_not_missing():
    # edit_file new_string="" means deleting the text; writing a file with empty content is valid too.
    assert check_args(_tool("edit_file"), {"path": "a", "old_string": "x", "new_string": ""}) == ""
    assert check_args(_tool("write_file"), {"path": "a", "content": ""}) == ""


def test_truncate_zero_or_negative_limit_does_not_return_whole_text():
    # text[-0:] used to return the whole text.
    from alice.tools.base import truncate
    text = "x" * 50
    for limit in (0, -3):
        out = truncate(text, limit)
        assert out == "... [50 characters cut] ..." and "x" not in out
    assert truncate("", 0) == ""
    out = truncate(text, 10)
    assert out.startswith("xxxxxx\n") and out.endswith("\nxxxx") and "40 characters cut" in out
