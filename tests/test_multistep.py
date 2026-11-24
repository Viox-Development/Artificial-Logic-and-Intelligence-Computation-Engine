from __future__ import annotations

import json
import threading
import time

from alice.multistep import TaskRunner, outcome, parse_plan
from alice.tools.tasks import TaskControl
from tests.conftest import tool_call
from tests.test_agent import make_agent


def plan(*steps, question=""):
    return json.dumps({"steps": list(steps), "question": question})


def test_parse_plan_marks_irreversible_steps_even_if_planner_forgot():
    p = parse_plan("g", "```json\n" + plan({"goal": "Gmail'i aç", "check": "gelen kutusu görünüyor"},
                                           {"goal": "Faturayı Ahmet'e gönder", "risky": False}) + "\n```")
    assert [s.risky for s in p.steps] == [False, True]
    assert p.steps[0].check == "gelen kutusu görünüyor"
    assert not parse_plan("g", plan({"goal": "gönderilen kutusunu aç"})).steps[0].risky


def test_outcome_reads_last_status_line():
    assert outcome("bir şeyler yaptım\nSONUÇ: TAMAM - a.txt yazıldı", "SONUÇ", "TAMAM") == (True, "a.txt yazıldı")
    assert outcome("**DOĞRULAMA:** HAYIR - dosya yok", "DOĞRULAMA", "EVET") == (False, "dosya yok")
    assert outcome("rapor yok", "SONUÇ", "TAMAM")[0] is None


def test_outcome_reads_english_and_older_turkish_markers():
    from alice.multistep import RESULT_KEY, RESULT_YES, VERIFY_KEY, VERIFY_YES

    assert outcome("did it\nRESULT: DONE - a.txt written", RESULT_KEY, RESULT_YES) == (True, "a.txt written")
    assert outcome("RESULT: FAILED - no access", RESULT_KEY, RESULT_YES) == (False, "no access")
    assert outcome("SONUÇ: TAMAM - yazıldı", RESULT_KEY, RESULT_YES) == (True, "yazıldı")
    assert outcome("**VERIFIED:** NO - file missing", VERIFY_KEY, VERIFY_YES) == (False, "file missing")
    assert outcome("DOĞRULAMA: EVET - var", VERIFY_KEY, VERIFY_YES) == (True, "var")


def test_run_plan_plans_executes_confirms_and_verifies(workdir):
    script = [
        tool_call("run_plan", {"goal": "a.txt'ye selam yaz, sonra b.txt'yi sil ve yerine yenisini yaz"}),
        plan({"goal": "a.txt dosyasına selam yaz"},
             {"goal": "b.txt'yi sil ve yerine yeni yaz", "check": "b.txt içinde yeni yazıyor", "risky": True,
              "risk": "eski b.txt gider"}),
        tool_call("write_file", {"path": "a.txt", "content": "selam"}, cid="s1"),
        "SONUÇ: TAMAM - a.txt yazıldı",
        tool_call("read_file", {"path": "b.txt"}, cid="r2"),  # existing file: never overwritten without reading it first
        tool_call("write_file", {"path": "b.txt", "content": "yeni"}, cid="s2"),
        "SONUÇ: TAMAM - b.txt yenilendi",
        tool_call("read_file", {"path": "b.txt"}, cid="v1"),
        "DOĞRULAMA: EVET - b.txt içinde yeni var",
        "İkisi de tamam.",
    ]
    agent, _, ui = make_agent(script, workdir)
    (workdir / "b.txt").write_text("eski", encoding="utf-8")
    assert agent.run_turn("şunları yap") == "İkisi de tamam."
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "selam" and (workdir / "b.txt").read_text(encoding="utf-8") == "yeni"
    assert ui.events.count(("ask", "run_plan")) == 1  # only for the irreversible step
    report = [m for m in agent.messages if m["role"] == "tool" and m["name"] == "run_plan"][0]["content"]
    assert report.startswith("done: 2/2")
    assert agent.ctx.state.get("active_task") is None


def test_rejected_risky_step_is_not_done(workdir):
    script = [
        tool_call("run_plan", {"goal": "x"}),
        plan({"goal": "a.txt yaz"}, {"goal": "rapor.txt'yi Ahmet'e gönder"}),
        tool_call("write_file", {"path": "a.txt", "content": "1"}, cid="s1"),
        "SONUÇ: TAMAM - yazıldı",
        "Göndermedim.",
    ]
    agent, _, ui = make_agent(script, workdir, approve="n")
    agent.run_turn("yap")
    report = [m for m in agent.messages if m["role"] == "tool" and m["name"] == "run_plan"][0]["content"]
    assert report.startswith("fail: 1/2")
    assert "kullanıcı onaylamadı" in report


