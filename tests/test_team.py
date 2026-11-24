from __future__ import annotations

import subprocess
import threading

import pytest

from alice.agent import Agent, NullUI
from alice.permissions import Permissions
from alice.providers.router import Router
from alice.team import Team as _Team
from alice.team import in_scope, list_projects, patch_paths, write_targets
from alice.tools import all_tools
from alice.tools.base import ToolContext, ToolError
from tests.conftest import FakeProvider, make_config, tool_call

NAMES = ("backend", "tests", "docs")


class Team(_Team):
    """Tests the lead's own review flow; the council is tested separately (test_council.py)."""

    def __init__(self, *a, **k):
        k.setdefault("council", False)
        super().__init__(*a, **k)


def who(messages) -> str:
    sys = messages[0]["content"]
    if "and the lead of the project" in sys:
        return "lead"
    for name in NAMES:
        if f'agent named "{name}"' in sys:
            return name
    return "alice"


def steps(messages) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def since_user(messages) -> tuple[str, int]:
    """The last user (event) message and the number of assistant replies after it."""
    n = 0
    for m in reversed(messages):
        if m["role"] == "user":
            return str(m.get("content")), n
        if m["role"] == "assistant":
            n += 1
    return "", n


def make(script, workdir, **agent_cfg):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}, **agent_cfg)
    prov = FakeProvider("a", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    agent = Agent(cfg, router, all_tools(), ctx, Permissions("auto"), NullUI(), system_prompt="sys")
    agent.stream = False
    return agent, prov, ctx


def lead_script(state):
    """Fake lead that plans, answers, reviews, asks for revisions and finishes."""

    def run(messages):
        event, k = since_user(messages)
        plan = []
        if "henüz ajan yok" in event:
            plan += [tool_call("assign", {"name": "backend", "task": "src/api.py içine add(a, b) yaz", "scope": ["src"],
                                          "thinking": "high"}),
                     tool_call("assign", {"name": "tests", "task": "tests/test_api.py yaz", "scope": ["tests"],
                                          "model": "fast", "after": ["backend"]}, "c2")]
        if "[tests -> alice]" in event:
            plan.append(tool_call("message", {"to": "tests", "text": "evet, negatif tutar reddedilsin"}, "c3"))
        if "backend bitirdi, inceleme bekliyor" in event:
            plan += [tool_call("diff", {"name": "backend"}, "c4"),
                     tool_call("review", {"name": "backend", "decision": "approve"}, "c5")]
        if "tests bitirdi, inceleme bekliyor" in event:
            if not state.get("revised"):
                state["revised"] = True
                plan.append(tool_call("review", {"name": "tests", "decision": "revise",
                                                 "feedback": "negatif tutar testi eksik"}, "c6"))
            else:
                plan.append(tool_call("review", {"name": "tests", "decision": "approve"}, "c7"))
        if "Bütün ajanlar bitti" in event:
            plan.append(tool_call("finish", {"summary": "add() yazıldı ve test edildi."}, "c8"))
        return plan[k] if k < len(plan) else "tamam"

    return run


def scripted(state):
    lead = lead_script(state)

    def run(model, messages, tools, **kw):
        me, n = who(messages), steps(messages)
        if me == "lead":
            last = messages[-1]
            if last["role"] == "tool" and "def add" in str(last["content"]):
                state["saw_diff"] = True
            return lead(messages)
        if me == "backend":
            return [tool_call("write_file", {"path": "src/api.py", "content": "def add(a, b):\n    return a + b\n"}),
                    "rapor: src/api.py yazıldı"][min(n, 1)]
        if me == "tests":
            state.setdefault("tests_started_after", state.get("backend_done", False))
            # The lead's answer can land before team_wait (delivered after the tool step as a "[takım mesajı]" user
            # message) or during it (as the tool result). If it is already here, don't sit out the whole wait.
            answered = any(m["role"] == "user" and "negatif tutar reddedilsin" in str(m.get("content"))
                           for m in messages[1:])
            return [tool_call("write_file", {"path": "src/api.py", "content": "bozuk"}),
                    tool_call("team_send", {"to": "alice", "text": "negatif tutar reddedilsin mi?"}, "c2"),
                    tool_call("team_wait", {"seconds": 1 if answered else 20}, "c3"),
                    tool_call("write_file", {"path": "tests/test_api.py", "content": "from src.api import add\n"}, "c4"),
                    "rapor: testler v1",
                    "rapor: testler v2 (negatif tutar eklendi)"][min(n, 5)]
        raise AssertionError(me)

    return run


def test_autonomous_project_plans_reviews_and_finishes(workdir):
    state: dict = {}
    agent, prov, ctx = make(scripted(state), workdir)
    events = []
    team = Team(ctx, "Toplama API'si ve testi", publish=events.append)
    orig_review = team.review_member

    def watch(name, approve, feedback="", by="alice"):
        if name == "backend" and approve:
            state["backend_done"] = True  # approval starts tests; the flag must be set before that
        return orig_review(name, approve, feedback, by)

    team.review_member = watch
    state["backend_done"] = False
    team.start(plan=True)
    assert team.wait_until(lambda: team.finished, 30), team.status_text()

    assert team.summary == "add() yazıldı ve test edildi."
    assert team.status() == "done" and not team.active()
    assert {m.status for m in team.members.values()} == {"done"}
    assert (workdir / "src/api.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"  # blocked: out of scope
    assert (workdir / "tests/test_api.py").exists()
    assert state.get("tests_started_after") is True  # after: tests started after backend was approved
    assert state.get("saw_diff")
    tests = team.members["tests"]
    assert tests.reviews == 1 and tests.result.startswith("rapor: testler v2")
    tool_texts = "\n".join(str(m.get("content")) for m in tests.agent.messages if m["role"] == "tool")
    assert "kapsam dışı" in tool_texts
    # the lead's answer reached the agent: as team_wait's result, or as a team message after the team_send step
    # (which one depends on thread timing; checking only tool results made this test flaky, #379)
    received = "\n".join(str(m.get("content")) for m in tests.agent.messages[1:]
                         if m["role"] == "tool" or (m["role"] == "user" and "[takım mesajı]" in str(m.get("content"))))
    assert "[alice -> tests] evet, negatif tutar reddedilsin" in received
    assert any(x.sender == "alice" and x.to == "tests" and "düzeltme" in x.text for x in team.log)
    assert "def add" in team.diff("backend")
    # agent prompt: no personality, has the task and the team list
    sys = team.members["tests"].agent.messages[0]["content"]
    assert "Your task: tests/test_api.py" in sys and "- backend:" in sys and "Your personality" not in sys
    assert "backend" in sys.split("This job started after these finished:")[1].splitlines()[0]
    # the late-joining agent was announced to the earlier one
    backend_users = [str(m["content"]) for m in team.members["backend"].agent.messages if m["role"] == "user"]
    assert any("yeni ajan katıldı: tests" in c for c in backend_users) or \
        any(x.sender == "sistem" and "tests" in x.text for x in team.log)
    # finish saves first, then publishes: wait for the last publish
    assert team.wait_until(lambda: [e for e in events if e["type"] == "team"][-1]["status"] == "done", 5)
    assert [e for e in events if e["type"] == "team"][-1]["summary"]
    assert any(e["type"] == "project_note" and e["kind"] == "done" for e in events)
    assert list_projects()[0]["status"] == "done"


def test_member_tools_are_limited(workdir):
    agent, _, ctx = make(lambda *a, **k: "x", workdir)
    team = Team(ctx, "hedef")
    m = team.add({"name": "docs", "task": "oku", "isolation": "readonly"})
    member = team._make_agent(m)
    names = set(member.tools)
    assert {"read_file", "grep", "team_send", "team_wait"} <= names
    assert not names & {"computer", "browser", "task", "team", "run_plan", "github", "assign", "finish"}
    assert member.permissions.mode == "plan"
    lead = team._make_lead()
    assert {"assign", "message", "review", "diff", "ask_user", "finish", "project_status", "run_command"} <= set(lead.tools)


def test_validation(workdir):
    agent, _, ctx = make(lambda *a, **k: "x", workdir)
    team = Team(ctx, "hedef")
    for bad in ({"name": "Alice", "task": "x"}, {"name": "ok", "task": ""}, {"name": "ok", "task": "x", "model": "yok"},
                {"name": "ok", "task": "x", "after": ["yok"]}):
        with pytest.raises(ToolError):
            team.add(bad)
    team.add({"name": "ok", "task": "x", "model": "a:m"})
    with pytest.raises(ToolError):
        team.add({"name": "ok", "task": "x"})
    with pytest.raises(ToolError):
        team.send("alice", "kimse", "selam")


def simple(model, messages, tools, **kw):
    me = who(messages)
    if me == "lead":
        return "tamam"  # lead that does nothing: the project must close on its own when nudged
    if me == "backend":
        return [tool_call("write_file", {"path": "src/a.py", "content": "x = 1\n"}), "rapor"][min(steps(messages), 1)]
    return "rapor"


def test_lazy_lead_is_nudged_then_project_closes(workdir):
    agent, _, ctx = make(simple, workdir)
    team = Team(ctx, "hedef", review=False)
    team.add({"name": "backend", "task": "a.py yaz"})
    team.start()
    assert team.wait_until(lambda: team.finished, 20)
    lead_users = [str(m["content"]) for m in team.lead_agent.messages if m["role"] == "user"]
    assert sum("proje kapanmadı" in c for c in lead_users) == 2
    assert team.summary == "tamam"


def test_user_review_revert_restart_and_questions(workdir):
    agent, _, ctx = make(simple, workdir)
    team = Team(ctx, "hedef")
    team.add({"name": "backend", "task": "a.py yaz"})
    team.start()
    assert team.wait_until(lambda: team.members["backend"].status == "review", 10)
    assert (workdir / "src/a.py").exists() and "+x = 1" in team.diff("backend")
    assert team.revert("backend") == ["src/a.py"] and not (workdir / "src/a.py").exists()

    q = team.ask_user("Hangi veritabanı?")
    assert team.status() == "needs_you"
    team.answer(q["id"], "SQLite")
    assert q["answer"] == "SQLite" and team.status() != "needs_you"
    assert any("SQLite" in x.text and x.to == "alice" for x in team.log)

    team.review_member("backend", True, by="kullanıcı")
    assert team.members["backend"].status == "done"
    team.restart("backend")
    assert team.wait_until(lambda: team.members["backend"].status == "review", 10)
    assert team.members["backend"].steps == 1  # start over from scratch with a clean conversation


def test_project_is_saved_loaded_and_resumed(workdir):
    agent, _, ctx = make(simple, workdir)
    team = Team(ctx, "Kaydedilen hedef\nikinci satır")
    team.add({"name": "backend", "task": "a.py yaz", "scope": ["src"]})
    team.start()
    assert team.wait_until(lambda: team.members["backend"].status == "review", 10)
    team.save()
    rows = list_projects()
    assert rows[0]["id"] == team.id and rows[0]["title"] == "Kaydedilen hedef"
    assert rows[0]["members"] == [{"name": "backend", "status": "review"}]

    # app reopened: the project is loaded from disk (stopped), conversation and diff are kept, resumes with a message
    loaded = Team.load(team.id, ctx)
    m = loaded.members["backend"]
    assert loaded.stopped and m.status == "review" and m.agent is None
    assert "+x = 1" in loaded.diff("backend")
    assert any(x.get("role") == "tool" for x in m.messages())
    loaded.send("kullanıcı", "backend", "devam et")
    assert not loaded.stopped
    assert loaded.wait_until(lambda: m.status == "review" and m.agent is not None, 10)
    contents = [str(x.get("content")) for x in m.agent.messages]
    assert "Görevine başla." in contents and any("devam et" in c for c in contents)


def test_after_dependency_and_max_parallel(workdir):
    gate = threading.Event()

    def script(model, messages, tools, **kw):
        if who(messages) == "lead":
            return "tamam"
        gate.wait(10)
        return "bitti"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "hedef", max_parallel=1, review=False)
    team.add({"name": "backend", "task": "a"})
    team.add({"name": "tests", "task": "b", "after": ["backend"]})
    team.add({"name": "docs", "task": "c"})
    team.start()
    assert [m.status for m in team.members.values()] == ["running", "queued", "queued"]
    gate.set()
    assert team.wait_until(team.all_done, 20)


def test_worktree_member_is_merged_on_approve(workdir):
    def git(*args):
        subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "ilk")

    def script(model, messages, tools, **kw):
        if who(messages) == "backend":
            return [tool_call("write_file", {"path": "yeni.py", "content": "print(1)\n"}), "rapor"][
                min(steps(messages), 1)]
        return "tamam"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "hedef")
    m = team.add({"name": "backend", "task": "a", "isolation": "worktree"})
    assert m.cwd.resolve() == (workdir / ".alice-team" / f"{team.id}-backend").resolve() and m.cwd.is_dir()
    assert m.branch.startswith("alice-team/backend-")
    assert ".alice-team/" in (workdir / ".git/info/exclude").read_text(encoding="utf-8")
    team.start()
    assert team.wait_until(lambda: m.status == "review", 10)
    assert not (workdir / "yeni.py").exists()
    note = team.review_member("backend", True)
    assert "birleştirildi" in note and (workdir / "yeni.py").read_text(encoding="utf-8") == "print(1)\n"


