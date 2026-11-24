"""Hook management (add/remove in the file, live reload in an open chat), usage breakdown, and aligning PR line
comments to the diff."""

from __future__ import annotations

import pytest

from alice import hooks as hk
from alice import review
from alice.tools.base import DANGER, ToolContext, ToolError
from alice.tools.hooks_usage import HooksTool, Usage
from tests.conftest import make_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("alice.config.alice_home", lambda: tmp_path)
    return tmp_path


def test_add_remove_hooks_roundtrip_and_live_reload(home):
    h = hk.Hooks.from_config(make_config({"brain": ["a:m"]}), home)
    assert not h.has("PostToolUse")
    hk.add_hook("PostToolUse", 'ruff format "$ALICE_TOOL_PATH"', "edit_file|write_file", 20)
    hk.add_hook("Stop", "pytest -q")
    assert h.has("PostToolUse") and h.has("Stop")  # an open chat re-reads the hook file when it changes
    rows = hk.file_hooks()
    assert rows[0].command == 'ruff format "$ALICE_TOOL_PATH"' and rows[0].matcher == "edit_file|write_file"
    assert rows[0].timeout == 20
    hk.remove_hook(0)
    assert [r.event for r in hk.file_hooks()] == ["Stop"]
    with pytest.raises(ValueError):
        hk.add_hook("Nope", "x")
    with pytest.raises(ValueError):
        hk.add_hook("PreToolUse", "x", "(")


def test_hooks_tool_asks_before_adding_and_runs_test(home, workdir):
    t = HooksTool()
    assert t.risk_for({"action": "add"}) == DANGER and t.risk_for({"action": "list"}) == "read"
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = t.run(ctx, "add", event="PostToolUse", command="echo tamam", matcher="edit_file")
    assert "added" in out.text
    assert "0. PostToolUse [edit_file] echo tamam" in t.run(ctx, "list").text
    assert "exit code 0" in t.run(ctx, "test", index=0).text and "tamam" in t.run(ctx, "test", index=0).text
    with pytest.raises(ToolError):
        t.run(ctx, "remove", index=5)


def test_usage_breakdown_by_model_and_tool(workdir):
    from alice.providers.openai_compat import ChatResult

    agent = type("A", (), {})()
    from alice.agent import Agent

    a = Agent.__new__(Agent)
    a.stats = {k: 0 for k in ("turns", "steps", "llm_s", "tool_s", "ttft_s", "ttft_n", "gen_s", "gen_tokens", "input",
                              "cached", "output")}
    a.usage_models, a.usage_tools, a.messages = {}, {}, [{"role": "system", "content": "x"}]
    a._specs = lambda: []
    a.context_tokens = lambda: 100
    a._count_call(ChatResult({}, "chatgpt", "gpt-6", {"prompt_tokens": 1000, "completion_tokens": 50,
                                                        "cached_tokens": 800}), 0.0, 0.0)
    a._count_call(ChatResult({}, "chatgpt", "gpt-6", {"prompt_tokens": 500, "completion_tokens": 10}), 0.0, 0.0)
    a.usage_tools["read_file"] = {"calls": 3, "tokens": 900, "errors": 0}
    u = a.usage_breakdown()
    m = u["models"][0]
    assert m["model"] == "chatgpt:gpt-6" and m["calls"] == 2 and m["input"] == 1500 and m["cached"] == 800
    assert u["tools"][0]["tool"] == "read_file"
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    ctx.state["agent"] = a
    out = Usage().run(ctx)
    assert "chatgpt:gpt-6: 2 calls" in out.text and out.card["kind"] == "usage"
    del agent


DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -10,4 +10,5 @@ def f():
 x = 1
-y = 2
+y = 3
+z = 4
 return x
"""


def test_diff_lines_and_finding_anchoring():
    lines = review.diff_lines(DIFF)
    assert lines["a.py"] == {10, 11, 12, 13}
    f = review.Finding("a.py", 15, "high", "t", "d")
    assert review._anchor(f, lines) == 13  # nearest diff line within 3 lines
    assert review._anchor(review.Finding("a.py", 40, "high", "t", "d"), lines) == 0
    assert review._anchor(review.Finding("b.py", 11, "high", "t", "d"), lines) == 0


def test_added_line_starting_with_plus_plus_is_not_a_file_header():
    # A line added as "++ x" shows up in the diff as "+++ x"; inside a hunk it must not be mistaken for a file header.
    diff = ("diff --git a/notes.md b/notes.md\n--- a/notes.md\n+++ b/notes.md\n@@ -1,3 +1,4 @@\n baş\n"
            "+++ yeni madde\n--- eski madde\n+son\n son satır\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -5 +5 @@\n-x = 1\n+x = 2\n")
    assert review.diff_lines(diff) == {"notes.md": {1, 2, 3, 4}, "b.py": {5}}
    assert review._added_lines(diff) == {"notes.md": {2: "++ yeni madde", 3: "son"}, "b.py": {5: "x = 2"}}


def test_post_puts_inline_and_outside_findings_correctly():
    sent = []

    class API:
        def post(self, path, body):
            sent.append(body)
            return {"html_url": "u"}

    fs = [review.Finding("a.py", 11, "high", "satırda", "d"), review.Finding("c.py", 3, "low", "dışarıda", "d")]
    assert review.post(API(), "o/r", 1, "sha", fs, DIFF) == "u"
    body = sent[0]
    assert [c["path"] for c in body["comments"]] == ["a.py"] and body["comments"][0]["line"] == 11
    assert "Findings outside the diff" in body["body"] and "dışarıda" in body["body"] and "1 line comments" in body["body"]
