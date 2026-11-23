from __future__ import annotations

import json

from alice.council import ROLES, Council, Finding, Vote, fix_list, parse_block, plan_council, report, retally, tally
from alice.team import Team
from tests.conftest import tool_call
from tests.test_team import make, since_user, steps


def role_of(messages) -> str:
    sys = messages[0]["content"]
    for key, r in ROLES.items():
        if f"Your council role: {r.title} " in sys:
            return key
    return ""


def js(**kw) -> str:
    return "Değerlendirmem:\n```json\n" + json.dumps(kw, ensure_ascii=False) + "\n```"


def finding(sev="major", title="sıfıra bölme kontrolü yok"):
    return {"severity": sev, "title": title, "evidence": "src/a.py:3", "fix": "kontrol ekle"}


def test_parse_block_and_votes():
    assert parse_block(js(vote="reject"))["vote"] == "reject"
    assert parse_block('bla {"vote": "approve"} son') == {"vote": "approve"}
    assert parse_block("json yok") == {}


def test_tally_rules():
    f1 = Finding("F1", "critic", "major", "x", status="refuted")
    f2 = Finding("F2", "critic", "critical", "y", status="confirmed")
    crit = Vote("critic", "m", 1, "reject", "reject", basis=["F1"])
    adv = Vote("advocate", "m", 1, "approve", "approve")
    ver = Vote("verifier", "m", 1, "approve", "approve")
    r = tally([crit, adv, ver], [f1])
    assert r["decision"] == "approve" and not crit.counted  # a rejection based on a refuted finding is not counted
    r = tally([Vote("critic", "m", 1, "reject", "reject", basis=["F2"]), adv, ver], [f2])
    assert r["decision"] == "revise" and r["veto"] == ["F2"]  # a confirmed critical finding is a veto
    cond = Vote("critic", "m", 1, "conditional", "conditional", conditions=["log ekle"])
    r = tally([cond, adv, ver], [])
    assert r["decision"] == "approve_conditions" and r["conditions"] == ["log ekle"]
    sec = Vote("security", "m", 1, "reject", "reject", basis=["F3"])
    f3 = Finding("F3", "security", "major", "z", status="unclear")
    r = tally([sec, adv], [f3], relevant=["security"])  # expert weight 2: 1/3 approval
    assert r["decision"] == "revise" and sec.weight == 2
    r = tally([sec, adv], [f3], user={"vote": "approve"}, relevant=["security"])  # the user counts 3x: 4/6
    assert r["decision"] == "approve"


def test_plan_council_scales_with_work():
    roles, rounds = plan_council("work", "küçük yardımcı fonksiyon düzeltmesi", 10, False)
    assert roles == ["critic", "verifier"] and rounds == 1
    roles, rounds = plan_council("work", "login token kontrolü", 200, False)
    assert {"critic", "advocate", "verifier", "security"} <= set(roles) and rounds == 2
    roles, rounds = plan_council("final", "her şey", 50, True)
    assert set(roles) == set(ROLES) and rounds == 3


def council_script(state):
    def run(model, messages, tools, **kw):
        role = role_of(messages)
        prompt = str(messages[1]["content"])
        state.setdefault("calls", []).append((role, "debate" if "Debate round" in prompt else "first"))
        if role == "critic":
            if "Debate round" in prompt:
                return js(vote="reject", reason="hâlâ ret", stances=[{"id": "F1", "stance": "agree", "note": "kanıt"}])
            return js(vote="reject", reason="hata var", findings=[finding(state.get("sev", "major"))])
        if role == "advocate":
            if "Debate round" in prompt:
                return js(vote="conditional", reason="ikna oldum", conditions=["sıfır kontrolü"])
            return js(vote="approve", reason="iş iyi")
        if role == "verifier":
            return js(checks=[{"id": "F1", "status": state.get("check", "refuted"), "note": "denedim"}],
                      vote="approve" if state.get("check", "refuted") == "refuted" else "reject", reason="kontrol")
        return js(vote="approve", reason="uzman")
    return run