def test_chat_team_tool_waits_for_summary(workdir):
    def script(model, messages, tools, **kw):
        me = who(messages)
        if me == "alice":
            n = steps(messages)
            return [tool_call("team", {"action": "start", "goal": "küçük iş",
                                       "agents": [{"name": "backend", "task": "a"}], "review": False}),
                    tool_call("team", {"action": "wait", "timeout": 30}, "c2"), "bitti"][min(n, 2)]
        if me == "lead":
            event, k = since_user(messages)
            if "Bütün ajanlar bitti" in event and k == 0:
                return tool_call("finish", {"summary": "özet burada"})
            return "tamam"
        return "rapor"

    agent, _, ctx = make(script, workdir)
    assert agent.run_turn("büyük iş") == "bitti"
    tool_texts = "\n".join(str(m.get("content")) for m in agent.messages if m["role"] == "tool")
    assert "proje başladı" in tool_texts and "özet burada" in tool_texts


def test_scope_helpers(tmp_path):
    assert in_scope(tmp_path / "src/a.py", [tmp_path / "src"])
    assert not in_scope(tmp_path / "srcx/a.py", [tmp_path / "src"])
    assert in_scope(tmp_path / "x", [])
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b/c.py\n+z\n*** End Patch"
    assert patch_paths(patch) == ["a.py", "b/c.py"]
    assert write_targets("file_ops", {"path": "a", "paths": ["b"], "destination": "c"}) == ["a", "c", "b"]


