from __future__ import annotations

import subprocess
import sys
import threading
import time

from alice.council import Council
from alice.providers.openai_compat import ProviderError
from alice.team import Team, guess_check, list_templates
from tests.conftest import tool_call
from tests.test_council import finding, js, role_of
from tests.test_team import make, steps, who

LEAD_SYS = "and the lead of the project"


def is_lead(messages) -> bool:
    return LEAD_SYS in messages[0]["content"]


def py(args: str) -> str:
    """Python call for run_command (on Windows, runs the quoted path via & in PowerShell)."""
    exe = f'"{sys.executable}"'
    return f"& {exe} {args}" if sys.platform == "win32" else f"{exe} {args}"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def git_init(cwd):
    git(cwd, "init", "-q")
    git(cwd, "config", "user.email", "a@b")
    git(cwd, "config", "user.name", "a")


def test_council_retries_once_when_json_missing(workdir):
    asked = {"n": 0}

    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role == "critic":
            asked["n"] += 1
            if "give only that JSON block" in str(messages[-1]["content"]):
                return js(vote="approve", reason="tamam")
            return "Bence iş iyi, sorun görmedim."  # forgot the JSON
        return js(vote="approve")

    agent, _, ctx = make(script, workdir)
    snap = Council(ctx, "work", "iş", "a\n" * 10, depth=1).run()
    critic = next(v for v in snap["votes"] if v["role"] == "critic")
    assert asked["n"] == 2 and critic["last"] == "approve" and snap["result"]["decision"] == "approve"


def test_lead_that_never_plans_gets_a_fallback_worker(workdir):
    def script(model, messages, tools, **kw):
        if is_lead(messages):
            return "Hmm, düşünüyorum."  # never calls any tool
        return "rapor: yaptım"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "küçük bir iş", review=False, council=False)
    team.start(plan=True)
    assert team.wait_until(lambda: "worker" in team.members, 20)
    lead_users = [str(m["content"]) for m in team.lead_agent.messages if m["role"] == "user"]
    assert sum("Henüz ajan kurmadın" in c for c in lead_users) == 2
    assert team.wait_until(lambda: team.members["worker"].status == "done", 10)
    assert any(x["kind"] == "plan" for x in team.timeline)


def test_member_fails_when_no_model_answers(workdir):
    def script(model, messages, tools, **kw):
        if who(messages) == "backend":
            raise ProviderError("auth", "anahtar geçersiz")
        return "tamam"

    agent, _, ctx = make(script, workdir)
    seen = []
    team = Team(ctx, "iş", council=False)
    team._notify = lambda text, kind: seen.append(kind)
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.status == "failed" and "failed" in seen, 15)
    assert m.result.startswith("Modellere ulaşılamadı: ") and "team:" not in m.result and "anahtar" in m.result


def test_stalled_member_is_reported(workdir):
    gate = threading.Event()

    def script(model, messages, tools, **kw):
        if is_lead(messages):
            return "tamam"
        if steps(messages) == 0:
            return tool_call("list_dir", {})
        gate.wait(10)  # stuck on the second model call
        return "rapor"

    agent, _, ctx = make(script, workdir, team_stall_minutes=0.01)
    team = Team(ctx, "iş", council=False, review=False)
    team._notify = lambda text, kind: None
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.stall_warned, 15)
    assert team.wait_until(lambda: any("ilerlemiyor" in str(x.get("content")) for x in
                                       (team.lead_agent.messages if team.lead_agent else [])), 10)
    gate.set()
    assert team.wait_until(lambda: m.status == "done", 10)


