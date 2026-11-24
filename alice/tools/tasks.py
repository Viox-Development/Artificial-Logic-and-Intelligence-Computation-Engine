"""Multi-step task tools: run_plan (plan, execute, verify) and task_control (approval, stop, status)."""

from __future__ import annotations

from typing import Any

from alice.i18n import _
from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class RunPlan(Tool):
    name = "run_plan"
    description = (
        "Plans a request made of several separate steps and carries it out from start to finish: splits it into 2-8 "
        "steps, does each step with tools and checks the result; a step that fails is tried once another way. Asks "
        "the user for approval before irreversible steps (sending a message/email, deleting, buying, installing); the "
        "answer is given with task_control. Can take minutes.\n"
        "- Use for: 'download the latest invoice from Gmail, save it to the desktop and send it to Ahmet on "
        "WhatsApp'.\n"
        "- Don't use for: jobs one tool can finish (call that tool directly); screen work in a single app "
        "(computer_task). Only one task runs at a time."
    )
    parameters = obj({"goal": {"type": "string", "description": "The user's request in full, with names of people, "
                                                                "files and apps; written so that someone who hasn't "
                                                                "seen the conversation would understand it."}}, ["goal"])
    risk = COMPUTER  # each tool call inside it goes through its own permission check

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("goal", ""))[:150]

    def run(self, ctx: ToolContext, goal: str) -> ToolResult:
        from alice.multistep import TaskRunner, task_log

        if not (goal or "").strip():
            raise ToolError("goal is empty")
        if ctx.state.get("active_task") is not None:
            return ToolResult("a task is already running; wait for it to finish or stop it with task_control action=stop.",
                              is_error=True)
        a = ctx.config.agent
        hooks = ctx.state.get("task_hooks") or {}
        log = hooks.get("log") or (lambda s: None)

        def both(s: str) -> None:
            task_log(s)
            log(f"task: {s}")

        runner = TaskRunner(ctx, goal.strip(), max_steps=int(a.get("plan_max_steps", 8)),
                            step_tool_calls=int(a.get("plan_step_tool_calls", 14)),
                            answer_timeout=float(a.get("plan_answer_timeout", 120)), log=both)
        ctx.state["active_task"] = runner
        try:
            text = runner.run()
        finally:
            ctx.state.pop("active_task", None)
        both(f"finished: {text.splitlines()[0] if text else ''}")
        return ToolResult(text, is_error=text.startswith("error:"))


class TaskControl(Tool):
    name = "task_control"
    description = ("Manages a running run_plan task: passes on the user's approval or refusal, stops the task or "
                   "returns its status.\n"
                   "- approve/reject only when the user clearly said yes/no ('evet'/'hayır') in this conversation. "
                   "NEVER give the approval yourself or guess it.\n"
                   "- stop when told to stop or cancel ('dur', 'vazgeç'); status for 'how is it going' ('ne "
                   "durumda').\n"
                   "- Returns an error if no run_plan task is running.")
    parameters = obj({"action": {"type": "string", "enum": ["approve", "reject", "stop", "status"],
                                 "description": "approve: the task asked for approval and the user said yes | reject: "
                                                "said no | stop: 'stop', 'cancel' | status: 'how is it going'."}}, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str) -> ToolResult:
        runner = ctx.state.get("active_task")
        if runner is None:
            return ToolResult("no task is running right now")
        if action == "status":
            return ToolResult(runner.status_text())
        if action == "stop":
            runner.stop()
            return ToolResult("stopping the task; it stops as soon as the current step finishes")
        if action in ("approve", "reject"):
            if not runner.answer(action == "approve"):
                return ToolResult("the task isn't waiting for approval right now", is_error=True)
            return ToolResult("approved, continuing" if action == "approve" else "rejected, that step won't be done")
        raise ToolError(f"bilinmeyen action: {action}")