def test_saved_project_read_retries_while_file_is_being_replaced(tmp_path, monkeypatch):
    from pathlib import Path

    from alice import team as team_mod

    f = tmp_path / "p.json"
    f.write_text('{"id": "p"}', encoding="utf-8")
    real, calls = Path.read_text, []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    monkeypatch.setattr(team_mod.time, "sleep", lambda s: None)
    assert team_mod._read_saved(f) == '{"id": "p"}' and len(calls) == 3


def test_save_survives_brief_windows_file_lock(tmp_path, monkeypatch):
    # Windows: antivirus briefly locks the new file; the save must survive a few PermissionErrors from replace.
    from pathlib import Path

    from alice import team as team_mod

    src, dst = tmp_path / "p.json.tmp", tmp_path / "p.json"
    src.write_text("{}", encoding="utf-8")
    real, fails = Path.replace, [2]

    def flaky(self, target):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError("kilitli")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    monkeypatch.setattr(team_mod.time, "sleep", lambda s: None)
    team_mod._replace(src, dst)
    assert dst.read_text(encoding="utf-8") == "{}" and not src.exists()
    fails[0] = 99
    src.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError):
        team_mod._replace(src, dst, tries=3)


def test_list_projects_survives_file_being_replaced(monkeypatch):
    # Windows: reading raised PermissionError while the project was being saved, so it dropped out of the list
    # (On Windows CI, list_projects() returned empty in test_autonomous_project_...).
    import json
    from pathlib import Path

    from alice import team_store

    (team_store.projects_dir() / "p1.json").write_text(json.dumps({"id": "p1", "status": "done"}), encoding="utf-8")
    real, calls = Path.read_text, []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    monkeypatch.setattr(team_store.time, "sleep", lambda s: None)
    rows = team_store.list_projects()
    assert [r["id"] for r in rows] == ["p1"] and rows[0]["status"] == "done"