def test_worktree_conflict_is_handed_back_to_the_agent(workdir):
    git_init(workdir)
    (workdir / "a.txt").write_text("1\n", encoding="utf-8")
    git(workdir, "add", "-A")
    git(workdir, "commit", "-qm", "ilk")
    base = subprocess.run(["git", "-C", str(workdir), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    state = {"resolved": False, "base": base}

    def script(model, messages, tools, **kw):
        if who(messages) != "backend":
            return "tamam"
        last = str(messages[-1].get("content"))
        if "git merge" in last and not state["resolved"]:
            state["resolved"] = True
            return tool_call("run_command", {"command": f"git merge -X theirs --no-edit {state['base']}"}, "m1")
        if state["resolved"]:
            return "çakışmayı çözdüm"
        return [tool_call("read_file", {"path": "a.txt"}, "r0"),  # overwrite without a read is rejected
                tool_call("write_file", {"path": "a.txt", "content": "ajan\n"}), "rapor"][min(steps(messages), 2)]

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "iş", council=False)
    team.add({"name": "backend", "task": "a", "isolation": "worktree"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.status == "review", 10)
    (workdir / "a.txt").write_text("ana dal\n", encoding="utf-8")  # meanwhile main changed the same line too
    git(workdir, "commit", "-qam", "ana dalda değişiklik")
    note = team.review_member("backend", True)
    assert "çakışmayı çözüyor" in note and m.merge_attempts == 1
    assert team.wait_until(lambda: m.status == "review" and state["resolved"], 15)
    assert "birleştirildi" in team.review_member("backend", True)
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "ana dal\n"  # -X theirs: agent took main's side
    assert any(x["kind"] == "conflict" for x in team.timeline)


def test_run_command_changes_are_tracked_and_revertable(workdir):
    git_init(workdir)
    (workdir / "a.py").write_text("eski\n", encoding="utf-8")
    git(workdir, "add", "-A")
    git(workdir, "commit", "-qm", "ilk")
    (workdir / "kirli.txt").write_text("başta zaten vardı\n", encoding="utf-8")  # dirty before start: leave it alone
    (workdir / "w.py").write_text("open('a.py', 'w').write('yeni\\n')\nopen('b.py', 'w').write('x\\n')\n", encoding="utf-8")
    git(workdir, "add", "w.py")
    git(workdir, "commit", "-qm", "betik")
    cmd = py("w.py")

    def script(model, messages, tools, **kw):
        if who(messages) == "backend":
            return [tool_call("run_command", {"command": cmd}), "rapor"][min(steps(messages), 1)]
        return "tamam"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "iş", council=False)
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.status == "review", 10)
    assert sorted(m.files) == ["a.py", "b.py"]
    d = team.diff("backend")
    assert "-eski" in d and "+yeni" in d and "+x" in d
    team.revert("backend")
    assert (workdir / "a.py").read_text(encoding="utf-8") == "eski\n" and not (workdir / "b.py").exists()
    assert (workdir / "kirli.txt").exists()


def test_check_command_lessons_and_rules_reach_the_council(workdir):
    (workdir / "REVIEW.md").write_text("Her public fonksiyonun testi olmalı.\n", encoding="utf-8")
    seen = {}

    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role:
            seen.setdefault(role, []).append((messages[0]["content"], str(messages[1]["content"])))
            if role == "critic":
                return js(vote="reject", findings=[finding("major", "test eksik")])
            if role == "verifier":
                return js(checks=[{"id": "F1", "status": "confirmed"}], vote="reject")
            return js(vote="reject")
        if is_lead(messages):
            return "tamam"
        return [tool_call("write_file", {"path": "a.py", "content": "x\n"}), "rapor"][min(steps(messages), 1)]

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "iş", council=True, check=f'{sys.executable} -c "print(\'KONTROL-TAMAM\')"')
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.councils and m.councils[0].get("state") == "done", 15)
    sys_prompt, material = seen["critic"][0]
    assert "Her public fonksiyonun testi olmalı" in sys_prompt and "KONTROL-TAMAM" in material
    assert any("kontrol komutu" in x["text"] for x in team.timeline)
    assert team.wait_until(lambda: team.lessons == ["test eksik → kontrol ekle"], 10)
    # on a fix request the lesson goes to the agent too; the second council's prompt includes it
    assert team.wait_until(lambda: len(m.councils) >= 2 and m.councils[1].get("state") == "done", 20)
    assert any("test eksik" in s for s, _ in seen["critic"][1:])
    team.stop()