def test_failed_verification_retries_step_once(workdir):
    script = [
        tool_call("run_plan", {"goal": "x"}),
        plan({"goal": "c.txt yaz", "check": "c.txt var"}),
        "SONUÇ: TAMAM - yazdım",  # actually did not write it
        tool_call("list_dir", {"path": "."}, cid="v1"),
        "DOĞRULAMA: HAYIR - c.txt yok",
        tool_call("write_file", {"path": "c.txt", "content": "ok"}, cid="s2"),
        "SONUÇ: TAMAM - şimdi yazdım",
        "DOĞRULAMA: EVET - var",
        "tamam",
    ]
    agent, provs, _ = make_agent(script, workdir)
    agent.run_turn("yap")
    assert (workdir / "c.txt").exists()
    retry_prompt = [c for c in provs["a"].calls if "The previous try failed" in json.dumps(c["messages"], ensure_ascii=False)]
    assert retry_prompt
    report = [m for m in agent.messages if m["role"] == "tool" and m["name"] == "run_plan"][0]["content"]
    assert report.startswith("done: 1/1")


def test_planner_question_stops_before_doing_anything(workdir):
    script = [tool_call("run_plan", {"goal": "mesajı ona gönder"}), plan(question="Kime göndereyim?"), "Kime?"]
    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("yap")
    report = [m for m in agent.messages if m["role"] == "tool" and m["name"] == "run_plan"][0]["content"]
    assert report.startswith("needs_info: Kime göndereyim?")


def test_voice_confirmation_goes_through_task_control(workdir):
    agent, _, _ = make_agent([], workdir)
    said: list[str] = []
    agent.ctx.state["task_hooks"] = {"say": said.append}
    runner = TaskRunner(agent.ctx, "g", answer_timeout=5)
    agent.ctx.state["active_task"] = runner
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("ok", runner.confirm("Gönderilsin mi?")))
    t.start()
    for _ in range(50):
        if runner.waiting:
            break
        time.sleep(0.02)
    assert "Gönderilsin mi?" in said[-1] and "task_control" in said[-1]
    assert "onay bekliyor" in TaskControl().run(agent.ctx, action="status").text
    assert TaskControl().run(agent.ctx, action="approve").text.startswith("approved")
    t.join(2)
    assert box["ok"] is True
    assert TaskControl().run(agent.ctx, action="approve").is_error  # no longer waiting


def test_stop_cancels_pending_confirmation(workdir):
    agent, _, _ = make_agent([], workdir)
    agent.ctx.state["task_hooks"] = {"say": lambda s: None}
    runner = TaskRunner(agent.ctx, "g", answer_timeout=5)
    agent.ctx.state["active_task"] = runner
    threading.Timer(0.1, lambda: TaskControl().run(agent.ctx, action="stop")).start()
    assert runner.confirm("?") is False


def test_task_tools_run_on_live_tool_thread(workdir):
    script = [
        plan({"goal": "d.txt yaz"}),
        tool_call("write_file", {"path": "d.txt", "content": "x"}, cid="s1"),
        "SONUÇ: TAMAM - yazıldı",
    ]
    agent, _, _ = make_agent(script, workdir)
    threads: list[str] = []

    def run_tool(fn):
        box = {}

        def work():
            threads.append(threading.current_thread().name)
            box["r"] = fn()

        th = threading.Thread(target=work, name="tool-thread")
        th.start()
        th.join()
        return box["r"]

    agent.ctx.state["task_hooks"] = {"say": lambda s: None, "run_tool": run_tool}
    assert TaskRunner(agent.ctx, "d.txt yaz").run().startswith("done: 1/1")
    assert threads == ["tool-thread"]


def test_live_runs_task_tools_on_its_tool_thread():
    from alice.tools.base import ToolResult
    from alice.voice.live import LiveSession

    s = LiveSession.__new__(LiveSession)
    s._tools_q = None
    res = s._on_tool_thread(lambda: ToolResult(threading.current_thread().name))
    assert res.text == "alice-live-tools"
    assert "run_plan" in LiveSession.BACKGROUND
