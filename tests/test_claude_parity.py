"""ALICE counterparts of Claude Code features: Google search, permission rules, hooks, calendar tracking, skills, custom
sub-agents, choice questions, JSON output, notebook editing, review commands and alice eval."""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from alice import evals
from alice.commands import builtin_prompt, catalog, expand_custom
from alice.hooks import Hook, Hooks
from alice.permissions import Permissions, rule_matches
from alice.tools import ToolContext, builtin_tools
from alice.tools import web
from alice.tools.base import ToolError, ToolResult
from alice.tools.files import NotebookEdit
from alice.tools.meta import AskUser, SkillTool
from tests.conftest import make_config, tool_call
from tests.test_agent import make_agent

TOOLS = {t.name: t for t in builtin_tools()}
PY = f'"{sys.executable}"'


# ----------------------------------------------------------------------------------------- Google search
class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._data, self.text = code, data, json.dumps(data or {})

    def json(self):
        return self._data


def test_web_search_uses_google_grounding_then_falls_back(monkeypatch, workdir):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setitem(web._grounding, "down_until", 0.0)
    calls = []

    def post(url, headers, json, timeout):
        calls.append((url, json))
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": "DCS 2.9.10 dün çıktı."}]},
                                          "groundingMetadata": {"groundingChunks": [
                                              {"web": {"uri": "https://x/redirect", "title": "eagle.ru"}}]}}]})

    monkeypatch.setattr(web.httpx, "post", post)
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = TOOLS["web_search"].run(ctx, "DCS son sürüm").text
    assert out.startswith("Google search summary") and "2.9.10" in out and "eagle.ru" in out
    assert calls[0][1]["tools"] == [{"google_search": {}}] and "gemini-flash-latest" in calls[0][0]

    monkeypatch.setattr(web.httpx, "post", lambda *a, **k: Resp(429, {"error": "quota"}))
    monkeypatch.setattr(web, "ddg_search", lambda q, n: [{"title": "T", "url": "https://a.com", "snippet": "s"}])
    out = TOOLS["web_search"].run(ctx, "başka bir soru").text
    assert out.startswith("1. T")  # quota: classic search
    assert web._grounding["down_until"] > time.monotonic() + 3000  # not retried for an hour
    # The domain filter cannot be applied to the Google summary, so classic search is used.
    monkeypatch.setitem(web._grounding, "down_until", 0.0)
    out = TOOLS["web_search"].run(ctx, "x", allowed_domains=["a.com"]).text
    assert out.startswith("1. T")


# ----------------------------------------------------------------------------------------- permission rules
def ctx_with(workdir, rules=None):
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["permissions"] = rules or {}
    return ToolContext(cwd=workdir, config=cfg)


def test_rule_matching_patterns():
    assert rule_matches("run_command(git status*)", "run_command", {"command": "git status -s"})
    assert not rule_matches("run_command(git status*)", "run_command", {"command": "git push"})
    assert rule_matches("mcp__github__*", "mcp__github__delete_repo", {})
    assert rule_matches("write_file(*.env)", "write_file", {"path": "C:\\proje\\.env"})


def test_path_rules_use_normalized_path(workdir):
    """Path rules check the normalized path, not the raw text: "./gizli/a", the full path and ".." cannot get past the
    deny rule; an allow does not leak outside the project via ".."; a relative allow also applies to full-path calls."""
    wf = TOOLS["write_file"]
    ctx = ctx_with(workdir, {"deny": ["write_file(gizli/*)"], "allow": ["write_file(src/*)"]})
    yolo, ask = Permissions("yolo"), Permissions("ask")
    for p in ("gizli/a.txt", "./gizli/a.txt", str(workdir / "gizli" / "a.txt"), "src/../gizli/a.txt"):
        assert yolo.decide(wf, {"path": p, "content": "x"}, ctx) == "deny", p
    assert ask.decide(wf, {"path": "src/a.py", "content": "x"}, ctx) == "allow"
    assert ask.decide(wf, {"path": str(workdir / "src" / "a.py"), "content": "x"}, ctx) == "allow"
    assert ask.decide(wf, {"path": "src/../../disari.txt", "content": "x"}, ctx) == "ask"


