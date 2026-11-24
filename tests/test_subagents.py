"""Subagents (parallel, background, continue, stop), custom agent definitions, linking a project to a PR, and scheduled
projects."""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from alice import subagents
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config, tool_call
from tests.test_agent import make_agent


def _user(messages) -> str:
    last = messages[-1]
    return last["content"] if last["role"] == "user" and isinstance(last["content"], str) else ""


def test_parallel_task_calls_run_at_the_same_time(workdir):
    both = threading.Barrier(2, timeout=5)  # if they ran one after another, the first subagent would wait here forever

    def script(model, messages, tools, **kw):
        q = _user(messages)
        if q in ("A işi", "B işi"):
            both.wait()
            return f"{q} bitti"
        if messages[-1]["role"] == "tool":
            return "ana bitti"
        return {"content": "", "tool_calls": [
            tool_call("task", {"prompt": "A işi", "name": "a"}, "c1")["tool_calls"][0],
            tool_call("task", {"prompt": "B işi", "name": "b"}, "c2")["tool_calls"][0]]}

    agent, _, _ = make_agent(script, workdir)
    assert agent.run_turn("ikisini paralel yap") == "ana bitti"
    tools = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert any("A işi bitti" in t for t in tools) and any("B işi bitti" in t for t in tools)
    assert not any("A işi bitti" in m.get("content", "") for m in agent.messages if m["role"] == "assistant")


def test_background_subagent_reports_back_and_can_be_continued(workdir):
    seen: list[list[dict]] = []

    def script(model, messages, tools, **kw):
        q = _user(messages)
        if q == "ilk iş":
            return "ilk bulgu: X"
        if q == "devam et":
            seen.append(messages)
            return "ikinci bulgu: Y"
        if messages[-1]["role"] == "tool":
            return "tamam"
        return tool_call("task", {"prompt": "ilk iş", "background": True, "name": "tarama"})

    agent, _, _ = make_agent(script, workdir)
    got: list[str] = []
    agent.ctx.state["deliver"] = got.append
    agent.run_turn("arka planda tara")
    tool_out = next(m["content"] for m in agent.messages if m["role"] == "tool")
    assert "a1" in tool_out and "in the background" in tool_out
    sub = subagents.registry(agent.ctx)["a1"]
    sub.thread.join(5)
    assert sub.status == "done" and got and got[0].startswith("[Alt ajan a1 · tarama bitti]") and "ilk bulgu" in got[0]

    subagents.resume(agent.ctx, "a1", "devam et").thread.join(5)
    assert sub.result == "ikinci bulgu: Y" and sub.turns == 2
    assert any("ilk bulgu: X" in str(m.get("content")) for m in seen[0])  # it remembers its previous conversation
    with pytest.raises(ToolError, match="böyle bir alt ajan yok"):
        subagents.resume(agent.ctx, "a9", "x")


def test_stopping_a_background_subagent(workdir):
    gate = threading.Event()

    def script(model, messages, tools, **kw):
        if _user(messages) == "uzun iş":
            gate.wait(5)
            return "geç"
        return "ana"

    agent, _, _ = make_agent(script, workdir)
    got: list[str] = []
    agent.ctx.state["deliver"] = got.append
    sub = subagents.start(agent, agent.ctx, "uzun iş", background=True)
    assert subagents.stop(sub)
    gate.set()
    sub.thread.join(5)
    assert sub.status == "stopped" and got == []  # a stopped job does not report back to the chat as "bitti"
    assert not subagents.stop(sub)


def test_subagent_has_its_own_context(workdir):
    agent, _, _ = make_agent(["x"], workdir)
    agent.ctx.state["fg_procs"] = {"ana"}
    sub = subagents.start(agent, agent.ctx, "x")
    sub.thread.join(5)
    child_ctx = sub.child.ctx
    assert child_ctx is not agent.ctx and child_ctx.state["agent"] is sub.child
    assert "fg_procs" not in child_ctx.state and agent.ctx.state["agent"] is agent
    agent.cancel.set()  # "Durdur" (Stop) in the main chat: the subagent's tools don't see it
    assert not child_ctx.cancelled()


