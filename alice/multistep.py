"""Multi-step task execution: derive a plan from one request, do the steps in order, verify each step.

  1. plan     -> the brain model splits the request into 2-8 steps (JSON). Each step gets a goal, a way to verify it,
                 and an irreversible flag (sending, deleting, paying...). An unclear request returns a single question.
  2. execute  -> for each step a clean-context child agent does only that step; it sees the results of earlier steps.
                 Before a risky step the user's approval is requested (by voice in voice conversation).
  3. verify   -> the step's result is checked by a separate, read-only child agent (screen/page/file). If it failed,
                 the step is retried once another way; if it still fails, the task stops and the failure is reported.

In voice conversation (Live) the task runs in the background; progress and approval questions go to the chat, and
the user's answer arrives via the task_control tool. Thread-bound tools (like the browser) run on Live's tool thread
(ctx.state["task_hooks"]["run_tool"]).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from alice.tools.base import DANGER, Tool, ToolContext, ToolResult

PLANNER = """You are a task planner. Split the user's request into 2-{max_steps} concrete steps to be done one after
another on the computer. Each step must be doable on its own and its result checkable by looking. The user's computer
runs Windows; their browser is open and can be connected to. Tools available: {tools}.

Return only JSON, nothing else:
{{"steps": [{{"goal": "<the step, in Turkish (the user reads it), clear and complete: which app/site, what to type>",
             "check": "<what should be visible on screen/page/in a file when the step is done; empty if no check is
needed>",
             "risky": <true|false>, "risk": "<if risky, why: what goes to whom, what gets deleted>"}}],
 "question": "<ONE short question for the user if the request is unclear, otherwise empty>"}}

Rules:
- risky=true: anything that can't be undone, such as sending or sharing a message/email/comment, deleting or
  overwriting a file/record, buying, paying, installing/uninstalling something, changing a system setting, closing
  unsaved work. Only opening, searching, reading, looking and copying are not risky.
- Don't add work the user didn't ask for. If the request is one step, write one step.
- If it isn't clear who/what is meant and a wrong guess would lead to something irreversible, write a question.
- Don't write steps that enter a password or fill in payment details; leave those to the user at that step."""

STEP = """[Multi-step task] Main goal: {goal}
Plan:
{plan}
Results of the previous steps:
{done}
{retry}Now do ONLY step {n}: {step}
Really do it with the tools; don't move on to the next steps and don't ask the user questions.
When done, write one last line: "RESULT: DONE - <what happened, information the next steps need (name, path, link)>"
or, if you couldn't do it, "RESULT: FAILED - <why>"."""

VERIFY = """[Verification] Check whether a computer step was really done. DON'T CHANGE ANYTHING: use only tools that
look (browser snapshot/text/tabs, screenshot, ui_elements, windows list, read_file, list_dir...). Don't trust the
executor's report blindly; look yourself where you can.
Step: {step}
Expected: {check}
Executor's report: {report}
Write one last line: "VERIFIED: YES - <what you saw>" or "VERIFIED: NO - <what is missing>"."""

# Even if the planner forgets to flag a risky step, approval is asked for any step containing these words.
RISKY_RX = re.compile(
    r"\bgönder(\b|in\b|ip\b|eyim)|\byolla(\b|yıp)|\bpaylaş(\b|ıp)|\byayınla(\b|yıp)|\bsil(\b|in\b|ip\b)|"
    r"\bkaldır(\b|ın\b|ıp\b)|üzerine yaz|satın al|sipariş ver|\böde(\b|me yap|yip)|\bkur(\b|ulum)|"
    r"\bkaldır|biçimlendir|\bformatla|kaydetmeden kapat|"
    r"\b(send|delete|remove|uninstall|install|purchase|buy|pay|publish)\b", re.I)

# Tools the child agents inside a task cannot see: a nested task and task control would make no sense.
HIDDEN = {"run_plan", "task_control", "task"}


@dataclass
class Step:
    goal: str
    check: str = ""
    risky: bool = False
    risk: str = ""
    status: str = "pending"  # pending | running | done | failed | skipped
    result: str = ""
    tries: int = 0


@dataclass
class Plan:
    goal: str
    steps: list[Step] = field(default_factory=list)
    question: str = ""