def test_refuted_finding_does_not_block(workdir):
    state = {"check": "refuted"}
    agent, _, ctx = make(council_script(state), workdir)
    snap = Council(ctx, "work", "küçük iş", "a\n" * 100, depth=1).run()
    assert snap["findings"][0]["status"] == "refuted"
    assert snap["result"]["decision"] == "approve"
    critic = next(v for v in snap["votes"] if v["role"] == "critic")
    assert not critic["counted"] and "çürütüldü" in critic["note"]
    assert "ONAY" in report(snap) and "çürütüldü" in report(snap)
    assert not any(k.startswith("council:") for k in ctx.router.role_overrides)  # roles were cleaned up


def test_confirmed_critical_finding_vetoes_and_debate_changes_votes(workdir):
    state = {"check": "confirmed", "sev": "critical"}
    agent, _, ctx = make(council_script(state), workdir)
    snap = Council(ctx, "work", "ödeme kodu", "a\n" * 200, depth=2).run()
    assert snap["result"]["decision"] == "revise" and snap["result"]["veto"] == ["F1"]
    adv = next(v for v in snap["votes"] if v["role"] == "advocate")
    assert adv["first"] == "approve" and adv["last"] == "conditional"  # convinced in the debate
    assert any(r == "advocate" and k == "debate" for r, k in state["calls"])
    assert "F1" in fix_list(snap) and "sıfır kontrolü" in fix_list(snap)
    again = retally(snap, {"vote": "approve"})  # the user's vote does not lift the veto
    assert again["result"]["decision"] == "revise"


def test_debate_stops_early_when_nobody_moves(workdir):
    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role == "critic":
            return js(vote="conditional", reason="küçük", findings=[finding("minor")], conditions=["yorum ekle"])
        if role == "verifier":
            return js(checks=[{"id": "F1", "status": "unclear"}], vote="approve")
        return js(vote="approve")

    agent, prov, ctx = make(script, workdir)
    snap = Council(ctx, "work", "iş", "a\n" * 200, depth=3).run()
    assert snap["round"] == 2  # one debate round, nobody changed, done
    assert snap["result"]["decision"] == "approve_conditions"


def test_chat_council_tool_and_slash_command(workdir):
    from alice.commands import council_prompt

    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role:
            return js(vote="approve", reason=f"{role} onaylıyor")
        n = steps(messages)
        return [tool_call("council", {"subject": "README yapısı iyi mi?", "depth": "1"}), "Kurul onayladı."][min(n, 1)]

    (workdir / "README.md").write_text("# x\n", encoding="utf-8")
    agent, _, ctx = make(script, workdir)
    assert agent.run_turn(council_prompt("README")) == "Kurul onayladı."
    tool_text = next(m["content"] for m in agent.messages if m["role"] == "tool")
    assert "Kurula sor: ONAY" in tool_text and "eleştirmen" in tool_text
    assert "council aracı" in council_prompt("x")