def test_agent_definitions_save_list_delete(workdir):
    from alice import skills
    from alice.tools.meta import AgentDefs

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    t = AgentDefs()
    assert t.risk_for({"action": "save"}) == "write" and t.risk_for({"action": "list"}) == "read"
    out = t.run(ctx, "save", name="Test Yazici", description="test yazar", prompt="Sen test yazarısın.",
                tools=["read_file", "grep"], max_steps=12, where="project").text
    assert "test-yazici" in out and (workdir / ".alice" / "agents" / "test-yazici.md").exists()
    a = skills.load_agents(workdir)["test-yazici"]
    assert a.tools == ["read_file", "grep"] and a.max_steps == 12 and a.prompt == "Sen test yazarısın."
    t.run(ctx, "save", name="test-yazici", description="güncel", prompt="Yeni talimat.", where="project")
    assert skills.load_agents(workdir)["test-yazici"].description == "güncel"
    assert "test-yazici" in t.run(ctx, "list").text
    with pytest.raises(ToolError):
        t.run(ctx, "save", name="kötü/ad", description="d", prompt="p")
    t.run(ctx, "delete", name="test-yazici")
    assert "test-yazici" not in skills.load_agents(workdir)


def test_agent_definition_model_does_not_touch_shared_router(workdir):
    """A project agent's "model: sağlayıcı:model" applies only to that subagent; the shared router stays put."""
    from alice import skills

    agent, _, _ = make_agent(["x"], workdir)
    adef = skills.AgentDef(name="kotu", description="d", prompt="p", model="b:v")
    child = skills.spawn(agent, adef)
    assert child.role == "agent:kotu" and child.router.chain("agent:kotu")[0] == "b:v"
    assert "agent:kotu" not in agent.router.role_overrides
    assert child.router is not agent.router and child.router.cooldown_until is agent.router.cooldown_until


# --------------------------------------------------------------------------------------------- project → PR
def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git yok")
def test_project_runs_on_its_own_branch_and_ends_in_a_watched_pr(tmp_path, monkeypatch):
    from alice import prwatch, team_pr
    from alice.tools import github as gh

    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work.mkdir()
    git(work, "init", "-b", "main")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    git(work, "add", "a.txt")
    git(work, "commit", "-m", "ilk")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    (work / "a.txt").write_text("kullanıcının yarım işi\n", encoding="utf-8")  # should not go into the project
    monkeypatch.setattr(gh, "origin_repo", lambda cwd: "o/r")
    posted, watched = [], []

    class API:
        def __init__(self, token=None):
            self.token = token

        def post(self, path, body):
            posted.append((path, body))
            return {"html_url": "https://github.com/o/r/pull/5", "number": 5}

    monkeypatch.setattr(gh, "GitHubAPI", API)
    monkeypatch.setattr(prwatch, "watch", lambda repo, n, path="", session="", **k: watched.append((repo, n, session)))
    team = SimpleNamespace(id="p1", title="Özellik", goal="hedef", summary="özet", members={}, cwd=work,
                           parent_ctx=SimpleNamespace(config=None, state={"session_id": "s1"}),
                           _repo_top=lambda: work)
    note = team_pr.prepare(team)
    assert "alice/proje-p1" in note and "commit'lenmemiş" in note and team.cwd != work
    assert team_pr.open_pr(team) == "PR açılmadı: projede değişiklik yok"
    (team.cwd / "b.txt").write_text("b\n", encoding="utf-8")
    (team.cwd / ".alice-team" / "p1-x").mkdir(parents=True)
    (team.cwd / ".alice-team" / "p1-x" / "f.txt").write_text("ajan kopyası\n", encoding="utf-8")
    out = team_pr.open_pr(team)
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", "alice/proje-p1"], cwd=remote, capture_output=True,
                          text=True).stdout
    assert "b.txt" in tree and ".alice-team" not in tree  # agents' copies do not go into the PR
    assert out.startswith("PR #5 açıldı") and watched == [("o/r", 5, "s1")]
    assert posted[0][1]["head"] == "alice/proje-p1" and posted[0][1]["base"] == "main"
    log = subprocess.run(["git", "log", "--oneline", "alice/proje-p1"], cwd=remote, capture_output=True,
                         text=True).stdout
    assert "ALICE projesi" in log
    assert (work / "a.txt").read_text(encoding="utf-8") == "kullanıcının yarım işi\n"  # the user's copy stays in place
    assert not (work / "b.txt").exists()


def test_project_pr_refuses_to_push_a_secret(tmp_path, monkeypatch):
    from alice import team_pr
    from alice.tools import github as gh

    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work.mkdir()
    git(work, "init", "-b", "main")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    git(work, "add", "a.txt")
    git(work, "commit", "-m", "ilk")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    monkeypatch.setattr(gh, "origin_repo", lambda cwd: "o/r")
    monkeypatch.setattr(gh, "GitHubAPI", lambda token=None: pytest.fail("PR açılmamalı"))
    team = SimpleNamespace(id="p2", title="Özellik", goal="hedef", summary="özet", members={}, cwd=work,
                           parent_ctx=SimpleNamespace(config=None, state={"session_id": "s1"}),
                           _repo_top=lambda: work)
    team_pr.prepare(team)
    (team.cwd / "conf.py").write_text('KEY = "ghp_' + "a1B2" * 9 + '"\n', encoding="utf-8")
    out = team_pr.open_pr(team)
    assert out.startswith("PR açılmadı: projede anahtar") and "conf.py" in out
    branches = subprocess.run(["git", "branch", "--list", "alice/proje-p2"], cwd=remote, capture_output=True,
                              text=True).stdout
    assert branches.strip() == ""  # nothing was pushed