def _first_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?", "", text or "")
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        raise ValueError(f"planda JSON yok: {(text or '')[:160]}")
    val = json.loads(m.group(0))
    if not isinstance(val, dict):
        raise ValueError("plan JSON obje değil")
    return val


def parse_plan(goal: str, text: str, max_steps: int = 8) -> Plan:
    data = _first_json(text)
    steps = []
    raw = data.get("steps") or []
    for s in raw if isinstance(raw, list) else [raw]:  # a single step may come as text: not one step per letter
        if isinstance(s, str):
            s = {"goal": s}
        if not isinstance(s, dict):
            continue
        g = str(s.get("goal") or "").strip()
        if not g:
            continue
        risky = bool(s.get("risky")) or bool(RISKY_RX.search(g))
        steps.append(Step(goal=g, check=str(s.get("check") or "").strip(), risky=risky,
                          risk=str(s.get("risk") or "").strip()))
    question = str(data.get("question") or "").strip()
    if not steps and not question:
        raise ValueError("planda adım yok")
    return Plan(goal=goal, steps=steps[:max_steps], question=question)


# The report markers. The Turkish ones are what the prompts asked for before; a model may still write them.
RESULT_KEY, RESULT_YES = r"RESULT|SONUÇ", ("DONE", "TAMAM")
VERIFY_KEY, VERIFY_YES = r"VERIFIED|DOĞRULAMA", ("YES", "EVET")


def outcome(text: str, key: str, yes: str | tuple[str, ...]) -> tuple[bool | None, str]:
    """Read the last "<key>: <yes|...> - explanation" line of the agent's reply. Without one, return (None, text).
    key is a regex (alternatives allowed); yes is the word, or the words, that mean success."""
    yes_words = {yes} if isinstance(yes, str) else set(yes)
    for line in reversed((text or "").strip().splitlines()):
        m = re.match(rf"\s*\**(?:{key})\**\s*:\s*\**\s*(\w+)\s*\**\s*[-–—:]?\s*(.*)", line, re.I)
        if m:
            return m.group(1).upper().replace("İ", "I") in yes_words, m.group(2).strip() or line.strip()
    return None, (text or "").strip()[-400:]


class _TaskUI:
    """Child agents' UI: writes tool events to the task log, sends permission questions to the task's approval path."""

    def __init__(self, runner: "TaskRunner"):
        self.runner = runner

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.runner.log(f"  araç {name}({summary[:100]})")

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        if result.is_error:
            self.runner.log(f"  araç hatası {name}: {result.text[:160]}")

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        if self.runner.step_approved and risk != DANGER:
            return "y"
        return "y" if self.runner.confirm(f"{name} çalıştırılsın mı: {summary[:160]}") else "n"


