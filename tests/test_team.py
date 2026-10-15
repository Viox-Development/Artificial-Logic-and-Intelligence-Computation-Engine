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
    """Bu dosya yöneticinin kendi inceleme akışını sınar; kurul ayrı (test_council.py)."""

    def __init__(self, *a, **k):
        k.setdefault("council", False)
        super().__init__(*a, **k)


def who(messages) -> str:
    sys = messages[0]["content"]
    if "projesinin yöneticisisin" in sys:
        return "lead"
    for name in NAMES:
        if f'"{name}" adlı ajanı' in sys:
            return name
    return "alice"


def steps(messages) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def since_user(messages) -> tuple[str, int]:
    """Son kullanıcı (olay) mesajı ve ondan sonraki asistan cevabı sayısı."""
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
    """Planlayan, cevaplayan, inceleyen, düzeltme isteyen ve bitiren sahte yönetici."""

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
            return [tool_call("write_file", {"path": "src/api.py", "content": "bozuk"}),
                    tool_call("team_send", {"to": "alice", "text": "negatif tutar reddedilsin mi?"}, "c2"),
                    tool_call("team_wait", {"seconds": 20}, "c3"),
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
            state["backend_done"] = True  # onay tests'i başlatır; bayrak ondan önce konmalı
        return orig_review(name, approve, feedback, by)

    team.review_member = watch
    state["backend_done"] = False
    team.start(plan=True)
    assert team.wait_until(lambda: team.finished, 30), team.status_text()

    assert team.summary == "add() yazıldı ve test edildi."
    assert team.status() == "done" and not team.active()
    assert {m.status for m in team.members.values()} == {"done"}
    assert (workdir / "src/api.py").read_text() == "def add(a, b):\n    return a + b\n"  # kapsam dışı yazma engellendi
    assert (workdir / "tests/test_api.py").exists()
    assert state.get("tests_started_after") is True  # after: tests, backend onaylandıktan sonra başladı
    assert state.get("saw_diff")
    tests = team.members["tests"]
    assert tests.reviews == 1 and tests.result.startswith("rapor: testler v2")
    tool_texts = "\n".join(str(m.get("content")) for m in tests.agent.messages if m["role"] == "tool")
    assert "kapsam dışı" in tool_texts and "negatif tutar reddedilsin" in tool_texts
    assert any(x.sender == "alice" and x.to == "tests" and "düzeltme" in x.text for x in team.log)
    assert "def add" in team.diff("backend")
    # ajan istemi: kişilik yok, görev ve takım listesi var
    sys = team.members["tests"].agent.messages[0]["content"]
    assert "Senin görevin: tests/test_api.py" in sys and "- backend:" in sys and "Your personality" not in sys
    assert "backend" in sys.split("Bu iş şunlar bittikten sonra başladı:")[1].splitlines()[0]
    # sonradan katılan ajan önceki ajana duyuruldu
    backend_users = [str(m["content"]) for m in team.members["backend"].agent.messages if m["role"] == "user"]
    assert any("yeni ajan katıldı: tests" in c for c in backend_users) or \
        any(x.sender == "sistem" and "tests" in x.text for x in team.log)
    # finish önce kaydeder, sonra yayınlar: son yayını bekle
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
        return "tamam"  # hiçbir şey yapmayan yönetici: proje dürtülerek kendiliğinden kapanmalı
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
    assert team.members["backend"].steps == 1  # temiz konuşmayla baştan


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

    # uygulama yeniden açıldı: proje diskten yüklenir (durmuş), konuşma ve fark korunur, mesajla devam eder
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
    assert ".alice-team/" in (workdir / ".git/info/exclude").read_text()
    team.start()
    assert team.wait_until(lambda: m.status == "review", 10)
    assert not (workdir / "yeni.py").exists()
    note = team.review_member("backend", True)
    assert "birleştirildi" in note and (workdir / "yeni.py").read_text() == "print(1)\n"


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