def test_git_output_is_decoded_as_utf8(tmp_path):
    # On Windows the local code page (cp1254) couldn't decode "Ğ"/"Ş" bytes (0x9E): git show/diff UnicodeDecodeError
    # which dropped the agent's step.
    from alice import team as team_mod

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "t.txt").write_bytes("Ğ Ş ı 😀\n".encode("utf-8"))
    git("add", "t.txt")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "ilk")
    r = team_mod._git(tmp_path, "show", "HEAD:t.txt")
    assert r.returncode == 0 and r.stdout == "Ğ Ş ı 😀\n"


def test_git_dirty_handles_non_ascii_and_renamed_paths(tmp_path):
    # Plain porcelain output escaped "dü.py" as "d\303\274.py", so files changed by a command were not found.
    from types import SimpleNamespace

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "eski.py").write_text("x\n", encoding="utf-8")
    git("add", "eski.py")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "ilk")
    (tmp_path / "dü şey.py").write_text("y\n", encoding="utf-8")
    git("mv", "eski.py", "yeni.py")
    dirty = _Team._git_dirty(SimpleNamespace(cwd=tmp_path))
    assert str((tmp_path / "dü şey.py").resolve()) in dirty
    assert str((tmp_path / "yeni.py").resolve()) in dirty
    assert str((tmp_path / "eski.py").resolve()) not in dirty