def test_permission_rules_allow_ask_deny(workdir):
    rc, wf = TOOLS["run_command"], TOOLS["write_file"]
    ctx = ctx_with(workdir, {"allow": ["run_command(git status*)", "run_command"], "ask": ["run_command(git push*)"],
                             "deny": ["run_command(*rm -rf*)", "write_file(*.env)"]})
    assert Permissions("ask").decide(rc, {"command": "git status"}, ctx) == "allow"
    assert Permissions("auto").decide(rc, {"command": "git push origin x"}, ctx) == "ask"
    assert Permissions("yolo").decide(rc, {"command": "rm -rf build"}, ctx) == "deny"  # deny applies in yolo mode too
    assert Permissions("yolo").decide(wf, {"path": ".env", "content": "x"}, ctx) == "deny"
    # A "run_command" rule without a pattern does not allow dangerous commands.
    assert Permissions("ask").decide(rc, {"command": "format c:"}, ctx) == "ask"
    assert Permissions("plan").decide(rc, {"command": "git status"}, ctx) == "deny"  # does not bypass plan mode


def test_project_permission_file(workdir):
    (workdir / ".alice").mkdir()
    (workdir / ".alice" / "permissions.toml").write_text('[permissions]\ndeny = ["run_command(npm publish*)"]\n', encoding="utf-8")
    assert Permissions("yolo").decide(TOOLS["run_command"], {"command": "npm publish"}, ctx_with(workdir)) == "deny"


def test_agent_explains_rule_denial(workdir):
    agent, _, _ = make_agent([tool_call("run_command", {"command": "rm -rf build"}), "Tamam, silmedim."], workdir)
    agent.config.data["permissions"] = {"deny": ["run_command(*rm -rf*)"]}
    agent.run_turn("build klasörünü sil")
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "permission rule" in tool_msg["content"] and "rm -rf" in tool_msg["content"]


# ----------------------------------------------------------------------------------------- hooks
def test_hooks_block_feedback_context_and_stop(workdir):
    script = [tool_call("write_file", {"path": "a.txt", "content": "x"}),
              tool_call("run_command", {"command": "echo hi"}, cid="c2"), "İlk bitti.", "Şimdi gerçekten bitti."]
    agent, provs, _ = make_agent(script, workdir)
    flag = workdir / "stop_once"
    agent.hooks = Hooks([
        Hook("UserPromptSubmit", f"{PY} -c \"print('kullanıcı Hayat')\""),
        Hook("PreToolUse", f"{PY} -c \"import sys; sys.stderr.write('komut yasak'); sys.exit(2)\"", "run_command"),
        Hook("PostToolUse", f"{PY} -c \"import sys,json; d=json.load(sys.stdin); sys.stderr.write('lint: '+d['tool']); "
                            "sys.exit(2)\"", "write_file|edit_file"),
        Hook("Stop", f"{PY} -c \"import os,sys; p=r'{flag}'; "
                     "(open(p,'w').close(), sys.stderr.write('testler kırık'), sys.exit(2)) "
                     "if not os.path.exists(p) else sys.exit(0)\""),
    ], workdir)
    assert agent.run_turn("dosya yaz") == "Şimdi gerçekten bitti."
    first = provs["a"].calls[0]["messages"][-1]["content"]
    assert "[hook context]" in first and "kullanıcı Hayat" in first
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert "[hook geri bildirimi]" in tools[0]["content"] and "lint: write_file" in tools[0]["content"]
    assert "hook blocked this call" in tools[1]["content"] and "komut yasak" in tools[1]["content"]
    assert any(m.get("_nudge") and "testler kırık" in m["content"] for m in agent.messages)


