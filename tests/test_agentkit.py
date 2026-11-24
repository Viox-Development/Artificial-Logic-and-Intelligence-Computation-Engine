"""Tools missing from Claude Code: notify, enter_plan_mode, mcp_resources, code_intel."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alice.tools import ToolContext, builtin_tools
from alice.tools.agentkit import CodeIntel, EnterPlanMode, McpResources, Notify
from alice.tools.base import ToolError
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_new_tools_are_registered_and_grouped():
    from alice.tools.groups import GROUPS

    names = {t.name for t in builtin_tools()}
    assert {"notify", "enter_plan_mode", "mcp_resources", "code_intel"} <= names
    grouped = set().union(*(g[0] for g in GROUPS.values()))
    assert {"notify", "enter_plan_mode", "mcp_resources", "code_intel"} <= grouped


def test_notify_goes_through_the_core_notifier_and_respects_muting(ctx, monkeypatch):
    from alice.core import notify as nt

    sent = []

    class Hub:
        allow = True

        def send(self, n):
            sent.append(n)
            return self.allow

    hub = Hub()
    monkeypatch.setattr(nt, "ACTIVE", hub)
    assert Notify().run(ctx, "Testler bitti", "142 test geçti").text == "notification sent"
    assert sent[0].title == "Testler bitti" and sent[0].kind == "task"
    hub.allow = False
    assert "muted" in Notify().run(ctx, "x").text


def test_enter_plan_mode_switches_and_exit_can_return(ctx):
    perms = SimpleNamespace(mode="auto")
    ctx.state["agent"] = SimpleNamespace(permissions=perms, is_child=False)
    assert "Switched to plan mode" in EnterPlanMode().run(ctx, "üç modül değişecek").text
    assert perms.mode == "plan"
    assert "already in plan mode" in EnterPlanMode().run(ctx).text
    ctx.state["agent"] = SimpleNamespace(permissions=perms, is_child=True)
    with pytest.raises(ToolError):
        EnterPlanMode().run(ctx)


def test_mcp_resources_list_and_read(ctx):
    class Srv:
        name = "docs"

        def list_resources(self):
            return [{"uri": "file:///a.md", "name": "A", "mimeType": "text/markdown", "description": "ilk belge"}]

        def read_resource(self, uri):
            return {"contents": [{"uri": uri, "text": "# A\nmerhaba"}]}

    tool = McpResources()
    with pytest.raises(ToolError, match="no MCP servers"):
        tool.run(ctx, "list")
    ctx.state["mcp_servers"] = [Srv()]
    assert "file:///a.md — A [text/markdown]: ilk belge" in tool.run(ctx, "list").text
    assert tool.run(ctx, "read", uri="file:///a.md").text == "# A\nmerhaba"
    assert tool.untrusted


def test_code_intel_symbols_definition_references(ctx, workdir):
    (workdir / "pkg").mkdir()
    (workdir / "pkg" / "core.py").write_text(
        "LIMIT = 3\n\n\nclass Loader:\n    def load(self, path):\n        return read_config(path)\n\n\n"
        "def read_config(path):\n    return path\n", encoding="utf-8")
    (workdir / "app.ts").write_text("export function readConfig(p: string) { return p }\n"
                                    "export interface Settings { a: number }\n", encoding="utf-8")
    tool = CodeIntel()
    syms = tool.run(ctx, "symbols", paths=["pkg/core.py", "app.ts"]).text
    assert "4: class Loader" in syms and "5: def Loader.load(self, path)" in syms and "9: def read_config(path)" in syms
    assert "1: function readConfig" in syms and "2: interface Settings" in syms
    d = tool.run(ctx, "definition", symbol="read_config").text
    assert "core.py:9:" in d and "core.py:6:" not in d  # a definition, not a usage
    r = tool.run(ctx, "references", symbol="read_config").text
    assert "core.py:6:" in r and "core.py:9:" in r
    with pytest.raises(ToolError):
        tool.run(ctx, "definition", symbol="a b")
    assert tool.risk_for({"action": "diagnostics"}) == "exec" and tool.risk_for({"action": "symbols"}) == "read"


def test_code_intel_diagnostics_reports_syntax_and_data_errors(ctx, workdir):
    (workdir / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    (workdir / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    (workdir / "bad.json").write_text('{"a": 1,}', encoding="utf-8")
    out = CodeIntel().run(ctx, "diagnostics", paths=["bad.py", "ok.json", "bad.json", "yok.py"]).text
    assert "bad.py:1" in out and "syntax" in out
    assert "## ok.json (json)\nno problems" in out and "## bad.json (json)\nbad.json:" in out
    assert "file not found" in out


def test_advisor_sees_conversation_and_uses_configured_model(tmp_path):
    from types import SimpleNamespace

    from alice.tools.agentkit import Advisor
    from alice.tools.base import ToolContext, ToolError

    calls = []

    class R:
        def chat(self, role, messages, **kw):
            calls.append((role, messages))
            return SimpleNamespace(text="B seçeneği daha güvenli.")

    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "A mı B mi?"},
                                      {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}])
    cfg = make_config({"brain": ["a:m"]})
    ctx = ToolContext(cwd=tmp_path, config=cfg, router=R())
    ctx.state["agent"] = agent
    with pytest.raises(ToolError, match="no advisor model is set"):
        Advisor().run(ctx, "hangisi?")
    cfg.data["agent"]["advisor"] = "a:big"
    out = Advisor().run(ctx, "hangisi?").text
    assert "B seçeneği" in out and calls[-1][0] == "a:big"
    sent = calls[-1][1]
    assert sent[1]["content"] == "A mı B mi?" and not sent[-2].get("tool_calls")  # half-finished call step is not sent
    assert "Question: hangisi?" in sent[-1]["content"]