AGENT_SPEC = obj({
    "name": {"type": "string", "description": "A short name, e.g. 'backend', 'tests', 'ui'."},
    "task": {"type": "string", "description": "The job, in enough detail for someone who hasn't seen the conversation: which "
             "files, what changes, how to verify it, what to report."},
    "constraints": {"type": "string", "description": "Limits: places it must not touch, rules it must follow."},
    "scope": {"type": "array", "items": {"type": "string"},
              "description": "Files/folders it may write (relative to the working folder). Empty: anywhere. Keep them from overlapping."},
    "model": {"type": "string", "description": "brain (strong), fast (quick/cheap) or provider:model."},
    "thinking": {"type": "string", "enum": ["off", "low", "medium", "high", "default"],
                 "description": "Thinking level: high for hard design/debugging, low/off for routine work."},
    "isolation": {"type": "string", "enum": ["shared", "worktree", "readonly"],
                  "description": "shared: the same folder | worktree: its own git branch (merged on approval) | readonly: "
                  "review/research only."},
    "after": {"type": "array", "items": {"type": "string"}, "description": "Start after these agents are approved."},
    "max_steps": {"type": "integer", "description": "Maximum tool steps. Default 60."},
}, ["name", "task"])


class TeamTool(Tool):
    name = "team"
    description = (
        "Starts a large job as an autonomous PROJECT: the project's own manager (you, in the background) splits the "
        "work among agents running in parallel, answers their questions, reviews each finished job (approves it or "
        "sends it back for fixes), and at the end verifies and writes a summary. The user follows progress on the "
        "Projects page in the app.\n"
        "- Use for: large jobs with parts that can be separated (several modules in a large codebase, "
        "frontend+backend+tests, a broad review/research). Don't use it for small jobs: do them yourself or use "
        "task.\n"
        "- start: put the user's request, what you know and the definition of done in goal. If you don't give agents, "
        "the manager reads the code and sets up the agents itself (usually best). Then tell the user the project has "
        "started; if they want the result in the chat, wait with action=wait.\n"
        "- Agents use coding tools (files, shell, web, git); screen and browser tools are closed to them."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "wait", "send", "status", "stop", "resume"],
                   "description": "start: start the project | wait: wait until the project finishes or asks the user "
                                  "a question | send: message the manager (to=alice) or an agent | status: status | "
                                  "stop: stop an agent (name) or the project | resume: resume a stopped project."},
        "goal": {"type": "string", "description": "start: the overall goal and context of the work; everyone sees it."},
        "agents": {"type": "array", "description": "start (optional): agents (at most 8). Empty: the manager plans "
                                                   "them.",
                   "items": AGENT_SPEC},
        "review": {"type": "boolean", "description": "start: whether the manager reviews finished jobs. Default true."},
        "council": {"type": "boolean", "description": "start: review council (the plan, each job and the final result "
                                                      "are debated and voted on). Default true; false for a small or "
                                                      "urgent job."},
        "pr": {"type": "boolean", "description": "start: the result becomes a PR. The project works on its own branch "
                                                 "without touching the user's copy; when done the branch is pushed, a "
                                                 "PR is opened and watched (red CI and comments come to this chat). "
                                                 "Use in a code repository when the user asked for a PR or the job is "
                                                 "a large change that should be reviewed."},
        "project": {"type": "string", "description": "Project id. Empty: the one started last in this chat."},
        "to": {"type": "string", "description": "send: 'alice' (the manager), an agent's name or 'all'."},
        "text": {"type": "string", "description": "send: mesaj."},
        "name": {"type": "string", "description": "stop: a single agent. Empty: the project."},
        "timeout": {"type": "integer", "description": "wait: at most this many seconds. Default 600, at most 1800."},
    }, ["action"])
    risk = READ  # each tool call the agents make goes through its own permission check

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a == "start":
            names = ", ".join(str(x.get("name")) for x in args.get("agents") or [] if isinstance(x, dict))
            return "proje: " + (names or "the lead will plan") + " · " + str(args.get("goal", ""))[:80]
        if a == "send":
            return f"{args.get('to')}: {str(args.get('text', ''))[:120]}"
        return str(a)

    def run(self, ctx: ToolContext, action: str, goal: str = "", agents: list[dict[str, Any]] | None = None,
            review: bool = True, project: str = "", to: str = "", text: str = "", name: str = "",
            timeout: int = 600, council: bool = True, pr: bool = False) -> ToolResult:
        from alice.team import LEAD, Team

        teams = ctx.state.setdefault("teams", {})
        if action == "start":
            if not (goal or "").strip():
                raise ToolError("start needs goal")
            specs = [a for a in agents or [] if isinstance(a, dict)]
            if len(specs) > 8:
                raise ToolError("at most 8 agents")
            a = ctx.config.agent
            team = Team(ctx, goal.strip(), max_parallel=int(a.get("team_max_parallel", 5)),
                        publish=ctx.state.get("publish"), review=review is not False, council=council is not False,
                        pr=bool(pr))
            for spec in specs:
                team.add(spec)
            ctx.state["team"] = team
            teams[team.id] = team
            team.start(plan=not specs)
            return ToolResult(f"project started (id {team.id}). "
                              + ("The lead will look at the code and set up the agents. " if not specs else
                                 "Agents:\n" + team.status_text() + "\n")
                              + "The user can follow it on the Projects page. If they want the result here, action=wait.")
        team = teams.get(project) if project else ctx.state.get("team")
        if team is None:
            return ToolResult("no such project; action=start first", is_error=True)
        if action == "status":
            return ToolResult(f"proje {team.id}: {team.status()}\n{team.status_text()}"
                              + (f"\n\nSummary:\n{team.summary}" if team.summary else ""))
        if action == "send":
            target = (to or LEAD).strip().lower()
            return ToolResult(team.send("kullanıcı", target, text))  # the ALICE in the chat, writing on the user's behalf
        if action == "stop":
            target = (name or "").strip().lower() or None
            if target and target not in team.members:
                raise ToolError(f"there is no '{target}' in the project")
            team.stop(target)
            return ToolResult(f"durduruldu: {target or 'proje'}")
        if action == "resume":
            team.resume()
            return ToolResult("project resumed")
        if action == "wait":
            limit = max(5, min(int(timeout or 600), 1800))
            team.wait_until(lambda: team.status() in ("done", "stopped", "needs_you"), limit, ctx.cancelled)
            st = team.status()
            if st == "done":
                return ToolResult("Project finished. The lead's summary:\n" + (team.summary or "(no summary)"))
            if st == "needs_you":
                qs = [q["text"] for q in team.questions if not q.get("answer")]
                return ToolResult("The project is asking the user:\n" + "\n".join(f"- {q}" for q in qs) +
                                  "\nAsk the user; pass their answer on with action=send to=alice.")
            return ToolResult(f"Proje durumu: {st}\n{team.status_text()}\n"
                              + ("Time ran out; the project keeps running in the background." if st != "stopped" else ""))
        raise ToolError(f"bilinmeyen action: {action}")