def test_hooks_from_config_and_prompt_block(workdir):
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["hooks"] = {"UserPromptSubmit": [{"command": f"{PY} -c \"import sys; sys.stderr.write('mesai dışı'); "
                                                          "sys.exit(2)\""}], "Bogus": [{"command": "x"}]}
    h = Hooks.from_config(cfg, workdir)
    assert [x.event for x in h.hooks] == ["UserPromptSubmit"]
    agent, provs, ui = make_agent(["olmamalı"], workdir)
    agent.hooks = h
    remembered = []
    agent._remember_turn = lambda u, r: remembered.append((u, r))
    assert agent.run_turn("merhaba") == "" and provs["a"].calls == []
    assert remembered == []  # a blocked message is not written to memory as an empty turn


# ----------------------------------------------------------------------------------------- calendar
def test_calendar_events_become_followups(monkeypatch):
    from alice.connect import runner
    from alice.core.service import calendar_followups
    from alice.memory import LongMemory

    monkeypatch.setattr(runner, "ready", lambda m: (True, "bağlı"))
    seen = {}

    def call(m, act, args, client=None, cache=True, **k):
        seen.update(args)
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        return {"items": [{"summary": "Matematik sınavı", "start": {"dateTime": f"{tomorrow}T10:00:00+03:00"}},
                          {"summary": "Haftalık toplantı", "start": {"dateTime": f"{tomorrow}T11:00:00+03:00"}},
                          {"summary": "Maç", "start": {"date": tomorrow}}]}

    monkeypatch.setattr(runner, "call", call)
    mem = LongMemory(":memory:", {})
    assert calendar_followups(mem, None) == 2
    texts = [f.text for f in mem.followups()]
    assert any("Matematik sınavı" in t for t in texts) and not any("toplantı" in t for t in texts)
    assert "timeMin" in seen and "timeMax" in seen
    assert calendar_followups(mem, None) == 2 and len(mem.followups()) == 2  # not added again


def test_memory_job_steps_survive_calendar_error(monkeypatch):
    # when the calendar failed, reminders had not gone out for two weeks, and the error was logged every hour
    import time as _t

    from alice.core import service
    from alice.memory import LongMemory

    mem = LongMemory(":memory:", {})
    mem.add_followup("ilacını al", _t.time() - 5, "remind")
    sent, events = [], []
    core = object.__new__(service.Core)
    core.cfg = None
    core.notifier = type("N", (), {"send": lambda self, n: sent.append(n.body)})()
    core.store = type("S", (), {"add_event": lambda self, *a, **k: events.append(a[1])})()
    monkeypatch.setattr(core, "_memory", lambda: mem, raising=False)
    monkeypatch.setattr(service, "calendar_followups", lambda m, c: (_ for _ in ()).throw(RuntimeError("404")))
    monkeypatch.setattr(core, "_latency_report", lambda: None, raising=False)
    monkeypatch.setattr(core, "_important_mail", lambda: None, raising=False)
    monkeypatch.setattr(core, "_health_report", lambda: None, raising=False)
    monkeypatch.setattr(core, "_weekly", lambda m: None, raising=False)
    monkeypatch.setattr(core, "_mail_dates", lambda m: None, raising=False)
    monkeypatch.setattr(core, "_night", lambda m: None, raising=False)
    core.anticipator = type("A", (), {"check": lambda self: False})()
    core.memory_job()
    core.memory_job()
    assert sent == ["ilacını al"]  # sent once; a calendar error does not stop it
    assert events == ["calendar follow-ups failed"]  # the same error is logged once a day


def test_calendar_manifest_loads():
    from alice.connect.manifest import discover

    m = discover()[0]["google_calendar"]
    assert m.auth.kind == "oauth2" and m.auth.extra_authorize["access_type"] == "offline"
    assert set(m.actions) >= {"upcoming", "quick_add", "delete"}


