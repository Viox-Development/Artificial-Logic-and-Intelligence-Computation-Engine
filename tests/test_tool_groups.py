"""Tool groups: which definitions go out per request, how the starting set is picked from the message and the context,
and how the model loads more (fake provider, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alice.agent import Agent, NullUI
from alice.permissions import Permissions
from alice.providers.router import Router
from alice.tools import ToolContext, all_tools, builtin_tools
from alice.tools.groups import (CODE_TOOLS, CORE, GROUPS, TOOL_HINTS, TOOL_TO_GROUP, _HINTED_GROUPS, is_project,
                                recent_tools, select_groups)
from alice.tools.measure import report
from tests.conftest import FakeProvider, make_config, tool_call


@pytest.fixture
def project(tmp_path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    return d


@pytest.fixture
def plain_dir(tmp_path) -> Path:
    d = tmp_path / "stuff"
    d.mkdir()
    return d


def make_agent(steps, cwd, mode="auto", **agent_cfg):
    """Agent with a fake model that records the tool names of every request and answers with the next step."""
    seen: list[set[str]] = []
    steps = list(steps)

    def script(model, messages, tools, **kw):
        seen.append({t["function"]["name"] for t in tools or []})
        return steps.pop(0) if steps else "tamam"

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}, **agent_cfg)
    router = Router(cfg, provider_factory=lambda pc: FakeProvider("a", script), sleep=lambda s: None)
    ctx = ToolContext(cwd=cwd, config=cfg, router=router)
    agent = Agent(cfg, router, all_tools(), ctx, Permissions(mode), NullUI("y"), system_prompt="sys")
    return agent, seen


# ---------------------------------------------------------------------------------------------- grouping
def test_every_hinted_group_has_a_hint_per_tool():
    names = {t.name for t in builtin_tools()}
    assert set(TOOL_HINTS) <= names
    assert all(n in TOOL_TO_GROUP for n in TOOL_HINTS)
    for g in _HINTED_GROUPS:
        # A tool without a hint in a hinted group would only ever open through load_tools.
        assert {n for n in GROUPS[g][0] if n in names} <= set(TOOL_HINTS), g


def test_every_tool_is_core_or_reachable_through_a_group():
    from alice.tools.groups import group_of

    for t in builtin_tools():
        assert t.name in CORE or group_of(t.name), t.name


# ------------------------------------------------------------------------------------------- selection
def test_project_detection(project, plain_dir, tmp_path, monkeypatch):
    assert is_project(project)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "deep").mkdir(parents=True)
    assert is_project(repo / "src" / "deep")  # inside a git repo, without markers in the folder itself
    assert not is_project(plain_dir)
    monkeypatch.setattr(Path, "home", lambda: project)
    assert not is_project(project)  # the home folder is never a project


@pytest.mark.parametrize("text, opened, closed", [
    ("fix the failing test in parser.py and run pytest", {"test_run"}, {"files", "apply_patch", "plan", "team"}),
    ("parser.py'deki hatayı düzelt", set(), {"files", "diagnostics", "multi_edit"}),
    ("refactor parser.py, then commit and push", {"apply_patch", "multi_edit", "git", "todo_write"}, {"team", "files"}),
    ("bu dosyayı masaüstüne taşı", {"file_ops"}, {"files"}),
    ("önce derle sonra testleri çalıştır", {"todo_write", "run_plan", "test_run"}, {"plan"}),
    ("dev server'ı arka planda başlat", {"process_output", "process_stop"}, {"plan"}),
    ("bu kodu bir takım halinde paralel yap", {"team", "task"}, set()),
])
def test_coding_words_open_single_tools_in_a_project(project, text, opened, closed):
    got = select_groups(text, cwd=project)
    assert opened <= got, got
    assert not closed & got, got


def test_code_only_tools_need_a_project(project, plain_dir):
    text = "refactor this and run the tests"
    assert {"apply_patch", "test_run"} <= select_groups(text, cwd=project)
    assert not select_groups(text, cwd=plain_dir) & CODE_TOOLS  # e.g. the home folder or a documents folder
    assert not select_groups(text, cwd=project, mode="voice") & CODE_TOOLS
    assert "git" in select_groups("commit this", cwd=plain_dir)  # not code-only: works anywhere


def test_recent_tools():
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "browser", "arguments": "{}"}},
                                                 {"id": "2", "function": {"name": None}}, "junk"]}]
    assert recent_tools(msgs) == {"browser"}
    assert recent_tools(msgs + [{"role": "user", "content": "y"}] * 5, limit=3) == set()


# ------------------------------------------------------------------------------------- agent integration
def test_typical_coding_request_sends_a_fraction(project):
    agent, seen = make_agent([], project)
    agent.run_turn("fix the failing test in parser.py and run pytest")
    assert {"read_file", "edit_file", "grep", "run_command", "load_tools", "test_run"} <= seen[0]
    assert not seen[0] & {"file_ops", "share", "hooks", "permissions", "team", "council", "browser", "computer"}
    assert len(seen[0]) <= 16 < len(agent.tools) // 4


def test_measure_report_numbers(project):
    r = report(project)
    by = {c["label"]: c for c in r["cases"]}
    assert r["core"]["tokens"] < r["all"]["tokens"] / 5
    coding = by["coding: fix a test"]
    assert coding["tokens"] < r["all"]["tokens"] / 5 and coding["tools"] <= 16
    assert by["assistant: fix a doc (home)"]["opened"] == []


def test_load_tools_makes_a_group_available_next_step(project):
    agent, seen = make_agent([tool_call("load_tools", {"groups": ["browser"]}), "bitti"], project)
    agent.run_turn("fix the bug")
    assert "browser" not in seen[0]
    assert {"browser", "preview"} <= seen[1]


def test_load_tools_by_tool_name_and_all(project):
    agent, seen = make_agent([tool_call("load_tools", {"groups": ["share"]}),
                              tool_call("load_tools", {"groups": ["all"]}, cid="c2"), "ok"], project)
    agent.run_turn("fix the bug")
    assert "share" in seen[1] and "file_ops" in seen[1]  # a tool name opens its whole group
    # "all" is refused (it bloated every request and rewrote the cache): the tool list stays as it was.
    assert seen[2] == seen[1] and not seen[2] >= {t.name for t in builtin_tools()}
    res = [m for m in agent.messages if m["role"] == "tool"][-1]["content"]
    assert "'all' is not accepted" in res


def test_calling_an_unloaded_tool_still_runs_and_loads_its_group(project):
    # Models often know a tool from the catalog and call it without load_tools first.
    agent, seen = make_agent([tool_call("system_info", {}), "ok"], project)
    agent.run_turn("fix the bug")
    assert "system_info" not in seen[0]
    assert {"system_info", "process_list"} <= seen[1]
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "bilinmeyen araç" not in tool_msg["content"]


def test_reopened_session_keeps_recently_used_tools(project):
    agent, seen = make_agent([], project)
    # A chat restored from disk (alice.session.load): the loaded tool set itself is not saved.
    agent.messages += [
        {"role": "user", "content": "localhost'u aç"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "browser", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "name": "browser", "content": "ok"},
        {"role": "assistant", "content": "açtım"},
    ]
    agent.run_turn("şimdi bir sonrakine geç")
    assert "browser" in seen[0]


def test_plan_mode_loads_only_what_planning_needs(project):
    agent, seen = make_agent([], project, mode="plan")
    agent.run_turn("bunu nasıl yaparız?")
    assert {"exit_plan_mode", "todo_write"} <= seen[0]
    assert not seen[0] & {"run_plan", "monitor", "session", "subagents", "agent_defs"}


def test_tool_groups_all_sends_everything(project):
    agent, seen = make_agent([], project, tool_groups="all")
    agent.run_turn("x")
    assert seen[0] == set(agent.tools)