def test_check_command_timeout_kills_grandchildren(tmp_path):
    # A process spawned by the shell kept the pipe open, so subprocess.run hung past its timeout (council stuck).
    import sys
    import time
    from types import SimpleNamespace

    fake = SimpleNamespace(check=f'"{sys.executable}" -c "import time; time.sleep(60)"', cwd=tmp_path,
                           parent_ctx=SimpleNamespace(config=SimpleNamespace(agent={"team_check_timeout": 1})),
                           mark=lambda *a: None)
    t0 = time.monotonic()
    res = _Team.run_check(fake)
    assert res["code"] == -1 and "zaman aşımı" in res["out"]
    assert time.monotonic() - t0 < 20


def test_stopped_lead_loop_exits_even_if_resumed_meanwhile(workdir):
    # If Resume started a new loop right after the loop said "stopped, exiting", the old loop saw lead_thread
    # was set and kept running: two loops on the same lead agent.
    _, _, ctx = make(simple, workdir)
    team = Team(ctx, "hedef")
    from types import SimpleNamespace

    team.lead_agent = SimpleNamespace(messages=[{"role": "system", "content": ""}])  # treated as already set up
    team.stopped = True
    other = threading.Thread(target=lambda: None)  # instead of the loop Resume started

    class Hooked(threading.Condition):
        fired = False

        def __exit__(self, *a):
            res = super().__exit__(*a)
            if not Hooked.fired:
                Hooked.fired = True
                team.stopped, team.lead_thread = False, other  # resumed at exactly this moment
            return res

    team.cond = Hooked()
    t = threading.Thread(target=team._lead_loop, daemon=True)
    t.start()
    t.join(3)
    alive = t.is_alive()
    team.finished = True  # if the loop is still running in old code, let it close
    t.join(5)
    assert not alive and team.lead_thread is other