# ----------------------------------------------------------------------------------------- scheduled project
def test_scheduled_project_starts_from_template(monkeypatch):
    import json

    from alice.core.schedule import ScheduledTask, start_project
    from alice.team_store import templates_dir

    (templates_dir() / "bagimlilik.json").write_text(json.dumps({"name": "Bağımlılık", "goal": "güncelle",
                                                                  "pr": True, "cwd": "C:/proje"}), encoding="utf-8")
    task = ScheduledTask(cron="0 9 * * 1", prompt="major sürümlere dokunma", project="bağımlılık")
    got = []
    assert "proje başladı (p9)" in start_project(task, lambda d: got.append(d) or "p9")
    assert got[0]["goal"] == "güncelle\n\nmajor sürümlere dokunma" and got[0]["pr"] and got[0]["cwd"] == "C:/proje"
    assert "uygulama" in start_project(task, None)
    assert "şablon yok" in start_project(ScheduledTask(cron="0 9 * * 1", prompt="", project="yok"), lambda d: "x")
    assert "proje şablonu 'bağımlılık'" in task.summary()


def test_schedule_tool_validates_project_template(workdir):
    from alice.tools.core import Schedule

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    with pytest.raises(ToolError, match="proje şablonu yok"):
        Schedule().run(ctx, "add", cron="0 9 * * 1", project="olmayan")


def test_agents_endpoint_lists_subagents_and_definitions(workdir):
    from alice.app.server import AppState  # noqa: F401 - so the endpoint module can be imported

    agent, _, _ = make_agent(["sonuç"], workdir)
    sub = subagents.start(agent, agent.ctx, "bak", label="bakıcı")
    sub.thread.join(5)
    snap = subagents.snapshots(agent.ctx)
    assert snap[0]["id"] == "a1" and snap[0]["label"] == "bakıcı" and snap[0]["status"] == "done"
    assert Path(workdir).is_dir()


# ------------------------------------------------------------------------- things that survive a reopen
def test_session_info_survives_restart_and_does_not_leak_into_new_chat(workdir):
    import time

    from alice.app.server import AppServer, AppState  # noqa: F401
    from tests.conftest import FakeProvider

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    chat = state.chat
    old_sid = chat.sid
    chat.agent.messages.append({"role": "user", "content": "selam"})
    chat.agent.stats["turns"], chat.agent.stats["input"] = 3, 1200
    chat.agent.usage_models["a:m"] = {"calls": 2, "input": 1200, "cached": 0, "output": 40, "seconds": 1.5}
    chat.agent.checkpoints[1] = {str(workdir / "x.py"): b"eski", str(workdir / "yeni.py"): None}
    chat.started = time.time() - 3600
    sub = subagents.Sub(id="a1", label="tarama", prompt="tara", status="done", result="buldum")
    sub.history_messages = [{"role": "user", "content": "tara"}, {"role": "assistant", "content": "buldum"}]
    subagents.registry(chat.ctx)["a1"] = sub
    chat.save()

    assert state.new_session() is None  # new chat: the previous one's info is not carried over
    assert state.agent.stats["turns"] == 0 and not state.agent.checkpoints and not subagents.registry(state.ctx)
    assert time.time() - state.chat.started < 60

    assert state.resume(old_sid)  # returning to a "kapanıp açılmış" chat: rebuilt from disk
    a = state.agent
    assert a.stats["turns"] == 3 and a.stats["input"] == 1200 and a.usage_models["a:m"]["calls"] == 2
    assert a.checkpoints[1][str(workdir / "x.py")] == b"eski" and a.checkpoints[1][str(workdir / "yeni.py")] is None
    assert time.time() - state.chat.started > 3000 and state.status()["started"] == state.chat.started
    restored = subagents.registry(state.ctx)["a1"]
    assert restored.status == "done" and restored.result == "buldum" and restored.child is None
    assert subagents._next_id(state.ctx) == "a2"  # ids don't collide