class CouncilTool(Tool):
    name = "council"
    description = (
        "Asks a review council about a subject: a critic (harsh but with evidence), an advocate, a checker (verifies "
        "claims itself) and, when needed, security/performance/user experts review it independently, debate and "
        "decide by weighted vote. For an idea, a design decision, a file/code, a change or a plan.\n"
        "- Use when the user says '/kurul', 'ask the council' ('kurula sor'), 'let them critique/debate it', or when "
        "the pros and cons of an important decision need a solid look. Can take minutes.\n"
        "- Summarize the result for the user: the decision, the verified findings, who argued for what and why."
    )
    parameters = obj({
        "subject": {"type": "string", "description": "What to review and what is expected; clear enough for someone "
                                                     "who hasn't seen the conversation. For code/files, include their "
                                                     "paths."},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Files to focus on (optional)."},
        "critical": {"type": "boolean", "description": "Critical subject (security, data, core architecture): full "
                                                       "council, the strongest models, deep debate."},
        "depth": {"type": "string", "enum": ["auto", "1", "2", "3"], "description": "Debate rounds. auto: depends on "
                                                                                    "the job."},
    }, ["subject"])
    risk = READ

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("subject", ""))[:150]

    def run(self, ctx: ToolContext, subject: str, paths: list[str] | None = None, critical: bool = False,
            depth: str = "auto") -> ToolResult:
        if not (subject or "").strip():
            raise ToolError("subject is empty")
        context = ""
        for p in (paths or [])[:8]:
            f = ctx.resolve(p)
            try:
                context += f"\n## {p}\n```\n{f.read_text(encoding='utf-8', errors='replace')[:8000]}\n```\n"
            except OSError:
                context += f"\n## {p}: could not read it\n"
        from alice.council import Council, report

        ctx.emit(_("the council is meeting…"))
        rules = ""
        for f in (ctx.cwd / "REVIEW.md", ctx.cwd / ".alice" / "REVIEW.md"):
            if f.is_file():
                rules = f.read_text(encoding="utf-8", errors="replace")[:4000]
                break
        c = Council(ctx, "adhoc", subject, context, critical=bool(critical), depth=depth if depth != "auto" else "auto",
                    rules=rules)
        snap = c.run()
        ctx.emit(_("the council reached its decision"))
        return ToolResult(report(snap))


TOOLS = [RunPlan(), TaskControl(), TeamTool(), CouncilTool()]