def project_script(state):
    """A project whose plan is rejected once, whose work goes back for one fix, and which passes the final review."""

    def run(model, messages, tools, **kw):
        role = role_of(messages)
        prompt = str(messages[1]["content"]) if len(messages) > 1 else ""
        if role:
            if "Proje planı" in prompt:
                second = "düzeltilmiş plan" in prompt
                ok = second or role != "critic"
                if role == "verifier":
                    return js(checks=[] if second else [{"id": "F1", "status": "confirmed"}],
                              vote="approve" if second else "reject")
                return js(vote="approve" if ok else "reject",
                          findings=[] if ok else [finding("critical", "test ajanı yok")])
            if "Projenin tamamı" in prompt:
                return js(vote="approve", reason="bütün iyi")
            fixed = "kontrol eklendi" in prompt or "The previous council's findings" in prompt
            if role == "critic":
                return js(vote="approve") if fixed else js(vote="reject", findings=[finding("critical")])
            if role == "verifier":
                return js(checks=[] if fixed else [{"id": "F1", "status": "confirmed"}],
                          vote="approve" if fixed else "reject")
            return js(vote="approve")
        if "and the lead of the project" in messages[0]["content"]:
            event, k = since_user(messages)
            last = messages[-1]
            if "henüz ajan yok" in event or ("Kurul planı onaylamadı" in str(last.get("content")) and k < 3):
                if last["role"] == "tool" and "ajanlar başladı" in str(last.get("content")):
                    return "tamam"
                second = "Kurul planı onaylamadı" in str(last.get("content"))
                return tool_call("plan", {"summary": "düzeltilmiş plan" if second else "tek ajan",
                                          "agents": [{"name": "backend", "task": "a.py yaz"}]}, f"p{k}")
            if "Final kurulu projeyi onayladı" in event and k == 0:
                return tool_call("finish", {"summary": "bitti"})
            return "tamam"
        # backend agent
        n = steps(messages)
        if any("Kurul kararı" in str(m.get("content")) for m in messages if m["role"] == "user"):
            return "rapor: kontrol eklendi"
        return [tool_call("write_file", {"path": "a.py", "content": "def f(x):\n    return 1 / x\n"}), "rapor v1"][min(n, 1)]

    return run


def test_project_council_flow(workdir):
    state: dict = {}
    agent, _, ctx = make(project_script(state), workdir)
    team = Team(ctx, "bölme fonksiyonu", council=True)
    team.start(plan=True)
    assert team.wait_until(lambda: team.finished, 40), team.status_text()
    kinds = [c["kind"] for c in team.councils]
    assert kinds.count("plan") == 2 and kinds[-1] == "final"
    m = team.members["backend"]
    assert [c["kind"] for c in m.councils] == ["work", "recheck"]
    assert m.councils[0]["result"]["decision"] == "revise" and m.councils[1]["result"]["decision"] == "approve"
    assert m.council_cycles == 1 and m.status == "done" and team.summary == "bitti"
    assert any(x.sender == "kurul" for x in team.log)
    snap = team.snapshot()
    assert snap["council"] and snap["members"][0]["council"]["decision"] == "approve"


def test_user_vote_overrides_contested(workdir):
    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role == "critic":
            return js(vote="reject", findings=[finding()])
        if role == "verifier":
            return js(checks=[{"id": "F1", "status": "unclear"}], vote="approve")
        if role:
            return js(vote="approve")
        if "and the lead of the project" in messages[0]["content"]:
            return "tamam"
        return [tool_call("write_file", {"path": "a.py", "content": "x\n"}), "rapor"][min(steps(messages), 1)]

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "iş", council=True)
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.councils and m.councils[-1].get("state") == "done" and not m.council_running, 20)
    assert m.status == "review" and m.councils[-1]["result"]["decision"] == "contested"
    assert team.user_vote("backend", "approve", "bence tamam") == "approve"
    assert m.status == "done"


def test_prose_drops_json_decisions():
    from alice.council import prose

    assert prose("Gerekçem şu.\n\n```json\n{\"vote\": \"reject\"}\n```") == "Gerekçem şu."
    assert prose('Kısa not {"vote": "approve", "findings": []}') == "Kısa not"
    assert prose('```json\n{"checks": [') == ""
    assert prose("a" * 2000).endswith("…") and len(prose("a" * 2000)) == 1501