def test_restored_subagent_can_be_continued_with_its_old_conversation(workdir):
    seen = []

    def script(model, messages, tools, **kw):
        seen.append(messages)
        return "devam sonucu"

    agent, _, _ = make_agent(script, workdir)
    subagents.restore(agent.ctx, [{"id": "a3", "label": "tarama", "prompt": "tara", "status": "running",
                                   "messages": [{"role": "user", "content": "tara"},
                                                {"role": "assistant", "content": "ilk bulgu: Z"}]}])
    sub = subagents.registry(agent.ctx)["a3"]
    assert sub.status == "stopped"  # a job that was running when it closed counts as stopped
    subagents.resume(agent.ctx, "a3", "devam et").thread.join(5)
    assert sub.result == "devam sonucu" and any("ilk bulgu: Z" in str(m.get("content")) for m in seen[-1])


# ---------------------------------------------------------------------- findings from the PR #192 review
def test_subagent_result_goes_to_the_chat_that_started_it(workdir):
    gate = threading.Event()

    def script(model, messages, tools, **kw):
        if _user(messages) == "uzun":
            gate.wait(5)
        return "bitti"

    agent, _, _ = make_agent(script, workdir)
    got = []
    agent.ctx.state["session_id"] = "eski"
    agent.ctx.state["deliver_to"] = lambda sid, text: got.append((sid, text))
    sub = subagents.start(agent, agent.ctx, "uzun", background=True)
    agent.ctx.state["session_id"] = "yeni"  # the user switched to a new chat in the meantime
    gate.set()
    sub.thread.join(5)
    assert got and got[0][0] == "eski"


def test_two_continue_messages_do_not_run_the_same_subagent_twice(workdir):
    gate = threading.Event()

    def script(model, messages, tools, **kw):
        if _user(messages) == "devam":
            gate.wait(5)
        return "ok"

    agent, _, _ = make_agent(script, workdir)
    sub = subagents.start(agent, agent.ctx, "ilk")
    sub.thread.join(5)
    subagents.resume(agent.ctx, "a1", "devam")
    with pytest.raises(ToolError, match="hâlâ çalışıyor"):
        subagents.resume(agent.ctx, "a1", "devam")
    gate.set()
    sub.thread.join(5)


def test_subagent_permission_follows_its_own_stop_and_uses_parents_lock(workdir):
    from alice.tools.base import DesktopLock

    asked = {}

    class UI:
        def ask_permission(self, name, summary, risk, cancelled=None):
            asked.update(summary=summary, cancelled=cancelled)
            return "y"

    agent, _, _ = make_agent(["x"], workdir)
    agent.ctx.state["desktop_lock"] = lock = DesktopLock()
    sub = subagents.start(agent, agent.ctx, "x")
    sub.thread.join(5)
    sub.child.ui.parent_ui = UI()
    assert sub.child.ui.ask_permission("run_command", "ls", "exec") == "y"
    assert asked["summary"].startswith("[alt ajan a1") and asked["cancelled"] == sub.child.cancel.is_set
    # The lock is in the main chat's name: while the main chat holds it, the subagent does not wait;
    # when the subagent takes it, it is released in the main chat's name.
    assert sub.child.ctx.state["lock_owner"] is agent.ctx
    assert lock.acquire(agent.ctx, lambda: False) and lock.held_by(sub.child.ctx.state["lock_owner"])


@pytest.mark.skipif(shutil.which("git") is None, reason="git yok")
def test_pr_project_members_added_before_start_work_in_the_project_branch(tmp_path, monkeypatch):
    from alice.team import Team
    from alice.tools import github as gh

    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    git(work, "add", "a.txt")
    git(work, "commit", "-m", "ilk")
    monkeypatch.setattr(gh, "origin_repo", lambda cwd: "o/r")
    ctx = ToolContext(cwd=work, config=make_config({"brain": ["a:m"]}))
    ctx.state["session_id"] = "baslatan"
    team = Team(ctx, "hedef", pr=True, cwd=work)
    shared = team.add({"name": "ortak", "task": "iş"})
    wt = team.add({"name": "ayri", "task": "iş", "isolation": "worktree"})
    assert team.pr_branch == "alice/proje-" + team.id and team.pr_session == "baslatan"
    assert Path(team.cwd) != work and Path(shared.cwd) == Path(team.cwd)  # not the user's folder
    assert str(wt.cwd).startswith(str(team.cwd)) and ".alice-team" in str(wt.cwd)
    excl = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=team.cwd, capture_output=True,
                          text=True).stdout.strip()
    excl_path = Path(excl) if Path(excl).is_absolute() else Path(team.cwd) / excl
    assert ".alice-team/" in excl_path.read_text(encoding="utf-8")
    branches = subprocess.run(["git", "branch", "--list"], cwd=work, capture_output=True, text=True).stdout
    assert "alice-team/" in branches  # the agent branch is in the same repo, but its worktree is inside the project copy
    assert subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True,
                          text=True).stdout.strip() == ""