# ----------------------------------------------------------------------------------------- skills and sub-agents
def test_skills_listed_and_loaded(workdir, isolated_home):
    from alice.prompts import build_system_prompt
    from alice.skills import load_skills

    d = isolated_home / "skills" / "unity-build"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: unity-build\ndescription: Unity'den Windows build almak\n---\n"
                                "1. scripts/build.ps1'i çalıştır.\n", encoding="utf-8")
    (d / "scripts" / "build.ps1").write_text("Unity.exe -batchmode", encoding="utf-8")
    assert "unity-build" in load_skills(workdir)
    prompt = build_system_prompt(workdir)
    assert "# Skills" in prompt and "Unity'den Windows build almak" in prompt and "build.ps1" not in prompt
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = SkillTool().run(ctx, "Unity-Build").text
    assert "scripts/build.ps1'i çalıştır" in out and "- scripts/build.ps1" in out
    with pytest.raises(ToolError):
        SkillTool().run(ctx, "yok")


def test_custom_subagent_prompt_tools_and_model(workdir):
    (workdir / ".alice" / "agents").mkdir(parents=True)
    (workdir / ".alice" / "agents" / "inceleyici.md").write_text(
        "---\nname: inceleyici\ndescription: kodu inceler\ntools: read_file, grep\nmodel: a:m\nmax_steps: 5\n---\n"
        "Sen titiz bir kod inceleyicisin.\n", encoding="utf-8")
    script = [tool_call("task", {"prompt": "main.py'yi incele", "agent": "inceleyici"}), "Alt ajan: sorun yok.",
              "İnceleyici sorun bulmadı."]
    agent, provs, _ = make_agent(script, workdir)
    assert agent.run_turn("incelet") == "İnceleyici sorun bulmadı."
    child_call = provs["a"].calls[1]
    assert child_call["messages"][0]["content"].startswith("Sen titiz bir kod inceleyicisin.")
    assert {t["function"]["name"] for t in child_call["tools"]} == {"read_file", "grep"}
    agent2, _, _ = make_agent([tool_call("task", {"prompt": "x", "agent": "olmayan"}), "tamam"], workdir)
    agent2.run_turn("x")
    assert "adında alt ajan yok" in next(m for m in agent2.messages if m["role"] == "tool")["content"]


# ----------------------------------------------------------------------------------------- choice question
def test_ask_user_uses_ui_or_falls_back(workdir):
    class ChoiceUI:
        def ask_choice(self, question, options, multi=False):
            return options[1]

    class Passthrough:
        def __getattr__(self, name):
            return lambda *a, **k: None

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    tool = AskUser()
    ctx.state["agent"] = SimpleNamespace(ui=ChoiceUI())
    assert tool.run(ctx, "Hangisi?", ["A — önerilen", "B"]).text == "The user's answer: B"
    for ui in (Passthrough(), SimpleNamespace()):
        ctx.state["agent"] = SimpleNamespace(ui=ui)
        assert "can't be shown" in tool.run(ctx, "Hangisi?", ["A", "B"]).text
    with pytest.raises(ToolError):
        tool.run(ctx, "?", ["tek"])


def test_server_choice_roundtrip():
    import threading

    from alice.app.server import AppUI

    published = []
    ui = AppUI.__new__(AppUI)
    ui.bus = SimpleNamespace(publish=published.append)
    ui.pending, ui._events, ui._answers, ui.agent = {}, {}, {}, None
    out = []
    t = threading.Thread(target=lambda: out.append(ui.ask_choice("Hangisi?", ["A", "B"])))
    t.start()
    for _ in range(100):
        if published:
            break
        time.sleep(0.01)
    assert published[0]["type"] == "ask_choice"
    assert ui.answer_choice(published[0]["id"], "B")
    t.join(2)
    assert out == ["B"] and published[-1] == {"type": "ask_choice_done", "id": published[0]["id"], "answer": "B"}


