"""What an independent review of the workflow engine broke, kept as tests."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from alice import guard, subagents
from alice.tools import workflow as wf
from alice.tools.base import READ, Tool, ToolError, ToolResult
from alice.workflow import engine, schema
from alice.workflow import spawn as spawn_mod
from alice.workflow.engine import AgentCall, AgentResult, launch
from alice.workflow.script import ScriptError, local_path, parse
from alice.workflow.spawn import make_spawn
from tests.conftest import tool_call
from tests.test_agent import make_agent
from tests.test_app import app  # noqa: F401 - fixture

META = 'META = {"name": "t", "description": "d"}\n'


def go(source, spawn=lambda c, cancel, p: AgentResult(True, text=c.prompt), **kw):
    run = launch(META + source, spawn, **kw)
    assert run.wait(30)
    return run


def until(fn, secs=15.0):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


class Page(Tool):
    name = "page"
    description = "reads a page"
    risk = READ
    untrusted = True

    def run(self, ctx):
        return ToolResult("hostile text")


# ------------------------------------------------------------------------------------------------ outside content
def tainted_agent(workdir):
    seen: list[str] = []

    def model(m, messages, tools, **kw):
        last = messages[-1]
        if last["role"] == "tool":
            return "what the page says"
        return tool_call("page", {}) if "read it" in str(last["content"]) else "ok"

    agent, _, _ = make_agent(model, workdir)
    agent.tools["page"] = Page()
    agent.ctx.state["tool_groups"] = {"all"}
    return agent, seen


def test_outside_content_follows_the_data_to_the_next_agent(workdir, monkeypatch):
    agent, _ = tainted_agent(workdir)
    marks = []
    build = spawn_mod.Spawner._build
    monkeypatch.setattr(spawn_mod.Spawner, "_build",
                        lambda self, call, ui, cctx, tree: (marks.append(guard.tainted(cctx)),
                                                            build(self, call, ui, cctx, tree))[1])
    sp = make_spawn(agent, agent.ctx)
    for prompt in ("read it", "act on what it said"):
        assert sp(AgentCall(1, prompt), threading.Event(), lambda p: None).ok
    assert marks == ["", "page"]


def test_resume_remembers_where_replayed_answers_came_from(workdir):
    agent, _ = tainted_agent(workdir)
    src = META + 'return await agent("read it")'
    first = wf.Workflow().run(agent.ctx, script=src)
    assert "<untrusted_content" in first.text
    guard.clear(agent.ctx)
    again = wf.Workflow().run(agent.ctx, resume_from="wf1")
    assert "0 tokens" in again.text and "<untrusted_content" in again.text and guard.tainted(agent.ctx)
    guard.clear(agent.ctx)
    assert "<untrusted_content" in wf.Workflow().run(agent.ctx, resume_from="wf2").text  # and the resume of a resume


# ------------------------------------------------------------------------------------------------ frozen project
def test_agents_keep_the_project_the_run_started_in(workdir, tmp_path):
    agent, _, _ = make_agent(["x"], workdir)
    agent.messages[0]["content"] = "project A"
    sp = make_spawn(agent, agent.ctx)
    other = tmp_path / "b"
    other.mkdir()
    agent.messages[0] = {"role": "system", "content": "project B"}
    agent.ctx.cwd = other
    agent.tools.pop("grep", None)
    ui = spawn_mod._UI(subagents.Sub("x", "x", "x"), None, lambda ev: None)
    child, _steps = sp._build(AgentCall(1, "x"), ui, subagents.child_context(sp.ctx), None)
    assert child.messages[0]["content"] == "project A" and child.ctx.cwd == workdir and "grep" in child.tools


# ------------------------------------------------------------------------------------------------ budget
def spending(c, cancel, progress):
    return AgentResult(True, text="x", tokens=15)


def test_target_of_a_request_covers_all_its_workflows(workdir, monkeypatch):
    agent, _, _ = make_agent(["ok"] * 5, workdir)
    agent.run_turn("do it +20k")
    agent.ctx.state["workflow_budget"] = 20
    monkeypatch.setattr(spawn_mod, "make_spawn", lambda parent, ctx: _Fake(spending))
    out = wf.Workflow().run(agent.ctx, script=META + 'await agent("a")\nreturn budget.total')
    assert "20" in out.text
    agent.run_turn("[Workflow wf1 · t finished]\n...")  # phase 2 starts from the result message
    out = wf.Workflow().run(agent.ctx, script=META + 'await agent("b")\nreturn [budget.total]')
    assert re.search(r"\[\s*5\s*\]", out.text)
    with pytest.raises(ToolError, match="used up"):
        wf.Workflow().run(agent.ctx, script=META + "return 1")
    agent.run_turn("next request")
    assert "None" not in wf.Workflow().run(agent.ctx, script=META + "return 1").text


class _Fake:
    def __init__(self, fn):
        self.fn, self.tainted, self.kept, self.run_id, self.folder = fn, "", [], "", None

    def __call__(self, *a):
        return self.fn(*a)


def test_resume_keeps_the_ceiling(workdir, monkeypatch):
    agent, _, _ = make_agent(["ok"], workdir)
    monkeypatch.setattr(spawn_mod, "make_spawn", lambda parent, ctx: _Fake(spending))
    src = META + 'for i in range(3):\n    await agent(f"a{i}")\nreturn "end"'
    wf.Workflow().run(agent.ctx, script=src, budget_tokens=20)
    with pytest.raises(ToolError, match="used up its token budget"):
        wf.start(agent.ctx, wf.source_of(agent.ctx, resume_from="wf1"), resume_from="wf1")
    out = wf.Workflow().run(agent.ctx, resume_from="wf1", budget_tokens=100)
    assert "end" in out.text
    wf.Workflow().run(agent.ctx, script=META + 'await agent("z")\nraise ValueError("x")', budget_tokens=50)
    entry = wf.start(agent.ctx, wf.source_of(agent.ctx, resume_from="wf3"), resume_from="wf3")
    assert entry.run.wait(20) and entry.run.budget == 35


# ------------------------------------------------------------------------------------------------ the app
def running(state, release):
    agent = state.chat.agent
    spawn = _Fake(lambda c, cancel, p: (release.wait(20), AgentResult(True, text="x"))[1])
    old, spawn_mod.make_spawn = spawn_mod.make_spawn, lambda parent, ctx: spawn
    try:
        return wf.start(agent.ctx, META + 'return await agent("x")')
    finally:
        spawn_mod.make_spawn = old


def test_chat_of_a_running_workflow_stays_reachable(app):  # noqa: F811
    state, *_ = app
    from alice.app.host import AppHost

    release = threading.Event()
    first, sid = state.chat, state.chat.sid
    entry = running(state, release)
    host = AppHost.__new__(AppHost)
    host.state = state
    assert first.working and host.busy and not state.apply_update()["ok"]
    state.new_session()  # not the same chat object: its questions would show up in the new one
    assert state.chat is not first and state.background.get(sid) is first and sid in state.running()
    state._finished(first)
    state._drop_if_idle(first)
    assert state.background.get(sid) is first and first.ui in state.all_uis()
    state.chat.agent.stop()  # Stop in the new chat has nothing to do with it
    assert entry.status == "running"
    assert state.resume(sid) and state.chat is first
    release.set()
    assert entry.run.wait(20) and not first.working


def test_deleting_a_chat_stops_its_workflow(app):  # noqa: F811
    state, *_ = app
    entry = running(state, threading.Event())
    state.chat.save()
    state.delete_session(state.chat.sid)
    assert until(lambda: entry.status == "stopped")


def test_background_result_is_not_the_answer_to_a_question(app):  # noqa: F811
    state, *_ = app
    answered = []
    chat = state.chat
    chat.ui.pending["q"] = {"id": "q", "type": "ask_choice"}
    chat.ui.answer_choice = lambda pid, text: answered.append(text) or True
    steered = []
    chat.agent.steer = lambda text, images=None: steered.append(text) or True
    chat.agent.busy = True
    try:
        assert state.deliver(chat.sid, "[Workflow wf1 · x finished]\nyes, delete all") == ""
        assert steered and not answered
        assert state.send("yes", sid=chat.sid)[0] and answered == ["yes"]
    finally:
        chat.agent.busy = False
        chat.ui.pending.clear()


def test_result_waits_for_room(app, monkeypatch):  # noqa: F811
    state, *_ = app
    from alice.app import server
    from alice.i18n import _

    full = _("the limit of {n} background chats is reached; try again when one finishes", n=state.MAX_BACKGROUND)
    tries = []
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(state, "send", lambda *a, **k: (tries.append(1), (len(tries) > 40, None if len(tries) > 40 else full))[1])
    assert state.deliver("other", "[Workflow wf1 · x finished]") == "" and len(tries) == 41


def test_ultracode_switch_keeps_the_effort(app):  # noqa: F811
    state, _, client, _ = app
    client.post("/api/thinking", json={"level": "low"})
    assert client.get("/api/state").json()["ultracode"] is False
    r = client.post("/api/ultracode", json={"on": True}).json()
    assert r["ok"] and r["ultracode"] and r["thinking"] == "low"
    r = client.post("/api/thinking", json={"level": "high"}).json()
    assert r["ultracode"] and r["thinking"] == "high"
    assert client.post("/api/ultracode", json={"on": False}).json()["ultracode"] is False
    assert client.post("/api/thinking", json={"level": "ultracode"}).json()["ultracode"]  # an older page


def test_agents_work_at_the_chats_effort_and_never_order_workflows(workdir):
    agent, _, _ = make_agent(["x"], workdir)
    agent.router.ultracode = True
    agent.router.thinking_overrides["brain"] = "low"
    sp = make_spawn(agent, agent.ctx)
    ui = spawn_mod._UI(subagents.Sub("x", "x", "x"), None, lambda ev: None)
    child, _steps = sp._build(AgentCall(1, "x"), ui, subagents.child_context(sp.ctx), None)
    assert child.router.thinking_level("brain") == "low" and not child.router.ultracode and agent.router.ultracode


# ------------------------------------------------------------------------------------------------ network paths
@pytest.mark.parametrize("name", [r"\\evil\share\x.py", "//evil/share/x.py", r"\/evil/share/x.py", r"/\evil\share\x.py"])
def test_network_paths_are_never_touched(workdir, monkeypatch, name):
    agent, _, _ = make_agent(["x"], workdir)
    touched = []
    for what in ("is_file", "stat", "read_text", "resolve"):
        real = getattr(Path, what)
        monkeypatch.setattr(Path, what, lambda self, *a, _r=real, **k: (
            touched.append(str(self)) if "evil" in str(self) else None, _r(self, *a, **k))[1])
    with pytest.raises(ScriptError):
        local_path(name)
    with pytest.raises(ToolError):
        wf.find(workdir, name)
    with pytest.raises(ToolError):
        wf.source_of(agent.ctx, script_path=name)
    wf.Workflow().summary_for(agent.ctx, {"script_path": name})
    run = go(f"return await workflow({name!r})")
    assert run.status == "failed" and "no workflow named" in run.error and not touched


# ------------------------------------------------------------------------------------------------ freezes and limits
def test_nested_fixed_repeats_do_not_freeze_alice():
    # The pattern of the review froze every thread for seconds; it must not run in this process.
    assert schema._cost("(?:(?:(?:(?=a)){450}){450}){450}", 10) > 1e6
    assert schema._cost(r"^[a-z0-9_-]{1,40}$", 100) <= 1e6  # ordinary ones stay in-process
    ticks, stop = [], threading.Event()
    t = threading.Thread(target=lambda: [ticks.append(time.monotonic()) or time.sleep(0.02) for _ in iter(stop.is_set, True)])
    t.start()
    try:
        schema.check({"type": "object", "properties": {"a": {"type": "string",
                     "pattern": "(?:(?:(?:(?=a)){600}){600}){600}"}}}, {"a": "a" * 10})
    finally:
        stop.set()
        t.join()
    assert max(b - a for a, b in zip(ticks, ticks[1:])) < 1.0


def test_large_scripts_are_checked_quickly():
    body = "for x in args:\n" + "".join(f"    y{i} = lambda: x + {i}\n" for i in range(1500))
    t0 = time.monotonic()
    parse(META + body)
    nested = "".join("    " * i + f"for x{i} in args:\n" for i in range(18))
    nested += "".join("    " * 18 + f"f{i} = lambda: x0 + x17 + {i}\n" for i in range(150))
    parse(META + nested)
    assert time.monotonic() - t0 < 8


def test_snapshot_stays_small_and_survives_deep_results():
    run = go('for i in range(3):\n    await agent("x", phase="A" * 300000, model="m" * 100000)\nreturn 1')
    assert len(json.dumps(run.snapshot())) < 20_000
    run = go("v = 1\nfor i in range(150):\n    v = [v]\nreturn v")

    def deeper(n):
        return run.snapshot() if n == 0 else deeper(n - 1)

    assert run.status == "done" and deeper(700)["status"] == "done"
    run = go("v = 1\nfor i in range(400):\n    v = [v]\nreturn v")
    assert run.status == "failed" and "nested" in run.error


def test_set_operators_of_dict_views_have_a_fixed_order():
    src = ('d = {f"k{i}": 1 for i in range(40)}\ne = {"k3": 1}\n'
           "return [list(d.keys() - e.keys()), list(d.keys() | e.keys()), list(d.keys() ^ e.keys()), 7 - 2, 6 | 1]")
    first, second = go(src).value, go(src).value
    assert first == second and first[3:] == [5, 7] and len(first[0]) == 39


@pytest.mark.parametrize("source", [
    "out = []\nfor c in [False, True]:\n    ok = c and (v := 5)\n    f = lambda: v\n    if ok:\n        out.append(f())\nreturn out",
    "out = []\nfor c in [1]:\n    x: int\n    f = lambda: x\n    x = 5\n    out.append(f())\nreturn out",
    "out = []\nfor c in [1]:\n    x = 5\n    del x\n    f = lambda: x\n    x = 5\n    out.append(f())\nreturn out",
])
def test_lambdas_in_loops_do_not_invent_unbound_names(source):
    run = go(source)
    assert run.status == "done" and run.value == [5], run.error


def test_loop_values_are_still_kept():
    run = go("fs = []\nfor i in range(3):\n    n = i * 2\n    fs.append(lambda: (i, n))\nreturn [f() for f in fs]")
    assert run.value == [[0, 0], [1, 2], [2, 4]]


def test_regex_debug_output_does_not_break_the_protocol():
    assert go("re.compile('a(b|c)*' * 50, 128)\nreturn 1").value == 1


# ------------------------------------------------------------------------------------------------ schemas
def test_schema_keywords_are_checked_or_refused():
    obj = {"type": "object", "properties": {}}
    for bad in ({"not": {}}, {"patternProperties": {}}, {"multipleOf": 2}, {"if": {}}):
        assert schema.schema_problems({**obj, "properties": {"a": bad}})
    assert not schema.schema_problems({**obj, "title": "t", "$schema": "x", "properties": {"a": {"type": "string",
                                                                                                "format": "email"}}})
    assert schema.check({"exclusiveMinimum": 0}, 0) and not schema.check({"exclusiveMinimum": 0}, 1)
    assert schema.check({"uniqueItems": True}, [1, 1]) and schema.check({"minProperties": 1}, {})
    assert schema.check({"pattern": "^a$"}, "a\n") and not schema.check({"pattern": "^a$"}, "a")
    assert schema.check({"pattern": r"^\d$"}, "٣") and not schema.check({"pattern": r"^\$$"}, "$")


def test_nested_any_of_has_a_limit():
    leaf = {"anyOf": [{"const": i} for i in range(200)]}
    t0 = time.monotonic()
    problems = schema.check({"anyOf": [{"anyOf": [leaf] * 200}] * 200}, "x")
    assert problems and time.monotonic() - t0 < 10


# ------------------------------------------------------------------------------------------------ worktrees
@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-q")
    git(d, "config", "user.email", "a@b")
    git(d, "config", "user.name", "a")
    (d / "a.txt").write_text("1")
    git(d, "add", ".")
    git(d, "commit", "-qm", "1")
    return d


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.parametrize("how", ["detached", "skip-worktree", "stash"])
def test_worktree_with_hidden_work_is_kept(repo, how):
    tree = spawn_mod._Worktree.add(repo, "x")
    branch = tree.branch
    if how == "detached":
        git(tree.path, "checkout", "-q", "--detach")
        (tree.path / "n.txt").write_text("new")
        git(tree.path, "add", ".")
        git(tree.path, "commit", "-qm", "2")
        git(tree.path, "checkout", "-q", branch)
    elif how == "skip-worktree":
        git(tree.path, "update-index", "--skip-worktree", "a.txt")
        (tree.path / "a.txt").write_text("2")
    else:
        (tree.path / "a.txt").write_text("2")
        git(tree.path, "stash", "-q")
    assert "changes kept" in tree.finish() and tree.path.exists()


def test_untouched_worktree_is_removed(repo):
    tree = spawn_mod._Worktree.add(repo, "x")
    assert tree.finish() == "" and not tree.path.exists()


def test_engine_constants_are_sane():
    assert engine.RESULT_CHARS == 4000 and engine.OPT_CHARS["phase"] == 200