def test_templates_timeline_and_check_guess(workdir):
    agent, _, ctx = make(lambda *a, **k: "rapor" if not is_lead(a[1]) else "tamam", workdir)
    team = Team(ctx, "Şablonluk proje", council=False, check="pytest -q")
    team.add({"name": "backend", "task": "a", "scope": ["src"], "model": "fast"})
    team.start()
    tpl = team.save_template("Benim akışım")
    rows = list_templates()
    assert rows[0]["name"] == "Benim akışım" and rows[0]["agents"][0]["scope"] == ["src"] and rows[0]["check"] == "pytest -q"
    assert tpl["agents"][0]["model"] == "fast"
    team.send("kullanıcı", "backend", "selam")
    kinds = [x["kind"] for x in team.timeline_view()]
    assert kinds[0] == "start" and "msg" in kinds
    (workdir / "tests").mkdir()
    assert guess_check(workdir) == "python -m pytest -q"
    (workdir / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    assert guess_check(workdir) == "npm test"
    team.stop()


def test_ask_and_done_notify(workdir):
    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    seen = []
    team = Team(ctx, "iş", council=False)
    team._notify = lambda text, kind: seen.append((kind, text))
    team.ask_user("Hangisi?")
    team.finish("özet")
    assert [k for k, _ in seen] == ["ask", "done"]


def test_member_output_streams_live(workdir):
    from alice.agent import Agent, NullUI
    from alice.permissions import Permissions
    from alice.providers.router import Router
    from alice.tools import all_tools
    from alice.tools.base import ToolContext
    from tests.conftest import FakeProvider, make_config

    class Streaming(FakeProvider):
        def chat_stream(self, model, messages, on_delta, cancel=None, on_reasoning=None, **kw):
            res = self.chat(model, messages, **kw)
            if on_reasoning:
                on_reasoning("önce dosyaya bakayım")
            for i in range(0, len(res.text), 4):  # slowly, like a real model
                on_delta(res.text[i:i + 4])
                time.sleep(0.05)
            return res

    def script(model, messages, tools, **kw):
        return "tamam" if is_lead(messages) else "Rapor: her şey yolunda, testler geçti."

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = Streaming("a", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    Agent(cfg, router, all_tools(), ctx, Permissions("auto"), NullUI(), system_prompt="sys")
    events = []
    team = Team(ctx, "iş", council=False, review=False, publish=events.append)
    team._notify = lambda text, kind: None
    team.add({"name": "backend", "task": "a"})
    team.start()
    assert team.wait_until(lambda: team.members["backend"].status == "done", 10)
    live = [e for e in events if e["type"] == "team_live" and e["name"] == "backend"]
    assert any(e["text"].startswith("Rapor") for e in live)  # text arrived in pieces while being written
    assert any(e["think"] == "önce dosyaya bakayım" for e in live)
    assert live[-1]["text"] == "" and live[-1]["think"] == ""  # the live box closed when it finished
    team.stop()


def test_member_tool_progress_counts_as_activity(tmp_path):
    from alice.team import Member, MemberUI

    events = []
    team = type("T", (), {"id": "p1", "publish": lambda self, ev: events.append(ev)})()
    m = Member(name="web", task="site")
    m.last_at, m.stall_warned = 1.0, True
    ui = MemberUI(team, m)
    ui.tool_delta("write_file", 12500)
    assert m.last_at > 1.0 and m.stall_warned is False
    assert events[-1]["tool"] == "write_file hazırlıyor · 12 KB"
    ui.stream_end("", "x")
    assert events[-1]["tool"] == ""


def test_stuck_member_is_restarted_on_next_model_and_usage_is_counted(workdir):
    from alice.agent import Agent, NullUI
    from alice.permissions import Permissions
    from alice.providers.router import Router
    from alice.tools import ToolContext, all_tools
    from tests.conftest import FakeProvider, make_config

    gate = threading.Event()

    def script(model, messages, tools, **kw):
        if is_lead(messages):
            return "tamam"
        if steps(messages) == 0:
            return tool_call("list_dir", {})
        if model == "m":
            gate.wait(20)  # first model gets stuck on the second step
            return "geç kaldım"
        return "rapor: bitti"

    cfg = make_config({"brain": ["a:m", "a:m2"], "fast": ["a:m"], "vision": ["a:m"]}, team_stall_minutes=0.01)
    prov = FakeProvider("a", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    agent = Agent(cfg, router, all_tools(), ctx, Permissions("auto"), NullUI(), system_prompt="sys")
    agent.stream = False
    ctx.state["agent"] = agent
    team = Team(ctx, "iş", council=False, review=False)
    seen = []
    team._notify = lambda text, kind: seen.append(text)
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    try:
        assert team.wait_until(lambda: m.status == "done", 20)
    finally:
        gate.set()
    assert m.recoveries == 1 and m.result == "rapor: bitti"
    resumed = [str(x["content"]) for x in m.agent.messages if x["role"] == "user"]
    assert any(c.startswith("[sistem] Önceki adımın") for c in resumed)
    assert any("otomatik yeniden başlatıldı" in x["text"] for x in team.timeline)
    usage = team.snapshot()["usage"]
    assert usage["by"]["backend"]["in"] >= 20 and usage["total"]["out"] > 0
    assert Team.load(team.id, ctx).usage["backend"]["in"] == usage["by"]["backend"]["in"]


def test_design_projects_get_a_design_direction_step(workdir):
    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    web = Team(ctx, "Savunma firması için çok sayfalı web sitesi", council=False)
    assert "DESIGN.md" in web.lead_brief() and "template" in web.lead_brief()
    assert "DESIGN.md" not in Team(ctx, "Ödeme servisini yeniden düzenle", council=False).lead_brief()
    (workdir / "DESIGN.md").write_text("Renk: kömür + sinyal turuncusu. Yazı: IBM Plex.", encoding="utf-8")
    m = web.add({"name": "sayfalar", "task": "about.html yaz"})
    assert m.model == "auto" and "sinyal turuncusu" in web.brief(m)


def test_minor_only_conditions_approve_without_a_new_round(workdir):
    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    team = Team(ctx, "iş", council=True)
    m = team.add({"name": "web", "task": "a"})
    m.status = "review"
    seen = []
    team.event = lambda text: seen.append(text)
    snap = {"result": {"decision": "approve_conditions", "conditions": ["sayaç biçimi daha okunur olsun"]},
            "findings": [{"id": "F1", "severity": "minor", "status": "confirmed", "title": "sayaç biçimi", "fix": "x"}]}
    team._apply_council(m, snap, "rapor", "- şart: sayaç biçimi")
    assert m.status == "done" and m.council_cycles == 0 and "zorunlu değil" in seen[-1]


def test_duplicate_conditions_and_lessons_are_dropped():
    from alice.council import dedupe

    items = ["initCounters içinde data-aura-counter guard ekleyin",
             "şart: initCounters içinde data-aura-counter koruması (guard) eklenmeli",
             "Bülten formu seçicisi spesifik hale getirin (örn. .newsletter-form)",
             "Newsletter form seçicisi .newsletter-form ile daraltılmalı", "sıfır kontrolü"]
    assert dedupe(items) == [items[0], items[2], items[3], items[4]]  # cautious: keeps items it is unsure about
    assert dedupe(["Mobile menu must close on link click", "Mobile menu must have aria-expanded"]) == \
        ["Mobile menu must close on link click", "Mobile menu must have aria-expanded"]
    assert len(dedupe(["Add tests for login", "Add tests for logout"])) == 2


def test_stopping_during_final_council_does_not_loop(workdir):
    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    team = Team(ctx, "iş", council=True)
    m = team.add({"name": "web", "task": "a"})
    m.status = "done"
    team.stop()
    team._final_pending = True
    team._maybe_final()
    time.sleep(0.5)
    assert len(team.councils) == 0 and team.council_busy == 0 and team._final_pending


def test_conditions_without_findings_still_block():
    from alice.council import blocking

    assert blocking({"result": {"conditions": ["mobil menü kapanmıyor"]}, "findings": []})
    assert not blocking({"result": {"conditions": ["x"]},
                         "findings": [{"severity": "minor", "status": "confirmed"}]})