def plan_council_script(state):
    """Plan council: the first plan has a split problem (one that breaks the plan) plus a minor suggestion;
    the corrected plan keeps only the suggestion. If state['stubborn'] is set, the council still objects."""
    def run(model, messages, tools, **kw):
        role = role_of(messages)
        prompt = str(messages[1]["content"]) if len(messages) > 1 else ""
        if role:
            state.setdefault("prompts", []).append((role, messages[0]["content"], prompt))
            fixed = "düzeltilmiş plan" in prompt and not state.get("stubborn")
            if role == "critic":
                findings = [] if fixed else [{"severity": "critical", "area": "bölme", "agent": "web",
                                              "title": "iki ajan aynı dosyaya yazıyor", "evidence": "web ve css",
                                              "fix": "css'i web'e bağla"}]
                findings.append({"severity": "minor", "area": "doğrulama", "agent": "web",
                                 "title": "sayfalar tarayıcıda açılıp bakılsın", "fix": "kontrol adımı ekle"})
                return js(vote="approve" if fixed else "reject", reason="plan",
                          scores={"kapsam": 4, "bölme": 2 if not fixed else 4, "sıra": 4, "doğrulama": 3, "risk": 4},
                          findings=findings)
            if role == "verifier":
                return js(checks=[{"id": "F1", "status": "confirmed"},
                                  {"id": "F2", "status": "unclear", "severity": "minor"}] if not fixed else
                          [{"id": "F1", "status": "unclear", "severity": "minor"}],
                          vote="approve" if fixed else "reject")
            return js(vote="approve", scores={"kapsam": 5})
        if "and the lead of the project" in messages[0]["content"]:
            last = messages[-1]
            if last["role"] == "tool" and "ajanlar başladı" in str(last.get("content")):
                return "tamam"
            if last["role"] == "tool" and "Kurul planı onaylamadı" in str(last.get("content")):
                return tool_call("plan", {"summary": "düzeltilmiş plan", "agents": [{"name": "web", "task": "site"}]}, "p2")
            if any("henüz ajan yok" in str(m.get("content")) for m in messages if m["role"] == "user") and \
                    not any(m.get("tool_calls") for m in messages if m["role"] == "assistant"):
                return tool_call("plan", {"summary": "ilk plan", "agents": [{"name": "web", "task": "site"}]}, "p1")
            return "tamam"
        return "rapor: yaptım"
    return run


def test_plan_council_blocks_only_plan_breaking_issues_and_routes_notes(workdir):
    (workdir / "index.html").write_text("x", encoding="utf-8")
    state: dict = {}
    agent, _, ctx = make(plan_council_script(state), workdir)
    team = Team(ctx, "kurumsal web sitesi", council=True)
    team.start(plan=True)
    assert team.wait_until(lambda: "web" in team.members, 30), team.status_text()
    plans = [c for c in team.councils if c["kind"] == "plan"]
    assert [c["result"]["decision"] for c in plans] == ["revise", "approve_conditions"]
    assert all(f["severity"] != "critical" for c in plans for f in c["findings"])  # no critical/veto in the plan
    assert plans[0]["result"]["veto"] == [] and plans[0]["result"]["blocking"] == ["F1"]
    assert plans[0]["rubric"]["bölme"] == 2 and plans[0]["rubric"]["kapsam"] >= 4
    assert "Kurul notu: sayfalar tarayıcıda açılıp bakılsın" in team.members["web"].constraints
    sys_prompt, material = next((s, p) for r, s, p in state["prompts"] if r == "critic")
    assert "Plan review" in sys_prompt and "Off limits: implementation details" in sys_prompt
    assert "Proje klasörü" in material and "index.html" in material and "DESIGN.md" in material
    # The work review can also arrive as soon as the agent starts: look at the last *plan* review.
    second = [p for r, s, p in state["prompts"] if r == "critic" and "Plan review" in s][-1]
    assert "The previous council's findings" in second  # the corrected plan is reviewed against the earlier one
    assert team.plan_rejects == 0