# ----------------------------------------------------------------------------------------- JSON output
def test_json_ui_streams_events():
    from alice.cli import JsonUI

    buf = io.StringIO()
    ui = JsonUI(buf, stream=True)
    ui.tool_start("c1", "read_file", "a.py")
    ui.tool_end("c1", "read_file", ToolResult("içerik"))
    ui.stream_end("bitti", "gemini:x")
    assert ui.ask_permission("run_command", "rm", "danger") == "n"
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert [x["type"] for x in lines] == ["tool_use", "tool_result", "assistant", "permission_denied"]
    assert ui.models == ["gemini:x"] and ui.tools == ["read_file"]
    quiet = JsonUI(io.StringIO())
    quiet.stream_delta("x")
    assert quiet.out.getvalue() == ""


# ----------------------------------------------------------------------------------------- notebook
def test_notebook_edit_replace_insert_delete(workdir):
    nb = {"cells": [{"cell_type": "code", "source": ["print(1)\n"], "outputs": [{"text": "1"}], "execution_count": 3,
                     "metadata": {}}, {"cell_type": "markdown", "source": ["# Başlık"], "metadata": {}}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    p = workdir / "a.ipynb"
    p.write_text(json.dumps(nb), encoding="utf-8")
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    tool = NotebookEdit()
    from alice.tools.files import ReadFile

    ReadFile().run(ctx, "a.ipynb")  # editing without reading is rejected (as in Claude Code)
    tool.run(ctx, "a.ipynb", 0, source="print(2)\nprint(3)")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["cells"][0]["source"] == ["print(2)\n", "print(3)"] and data["cells"][0]["outputs"] == []
    tool.run(ctx, "a.ipynb", 2, mode="insert", source="x = 1", cell_type="code")
    tool.run(ctx, "a.ipynb", 1, mode="delete")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert [c["cell_type"] for c in data["cells"]] == ["code", "code"] and data["nbformat"] == 4
    with pytest.raises(ToolError):
        tool.run(ctx, "a.ipynb", 9)


# ----------------------------------------------------------------------------------------- commands
def test_review_builtins_and_user_override(workdir):
    assert "code review" in builtin_prompt("/review PR 12") and "PR 12" in builtin_prompt("/review PR 12")
    assert "security review" in builtin_prompt("/security-review")
    assert builtin_prompt("/yok") is None
    deep = builtin_prompt("/review derin PR 12")
    assert "focus=deep" in deep and "PR 12" in deep and "derin" not in deep.split("Scope:")[1]
    assert "focus=performance" in builtin_prompt("/review performans")
    d = workdir / ".alice" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("---\ndescription: benim incelemem\n---\nBenim tarzımla incele: $ARGUMENTS\n", encoding="utf-8")
    assert expand_custom("/review hız", workdir) == "Benim tarzımla incele: hız"
    names = [c["name"] for c in catalog(workdir)]
    assert names.count("review") == 1 and "security-review" in names


def test_slash_command_name_ends_at_newline(workdir):
    # Typing "/kurul\nkonu" with Shift+Enter gave "bilinmeyen komut" (unknown command): the name split only at spaces.
    assert "giriş akışı" in builtin_prompt("/kurul\ngiriş akışı")
    assert "PR 12" in builtin_prompt("/review\tPR 12")
    d = workdir / ".alice" / "commands"
    d.mkdir(parents=True)
    (d / "ozet.md").write_text("Özetle: $ARGUMENTS\n", encoding="utf-8")
    assert expand_custom("/ozet\nşu dosya", workdir) == "Özetle: şu dosya"
    assert expand_custom("/ozet", workdir) == "Özetle: "
    assert builtin_prompt("/") is None


# ----------------------------------------------------------------------------------------- eval
def test_eval_cases_roundtrip_and_log_import(isolated_home):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "voice.log").write_text(
        "2026-10-22 10:00:00 duyulan [groq]: Unity projemi aç\n"
        "2026-10-22 10:00:05 duyulan (gürültü sayıldı) [groq]: hmm\n"
        "2026-10-22 10:01:00 live [m] duyulan: F-16 ile F-15 farkı ne | cevap: bilmiyorum araçlar: -\n", encoding="utf-8")
    assert evals.sayings_from_log(5) == ["Unity projemi aç", "F-16 ile F-15 farkı ne"]
    assert evals.add_cases(evals.sayings_from_log(5), "voice.log'dan") == 2
    assert evals.add_cases(["Unity projemi aç"]) == 0  # the same one is not added twice
    cases = evals.load_cases()
    assert [c.say for c in cases] == ["Unity projemi aç", "F-16 ile F-15 farkı ne"] and cases[0].role == "brain"


def test_eval_check_and_run(workdir):
    case = evals.Case("dosyayı sil", expect="silmedim", forbid="sildim", expect_tools=["file_ops"],
                      forbid_tools=["run_command"])
    assert evals.check(case, "Silmedim.", ["file_ops"]) == []
    assert len(evals.check(case, "sildim", ["run_command"])) == 4

    agent, _, _ = make_agent([tool_call("write_file", {"path": "x.txt", "content": "x"}), "Yazamadım, izin yok."],
                             workdir, mode="ask")
    o = evals.run_case(evals.Case("x.txt yaz", expect="izin", expect_tools=["write_file"]), lambda: agent)
    assert o.passed and o.tools == ["write_file"] and not (workdir / "x.txt").exists()  # chose it but did not run
    first = evals.save_results([o])
    assert first["before"] is None
    o2 = evals.Outcome(o.case, False, "", [], ["boş cevap"], 0.1)
    cmp = evals.save_results([o2])
    assert cmp["before"] == 1 and cmp["broken"] == [o.case.label]


# ----------------------------------------------------------------------------------------- persistent permission
def test_suggest_rule_is_narrow(workdir):
    from alice.permissions import suggest_rule

    assert suggest_rule(TOOLS["run_command"], {"command": "git status -s"}) == "run_command(git status*)"
    assert suggest_rule(TOOLS["run_command"], {"command": "pytest -q -x"}) == "run_command(pytest*)"
    assert suggest_rule(TOOLS["write_file"], {"path": "src/app/x.py"}, workdir) == "write_file(src/app/*)"
    assert suggest_rule(TOOLS["write_file"], {"path": "x.py"}, workdir) == "write_file(*)"


def test_project_allow_persists_across_sessions(workdir):
    class Ask:
        def __init__(self, answer):
            self.answer, self.asked, self.notes = answer, 0, []

        def __getattr__(self, name):
            return lambda *a, **k: None

        def ask_permission(self, name, summary, risk):
            self.asked += 1
            return self.answer

        def info(self, msg):
            self.notes.append(msg)

    script = [tool_call("run_command", {"command": "echo selam"}), "tamam"]
    agent, _, _ = make_agent(script, workdir, mode="ask")
    agent.ui = ui = Ask("p")
    agent.run_turn("çalıştır")
    assert ui.asked == 1 and any("permanent permission added: run_command(echo selam*)" in n for n in ui.notes)
    assert "run_command(echo selam*)" in (workdir / ".alice" / "permissions.toml").read_text(encoding="utf-8")
    # New session (new agent): the same kind of command is no longer asked about.
    agent2, _, _ = make_agent([tool_call("run_command", {"command": "echo selam dünya"}), "tamam"], workdir, mode="ask")
    agent2.ui = ui2 = Ask("n")
    agent2.run_turn("tekrar")
    assert ui2.asked == 0
    # No persistent permission is written for dangerous commands; it passes this time only.
    agent3, _, _ = make_agent([tool_call("run_command", {"command": "rm -rf build"}), "tamam"], workdir, mode="ask")
    agent3.ui = ui3 = Ask("p")
    agent3.run_turn("sil")
    assert "rm -rf" not in (workdir / ".alice" / "permissions.toml").read_text(encoding="utf-8")
    assert any("permanent permission is not given" in n for n in ui3.notes)