def test_next_survives_member_added_while_launching(workdir):
    # When an agent finishes, _next() walks the members (each _launch starts and records a thread). If the lead adds an
    # agent via assign/plan meanwhile, that thread dies with "dictionary changed size during iteration"; the rest stall.
    from alice.team import Member

    _, _, ctx = make(simple, workdir)
    team = Team(ctx, "hedef")
    for n in ("a", "b"):
        team.members[n] = Member(name=n, task="t")
    launched = []

    def launch(m, resume=False):
        launched.append(m.name)
        if "c" not in team.members:  # the lead added a new agent at exactly this moment
            team.members["c"] = Member(name="c", task="t", status="running")

    team._launch = launch
    team._next()
    assert launched == ["a", "b"]


def test_plan_council_counter_changes_under_lock(workdir):
    # The plan tool incremented council_busy without the lock, so an increment could be lost while council threads
    # decremented it under the lock: the counter never hit zero and finish waited forever on "kurul toplanıyor".
    from alice.team_tools import LeadPlan

    bad = []

    class Checked(_Team):
        @property
        def council_busy(self):
            return self._busy

        @council_busy.setter
        def council_busy(self, v):
            if hasattr(self, "cond") and not self.cond._is_owned():
                bad.append(v)
            self._busy = v

    _, _, ctx = make(simple, workdir)
    team = Checked(ctx, "hedef", council=True)
    team._council = lambda *a, **k: {"state": "stopped"}
    res = LeadPlan(team).run(ctx, "yaklaşım", [{"name": "backend", "task": "a"}])
    assert res.is_error and team.council_busy == 0 and not bad


def test_diff_survives_member_touching_new_file(workdir, monkeypatch):
    # If a running agent touched a new file while the council read the diff, originals grew and the diff was lost.
    from alice import team as team_mod
    from alice.team import Member

    _, _, ctx = make(simple, workdir)
    team = Team(ctx, "hedef")
    m = team.members["a"] = Member(name="a", task="t", cwd=workdir)
    for n in ("x.py", "y.py"):
        (workdir / n).write_text("yeni\n", encoding="utf-8")
        m.originals[str((workdir / n).resolve())] = "eski\n"
    real = team_mod._read_original

    def reading(p):
        m.originals.setdefault(str((workdir / "z.py").resolve()), None)  # the agent touched z.py at exactly this moment
        return real(p)

    monkeypatch.setattr(team_mod, "_read_original", reading)
    text = team.diff("a")
    assert "x.py" in text and "y.py" in text


def test_stop_member_before_its_agent_exists(workdir):
    gate = threading.Event()
    calls = []

    def script(model, messages, tools, **kw):
        calls.append(who(messages))
        return "rapor"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "hedef", review=False)
    m = team.add({"name": "backend", "task": "a"})
    real = team._make_agent

    def slow_make(member):
        gate.wait(10)  # model selection / worktree setup still in progress
        return real(member)

    team._make_agent = slow_make
    team._launch(m)
    assert m.status == "running" and m.agent is None
    team.stop("backend")
    gate.set()
    assert team.wait_until(lambda: m.status not in ("running", "waiting", "asking"), 10)
    assert m.status == "stopped" and "backend" not in calls
    with pytest.raises(ToolError):
        team.stop("kimse")