def test_plan_starts_with_notes_after_one_revision(workdir):
    state: dict = {"stubborn": True}
    agent, _, ctx = make(plan_council_script(state), workdir)
    team = Team(ctx, "kurumsal web sitesi", council=True)
    team.start(plan=True)
    assert team.wait_until(lambda: "web" in team.members, 30), team.status_text()
    assert [c["result"]["decision"] for c in team.councils if c["kind"] == "plan"] == ["revise", "revise"]
    assert not team.questions  # the user was not asked; it started with notes
    assert any("notlarıyla başlatıldı" in x["text"] for x in team.timeline)
    assert "Kurul notu" in team.members["web"].constraints


def test_plan_decision_edge_cases():
    from alice.council import Finding, Vote, norm_area, norm_scores, plan_decision, plan_override, tally

    # no vote could be counted (rejection without a reason): the plan is not rejected
    res = tally([Vote("critic", "m", 1.0, last="reject")], [])
    assert plan_decision(res, [])["decision"] == "approve"
    # an upper-case / English area name also counts as a plan-breaking issue
    f = Finding("F1", "critic", "major", "eksik sayfa", status="confirmed", area=norm_area("Kapsam"))
    assert plan_decision({"score": 0.9, "weight": 3}, [f])["decision"] == "revise"
    assert norm_area("SIRA") == "sıra" and norm_area("scope") == "kapsam" and norm_area("x") == ""
    # the verification area alone does not stop the plan
    g = Finding("F2", "critic", "major", "test adımı yok", status="confirmed", area="doğrulama")
    assert plan_decision({"score": 0.9, "weight": 3}, [g])["decision"] == "approve_conditions"
    # malformed scores do not crash the council
    assert norm_scores({"kapsam": float("nan"), "Bölme": "4", "sıra": True, "risk": float("inf"), "doğrulama": 9}) == \
        {"bölme": 4, "doğrulama": 5}
    # the user's rejection also applies
    assert plan_override({"decision": "approve"}, [], {"vote": "reject"})["decision"] == "revise"


def test_plan_verifier_cannot_escalate_or_veto(workdir):
    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role == "critic":
            return js(vote="approve", findings=[{"severity": "minor", "area": "doğrulama", "title": "öneri"}])
        if role == "verifier":
            return js(checks=[{"id": "F1", "status": "confirmed", "severity": "major"}], vote="approve",
                      findings=[{"severity": "critical", "title": "planda X yazmıyor"}])
        return js(vote="approve")

    agent, _, ctx = make(script, workdir)
    snap = Council(ctx, "plan", "Proje planı", "plan metni", depth=1).run()
    sev = {f["id"]: f["severity"] for f in snap["findings"]}
    assert sev["F1"] == "minor"  # cannot be escalated
    assert all(f["severity"] != "critical" for f in snap["findings"]) and snap["result"]["veto"] == []


def test_council_reports_live_progress(workdir):
    seen = []

    def script(model, messages, tools, **kw):
        return js(vote="approve")

    agent, _, ctx = make(script, workdir)
    c = Council(ctx, "plan", "Proje planı", "plan", depth=1)
    c.on_change = lambda: seen.append((c.phase, {r: a.get("state") for r, a in c.activity.items()}))
    snap = c.run()
    phases = [p for p, _ in seen]
    assert "Plan inceleniyor" in phases and "Kontrolcü bulguları doğruluyor" in phases
    assert any("working" in states.values() for _, states in seen)
    assert all(a["state"] == "done" and a["vote"] == "approve" for a in snap["activity"].values())


def test_council_setup_failure_marks_entry_failed(workdir):
    import pytest

    agent, _, ctx = make(lambda *a, **k: "x", workdir)
    team = Team(ctx, "hedef")

    def boom(*a, **k):
        raise RuntimeError("ekran görüntüsü alınamadı")

    team._screens = boom
    store: list = []
    with pytest.raises(RuntimeError):
        team._council("adhoc", "site.html", "bağlam", store)
    assert store[0]["state"] == "failed" and "ekran" in store[0]["result"]["error"]