class TaskRunner:
    def __init__(self, ctx: ToolContext, goal: str, max_steps: int = 8, step_tool_calls: int = 14,
                 answer_timeout: float = 120.0, log: Callable[[str], None] | None = None):
        self.ctx = ctx
        self.goal = goal
        self.max_steps = max_steps
        self.step_tool_calls = step_tool_calls
        self.answer_timeout = answer_timeout
        self.plan: Plan | None = None
        self.stopped = threading.Event()
        self.step_approved = False
        self.waiting: str = ""  # the approval question we are waiting on from the user
        self._answer: bool | None = None
        self._answered = threading.Event()
        self._child: Any = None
        self._log = log or (lambda s: None)
        self.lines: list[str] = []

    # ------------------------------------------------------------- hooks
    @property
    def hooks(self) -> dict[str, Any]:
        return self.ctx.state.get("task_hooks") or {}

    def log(self, msg: str) -> None:
        self.lines.append(msg)
        self._log(msg)
        try:
            self.ctx.emit(msg.strip())
        except Exception:  # noqa: BLE001
            pass

    def say(self, text: str) -> None:
        """Announce to the user: in voice chat it goes to Live (Alice speaks it), otherwise to the UI as info."""
        self.log(f"duyuru: {text}")
        fn = self.hooks.get("say")
        if fn is not None:
            try:
                fn(text)
                return
            except Exception:  # noqa: BLE001 - conversation already closed
                pass
        agent = self.ctx.state.get("agent")
        info = getattr(getattr(agent, "ui", None), "info", None)
        if info is not None:
            info(text)

    def confirm(self, question: str) -> bool:
        """Ask the user before a risky step. Voice chat answers come via task_control; no answer means no."""
        if self.stopped.is_set():
            return False
        self.log(f"onay soruluyor: {question}")
        say = self.hooks.get("say")
        if say is None:
            agent = self.ctx.state.get("agent")
            ask = getattr(getattr(agent, "ui", None), "ask_permission", None)
            ok = ask is not None and ask("run_plan", question, DANGER) in ("y", "a", "p")
            self.log("onay: " + ("evet" if ok else "hayır"))
            return ok
        self._answer, self.waiting = None, question
        self._answered.clear()
        try:
            say(f"[task waiting for approval] Ask the user briefly, in Turkish: {question} If they say yes, call "
                "task_control action=approve; if no, action=reject. Don't approve it yourself.")
        except Exception:  # noqa: BLE001
            self.waiting = ""
            return False
        deadline = time.monotonic() + self.answer_timeout
        while not self._answered.wait(0.2):
            if self.stopped.is_set() or time.monotonic() > deadline:
                break
        self.waiting = ""
        ok = bool(self._answer)
        self.log("onay: " + ("evet" if ok else "hayır" if self._answered.is_set() else "cevap gelmedi"))
        return ok

    def answer(self, approve: bool) -> bool:
        if not self.waiting:
            return False
        self._answer = approve
        self._answered.set()
        return True

    def stop(self) -> None:
        self.stopped.set()
        self._answered.set()
        if self._child is not None:
            try:
                self._child.cancel.set()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- sub-agent
    def _agent(self, verify: bool = False):
        from alice.agent import Agent
        from alice.permissions import Permissions

        parent = self.ctx.state.get("agent")
        if parent is None or self.ctx.router is None:
            raise RuntimeError("ajan yok")
        tools = [t for n, t in parent.tools.items() if n not in HIDDEN]
        perms = Permissions("plan") if verify else parent.permissions
        child = Agent(parent.config, parent.router, tools, self.ctx, perms, _TaskUI(self), parent.decider,
                      system_prompt=parent.messages[0]["content"], is_child=True)
        child.stream = False
        child.role = "brain"
        run_tool = self.hooks.get("run_tool")
        if run_tool is not None:  # browser/UIA objects are bound to Live's tool thread, so tools run there
            def on_tool_thread(tool: Tool, args: dict[str, Any], _child=child) -> ToolResult:
                return run_tool(lambda: Agent._run_tool(_child, tool, args))

            child._run_tool = on_tool_thread
        self._child = child
        return child

    def _ask(self, prompt: str, verify: bool = False, steps: int | None = None) -> str:
        child = self._agent(verify)
        try:
            return child.run_turn(prompt, max_steps=steps or self.step_tool_calls) or ""
        finally:
            self._child = None

    # ----------------------------------------------------------------- plan
    def make_plan(self) -> Plan:
        parent = self.ctx.state.get("agent")
        names = sorted(n for n in getattr(parent, "tools", {}) if n not in HIDDEN and not n.startswith("mcp__"))
        prompt = PLANNER.format(max_steps=self.max_steps, tools=", ".join(names) or "(bilinmiyor)")
        last: Exception | None = None
        for _ in range(2):
            res = self.ctx.router.chat("brain", [{"role": "system", "content": prompt},
                                                 {"role": "user", "content": self.goal}], max_tokens=1500)
            try:
                return parse_plan(self.goal, res.text, self.max_steps)
            except (ValueError, json.JSONDecodeError) as e:
                last = e
        raise RuntimeError(f"plan çıkarılamadı: {last}")

    def _plan_text(self, current: int) -> str:
        assert self.plan is not None
        mark = {"done": "[x]", "failed": "[!]", "skipped": "[-]", "running": "[~]"}
        return "\n".join(f"{mark.get(s.status, '[ ]')} {i}. {s.goal}" + ("  <- CURRENT" if i == current else "")
                         for i, s in enumerate(self.plan.steps, 1))

    # ------------------------------------------------------------- execution
    def _do_step(self, n: int, step: Step, retry: str = "") -> tuple[bool, str]:
        assert self.plan is not None
        done = "\n".join(f"{i}. {s.result}" for i, s in enumerate(self.plan.steps, 1)
                         if s.status == "done" and s.result) or "(none)"
        report = self._ask(STEP.format(goal=self.goal, plan=self._plan_text(n), done=done, n=n, step=step.goal,
                                       retry=f"The previous try failed: {retry}. Try another way.\n" if retry else ""))
        ok, why = outcome(report, RESULT_KEY, RESULT_YES)
        if ok is False or self.stopped.is_set():
            return False, why
        if not step.check:
            return True, why
        verdict = self._ask(VERIFY.format(step=step.goal, check=step.check, report=why[:600]), verify=True, steps=6)
        vok, vwhy = outcome(verdict, VERIFY_KEY, VERIFY_YES)
        if vok is False:
            return False, f"doğrulanamadı: {vwhy}"
        return True, why

    def run(self) -> str:
        self.log(f"görev: {self.goal}")
        try:
            self.plan = self.make_plan()
        except Exception as e:  # noqa: BLE001
            self.log(f"plan hatası: {e}")
            return f"error: {e}"
        plan = self.plan
        if plan.question:
            return (f"needs_info: {plan.question}\nAsk the user this question; with their answer, call run_plan again "
                    "with the clarified goal.")
        self.log("plan:\n" + self._plan_text(0))
        if len(plan.steps) > 1:
            self.say(f"[task plan: tell the user in one sentence, in Turkish; don't read the list] {len(plan.steps)} steps: "
                     + "; ".join(s.goal for s in plan.steps))
        for n, step in enumerate(plan.steps, 1):
            if self.stopped.is_set():
                break
            self.step_approved = False
            if step.risky:
                why = f" ({step.risk})" if step.risk else ""
                if not self.confirm(f"{n}. adım geri alınamaz: {step.goal}{why}. Yapayım mı?"):
                    step.status, step.result = "skipped", "kullanıcı onaylamadı"
                    self.log(f"{n}. adım atlandı: onay yok")
                    break
                self.step_approved = True
            step.status = "running"
            self.log(f"{n}. adım: {step.goal}")
            reason = ""
            ok = False
            while step.tries < 2 and not self.stopped.is_set():
                step.tries += 1
                try:
                    ok, reason = self._do_step(n, step, retry=reason if step.tries > 1 else "")
                except Exception as e:  # noqa: BLE001 - model/tool crashed: retry the step
                    ok, reason = False, f"{type(e).__name__}: {e}"
                if ok:
                    break
                self.log(f"{n}. adım olmadı ({step.tries}. deneme): {reason[:200]}")
                if step.risky:  # do not retry an irreversible step on our own, so it is not sent twice
                    break
            step.result = reason[:500]
            step.status = "done" if ok else "failed"
            self.log(f"{n}. adım {'tamam' if ok else 'olmadı'}: {step.result[:200]}")
            if not ok:
                break
        self.step_approved = False
        return self.report()

    def report(self) -> str:
        assert self.plan is not None
        steps = self.plan.steps
        ok = sum(s.status == "done" for s in steps)
        if self.stopped.is_set():
            status = "stopped"
        elif ok == len(steps):
            status = "done"
        else:
            status = "fail"
        lines = [f"{status}: {ok}/{len(steps)} steps done"]
        for i, s in enumerate(steps, 1):
            mark = {"done": "✓", "failed": "✗", "skipped": "-", "running": "~"}.get(s.status, " ")
            lines.append(f"{mark} {i}. {s.goal}" + (f" -> {s.result}" if s.result else ""))
        if status != "done":
            lines.append("Tell the user at which step it stopped and why; don't say steps that weren't done were done.")
        return "\n".join(lines)

    def status_text(self) -> str:
        head = f"görev: {self.goal}"
        if self.waiting:
            head += f"\nonay bekliyor: {self.waiting}"
        return head + "\n" + (self._plan_text(0) if self.plan is not None else "planlanıyor")


def task_log(text: str) -> None:
    from alice.config import alice_home

    try:
        with open(alice_home() / "tasks.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass
