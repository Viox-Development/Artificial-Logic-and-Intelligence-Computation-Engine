"""Projects: running a large job as an autonomous project, with parallel agents and its own lead.

Each project has a lead (named "alice") and worker agents (members). All of them are sub-agents, each running in its
own thread with its own context.

The lead works independently of the chat and wakes up when events arrive:
- Plan: if there are no agents yet, it reads the code, splits the work into parts, and sets up agents with `assign`.
- Monitor: it answers agents' questions with `message`; it asks the user about decisions that need them with `ask_user`.
- Review: each finished agent moves to the "review" state; the lead reads the report and the diff (`diff`), runs tests
  if needed, and approves with `review` (the worktree branch is merged) or sends the work back with feedback.
- Finish: once everyone is approved, it checks the whole and writes a summary for the user with `finish`.

If the review council (alice/council.py) is on: the plan is submitted to the council with the `plan` tool (if rejected,
the lead fixes it and resubmits; after the second rejection the user is asked); each finished piece of work is debated
and voted on by the council, and the decision is applied (approval, or fixes for verified findings; after fixes, a short
check round, at most 2 times); at the end of the project the final council meets (at most 2 times). Contested decisions
go to the lead, and the user can also vote or overturn a decision.

Members write to each other, the lead ("alice") or everyone with `team_send`, and wait with `team_wait`;
incoming messages are added to their conversation after each tool step. With `after`, a member starts once the work of
others is approved.

Each project is saved to ~/.alice/projects/<id>.json; it is loaded again even if the app is closed and reopened, and
when the user writes, it continues where it left off. The app's Projects page shows statuses, conversations, diffs
and questions.

Security: agents only use code tools (files, shell, web, git); screen/browser tools are off, since they would
share the user's single screen. An agent with a write scope cannot write outside it with file tools; a "readonly"
agent cannot write at all. Permission questions go to the user under the agent's name.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.i18n import _
from alice.tools.base import Tool, ToolContext, ToolError, ToolResult, make_diff

# The project store lives in team_store.py; it is re-exported here so it can also be imported from this module.
from alice.team_store import (  # noqa: F401
    _read_saved,
    _slim,
    _tpl_file,
    delete_template,
    guess_check,
    list_projects,
    list_templates,
    projects_dir,
    templates_dir,
)

LEAD = "alice"  # the project's lead
ORCHESTRATOR = LEAD  # old name
USER = "kullanıcı"  # the user, writing from the Projects page
SYSTEM = "sistem"
# Markers in the agents' conversations. The project page reads them; it also recognises the old Turkish ones.
START_TASK = "Start your task."
TEAM_MESSAGE = "[team message]"
EVENTS_HEAD = "[events]"
STATUS_HEAD = "[status]"
SLOP_HEAD = "Automatic pattern check"
MERGE_FAILED = "could not be merged"
COUNCIL = "kurul"
# Tools agents may use: code work. Screen, browser and desktop tools share the user's single screen.
MEMBER_TOOLS = {"read_file", "write_file", "edit_file", "multi_edit", "apply_patch", "file_ops", "list_dir", "glob",
                "grep", "run_command", "process_output", "process_stop", "web_fetch", "web_search", "http_request",
                "todo_write", "git"}
WRITE_TOOLS = {"write_file", "edit_file", "multi_edit", "apply_patch", "file_ops"}
ISOLATIONS = ("shared", "worktree", "readonly")
THINKING = ("off", "low", "medium", "high", "default")
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
ACTIVE = ("running", "waiting", "asking")
FINAL = ("done", "failed", "stopped")
SAVE_EVERY = 1.5  # seconds: during live updates, the disk is written at most this often
DESIGN_FILE = "DESIGN.md"
RESEARCH_DIR = "research"  # screenshots and analyses of reference sites (written by site_study)


def research_refs(root: Path, limit: int = 2) -> dict[str, str]:
    """Desktop screenshots of researched reference sites (name -> base64), for comparison."""
    import base64

    d = Path(root) / RESEARCH_DIR
    out: dict[str, str] = {}
    try:
        files = sorted(d.glob("*-masaustu.png"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return out
    for f in files[:limit]:
        try:
            out["referans-" + f.name[:-len("-masaustu.png")]] = base64.b64encode(f.read_bytes()).decode()
        except OSError:
            continue
    return out


def team_compact_cap(config) -> int:
    """Project agents' conversations are summarized once they pass this many tokens ([agent] team_compact_at,
    0 = unlimited). Every step resends the whole conversation; with no limit, a project on a 1M-context model
    burned ~100M tokens."""
    try:
        return max(0, int(config.agent.get("team_compact_at", 128000)))
    except (TypeError, ValueError):
        return 128000
MAX_MEMBERS = 8
ORIGINAL_LIMIT = 400_000  # original file contents kept for revert/diff (bytes); larger files are not kept


@dataclass
class Message:
    sender: str
    to: str
    text: str
    at: float = field(default_factory=time.time)

    def line(self) -> str:
        return f"[{self.sender} -> {self.to}] {self.text}"


@dataclass
class Member:
    name: str
    task: str
    constraints: str = ""
    scope: list[str] = field(default_factory=list)  # paths it may write to (empty: the whole working folder)
    model: str = "auto"  # auto (smart pick by task), a role name (brain, fast...) or provider:model
    thinking: str = "default"
    isolation: str = "shared"
    max_steps: int = 60
    after: list[str] = field(default_factory=list)  # starts once these are approved
    cwd: Path = Path(".")
    branch: str = ""
    base_sha: str = ""  # commit the worktree was opened at
    status: str = "queued"  # queued | running | waiting | asking | review | done | failed | stopped
    result: str = ""
    question: str = ""  # question asked to the lead/user, awaiting an answer
    steps: int = 0
    reviews: int = 0  # how many times it was sent back for fixes
    limit_hit: bool = False
    last: str = ""
    started: float = 0.0
    ended: float = 0.0
    files: list[str] = field(default_factory=list)  # paths changed with file tools (relative to the project folder)
    originals: dict[str, str | None] = field(default_factory=dict)  # absolute path -> original contents (None: did not exist)
    agent: Any = None
    thread: threading.Thread | None = None
    saved_messages: list[dict[str, Any]] = field(default_factory=list)  # loaded from disk (no system prompt)
    resume_wanted: bool = False  # got work after finishing, parallel limit full: resumes when a slot frees
    councils: list[dict[str, Any]] = field(default_factory=list)  # council results for this job (oldest first)
    council_cycles: int = 0  # number of rounds the council sent back for fixes
    council_running: bool = False
    last_at: float = 0.0  # last tool step (stall detection)
    stall_warned: bool = False
    merge_attempts: int = 0
    provider_failed: str = ""  # reason, if all models failed
    recovering: bool = False  # stalled and stopped; resumes with another model when it ends
    recoveries: int = 0  # how many times it was recovered automatically
    resume_note: str = ""  # note to give the agent on its next resume
    picked: str = ""  # with auto selection: "tasarım · glm-5.3"
    stop_requested: bool = False  # the agent was asked to stop before it was set up: it checks this on start and stops

    def snapshot(self) -> dict[str, Any]:
        now = self.ended or time.time()
        return {"name": self.name, "task": self.task, "model": self.model, "picked": self.picked, "thinking": self.thinking,
                "isolation": self.isolation, "status": self.status, "steps": self.steps, "last": self.last,
                "branch": self.branch, "scope": self.scope, "result": self.result[:1500], "files": self.files[-50:],
                "constraints": self.constraints, "after": self.after, "question": self.question,
                "reviews": self.reviews, "limit_hit": self.limit_hit, "council_running": self.council_running,
                "council": _council_brief(self.councils[-1]) if self.councils else None,
                "councils_n": len(self.councils), "secs": int(now - self.started) if self.started else 0}

    def messages(self) -> list[dict[str, Any]]:
        """Conversation without the system prompt: from the live agent or from disk."""
        if self.agent is not None:
            return self.agent.messages[1:]
        return self.saved_messages


def _add_usage(row: dict[str, float], usage: dict[str, Any]) -> None:
    """Token and (if the provider reports it) cost counter."""
    row["in"] = row.get("in", 0) + int(usage.get("prompt_tokens") or 0)
    row["out"] = row.get("out", 0) + int(usage.get("completion_tokens") or 0)
    cost = usage.get("cost")
    if isinstance(cost, (int, float)):
        row["cost"] = round(row.get("cost", 0.0) + float(cost), 6)


class _Stopped(Exception):
    """The project was stopped while the agent was starting."""


def human_failure(reasons: list[str]) -> str:
    """Turns the router's raw error lines ("ref: reason") into a short, readable explanation."""
    from alice.providers.router import COOLING_RX

    out: list[str] = []
    for line in reasons:
        ref, _sep, why = line.partition(": ")
        name = ref.split(":", 1)[-1].split("/")[-1] or ref
        low = why.lower()
        if m := COOLING_RX.search(why):
            secs = int(m.group(1))
            when = f"~{secs // 3600} sa" if secs >= 3600 else f"~{max(1, secs // 60)} dk"
            text = _("{name} is waiting (opens in {when})", name=name, when=when)
        elif "model sınırı" in low or "model limit" in low or "too_large" in low or "context" in low:
            text = _("the conversation is too long for {name}", name=name)
        elif any(w in low for w in ("quota", "kota", "429", "rate")):
            text = _("{name}'s quota is used up", name=name)
        elif any(w in low for w in ("auth", "401", "403", "anahtar", " yok")):
            text = _("{name} has no API key, or it is invalid", name=name)
        elif any(w in low for w in ("timeout", "timed out", "network", "unreachable", "connect", "bağlan", "veri göndermiyor",
                                   "hasn't sent data")):
            text = _("{name} connection problem", name=name)
        else:
            text = f"{name}: {why[:80]}"
        if text not in out:
            out.append(text)
    return ("; ".join(out[:4]) + (f" (+{len(out) - 4})" if len(out) > 4 else "")) or _("no model could answer")


def _council_brief(c: dict[str, Any]) -> dict[str, Any]:
    res = c.get("result") or {}
    act = c.get("activity") or {}
    return {"id": c.get("id"), "kind": c.get("kind"), "state": c.get("state"), "decision": res.get("decision"),
            "score": res.get("score"), "round": c.get("round"), "rounds": c.get("rounds"), "phase": c.get("phase", ""),
            "done": sum(1 for a in act.values() if a.get("state") in ("done", "failed")),
            "total": len(c.get("roles") or act),
            "started": c.get("started")}


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def in_scope(path: Path, scope: list[Path]) -> bool:
    return not scope or any(path == s or s in path.parents for s in scope)


def patch_paths(patch: str) -> list[str]:
    from alice.tools.patch import ADD, DELETE, MOVE, UPDATE

    out = []
    for line in (patch or "").splitlines():
        for prefix in (ADD, DELETE, UPDATE, MOVE):
            if line.startswith(prefix):
                out.append(line[len(prefix):].strip())
    return out


def write_targets(tool: str, args: dict[str, Any]) -> list[str]:
    """Paths a file tool will write to (for scope checks and diffs)."""
    if tool == "apply_patch":
        return patch_paths(str(args.get("patch") or ""))
    out = [str(args[k]) for k in ("path", "destination") if args.get(k)]
    out += [str(p) for p in args.get("paths") or []]
    return out


def _read_original(p: Path) -> str | None:
    if not p.exists() or p.is_dir():
        return None
    try:
        if p.stat().st_size > ORIGINAL_LIMIT:
            return "\0büyük"
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "\0okunamadı"


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _startupinfo() -> subprocess.STARTUPINFO | None:
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    # git output is UTF-8: without an explicit encoding, Windows decodes it with the local code page (cp1254), and a
    # a diff or file with "Ğ", "Ş" or emoji raises UnicodeDecodeError and drops the step. No console window either.
    from alice.gitsafe import harden  # so the hook/fsmonitor the sandbox left in .git does not run with ALICE's git

    return subprocess.run(["git", "-C", str(cwd), *harden(args, cwd)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                          startupinfo=_startupinfo())


log = logging.getLogger(__name__)


def _replace(src: Path, dst: Path, tries: int = 6) -> None:
    """Atomic replace. On Windows, the antivirus/indexer briefly locks a newly written file and replace raises
    PermissionError; retry after a short wait (otherwise the save was silently lost)."""
    for i in range(tries):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05 * (i + 1))


class Team:
    """A project: goal, lead and agents."""

    def __init__(self, parent_ctx: ToolContext, goal: str, max_parallel: int = 5,
                 publish: Callable[[dict[str, Any]], None] | None = None, origin: str = "alice",
                 project_id: str = "", title: str = "", review: bool = True, lead_model: str = "auto",
                 lead_thinking: str = "default", cwd: Path | None = None, council: bool = True, check: str = "",
                 pr: bool = False):
        self.parent_ctx = parent_ctx
        self.goal = goal
        self.id = project_id or time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.title = title or (goal.strip().splitlines()[0][:70] if goal.strip() else "Proje")
        self.origin = origin  # alice: created by ALICE in the chat | user: created by the user from the Projects page
        self.cwd = Path(cwd) if cwd else parent_ctx.cwd
        self.created = time.time()
        self.max_parallel = max(1, max_parallel)
        self.review = review  # whether the lead reviews finished work
        self.council = council and review  # review council: plan, each piece of work, and final
        self.councils: list[dict[str, Any]] = []  # project-level councils: plan, final, ask the council
        self.council_busy = 0
        self.final_runs = 0
        self.plan_rejects = 0
        self._final_pending = False
        self.check = (check or "").strip()  # check command (tests): runs automatically before the council
        self.lessons: list[str] = []  # problems the councils caught in this project (lessons for later work)
        self.timeline: list[dict[str, Any]] = []  # timeline: {at, who, kind, text}
        self.usage: dict[str, dict[str, float]] = {}  # who -> {in, out, cost}: token and cost tracking
        self._live_councils: set[Any] = set()  # councils in progress (they stop when the project stops)
        self.picks: dict[str, str] = {}  # auto model picks (name -> "domain · model")
        self._screens_warned = False
        self.plan_council = True  # plan also goes to the council when it's on (new-project window can turn it off)
        self.plan_prev_id = ""  # ID of the council that rejected the plan; the revised plan is judged against it
        self._lead_turns = 0
        self._plan_nudges = 0
        self._dirty0: set[str] | None = None  # files already changed at start (command tracking skips them)
        self.lead_model = lead_model
        self.lead_thinking = lead_thinking if lead_thinking in THINKING else "default"
        self.members: dict[str, Member] = {}
        self.log: list[Message] = []
        self.inbox: dict[str, list[Message]] = {LEAD: []}
        self.questions: list[dict[str, Any]] = []  # the lead's questions to the user
        self.summary = ""
        self.finished = False
        self.stopped = False
        self.lead_agent: Any = None
        self.lead_thread: threading.Thread | None = None
        self.lead_state = "idle"  # idle | running | done
        self.lead_last = ""
        self.lead_saved: list[dict[str, Any]] = []
        self._nudges = 0
        self.cond = threading.Condition()
        self.perm_lock = threading.Lock()  # permission windows open one at a time
        self.publish = publish or (lambda ev: None)
        self._live_ui = publish is not None  # in the app: notifications go out as clickable project cards
        self.roles: list[str] = []
        self._saved_at = 0.0
        # Deleted project: a late save must not bring the file back (an agent thread can call
        # _changed -> save while it shuts down; a deleted project used to come back this way).
        self.deleted = False
        self._save_lock = threading.RLock()
        self._pub_lock = threading.RLock()
        self._dir = projects_dir()  # the save folder is fixed when the project is created
        # Result is a PR (alice/team_pr.py): the project works on its own branch; a PR is opened when it ends.
        self.pr = pr
        self.pr_repo = self.pr_base = self.pr_branch = self.pr_top = self.pr_sha = self.pr_url = ""
        self.pr_session = ""  # the chat that started the project: PR follow-up fixes go there
        self.pr_number = 0

    # ---------------------------------------------------------- timeline
    def mark(self, who: str, kind: str, text: str) -> None:
        with self.cond:
            self.timeline.append({"at": time.time(), "who": who, "kind": kind, "text": text[:300]})
            if len(self.timeline) > 600:
                del self.timeline[:100]

    # --------------------------------------------------------------- status
    def status(self) -> str:
        if self.finished:
            return "done"
        if self.stopped:
            return "stopped"
        if any(not q.get("answer") for q in self.questions):
            return "needs_you"
        ms = list(self.members.values())
        if not ms:
            return "planning"
        if any(m.status in ACTIVE or m.status == "queued" for m in ms) or self.lead_state == "running":
            return "running"
        return "review"

    def active(self) -> bool:
        return not self.finished and not self.stopped

    def all_done(self) -> bool:
        """All agents are in a final state (the lead may not have finished)."""
        return bool(self.members) and all(m.status in FINAL and not m.resume_wanted for m in list(self.members.values()))

    # ------------------------------------------------------------- messages
    def send(self, sender: str, to: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise ToolError(_("message is empty"))
        to = (to or "").strip().lower()
        if to == "all":
            targets = [n for n in list(self.members) if n != sender] + ([LEAD] if sender != LEAD else [])
        else:
            targets = [to]
        unknown = [t for t in targets if t != LEAD and t not in self.members]
        if unknown:
            raise ToolError(_("no one by that name in the project: {names}. Agents: {agents}, lead: {lead}", names=", ".join(unknown),
                              agents=", ".join(self.members) or "-", lead=LEAD))
        boss = sender in (LEAD, USER, COUNCIL)  # the lead, the user and the council re-run the agent under review
        if sender == USER:
            self.stopped = False
            if LEAD in targets:
                self.finished = False  # the user gave new work to a finished project
        resume = []
        with self.cond:
            msg = Message(sender, to, text)
            self.log.append(msg)
            for t in targets:
                self.inbox.setdefault(t, []).append(msg)
                m = self.members.get(t)
                if m is None:
                    continue
                if boss:
                    m.question = ""  # the lead/user answered
                if self.stopped:
                    continue
                if m.status in ("done", "failed") or (m.status == "review" and boss) or \
                        (m.status == "stopped" and sender == USER):
                    resume.append(m)
            if sender in self.members and to in (LEAD, USER):
                self.members[sender].question = text  # question awaiting an answer
            self.cond.notify_all()
        for m in resume:  # work arrived for a finished agent: it continues its conversation where it left off
            self._launch(m, resume=True)
        if LEAD in targets and sender != LEAD:
            self._ensure_lead()
        self._changed()
        return _("sent: {targets}", targets=", ".join(targets))

    def event(self, text: str) -> None:
        """System event for the lead (agent finished, error...)."""
        with self.cond:
            self.inbox[LEAD].append(Message(SYSTEM, LEAD, text))
            self.cond.notify_all()
        self._ensure_lead()

    def take(self, name: str) -> list[Message]:
        with self.cond:
            msgs, self.inbox[name] = self.inbox.get(name, []), []
            return msgs

    def wait_messages(self, name: str, timeout: float, cancelled: Callable[[], bool] = lambda: False) -> list[Message]:
        end = time.monotonic() + timeout
        with self.cond:
            while not self.inbox.get(name) and not self.stopped and not cancelled():
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 0.5))
            msgs, self.inbox[name] = self.inbox.get(name, []), []
            return msgs

    def wait_until(self, pred: Callable[[], bool], timeout: float, cancelled: Callable[[], bool] = lambda: False) -> bool:
        end = time.monotonic() + timeout
        with self.cond:
            while not pred():
                left = end - time.monotonic()
                if left <= 0 or cancelled():
                    return False
                self.cond.wait(min(left, 0.5))
            return True

    def ask_user(self, question: str) -> dict[str, Any]:
        q = {"id": secrets.token_hex(3), "text": question.strip(), "at": time.time(), "answer": ""}
        with self.cond:
            self.questions.append(q)
            self.log.append(Message(LEAD, USER, question.strip()))
            self.cond.notify_all()
        self._emit(_("project '{title}' is asking you: {question}", title=self.title, question=question.strip()[:160]),
                   kind="ask")
        self._changed(save=True)
        return q

    def answer(self, qid: str, text: str) -> None:
        q = next((q for q in self.questions if q["id"] == qid), None)
        if q is None:
            raise ToolError(_("question not found"))
        if not (text or "").strip():
            raise ToolError(_("the answer is empty"))
        q["answer"] = text.strip()
        self.mark(USER, "answer", text.strip())
        self.send(USER, LEAD, f"Answer to your question ({q['text'][:200]}): {text.strip()}")

    # --------------------------------------------------------------- members
    def add(self, spec: dict[str, Any]) -> Member:
        self.prepare_pr()
        name = str(spec.get("name") or "").strip().lower()
        if not NAME_RX.match(name) or name in (LEAD, "all", USER, SYSTEM, COUNCIL):
            raise ToolError(_("invalid agent name '{name}': lowercase letters/digits/-/_, at most 24 characters; 'alice' and 'all' are "
                              "reserved", name=name))
        if name in self.members:
            raise ToolError(_("the name '{name}' is already taken", name=name))
        if len(self.members) >= MAX_MEMBERS:
            raise ToolError(_("a project can have at most {n} agents", n=MAX_MEMBERS))
        task = str(spec.get("task") or "").strip()
        if not task:
            raise ToolError(_("{name}: task is empty", name=name))
        iso = str(spec.get("isolation") or "shared")
        if iso not in ISOLATIONS:
            raise ToolError(_("{name}: isolation must be {values}", name=name, values=", ".join(ISOLATIONS)))
        think = str(spec.get("thinking") or "default")
        if think not in THINKING:
            raise ToolError(_("{name}: thinking must be {values}", name=name, values=", ".join(THINKING)))
        model = str(spec.get("model") or "auto").strip()
        self._check_model(model, name)
        after = [str(a).strip().lower() for a in spec.get("after") or [] if str(a).strip()]
        bad = [a for a in after if a not in self.members]
        if bad:
            raise ToolError(_("{name}: unknown agent in after: {bad} (add them first)", name=name, bad=", ".join(bad)))
        scope = [str(s) for s in spec.get("scope") or [] if str(s).strip()]
        m = Member(name=name, task=task, constraints=str(spec.get("constraints") or "").strip(), scope=scope,
                   model=model, thinking=think, isolation=iso, after=after,
                   max_steps=max(5, min(int(spec.get("max_steps") or 60), 200)))
        m.cwd = self._workdir(m)
        self.members[name] = m
        self.inbox[name] = []
        return m

    def _check_model(self, model: str, who: str) -> None:
        if model == "auto":
            return
        router = self.parent_ctx.router
        try:
            ok = router is None or bool(router.chain(model))
        except KeyError:
            ok = False
        if not ok:
            raise ToolError(_("{who}: no model for '{model}'. Write auto, a role name (brain, fast...) or provider:model.", who=who, model=model))

    def prepare_pr(self) -> None:
        """In a project whose result is a PR, open the project's branch and worktree (once). This must happen before
        agents are added: an agent's folder is decided when it is added; if this came later, agents would run in the
        user's copy."""
        if self.pr and not self.pr_branch:
            from alice import team_pr

            self.mark(LEAD, "pr", team_pr.prepare(self))

    def _repo_top(self) -> Path:
        r = _git(self.cwd, "rev-parse", "--show-toplevel", timeout=20)
        if r.returncode != 0:
            raise ToolError(_("worktree needs the working folder to be a git repository"))
        return Path(r.stdout.strip())

    def _workdir(self, m: Member) -> Path:
        base = self.cwd
        if m.isolation != "worktree":
            return base
        try:
            top = self._repo_top()
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(_("{name}: worktree needs the working folder to be a git repository ({error})", name=m.name, error=e)) from e
        path = top / ".alice-team" / f"{self.id}-{m.name}"
        m.branch = f"alice-team/{m.name}-{time.strftime('%m%d-%H%M%S')}"
        m.base_sha = _git(top, "rev-parse", "HEAD").stdout.strip()
        r = _git(top, "worktree", "add", "-b", m.branch, str(path), "HEAD")
        if r.returncode != 0:
            raise ToolError(_("{name}: could not open the worktree: {output}", name=m.name, output=(r.stderr or r.stdout).strip()[:300]))
        try:  # keep .alice-team out of git status (in a worktree .git is a file: ask git for the path)
            gp = _git(top, "rev-parse", "--git-path", "info/exclude").stdout.strip()
            excl = (top / gp) if gp and not Path(gp).is_absolute() else Path(gp or top / ".git" / "info" / "exclude")
            old = excl.read_text(encoding="utf-8", errors="replace") if excl.exists() else ""
            if excl.parent.is_dir() and ".alice-team/" not in old:
                with excl.open("a", encoding="utf-8") as f:
                    f.write("\n.alice-team/\n")
        except OSError:
            pass  # the exclude entry is a convenience; git may show .alice-team/ as untracked
        rel = base.resolve().relative_to(top.resolve()) if base.resolve() != top.resolve() else Path()
        return path / rel

    def start(self, plan: bool = False) -> None:
        """Start the project: agents start, and the lead starts monitoring (or planning, if there are no agents)."""
        self.mark(USER if self.origin == "user" else LEAD, "start", _("project started: {title}", title=self.title))
        self.prepare_pr()
        self._dirty0 = self._git_dirty()
        for m in list(self.members.values()):
            self._launch(m)
        if plan or not self.members:
            self.event("The project has started and there are no agents yet. Look over the code briefly, split the work into "
                       "non-overlapping parts and present the plan with the plan tool" + (" (the review council will discuss the plan and vote)" if self.council else "") +
                       ". If the job is small, one agent is enough.")
        else:
            self.event("The project has started; the agents are working. If there is nothing to do for now, end your turn; you will be "
                       "woken when a question or a review comes in.")
        self._changed(save=True)

    def _running(self) -> int:
        return sum(1 for m in list(self.members.values()) if m.status in ACTIVE)

    def _deps_ok(self, m: Member) -> bool:
        return all(self.members[a].status == "done" for a in m.after if a in self.members)

    def _launch(self, m: Member, resume: bool = False) -> None:
        with self.cond:
            if (self.stopped or self.finished) and not resume:
                return
            if m.status in ACTIVE:
                return
            if not resume and not self._deps_ok(m):
                m.status = "queued"
                return
            if self._running() >= self.max_parallel:
                if resume:
                    m.resume_wanted = True
                else:
                    m.status = "queued"
                return
            m.status = "running"
            m.resume_wanted = False
            m.stop_requested = False
            m.started = m.started or time.time()
            m.ended = 0.0
        m.last_at, m.stall_warned = time.time(), False
        self.mark(m.name, "run", _("continuing") if resume else _("started"))
        m.thread = threading.Thread(target=self._run_member, args=(m, resume), daemon=True, name=f"team-{m.name}")
        m.thread.start()
        self._changed()

    def _next(self) -> None:
        for m in list(self.members.values()):
            if self._running() >= self.max_parallel:
                return
            if m.status == "queued" and self._deps_ok(m):
                self._launch(m)
            elif m.resume_wanted and m.status not in ACTIVE:
                self._launch(m, resume=True)

    def _run_member(self, m: Member, resume: bool) -> None:
        final = "failed"
        try:
            if m.agent is None:
                m.agent = self._make_agent(m)
            if resume:
                msgs = self.take(m.name)
                prompt = ("[team messages]\n" + "\n".join(x.line() for x in msgs) if msgs else "Continue.") + \
                    "\n\nContinue your work accordingly; when done, write a short report again."
                if m.resume_note:
                    prompt, m.resume_note = m.resume_note + "\n\n" + prompt, ""
            else:
                prompt = START_TASK
            m.limit_hit, m.provider_failed = False, ""
            if self.stopped or m.stop_requested:  # the project or this agent was stopped just as it was starting
                raise _Stopped()
            m.agent.stop_check = lambda m=m: self.stopped or m.stop_requested  # also stops between turns
            text = m.agent.run_turn(prompt, max_steps=m.max_steps) or ""
            if self.stopped or m.stop_requested or m.agent.cancel.is_set():
                final = "stopped"
            elif m.provider_failed and not text.strip():
                final = "failed"  # no model answered (quota/access): it goes to error, not to review
                text = _("Could not reach the models: ") + m.provider_failed
            else:
                final = "review" if self.review and m.isolation != "readonly" else "done"
            m.result = text.strip() or m.result or _("(wrote no report)")
        except _Stopped:
            final = "stopped"
        except Exception as e:  # noqa: BLE001 - one agent's crash must not stop the project
            m.result = f"hata: {type(e).__name__}: {e}"
        finally:
            # The end shows only after the record is saved to disk, so waiting items and the list stay consistent.
            with self.cond:
                m.status, m.ended, m.question = final, time.time(), ""
                self.save()
                self.cond.notify_all()
        if final == "stopped" and m.recovering and not self.stopped:
            self._recover(m)
            return
        m.recovering = False  # if the project was stopped during recovery, the flag must not stay set
        self.mark(m.name, final, {"review": _("finished, in review"), "done": _("finished"), "failed": _("failed"),
                                  "stopped": _("stopped")}.get(final, final) + (f": {m.result[:160]}" if final != "stopped" else ""))
        if final == "failed":
            self._emit(_("{title} / {name} failed: {error}", title=self.title, name=m.name, error=m.result[:200]), kind="failed")
        if final == "review" and self.council:
            self._spawn(self._member_council, m)
        elif final == "review":
            self.event(self._review_note(m))
        elif final == "done":
            self.event(f"{m.name} finished (no review). Report:\n{m.result[:3000]}")
            self._after_done(m)
        elif final == "failed":
            self.event(f"{m.name} failed: {m.result[:600]}. If needed, retry with message or give the job to another "
                       "agent.")
        self._next()
        self._changed()

    def slop_report(self, cwd: Path | None = None) -> str:
        """AI-style patterns on the site (counted from the code). Empty if not a design project or if it is clean."""
        if not self.is_design():
            return ""
        from alice.design_check import lint, report

        try:
            return report(lint(cwd or self.cwd))
        except Exception:  # noqa: BLE001 - the check must not stop the project
            return ""

    def _review_note(self, m: Member) -> str:
        files = ", ".join(m.files[-30:]) or "changed no files"
        if re.search(r"\.(html?|css|scss|jsx|tsx|vue|svelte)\b", files):
            slop = self.slop_report(m.cwd)
            if slop:
                files += (f"\n\n{SLOP_HEAD} (the site still has AI-made patterns; if they are in the agent's area, "
                          "send the work back with revise):\n" + slop)
        extra = " The step limit ran out; the work may be half done." if m.limit_hit else ""
        return (f"{m.name} finished, waiting for review.{extra}\nReport:\n{m.result[:3000]}\n\nChanged files: {files}"
                + (f"\nBranch: {m.branch} (merged on approval)" if m.branch else "") +
                "\n\nReview: look at the changes with diff, run the tests if needed; then approve with review or "
                "ask for a fix.")

    def _after_done(self, m: Member) -> None:
        self._next()  # let those waiting on this agent start
        if not self.all_done() or self.finished:
            return
        if self.council and self.final_runs < 2:
            self._final_pending = True
            self._maybe_final()
            return
        self.event("All agents are done. Verify the whole (tests, build), make a small integration fix if needed, "
                   "then write the user a summary with finish.")

    # ----------------------------------------------------------------- council
    def _maybe_final(self) -> None:
        """The final council convenes when the councils still running (e.g. the last agent's work council) finish."""
        with self.cond:
            if not self._final_pending or self.council_busy or not self.all_done() or self.finished or self.stopped:
                return  # a stopped project does not convene the final council; it is retried on Resume
            self._final_pending = False
            self.final_runs += 1
        self._spawn(self._final_council)

    def _spawn(self, fn: Callable[..., Any], *args: Any) -> None:
        with self.cond:
            self.council_busy += 1

        def run() -> None:
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - a council error must not stop the project
                self.event(f"The council could not meet ({type(e).__name__}: {e}); do the review yourself.")
            finally:
                with self.cond:
                    self.council_busy -= 1
                    self.cond.notify_all()
                self._maybe_final()
                self._changed(save=True)

        threading.Thread(target=run, daemon=True, name=f"council-{self.id}").start()

    def _council(self, kind: str, subject: str, context: str, store: list[dict[str, Any]], cwd: Path | None = None,
                 previous: dict[str, Any] | None = None, critical: bool = False) -> dict[str, Any]:
        from alice.council import Council

        slot = len(store)
        store.append({"kind": kind, "state": "running", "title": subject[:80], "result": {}, "votes": [],
                      "findings": [], "round": 0, "rounds": 0})
        try:
            if kind in ("work", "recheck", "final") and self.check:
                chk = self.run_check(cwd)
                if chk:
                    context += (f"\n\n# Check command: `{chk['cmd']}` → exit {chk['code']} "
                                f"({'PASSED' if chk['code'] == 0 else 'FAILED'})\n```\n{chk['out'][-3000:]}\n```")
            if kind in ("work", "recheck", "final", "adhoc"):
                slop = self.slop_report(cwd)
                if slop:
                    context += (f"\n\n# {SLOP_HEAD}: patterns on the site that look AI-made\nThese were counted from the code, not "
                                "guessed. Each is a 'major' design problem (the goal is 'it must not look AI-made'); "
                                "look at them before small code details:\n" + slop)
            c = Council(self.parent_ctx, kind, subject, context, cwd=cwd or self.cwd, previous=previous,
                        critical=critical or self._critical(subject), rules=self.review_rules(),
                        lessons=self.lessons[-10:], check=self.check, images=self._screens(kind, subject + context, cwd))

            def live() -> None:
                store[slot] = c.snapshot()
                self._changed()

            c.on_change = live
            c.on_usage = lambda u: self._use(COUNCIL, u)
            self._live_councils.add(c)
            if self.stopped:
                c.stop()
        except Exception as e:
            # If the council could not be set up (check command, screenshot), the UI record must not stay "running".
            store[slot] = {**store[slot], "state": "failed", "result": {
                "decision": "contested", "score": 0.0, "veto": [], "conditions": [], "error": f"{type(e).__name__}: {e}"}}
            self._changed(save=True)
            raise
        try:
            snap = c.run()
        finally:
            self._live_councils.discard(c)
        store[slot] = snap
        self.learn(snap)
        res = snap.get("result") or {}
        self.mark(COUNCIL, "council", f"{subject[:60]}: {res.get('decision', '?')} (%{round(100 * (res.get('score') or 0))})")
        self._changed(save=True)
        return snap

    def _screens(self, kind: str, text: str, cwd: Path | None) -> dict[str, str]:
        """If the project is a website and the work touches its look, desktop and phone screenshots for the council."""
        if kind not in ("work", "recheck", "final", "adhoc"):
            return {}
        if kind != "final" and not re.search(r"\.(html?|css|scss|sass|less|jsx|tsx|vue|svelte|astro)\b", text):
            return {}
        if not bool(self.parent_ctx.config.agent.get("team_screenshots", True)):
            return {}
        from alice.site import find_entry, screenshots

        root = cwd or self.cwd
        entry = find_entry(root)
        if not entry:
            return {}
        shots = screenshots(root, entry)
        if shots and kind == "final":
            shots.update(research_refs(self.cwd))  # the council sees our site side by side with the reference sites
        if not shots and not self._screens_warned:
            self._screens_warned = True
            note = (_("The council couldn't see the site (no screenshot could be taken); it only looks at the code. Visual review "
                      "needs Edge or Chrome (the browser path can also be given with ALICE_CHROMIUM)."))
            self.mark(SYSTEM, "council", note)
            self._emit(f"{self.title}: {note}", kind="info")
        return shots

    @staticmethod
    def _critical(text: str) -> bool:
        """Is the work critical (full council: all experts, 3 rounds)? Only the task text is checked; the diff
        and code everywhere contain "token" and "delete". A word boundary is required: "para" used to match "paragraf"
        and made all 16 councils critical in a website project (the council used ~40M tokens)."""
        return bool(re.search(r"\b(?:güvenlik|security|şifre\w*|parola\w*|password|secret|kimlik doğrulama|auth\w*|"
                              r"veri kaybı|migration|göç\w*|ödeme\w*|payment|para(?:sı|yı|nın)?|kredi kartı|"
                              r"veritabanı silme|drop table)\b", text[:4000], re.I))

    def _member_material(self, m: Member) -> tuple[str, str]:
        subject = f"The work of agent '{m.name}'. Its task: {m.task}" + (f"\nConstraints: {m.constraints}" if m.constraints else "")
        context = (f"Project goal: {self.goal}\n\n# The agent's report\n{m.result[:4000]}\n\n# Changed files\n"
                   f"{', '.join(m.files) or '(none)'}\n\n# Diff\n{self.diff(m.name, limit=20_000) or '(no diff)'}")
        return subject, context

    def _member_council(self, m: Member, adhoc: bool = False) -> None:
        from alice.council import fix_list, report

        m.council_running = True
        self._changed()
        try:
            subject, context = self._member_material(m)
            done = [c for c in m.councils if c.get("state") != "stopped"]  # an interrupted council is not counted
            prev = done[-1] if done and m.council_cycles and not adhoc else None
            kind = "adhoc" if adhoc else ("recheck" if prev else "work")
            snap = self._council(kind, subject, context, m.councils, cwd=m.cwd, previous=prev)
        finally:
            m.council_running = False
        if snap.get("state") == "stopped":
            return  # project stopped; the council reconvenes when it is resumed
        with self.cond:
            self.log.append(Message(COUNCIL, m.name, report(snap).splitlines()[0]))
        self._apply_council(m, snap, report(snap), fix_list(snap))

    def _apply_council(self, m: Member, snap: dict[str, Any], rep_text: str, fixes: str) -> None:
        if m.status != "review":
            return  # the user/lead decided in the meantime
        decision = (snap.get("result") or {}).get("decision")
        from alice.council import blocking

        if decision == "approve_conditions" and not blocking(snap):
            # The conditions are minor: the work is approved and they go to the lead as a note (no new round).
            self.review_member(m.name, True, by=COUNCIL)
            self.event(f"The council approved {m.name}'s work; small improvement suggestions (optional):\n{fixes}")
            return
        if decision == "approve":
            self.review_member(m.name, True, by=COUNCIL)
        elif decision in ("approve_conditions", "revise") and m.council_cycles < 2:
            m.council_cycles += 1
            self.review_member(m.name, False, "Council decision: " + ("approved with conditions" if decision == "approve_conditions"
                                                                  else "revision needed") + ". Fix these:\n"
                               + fixes, by=COUNCIL)
        elif decision in ("approve_conditions", "revise"):
            self.event(f"The council still doesn't accept {m.name}'s work after two fix rounds. You decide "
                       f"(review approve or revise; ask_user if needed).\n{rep_text}")
        else:
            self.event(f"The council is split on {m.name}'s work. Read the report, look at the diff if needed and decide with review "
                       f"(if it's very critical, ask the user with ask_user).\n{rep_text}")

    def _final_council(self) -> None:
        from alice.council import report

        parts = []
        for m in list(self.members.values()):
            parts.append(f"## {m.name} ({m.status})\nTask: {m.task}\nReport: {m.result[:1500]}\n"
                         f"Files: {', '.join(m.files) or '-'}\nDiff:\n{self.diff(m.name, limit=6000) or '-'}")
        snap = self._council("final", f"The whole project: {self.goal}", "\n\n".join(parts)[:40_000], self.councils,
                             critical=True)
        if snap.get("state") == "stopped":  # reconvenes when resumed
            self.final_runs -= 1
            self._final_pending = True
            return
        decision = (snap.get("result") or {}).get("decision")
        last = self.final_runs >= 2
        if decision in ("approve", "approve_conditions"):
            note = "The final council approved the project. Verify the whole one last time and write the summary with finish" + \
                (" (note the conditions as still open in the summary)." if decision == "approve_conditions" else ".")
        elif last:
            note = ("The final council didn't approve a second time either. Write the remaining problems as 'still open' in the "
                    "finish summary, or if critical, ask the user with ask_user.")
        else:
            note = ("The final council didn't approve. Give the findings to the agents concerned as tasks with message (or set up a "
                    "new agent with assign); when the work is done, the final council meets once more.")
        self.event(f"{report(snap)}\n\n{note}")

    def ask_council(self, name: str = "") -> None:
        """The user's 'Ask the council' button: asks about one agent's work, or the whole project if no name."""
        if self.council_busy and not name:
            raise ToolError(_("the council is already meeting"))
        if name:
            m = self.members.get(name)
            if m is None:
                raise ToolError(f"projede '{name}' yok")
            if m.status in ACTIVE or m.council_running:
                raise ToolError(_("{name} is running or its council is in progress; ask when it's done", name=name))
            self._spawn(self._member_council, m, True)
        else:
            def run() -> None:
                from alice.council import report

                parts = [f"## {m.name}: {m.task}\n{m.result[:1500]}\n{self.diff(m.name, limit=5000)}"
                         for m in list(self.members.values())]
                snap = self._council("adhoc", f"The project's current state: {self.goal}", "\n\n".join(parts)[:40_000],
                                     self.councils)
                if snap.get("state") == "stopped":
                    return
                self.event(f"The user asked the council; the result:\n{report(snap)}\nGuide the agents if needed.")

            self._spawn(run)

    def user_vote(self, name: str, vote: str, note: str = "") -> str:
        """The user's vote (3x weight): the last council is recounted, and if the decision changes, it is applied."""
        from alice.council import VOTES, fix_list, report, retally

        if vote not in VOTES:
            raise ToolError(_("the vote must be approve, conditional or reject"))
        store = self.members[name].councils if name in self.members else self.councils
        done = [i for i, c in enumerate(store) if c.get("state") in ("done", "failed")]
        if not done:
            raise ToolError("oylanacak kurul sonucu yok")
        i = done[-1]
        store[i] = retally(store[i], {"vote": vote, "note": note.strip()})
        snap = store[i]
        with self.cond:
            self.log.append(Message(USER, COUNCIL, _("my vote: {vote}", vote=vote) + (f" — {note.strip()}" if note.strip() else "")))
        if name in self.members:
            m = self.members[name]
            if m.status == "review":
                fixes = fix_list(snap) + (f"\n- user: {note.strip()}" if note.strip() else "")
                dec = (snap.get("result") or {}).get("decision")
                if dec == "approve":
                    self.review_member(name, True, by=USER)
                elif dec in ("revise", "approve_conditions"):
                    self.review_member(name, False, "The council (with your vote) asked for a fix:\n" + fixes, by=USER)
        else:
            self.event(f"The user voted in the council; the new result:\n{report(snap)}")
        self._changed(save=True)
        return (snap.get("result") or {}).get("decision") or "?"

    # -------------------------------------------------------------- review
    def review_member(self, name: str, approve: bool, feedback: str = "", by: str = LEAD) -> str:
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if approve:
            if m.status not in ("review", "done", "failed", "stopped"):
                raise ToolError(_("{name} is {status} right now; it can be approved when it's done", name=name, status=m.status))
            if m.branch:
                try:
                    note = self._merge(m)
                except ToolError as e:
                    if m.merge_attempts < 2 and MERGE_FAILED in str(e):
                        return self._conflict(m, str(e), by)
                    raise
            else:
                note = ""
            m.status = "done"
            self.mark(by, "approve", _("{name} approved", name=name) + (f" ({note})" if note else ""))
            with self.cond:
                self.log.append(Message(by, name, _("approved") + (f": {feedback.strip()}" if feedback.strip() else "")))
                self.cond.notify_all()
            if by == USER:
                self.event(f"The user approved agent {name}'s work.")
            self._after_done(m)
            self._changed(save=True)
            return _("{name} approved", name=name) + (f"; {note}" if note else "")
        if not feedback.strip():
            raise ToolError(_("write the feedback for a fix: say what is wrong and what you expect"))
        m.reviews += 1
        self.mark(by, "revise", _("{name} sent back for a fix: {feedback}", name=name, feedback=feedback.strip()[:200]))
        lessons = ("\n\nProblems the council caught earlier in this project (don't repeat them):\n" +
                   "\n".join(f"- {x}" for x in self.lessons[-8:])) if self.lessons else ""
        self.send(by, name, "The review asks for a fix:\n" + feedback.strip() + lessons)
        return _("{name} sent back for a fix", name=name)

    def _conflict(self, m: Member, error: str, by: str) -> str:
        """The worktree branch did not merge: the agent fixes the conflict on its own branch and is reviewed again."""
        m.merge_attempts += 1
        base = _git(self._repo_top(), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
        self.mark(SYSTEM, "conflict", _("{name}'s branch did not merge; the agent is resolving the conflict", name=m.name))
        self.send(by if by in (LEAD, USER, COUNCIL) else LEAD, m.name,
                  f"Your work was approved but your branch did not merge into the main branch (conflict). Run `git merge {base}` in "
                  f"your own worktree, resolve the conflicts (keep both sides' intent), run the tests, commit and write a short "
                  f"report; then it will be reviewed again and merged.\nError: {error[:600]}")
        return _("{name}'s branch did not merge; the agent is resolving the conflict (attempt {n}/2)", name=m.name, n=m.merge_attempts)

    def _merge(self, m: Member) -> str:
        """Merge the worktree agent's branch into the main working folder (first commits any remaining changes)."""
        top = self._repo_top()
        wt = m.cwd
        ident = ["-c", "user.name=ALICE", "-c", "user.email=alice@localhost"]
        if _git(wt, "status", "--porcelain").stdout.strip():
            _git(wt, "add", "-A")
            r = _git(wt, *ident, "commit", "-q", "-m", f"alice-team {m.name}: {m.task[:60]}")
            if r.returncode != 0:
                raise ToolError(f"could not commit on {m.name}'s branch: {(r.stderr or r.stdout).strip()[:300]}")
        if _git(top, "merge-base", "--is-ancestor", m.branch, "HEAD").returncode == 0:
            return "nothing new to merge on the branch"
        r = _git(top, *ident, "merge", "--no-ff", "--no-edit", m.branch)
        if r.returncode == 0:
            m.merge_attempts = 0
        if r.returncode != 0:
            _git(top, "merge", "--abort")
            raise ToolError(f"{m.branch} {MERGE_FAILED} (conflict or a dirty working folder): "
                            f"{(r.stdout + r.stderr).strip()[:500]}. Ask the agent to update its branch or "
                            "resolve it yourself.")
        return f"{m.branch} merged"

    def diff(self, name: str, limit: int = 60_000) -> str:
        """Diff of the changes the agent made with file tools."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        parts = []
        # On a copy: if an agent touches a new file while running (council, final council), the dict grows and the
        # diff used to crash the council with "dictionary changed size during iteration".
        for path, before in list(m.originals.items()):
            p = Path(path)
            rel = _rel(p, m.cwd)
            after = _read_original(p)
            if (before or "").startswith("\0") or (after or "").startswith("\0"):
                parts.append(f"--- {rel}: the file is too large or couldn't be read; no diff shown")
                continue
            if before == after:
                continue
            if after is None:
                parts.append(f"--- a/{rel}\n+++ /dev/null\n@@ dosya silindi @@")
                continue
            parts.append(make_diff(rel, before or "", after, max_lines=800))
        text = "\n".join(x for x in parts if x)
        return text[:limit] + ("\n... (shortened)" if len(text) > limit else "")

    def revert(self, name: str) -> list[str]:
        """Revert the changes the agent made with file tools."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if m.status in ACTIVE:
            raise ToolError(_("{name} is running; stop it first", name=name))
        done = []
        for path, before in m.originals.items():
            p = Path(path)
            if (before or "").startswith("\0"):
                continue
            try:
                if before is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(before, encoding="utf-8")
                done.append(_rel(p, self.cwd))
            except OSError:
                pass  # that file can't be restored; the others still are
        m.originals.clear()
        m.files.clear()
        with self.cond:
            self.log.append(Message(USER, name, _("its changes were reverted ({n} files)", n=len(done))))
        self._changed(save=True)
        return done

    def restart(self, name: str) -> None:
        """Restart the agent from scratch with a clean conversation."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if m.status in ACTIVE:
            raise ToolError(_("{name} is running; stop it first", name=name))
        m.agent, m.saved_messages, m.result, m.steps, m.last = None, [], "", 0, ""
        m.started, m.ended, m.status = 0.0, 0.0, "queued"
        self.stopped = self.finished = False
        self._launch(m)
        self._changed(save=True)

    def finish(self, summary: str) -> None:
        self.mark(LEAD, "finish", _("project finished"))
        self.summary = summary.strip()
        if self.pr and self.pr_branch and not self.pr_url:
            from alice import team_pr

            note = team_pr.open_pr(self)
            self.mark(LEAD, "pr", note)
            self.summary += "\n\n" + note
        self.lead_state = "done"
        with self._save_lock:  # "bitti" (finished) shows only once saved (list and reopen stay consistent)
            self.finished = True
            self.save()
        with self.cond:
            self.log.append(Message(LEAD, USER, _("Project finished.") + "\n" + self.summary))
            self.cond.notify_all()
        self._emit(_("project '{title}' finished: {summary}", title=self.title,
                      summary=self.summary.splitlines()[0][:200] if self.summary else ""),
                   kind="done")
        self._changed(save=True)

    # --------------------------------------------------------------- agents
    def _role(self, name: str, model: str, thinking: str, text: str = "", domain: str = "") -> str:
        """The agent's router role. model=auto: the best model order for the task's domain (design, code, review...)."""
        from alice.providers import picker

        role = f"team:{self.id}:{name}"
        router = self.parent_ctx.router
        if model == "auto":
            domain = domain or picker.classify(text)
            chain = picker.chain_for(router, domain, fallback="fast" if domain == "light" else "brain")
            self.picks[name] = picker.explain(chain[0], domain) if chain else ""
        else:
            chain = list(router.chain(model))
            self.picks[name] = ""
        router.role_overrides[role] = chain
        if thinking != "default":
            router.thinking_overrides[role] = thinking
        if role not in self.roles:
            self.roles.append(role)
        return role

    def _make_agent(self, m: Member):
        from alice.agent import Agent
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt
        from alice.team_tools import DesignCheck, SiteStudy, TeamSend, TeamWait

        parent = self.parent_ctx.state.get("agent")
        if parent is None or self.parent_ctx.router is None:
            raise RuntimeError(_("the project can't run in this context"))
        ctx = ToolContext(cwd=m.cwd, config=self.parent_ctx.config, router=self.parent_ctx.router,
                          decider=self.parent_ctx.decider, emit=lambda s: None)
        tools = [t for n, t in parent.tools.items() if n in MEMBER_TOOLS] + [TeamSend(self, m.name),
                                                                            TeamWait(self, m.name)]
        if self.is_design() and m.isolation != "readonly":
            tools += [DesignCheck(self, m.name), SiteStudy(self, m.name)]
        perms = Permissions("plan" if m.isolation == "readonly" else parent.permissions.mode)
        perms.always_allow = set(parent.permissions.always_allow)
        agent = Agent(parent.config, parent.router, tools, ctx, perms, MemberUI(self, m), parent.decider,
                      system_prompt=build_system_prompt(m.cwd, personality=False) + "\n\n" + self.brief(m),
                      is_child=True)
        agent.stream = bool(parent.config.agent.get("stream", True))  # the answer streams live while it is written
        agent.compact_cap = team_compact_cap(parent.config)
        agent.role = self._role(m.name, m.model, m.thinking, text=f"{m.task}\n{m.constraints}")
        m.picked = self.picks.get(m.name, "")
        agent.on_usage = lambda u, _n=m.name: self._use(_n, u)
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]  # no groups: all are open
        ctx.state["agent"] = agent  # long-running tools see Stop
        scope = [ctx.resolve(s) for s in m.scope]
        base_run = agent._run_tool

        def guarded(tool: Tool, args: dict[str, Any]) -> ToolResult:
            targets = [ctx.resolve(p) for p in write_targets(tool.name, args)] if tool.name in WRITE_TOOLS else []
            if scope:
                bad = [str(p) for p in targets if not in_scope(p, scope)]
                if bad:
                    return ToolResult(f"out of scope: {', '.join(bad)}. You may only write to: "
                                      f"{', '.join(m.scope)}. If something elsewhere has to change, write to its owner or "
                                      f"{LEAD} with team_send. Don't get around the scope with a shell command (Set-Content, Out-File, "
                                      "echo >): it breaks the file encoding (Turkish letters) and the work is rejected.", is_error=True)
            for p in targets:  # keep the original for diff and revert
                m.originals.setdefault(str(p), _read_original(p))
            m.steps += 1
            m.last_at, m.stall_warned = time.time(), False
            m.last = f"{tool.name} {tool.summary(args)[:90]}"
            self._changed()
            res = base_run(tool, args)
            if tool.name == "run_command":
                self._track_command(m)
            if targets and not res.is_error:
                for p in targets:
                    rel = _rel(p, self.cwd)
                    if rel not in m.files:
                        m.files.append(rel)
            return res

        agent._run_tool = guarded

        def deliver() -> None:  # add messages that arrive after each tool step to the conversation
            msgs = self.take(m.name)
            if msgs:
                agent.messages.append({"role": "user", "content": TEAM_MESSAGE + "\n" +
                                       "\n".join(x.line() for x in msgs)})

        agent.on_step = deliver
        if m.saved_messages:  # project loaded from disk: continue the conversation where it left off
            agent.messages = agent.messages[:1] + m.saved_messages
            m.saved_messages = []
        return agent

    def _git_dirty(self, cwd: Path | None = None) -> set[str] | None:
        """Changed/new files (absolute paths) if this is a git repo; otherwise None."""
        cwd = cwd or self.cwd
        try:
            top = _git(cwd, "rev-parse", "--show-toplevel", timeout=10)
            if top.returncode != 0:
                return None
            # -z: paths come unquoted and unescaped. In plain output git escaped non-ASCII paths as "d\303\274.py";
            # that path never existed, so a file changed by a command was missing from the agent's diff.
            r = _git(cwd, "status", "--porcelain", "-uall", "-z", timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        root = Path(top.stdout.strip())
        out = set()
        recs = r.stdout.split("\0")
        i = 0
        while i < len(recs):
            rec = recs[i]
            i += 1
            if len(rec) < 4:
                continue
            if rec[0] in "RC":
                i += 1  # rename: the record that follows is the old path
            path = rec[3:]
            if path and not path.startswith(".alice-team/"):
                out.add(str((root / path).resolve()))
        return out

    def _track_command(self, m: Member) -> None:
        """Count files changed by run_command as the agent's changes too (git repo): the original comes from HEAD,
        and files already changed at project start or touched by another agent are skipped."""
        base0 = self._dirty0 if m.isolation != "worktree" else set()
        if base0 is None:
            return
        now = self._git_dirty(m.cwd)
        if not now:
            return
        others = {k for o in list(self.members.values()) if o is not m for k in list(o.originals)}
        top = _git(m.cwd, "rev-parse", "--show-toplevel", timeout=10).stdout.strip()
        for path in sorted(now - set(base0) - set(m.originals) - others)[:50]:
            rel = Path(path).resolve().relative_to(Path(top).resolve()).as_posix() if top else path
            r = _git(m.cwd, "show", f"HEAD:{rel}", timeout=10)
            m.originals[path] = r.stdout if r.returncode == 0 else None
            relp = _rel(Path(path), self.cwd)
            if relp not in m.files:
                m.files.append(relp)

    def run_check(self, cwd: Path | None = None) -> dict[str, Any] | None:
        """The project's check command (tests/lint): runs before the council; its output is evidence for it."""
        if not self.check:
            return None
        t0 = time.time()
        try:
            pop = subprocess.Popen(self.check, shell=True, cwd=str(cwd or self.cwd), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                   errors="replace", creationflags=_NO_WINDOW, startupinfo=_startupinfo())
            try:
                so, se = pop.communicate(timeout=int(self.parent_ctx.config.agent.get("team_check_timeout", 600)))
                out, code = (so or "") + (se or ""), pop.returncode
            except subprocess.TimeoutExpired:
                # Here subprocess.run only killed the shell and waited forever for the pipes to close: the test process
                # the shell started kept the pipe open, so the council thread (and the project) hung forever.
                from alice.tools.shell import kill_tree

                kill_tree(pop.pid)
                try:
                    pop.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # killed; its output is not needed
                out, code = "timed out", -1
        except OSError as e:
            out, code = str(e), -1
        res = {"cmd": self.check, "code": code, "out": out[-4000:], "secs": int(time.time() - t0)}
        self.mark(SYSTEM, "check", _("check command passed") if code == 0 else _("check command failed (exit {code})", code=code))
        return res

    def review_rules(self) -> str:
        for f in (self.cwd / "REVIEW.md", self.cwd / ".alice" / "REVIEW.md"):
            try:
                if f.is_file():
                    return f.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                pass  # unreadable rules file: try the next one
        return ""

    def learn(self, snap: dict[str, Any]) -> None:
        """Confirmed serious findings are added to the project's lessons (later agents and councils get them).
        The same lesson is not added again in other words."""
        from alice.council import dedupe

        new = [f"{f['title']}" + (f" → {f['fix']}" if f.get("fix") else "") for f in snap.get("findings", [])
               if f.get("status") == "confirmed" and f.get("severity") in ("critical", "major")]
        self.lessons += [x[:240] for x in dedupe(new, self.lessons)]
        del self.lessons[:-15]

    def brief(self, m: Member) -> str:
        roster = "\n".join(f"- {o.name}: {o.task[:160]}" + (f" (yazar: {', '.join(o.scope)})" if o.scope else "")
                           for o in list(self.members.values()) if o.name != m.name) or "- (you are alone)"
        where = {"shared": "You work in the same folder as the others; don't touch other people's files.",
                 "worktree": f"You are in your own git worktree (branch {m.branch}); your changes stay on this branch "
                             "and are merged when the lead approves.",
                 "readonly": "Read-only: you can't change files or run commands that change them; review and report."}
        lessons = ("Problems the council caught earlier in this project (don't repeat them):\n" +
                   "\n".join(f"- {x}" for x in self.lessons[-8:]) + "\n") if self.lessons else ""
        after = f"This job started after these finished: {', '.join(m.after)}. Their changes are in place.\n" \
            if m.after else ""
        design = self.design_brief(m)
        return f"""# Project task
You are the agent named "{m.name}" in the project "{self.title}". You do one part of a larger job; you talk not with
the user but with the project's lead (ALICE, named "{LEAD}") and your teammates.

Overall goal: {self.goal}

Your task: {m.task}
Constraints: {m.constraints or "no special constraints; still, do only your own task."}
Your write scope: {", ".join(m.scope) if m.scope else "the whole working folder (but stay out of other people's areas)"}
Working folder: {m.cwd}. {where[m.isolation]}
{after}{lessons}{design}
Your teammates:
{roster}

Rules:
- Do only your own task. If something in someone else's area needs to change, write to them with team_send.
- When you settle an interface/contract that affects others (a function name, an API, a data format), tell the person
  concerned early, or everyone with to="all".
- If a decision is needed, you are stuck, or you need to go outside your scope, ask {LEAD} and wait for the answer
  with team_wait. Don't send needless messages; write briefly and clearly.
- Messages to you are added to your conversation as "[team message]".
- Don't write large files (longer than about 300 lines) with a single write_file: write the skeleton first, then fill
  it in section by section with edit_file. One giant tool call is very slow, can break off, and makes you look stuck.
- Verify your work (tests, running it). When done, write a short report in your last message: what you did, which
  files changed, how you verified it, what is still open. The lead reviews your work; if it wants fixes, it writes to
  you again.
- Write user-facing text in the project (UI, messages, content) in the language the goal asks for; Turkish if it
  doesn't say.
{self._member_design_rule(m)}"""

    def _member_design_rule(self, m: Member) -> str:
        if not self.is_design() or m.isolation == "readonly":
            return ""
        return ("- Research before making a design decision: find real, good sites on the subject with web_search and "
                "look at them with site_study (screenshot + analysis). The good examples you saw are the standard, not "
                "the average site in your head.\n"
                "- Before finishing work that touches the look, run design_check: it takes screenshots of the site, "
                "counts AI patterns and has an art director critique it. Fix the problems it finds, then run it once "
                "more. If the pattern list isn't empty, your work is rejected.\n")

    # ------------------------------------------------------------- lead
    def _ensure_lead(self) -> None:
        """Start the lead loop if it is not running (it handles the events in the inbox)."""
        with self.cond:
            if self.lead_thread is not None and self.lead_thread.is_alive():
                return  # the loop is running; the event will be taken from the inbox
            if self.stopped:
                return  # the lead does not run in a stopped project; events wait and are handled on resume
            if self.parent_ctx.state.get("agent") is None or self.parent_ctx.router is None:
                return
            self.lead_thread = threading.Thread(target=self._lead_loop, daemon=True, name=f"lead-{self.id}")
            self.lead_thread.start()

    def _make_lead(self):
        from alice.agent import Agent
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt
        from alice.team_tools import (
            LeadAskUser,
            LeadAssign,
            LeadDiff,
            LeadFinish,
            LeadPlan,
            LeadReview,
            LeadStatus,
            TeamSend,
        )

        parent = self.parent_ctx.state["agent"]
        ctx = ToolContext(cwd=self.cwd, config=self.parent_ctx.config, router=self.parent_ctx.router,
                          decider=self.parent_ctx.decider, emit=lambda s: None)
        tools = [t for n, t in parent.tools.items() if n in MEMBER_TOOLS] + [
            LeadPlan(self), LeadAssign(self), TeamSend(self, LEAD), LeadReview(self), LeadDiff(self), LeadAskUser(self),
            LeadFinish(self), LeadStatus(self)]
        perms = Permissions(parent.permissions.mode)
        perms.always_allow = set(parent.permissions.always_allow)
        agent = Agent(parent.config, parent.router, tools, ctx, perms, MemberUI(self, Member(name=LEAD, task="")),
                      parent.decider,
                      system_prompt=build_system_prompt(self.cwd, personality=False) + "\n\n" + self.lead_brief(),
                      is_child=True)
        agent.stream = bool(parent.config.agent.get("stream", True))  # the answer streams live while it is written
        agent.compact_cap = team_compact_cap(parent.config)
        agent.role = self._role(LEAD, self.lead_model, self.lead_thinking, domain="review")
        agent.on_usage = lambda u: self._use(LEAD, u)
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]
        ctx.state["agent"] = agent
        if self.lead_saved:
            agent.messages = agent.messages[:1] + self.lead_saved
            self.lead_saved = []
        return agent

    def _lead_loop(self) -> None:
        try:
            if self.lead_agent is None:
                self.lead_agent = self._make_lead()
            while True:
                if self.stopped and not self.finished:
                    with self.cond:  # events stay in the inbox; Resume or a message from the user restarts it
                        exiting = self.stopped  # check again under the lock: if it was resumed just now, do not exit
                        if exiting:
                            self.lead_thread = None
                    # Decided under the lock: re-reading lead_thread after unlocking could see the new loop Resume
                    # started, and keep this one running too (two loops on one lead agent).
                    if exiting:
                        self.lead_state = "idle"
                        self._changed()
                        return
                msgs = self.wait_messages(LEAD, 1.0, lambda: self.finished or self.stopped)
                if msgs and self.stopped and not self.finished:
                    with self.cond:
                        self.inbox[LEAD] = msgs + self.inbox.get(LEAD, [])
                    continue
                if not msgs:
                    self._watch_stalls()
                    nudge = "" if self.finished or self.stopped else self._nudge()
                    if not nudge:
                        if self._lead_exit():
                            return
                        continue
                    msgs = [Message(SYSTEM, LEAD, nudge)]
                self.lead_state = "running"
                self._changed()
                prompt = EVENTS_HEAD + "\n" + "\n\n".join(
                    (x.text if x.sender == SYSTEM else x.line()) for x in msgs) + "\n\n" + STATUS_HEAD + "\n" + self.status_text()
                text = self.lead_agent.run_turn(prompt, max_steps=int(self.parent_ctx.config.agent.get(
                    "team_lead_steps", 40))) or ""
                self._lead_turns += 1
                self.lead_last = text.strip() or self.lead_last
                self.lead_state = "done" if self.finished else "idle"
                self._changed(save=True)
        except Exception as e:  # noqa: BLE001
            self.lead_state = "idle"
            self.lead_last = f"lead failed: {type(e).__name__}: {e}"
            self._emit(self.lead_last)
            self._changed(save=True)

    def _lead_exit(self) -> bool:
        """If the lead has nothing left to wait for, leave the loop (_ensure_lead restarts it when a new event arrives).
        The inbox is checked one last time under the lock: an event that arrives while leaving must not be lost."""
        idle = all(m.status in FINAL + ("review",) and not m.resume_wanted for m in list(self.members.values())) and \
            not self.council_busy
        if not (self.stopped or self.finished or (self.members and idle and self._nudges >= 2)):
            return False
        with self.cond:
            if self.inbox.get(LEAD):
                return False
            self.lead_thread = None
            return True

    def _watch_stalls(self) -> None:
        """An agent making no progress for long: the lead and user are told first; if it stalls as long again,
        it is stopped and resumed with the next model (at most twice, then left to the user)."""
        limit = float(self.parent_ctx.config.agent.get("team_stall_minutes", 10)) * 60
        now = time.time()
        for m in list(self.members.values()):
            if m.status != "running" or not m.last_at or m.recovering:
                continue
            idle = now - m.last_at
            if not m.stall_warned and idle > limit:
                m.stall_warned = True
                mins = int(idle // 60)
                self.event(f"{m.name} hasn't progressed for {mins} minutes (last step: {m.last or '-'}). If it doesn't progress a bit more, "
                           "the system will restart it with another model where it left off; you can also ask how it's "
                           "doing with message or make the task smaller.")
                self._emit(_("{title} / {name} hasn't progressed for {mins} minutes", title=self.title, name=m.name, mins=mins), kind="stall")
            elif m.stall_warned and idle > 2 * limit and m.agent is not None:
                if m.recoveries >= 2:
                    if m.recoveries == 2:  # tell the user once
                        m.recoveries += 1
                        self.event(f"{m.name} got stuck again after two automatic restarts. Split the task, "
                                   "give it to another agent or ask the user.")
                        self._emit(_("{title} / {name} got stuck again; you may need to take a look", title=self.title, name=m.name), kind="stall")
                    continue
                m.recovering = True
                self.mark(SYSTEM, "stall", _("{name} got stuck; it will be stopped and continued with another model", name=m.name))
                m.agent.stop()

    def _recover(self, m: Member) -> None:
        """Resume an agent that was stopped because it stalled, from where it left off, with the next model."""
        m.recovering = False
        m.recoveries += 1
        model = self._rotate_model(m)
        m.resume_note = ("[system] Your previous step was cut off because it made no progress for a long time. Continue where you left off. "
                         "Don't write large files in one go: write the skeleton first, then edit it section by section.")
        self.mark(SYSTEM, "run", _("{name} restarted automatically", name=m.name) + (f" ({model})" if model else ""))
        self.event(f"{m.name} was stuck; the system stopped it and " + (f"{model} ile " if model else "") +
                   "continued it where it left off. You don't need to do anything.")
        self._emit(_("{title} / {name} got stuck and was restarted automatically", title=self.title, name=m.name), kind="info")
        self._launch(m, resume=True)
        self._changed(save=True)

    def _rotate_model(self, m: Member) -> str:
        """Move the agent to the next model in its chain; the stalled one goes last. Returns the new first model."""
        router = self.parent_ctx.router
        chain = router.role_overrides.get(f"team:{self.id}:{m.name}") if router is not None else None
        if not chain or len(chain) < 2:
            return ""
        chain.append(chain.pop(0))
        return str(chain[0])

    def _use(self, who: str, usage: dict[str, Any]) -> None:
        _add_usage(self.usage.setdefault(who, {}), usage)

    def usage_total(self) -> dict[str, float]:
        tot: dict[str, float] = {"in": 0, "out": 0}
        for row in self.usage.values():
            tot["in"] += row.get("in", 0)
            tot["out"] += row.get("out", 0)
            if "cost" in row:
                tot["cost"] = round(tot.get("cost", 0.0) + row["cost"], 6)
        return tot

    def _nudge(self) -> str:
        """If the lead ended its turn with work hanging, remind it; after two reminders the project closes itself."""
        if self.finished or self.stopped:
            return ""
        if not self.members:
            return self._plan_nudge()
        if any(not q.get("answer") for q in self.questions) or self._nudges >= 3 or self.council_busy or \
                any(m.council_running for m in list(self.members.values())):
            return ""
        pending = [m.name for m in list(self.members.values()) if m.status == "review"]
        if pending and all(m.status in FINAL + ("review",) for m in list(self.members.values())):
            self._nudges += 1
            if self._nudges <= 2:
                return f"Agents are waiting for review: {', '.join(pending)}. Look with diff and decide with review."
            return ""
        if not self.all_done():
            return ""
        self._nudges += 1
        if self._nudges <= 2:
            return ("All agents are done but the project isn't closed. Verify the whole and write the summary with finish "
                    "(if there is work left, continue with assign or message).")
        self.finish(self.lead_last or _("The agents finished their work; the lead wrote no summary. Look at the agents' reports."))
        return ""

    def _plan_nudge(self) -> str:
        """If the lead ended its turn without planning, remind it; after two reminders, give the work to one agent."""
        if self._lead_turns == 0 or self.council_busy or self.lead_state == "running" or \
                any(not q.get("answer") for q in self.questions):
            return ""
        self._plan_nudges += 1
        if self._plan_nudges <= 2:
            return ("You haven't set up any agents yet. Present the plan now with the plan tool (one agent is enough for a small "
                    "job); don't end the turn with just an explanation.")
        if self._plan_nudges == 3:
            self.mark(SYSTEM, "plan", _("the lead presented no plan; the job was given to one agent"))
            self.add_member({"name": "worker", "task": f"Do the project's goal from start to finish: {self.goal}\n"
                             "First look over the code, then make the changes, verify them with tests and write a report."},
                            announce=False)
        return ""

    def lead_brief(self) -> str:
        review = ("Each agent that finishes moves to 'review' and you get a review event." if self.review else
                  "Review is off: agents that finish go straight to 'done'; still read their reports.")
        council_plan = ""
        if self.council:
            review = ("The review council (critic, advocate, verifier, experts when needed) debates and votes on "
                      "each finished job and applies the decision; only contested jobs, or ones still not accepted "
                      "after two fixes, come to you. For those, read the council report and decide yourself.")
            council_plan = ("; the council reviews the plan, and if it finds a plan-breaking problem you fix it once "
                            "and submit again; the council's suggestions are added as notes to the agents' tasks"
                            ) if self.plan_council else ""
        return f"""# Project lead
You are ALICE and the lead of the project "{self.title}" (the orchestrator; agents write to you as "{LEAD}").
The project runs in the background, separately from the user's chat. You talk to the user only with ask_user and
finish, in Turkish.

Goal: {self.goal}
Working folder: {self.cwd}

Your jobs:
1. Plan: if there are no agents, look over the code briefly (glob, grep, read_file), split the work into
   non-overlapping parts and submit all the agents at once with the plan tool (at most {MAX_MEMBERS}){council_plan}.
   Use assign for an extra agent later. Give each agent a task, a way to verify it and a write scope (scope) clear
   enough for someone who hasn't seen the conversation; scopes must not overlap. Leave model empty (auto): the system
   picks the best model for the job's domain (design, code, review, text, simple work); write one only if a specific
   model is needed. Hard design/debugging: thinking=high; mechanical work: thinking=low. For dependent jobs use after
   (e.g. tests after the backend). For large changes that may touch the same files, isolation=worktree. For review/
   research only, isolation=readonly.
2. Watch: events come to you as an "[events]" message. If an agent asks a question, answer with message.
   Ask the user with ask_user only about things that depend on their preference and that you can't decide (no
   needless questions).
3. Review: {review} Read the report, look at the change with diff, run the tests if needed (run_command). If it is
   right, review decision=approve; if it is incomplete or wrong, decision=revise with concrete feedback (what is wrong,
   what you expect).
4. Finish: once everyone is approved,{" the final council convenes;" if self.council else ""} verify the whole
   (tests, build), make a small integration fix yourself if needed, then write the summary for the user with finish:
   what was done, which files, how it was verified, what is still open.

{self._design_note()}Rules:
- Don't do the agents' work yourself; you plan, steer and review.
- If there are no events, end your turn; there is no wait tool, you are woken up when a new event arrives.
- Write briefly and clearly; don't send agents needless messages.
"""

    def design_brief(self, m: Member) -> str:
        """If a design direction document exists, every agent sees it (colors, typefaces, layout from one place)."""
        f = (m.cwd if m.cwd else self.cwd) / DESIGN_FILE
        if not f.is_file():
            f = self.cwd / DESIGN_FILE
        try:
            text = f.read_text(encoding="utf-8")[:5000] if f.is_file() else ""
        except OSError:
            text = ""
        return (f"\n# Design direction ({DESIGN_FILE}) — follow it, don't make up your own colors/typefaces\n{text}\n"
                if text.strip() else "")

    def is_design(self) -> bool:
        """Is the project's look an important part of the work (website, interface)?"""
        from alice.providers import picker

        return picker.classify(self.goal) == "design" or bool(re.search(r"web ?site|internet sitesi|arayüz|landing",
                                                                         self.goal.lower()))

    def _design_note(self) -> str:
        if not self.is_design():
            return ""
        return f"""Design work: this project's success depends largely on its look. A template site that looks AI-made is
a failure, however many pages and lines of code it has. Models keep sliding into these patterns, all FORBIDDEN:
- code/terminal-style labels ("AETH-SYS // BRİFİNG-01", "SEC-02 // ..."), buttons starting with ">" ("> İNCELE"),
- counter/statistics strips ("128.400+", "%99,2", "640+"), fake technical notes ("ŞEKİL 01", "ÇİZİM NO", "REV: D"),
- a dark navy/black background with a single neon accent color, neon glow shadows, gradient text, frosted-glass cards,
- grid/scanline/radar/particle backgrounds, ALL CAPS + a monospace typeface everywhere,
- small drawings/diagrams in boxes, the same card grid in every section, emoji,
- empty slogans ("Geleceği şekillendiriyoruz", "Otonom üstünlük", "yeni nesil", "tavizsiz").
This list overrides {DESIGN_FILE}: a direction such as "a technical/military feel" is not given with these patterns.

How to do it:
- Make the first agent of the plan "tasarim" and have it start with RESEARCH (max_steps at least 70):
  1. Find 4-6 real, well-designed sites on the subject with web_search (leading firms in that sector; if needed,
     award-winning corporate sites of a similar kind).
  2. Look at each with site_study: screenshot + analysis of layout, typographic sizes, color proportions, visual
     language and tone of the text. Skip one that won't open and try another; don't count a site it hasn't seen as a
     reference.
  3. Base {DESIGN_FILE} on this research. Measures and decisions, not adjectives: "heading 72px/1.05, weight 600, at
     most 6 words (as on site X)", "8px spacing unit, 160px between sections", "85% off-white, 12% graphite, 3%
     accent", "70% of the first screen is a full-width photo". Each important decision says which reference it came
     from. Also: who we are speaking to and with what feeling, a page-by-page layout sketch, component rules, motion
     principles (little and meaningful), text tone (with example headings) and a "don't" list that includes the
     forbidden list above.
  4. Then set up the shared style base (variables, base components). The other agents wait for it with after.
- Images: a real photo is the strongest tool, not abstract decoration. Photos that fit the subject can come from
  Wikimedia Commons or Unsplash; the address must be checked with web_fetch, never a made-up URL. Without photos, use
  strong typography and space, not decorative drawings.
- Tell the page agents in their tasks: "follow the direction in {DESIGN_FILE}, run design_check before finishing".
- When reviewing, look at the appearance first: if the review note has "{SLOP_HEAD}", the work is not approved as it is. Looking like a template is a "major" problem; the company name, spelling
  and small code details come after it.
- Less but good: 3 good pages are worth more than 6 mediocre ones. Keep the number of agents in line with the job."""

    # ----------------------------------------------------------------- status
    def status_text(self) -> str:
        icon = {"queued": "…", "running": "▶", "waiting": "⏸", "asking": "?", "review": "◎", "done": "✓",
                "failed": "✗", "stopped": "■"}
        rows = []
        for m in list(self.members.values()):
            s = m.snapshot()
            rows.append(f"{icon.get(m.status, '?')} {m.name} [{m.status}, {m.model}"
                        f"{', thinking ' + m.thinking if m.thinking != 'default' else ''}, {m.steps} steps, {s['secs']} s]"
                        + (f" sonra: {','.join(m.after)}" if m.after and m.status == "queued" else "")
                        + (f" dal={m.branch}" if m.branch else "") + (f" son: {m.last}" if m.last else ""))
        open_q = [q["text"] for q in self.questions if not q.get("answer")]
        if open_q:
            rows.append("Open questions to the user: " + " | ".join(open_q))
        return "\n".join(rows) or "(no agents yet)"

    def snapshot(self) -> dict[str, Any]:
        done = sum(1 for m in list(self.members.values()) if m.status == "done")
        return {"type": "team", "id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin,
                "cwd": str(self.cwd), "created": self.created, "status": self.status(), "review": self.review,
                "summary": self.summary, "lead": {"state": self.lead_state, "model": self.lead_model,
                                                  "picked": self.picks.get(LEAD, ""),
                                                  "thinking": self.lead_thinking, "last": self.lead_last[:600]},
                "questions": self.questions[-20:], "progress": [done, len(self.members)], "council": self.council,
                "council_busy": self.council_busy, "councils": [_council_brief(c) for c in self.councils],
                "check": self.check, "lessons": self.lessons[-15:], "timeline_n": len(self.timeline),
                "pr": {"on": self.pr, "branch": self.pr_branch, "base": self.pr_base, "url": self.pr_url,
                       "number": self.pr_number},
                "usage": {"total": self.usage_total(), "by": self.usage},
                "members": [m.snapshot() for m in list(self.members.values())],
                "log": [{"from": x.sender, "to": x.to, "text": x.text[:400], "at": x.at} for x in self.log[-40:]],
                "active": self.active()}

    def _changed(self, save: bool = False) -> None:
        # Reading the state and publishing it happen under one lock: when two threads publish at the same time, an old
        # state must not land after the new one on screen and stay there (e.g. showing a finished project as "running").
        with self._pub_lock:
            try:
                self.publish(self.snapshot())
            except Exception:  # noqa: BLE001
                pass
        if save or time.monotonic() - self._saved_at > SAVE_EVERY:
            self.save()

    # ---------------------------------------------------------------- saving
    def path(self) -> Path:
        return self._dir / f"{self.id}.json"

    def save(self) -> None:
        # The content is also prepared under the lock: otherwise old content ("running") could be written over the new
        # content ("finished"), and a reopened project showed the wrong state.
        if self.deleted:
            return
        try:
            with self._save_lock:  # agents may try to save at the same time
                if self.deleted:  # it may have been deleted while waiting for the lock
                    return
                self._saved_at = time.monotonic()
                body = self._body()
                tmp = self.path().with_suffix(".json.tmp")
                tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
                _replace(tmp, self.path())
        except (OSError, TypeError, ValueError, RuntimeError):
            log.debug("proje kaydedilemedi: %s", self.id, exc_info=True)

    def _body(self) -> dict[str, Any]:
        members = []
        for m in list(self.members.values()):
            d = m.snapshot()
            d.update(result=m.result, max_steps=m.max_steps, cwd=str(m.cwd), started=m.started, ended=m.ended,
                     base_sha=m.base_sha, messages=_slim(m.messages()), councils=m.councils[-6:],
                     council_cycles=m.council_cycles,
                     originals={k: v for k, v in list(m.originals.items()) if v is None or len(v) < 200_000})
            members.append(d)
        lead_msgs = self.lead_agent.messages[1:] if self.lead_agent is not None else self.lead_saved
        return {"id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin, "cwd": str(self.cwd),
                "created": self.created, "updated": time.time(), "status": self.status(), "review": self.review,
                "lead_model": self.lead_model, "lead_thinking": self.lead_thinking, "summary": self.summary,
                "finished": self.finished, "lead_last": self.lead_last, "lead_messages": _slim(lead_msgs),
                "questions": self.questions, "members": members, "council": self.council,
                "councils": self.councils[-10:], "final_runs": self.final_runs, "plan_rejects": self.plan_rejects,
                "plan_council": self.plan_council, "plan_prev_id": self.plan_prev_id,
                "check": self.check, "lessons": self.lessons, "pr": self.pr, "pr_repo": self.pr_repo,
                "pr_base": self.pr_base, "pr_branch": self.pr_branch, "pr_top": self.pr_top, "pr_sha": self.pr_sha,
                "pr_url": self.pr_url, "pr_number": self.pr_number, "pr_session": self.pr_session, "timeline": self.timeline[-500:], "usage": self.usage,
                "log": [{"from": x.sender, "to": x.to, "text": x.text, "at": x.at} for x in self.log[-500:]]}

    @classmethod
    def load(cls, project_id: str, parent_ctx: ToolContext, max_parallel: int = 5,
             publish: Callable[[dict[str, Any]], None] | None = None) -> "Team":
        f = projects_dir() / f"{Path(project_id).name}.json"
        data = json.loads(_read_saved(f))
        team = cls(parent_ctx, data.get("goal", ""), max_parallel, publish, origin=data.get("origin", "alice"),
                   project_id=data.get("id") or f.stem, title=data.get("title", ""),
                   review=bool(data.get("review", True)), lead_model=data.get("lead_model", "brain"),
                   lead_thinking=data.get("lead_thinking", "default"),
                   cwd=Path(data["cwd"]) if data.get("cwd") else None, council=bool(data.get("council", False)),
                   pr=bool(data.get("pr")))
        for k in ("pr_repo", "pr_base", "pr_branch", "pr_top", "pr_sha", "pr_url", "pr_session"):
            setattr(team, k, str(data.get(k) or ""))
        team.pr_number = int(data.get("pr_number") or 0)
        team.councils = [c for c in data.get("councils") or [] if c.get("state") != "running"]
        team.check = str(data.get("check") or "")
        from alice.council import dedupe

        team.lessons = dedupe([str(x) for x in data.get("lessons") or []])
        team.timeline = list(data.get("timeline") or [])
        team.usage = {k: dict(v) for k, v in (data.get("usage") or {}).items() if isinstance(v, dict)}
        team.final_runs = int(data.get("final_runs") or 0)
        team.plan_rejects = int(data.get("plan_rejects") or 0)
        team.plan_council = data.get("plan_council") is not False
        team.plan_prev_id = str(data.get("plan_prev_id") or "")
        team.created = float(data.get("created") or time.time())
        team.summary = data.get("summary", "")
        team.finished = bool(data.get("finished"))
        team.lead_last = data.get("lead_last", "")
        team.lead_saved = list(data.get("lead_messages") or [])
        team.lead_state = "done" if team.finished else "idle"
        team.questions = list(data.get("questions") or [])
        for d in data.get("members") or []:
            m = Member(name=d["name"], task=d.get("task", ""), constraints=d.get("constraints", ""),
                       scope=list(d.get("scope") or []), model=d.get("model", "brain"),
                       thinking=d.get("thinking", "default"), isolation=d.get("isolation", "shared"),
                       max_steps=int(d.get("max_steps") or 60), after=list(d.get("after") or []),
                       cwd=Path(d.get("cwd") or team.cwd), branch=d.get("branch", ""), base_sha=d.get("base_sha", ""),
                       result=d.get("result", ""), steps=int(d.get("steps") or 0), reviews=int(d.get("reviews") or 0),
                       last=d.get("last", ""), started=float(d.get("started") or 0), ended=float(d.get("ended") or 0),
                       files=list(d.get("files") or []), originals=dict(d.get("originals") or {}),
                       saved_messages=list(d.get("messages") or []),
                       councils=[c for c in d.get("councils") or [] if c.get("state") != "running"],
                       council_cycles=int(d.get("council_cycles") or 0))
            # Work cut off during shutdown counts as stopped; finished, under-review and failed ones stay as they are.
            m.status = d.get("status") if d.get("status") in ("done", "failed", "review") else "stopped"
            m.ended = m.ended or team.created
            team.members[m.name] = m
            team.inbox[m.name] = []
        team.stopped = not team.finished  # a loaded unfinished project stays stopped until the user resumes it
        team.log = [Message(x.get("from", "?"), x.get("to", "?"), x.get("text", ""), float(x.get("at") or 0))
                    for x in data.get("log") or []]
        return team

    def thread_view(self, name: str) -> dict[str, Any]:
        """An agent's (or the lead's) conversation and details."""
        if name == LEAD:
            msgs = self.lead_agent.messages[1:] if self.lead_agent is not None else self.lead_saved
            return {"name": LEAD, "status": self.lead_state, "model": self.lead_model, "thinking": self.lead_thinking,
                    "task": "The project's lead: plans, answers questions, reviews finished work, summarizes the result.",
                    "isolation": "shared", "steps": 0, "secs": 0, "files": [], "scope": [], "messages": msgs,
                    "result": self.summary or self.lead_last, "after": [], "constraints": "", "branch": "",
                    "question": "", "reviews": 0, "limit_hit": False, "last": "", "councils": self.councils}
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        return {**m.snapshot(), "messages": m.messages(), "councils": m.councils}

    member_view = thread_view

    def add_member(self, spec: dict[str, Any], announce: bool = True) -> Member:
        """Add a new agent to a running or finished project, start it, and tell the others."""
        m = self.add(spec)
        self.stopped = self.finished = False
        self._nudges = 0
        if announce and len(self.members) > 1:
            others = [n for n in list(self.members) if n != m.name]
            note = f"a new agent joined the team: {m.name}: {m.task[:200]}" + (f" (yazar: {', '.join(m.scope)})" if m.scope else "")
            with self.cond:
                msg = Message(SYSTEM, "all", note)
                self.log.append(msg)
                for n in others:  # tell finished agents without waking them
                    self.inbox[n].append(msg)
        self._launch(m)
        self._changed(save=True)
        return m

    def _emit(self, text: str, kind: str = "info") -> None:
        if kind in ("ask", "done", "stall", "failed"):
            self.mark(LEAD if kind != "failed" else SYSTEM, kind, text)
            self._notify(text, kind)
        try:
            if self._live_ui:
                self.publish({"type": "project_note", "id": self.id, "title": self.title, "text": text, "kind": kind})
            else:
                self.parent_ctx.emit(f"proje: {text}")
        except Exception:  # noqa: BLE001
            pass

    def _notify(self, text: str, kind: str) -> None:
        """Desktop notification, plus ntfy to the phone if configured. In the background; errors are ignored."""
        cfg = self.parent_ctx.config
        a = cfg.agent
        if not a.get("team_notify", True):
            return
        title = {"ask": _("ALICE is asking"), "done": _("Project finished"), "stall": _("An agent got stuck"),
                 "failed": _("An agent failed")}.get(kind, "ALICE")

        def run() -> None:
            try:
                from alice.core.notify import Notification, desktop_notify

                desktop_notify(Notification(title, text[:300], "warn" if kind in ("ask", "stall", "failed") else "info",
                                            "project", kind="task"))
            except Exception:  # noqa: BLE001
                pass
            topic = str((cfg.data.get("notify") or {}).get("ntfy") or a.get("team_ntfy") or "").strip()
            if topic:
                url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
                try:
                    import httpx

                    # The title goes as a query parameter: HTTP headers cannot carry Turkish characters.
                    httpx.post(url, content=text[:1000].encode("utf-8"), timeout=10,
                               params={"title": title, "tags": "robot"})
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=run, daemon=True, name="project-notify").start()

    def stop(self, name: str | None = None) -> None:
        if name and name not in self.members:
            raise ToolError(f"projede '{name}' yok")
        targets = [self.members[name]] if name else list(self.members.values())
        self.mark(USER, "stop", _("{name} stopped", name=name) if name else _("project stopped"))
        if not name:
            self.stopped = True
            self.lead_state = "idle"
            if self.lead_agent is not None:
                self.lead_agent.stop()
            for c in list(self._live_councils):
                c.stop()
        for m in targets:
            if m.status == "queued":
                m.status = "stopped"
            m.resume_wanted = False
            if m.status in ACTIVE:
                # Also stop a member not set up yet (model choice/worktree in progress); it checks this flag on start.
                m.stop_requested = True
                m.recovering = False  # a stop by the user must not be restarted by stall recovery
                if m.agent is not None:
                    m.agent.stop()
        with self.cond:
            self.cond.notify_all()
        self._changed(save=True)

    def resume(self) -> None:
        """Resume a stopped project: stopped agents continue where they left off, and the lead wakes up."""
        self.mark(USER, "resume", _("project resumed"))
        self.stopped = False
        self._nudges = 0
        for m in list(self.members.values()):
            if m.status == "stopped":
                if m.agent is None and not m.saved_messages:
                    m.status = "queued"
                    self._launch(m)
                else:
                    self._launch(m, resume=True)
        for m in list(self.members.values()):  # councils cut off during the stop are convened again
            if m.status == "review" and self.council and m.councils and m.councils[-1].get("state") == "stopped" \
                    and not m.council_running:
                self._spawn(self._member_council, m)
        self.event("The user resumed the project. Review the state and guide the agents if needed.")
        self._maybe_final()
        self._changed(save=True)

    def save_template(self, name: str) -> dict[str, Any]:
        """Save the project as a template: goal, settings and agent definitions (not conversations)."""
        tpl = {"name": name.strip()[:60], "goal": self.goal, "review": self.review, "council": self.council,
               "plan_council": self.plan_council,
               "lead_model": self.lead_model, "lead_thinking": self.lead_thinking, "check": self.check,
               "pr": self.pr, "cwd": str(self.pr_top or self.cwd),
               "agents": [{"name": m.name, "task": m.task, "constraints": m.constraints, "scope": m.scope,
                           "model": m.model, "thinking": m.thinking, "isolation": m.isolation, "after": m.after,
                           "max_steps": m.max_steps} for m in list(self.members.values())]}
        _tpl_file(name).write_text(json.dumps(tpl, ensure_ascii=False, indent=1), encoding="utf-8")
        return tpl

    def timeline_view(self) -> list[dict[str, Any]]:
        """Timeline: events and messages in one stream, oldest first."""
        items = list(self.timeline) + [{"at": x.at, "who": x.sender, "kind": "msg", "text": f"→ {x.to}: {x.text[:300]}"}
                                       for x in self.log]
        return sorted(items, key=lambda x: x.get("at") or 0)[-400:]

    def cleanup(self) -> None:
        router = self.parent_ctx.router
        for role in self.roles:
            router.role_overrides.pop(role, None)
            router.thinking_overrides.pop(role, None)
        self.roles.clear()


class MemberUI:
    """The agent's UI: reflects events in the project state and passes permission questions to the user by agent name.
    While the model writes, it streams the text and thinking live (at most every 0.25 s; not saved to the record)."""

    LIVE_EVERY = 0.25

    def __init__(self, team: Team, member: Member):
        self.team, self.member = team, member
        self.text, self.think, self.tool = "", "", ""
        self._last = 0.0

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def _live(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < self.LIVE_EVERY:
            return
        self._last = now
        try:
            self.team.publish({"type": "team_live", "id": self.team.id, "name": self.member.name,
                               "text": self.text[-8000:], "think": self.think[-4000:], "tool": self.tool})
        except Exception:  # noqa: BLE001
            pass

    def tool_delta(self, name: str, size: int) -> None:
        """While the model writes a tool call (e.g. a big file): counts as progress; the screen shows the writing."""
        self.member.last_at, self.member.stall_warned = time.time(), False
        self.tool = _("{name} is preparing · {kb} KB", name=name, kb=size // 1000) if size >= 1000 else _("{name} is preparing", name=name)
        self._live()

    def stream_start(self) -> None:
        self.tool = ""
        self.text, self.think = "", ""
        self._live(force=True)
        self._last = 0.0  # show the first chunk without waiting

    def stream_delta(self, text: str) -> None:
        self.text += text
        self._live()

    def thinking_delta(self, text: str) -> None:
        self.think += text
        self._live()

    def stream_reset(self) -> None:
        self.text = ""
        self._live(force=True)

    def stream_end(self, text: str, source: str) -> None:
        self.text, self.think, self.tool = "", "", ""  # the answer is now in the conversation; the live box closes
        self._live(force=True)

    def info(self, msg: str) -> None:
        # The notices follow the interface language (router/agent _()), so both languages are recognised.
        if any(w in msg for w in ("adım limiti", "Adım sınırına", "step limit")):
            self.member.limit_hit = True
        elif "hiçbir model cevap veremedi" in msg or msg.startswith("No model for the"):
            reasons = [x.strip() for x in msg.splitlines()[1:] if x.strip()]  # first line holds the internal role name
            self.member.provider_failed = human_failure(reasons)

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        if result.is_error:
            self.member.last = f"{name} hata: {result.text[:90]}"
            self.team._changed()

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        parent = self.team.parent_ctx.state.get("agent")
        ask = getattr(getattr(parent, "ui", None), "ask_permission", None)
        if ask is None:
            return "n"
        real = self.member.name in self.team.members
        prev = self.member.status
        if real:
            self.member.status = "waiting"
            self.team._changed()
        try:
            with self.team.perm_lock:
                ans = ask(name, f"[{self.team.title[:30]} / {self.member.name}] {summary}", risk)
        finally:
            if real and self.member.status == "waiting":
                self.member.status = prev
                self.team._changed()
        return "y" if ans == "a" else ans  # "always" belongs to the main session, not to this one agent


# Tool classes live in team_tools.py; it uses the constants here, so it is loaded lazily (no circular import).
# (old imports such as `from alice.team import TeamSend` keep working).
_TOOL_EXPORTS = frozenset({
    "TeamSend", "DesignCheck", "SiteStudy", "TeamWait", "LeadPlan", "LeadAssign", "LeadReview", "LeadDiff",
    "LeadAskUser", "LeadFinish", "LeadStatus", "_tree", "route_plan_notes", "_check_specs",
})


def __getattr__(name: str) -> Any:
    if name in _TOOL_EXPORTS:
        from alice import team_tools

        return getattr(team_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
