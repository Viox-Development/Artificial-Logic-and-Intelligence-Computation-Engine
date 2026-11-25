"""The workflow tool: the model writes a script, the engine (alice/workflow) runs it with many subagents.

A run belongs to the chat that started it: it is listed there (ctx.state["workflows"]), saved with the session as a
snapshot, stopped by the chat's Stop, and its result comes back to that chat as a message when it ends."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alice import guard
from alice.config import alice_home
from alice.i18n import pick
from alice.tools.base import EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj

LIVE_EVERY = 0.4  # seconds: live status is published at this interval at most
KEEP = 20  # runs remembered per chat
RESULT_CHARS = 16_000  # of the result that goes into the chat; the whole of it is in the run's folder
MAX_AGENTS = 200
STOP_WAIT = 10.0  # seconds a stopped run in the foreground is waited for
MAX_SOURCE = 512_000  # characters of a script file
_ID_RX = re.compile(r"[A-Za-z0-9][\w-]{0,63}")

DESCRIPTION = (
    "Runs a workflow: a Python script you write that fans work out to many subagents deterministically, has them check "
    "each other and returns data. Runs in the background: returns a run id and the script's path at once; the result "
    "arrives as a '[Workflow wfN ...]' message, so go on or end your turn; don't poll or guess it.\n"
    "- Only if the user asked for a workflow or many agents, or ultracode is on. One or two agents: task. A small job: "
    "do it yourself.\n"
    "- Scout inline first (which files, which parts), then pass that list in args; for big jobs run one workflow per "
    "phase and read the result before the next.\n"
    "- Give exactly one of script, name, script_path. The syntax is in the script parameter.\n"
    "- To change a run: edit the file at its script_path, call with script_path and resume_from; unchanged agent() "
    "calls are replayed for free.\n"
    "- Agents share the working folder: no two write the same file at once.")

REFERENCE = '''The script: top-level async Python. First statement: META = {"name": "short-id", "description": "one \
line for the approval question", "phases": [{"title": "Find"}]} (a literal; name and description required). Then the \
body, with top-level await; end with `return value` (JSON data). No import, class, files, network, clock, randomness, \
names or attributes starting with _, str.format (f-strings are fine); json, re and the usual builtins are there.
- await agent(prompt, label=, phase=, schema=, model=, effort=, isolation=, agent_type=) -> the agent's final text, \
or the validated object if schema (JSON Schema, root type object) is given; None if it failed: filter those out. The \
agent sees only its prompt: write a complete brief (goal, paths, what to return). It has the normal tools and \
permissions and can't start agents. model and effort per call: left out, the agent works like the chat does (its model \
and effort). Spend where it matters: give mechanical stages (searching, listing, extracting, running tests, simple \
edits) effort="low" or a cheaper model ("provider:model" or a role such as "fast"), keep the chat's model for design, \
hard edits, judging and the last verification. effort: low, medium, high, xhigh, max. isolation="worktree": its own git worktree (slow; only for parallel edits of the same \
files), kept if it changed. agent_type: a custom agent.
- await parallel([lambda: agent(...), ...]): runs all, waits for all; a failed item is None. In a loop bind the \
variable: lambda f=f: agent(...).
- await pipeline(items, stage1, stage2): every item goes through the stages on its own, without waiting for the \
others; stage(prev, item, index). The default for several steps per item; parallel only if a step needs ALL earlier \
results (dedupe, compare, stop early).
- phase(title) groups what follows (inside pipeline/parallel pass phase= instead); log(text) shows the user a line; \
args; budget.total (None without a target), budget.spent(), budget.remaining(); agent() raises once the budget is \
used up.
- await workflow(name_or_path, args): another workflow inline, one level.
Limits: a few agents at once (the rest wait), 200 per run by default, 4096 items per call. Agent calls are numbered \
in the order they are made: keep that order deterministic, resume depends on it.
Quality patterns: adversarial verify (several sceptics per finding told to refute it; keep it if most of them can not); \
different lenses (correctness, security, does it reproduce); judge panel (independent attempts, judges score, best \
one improved); loop until dry (until 2 rounds find nothing new; compare with everything seen, not only confirmed); \
completeness critic (what was not checked?). Log what you cut off. Scale to the request: a quick check needs few \
agents.
Example:
META = {"name": "review", "description": "Review the changed files and verify each finding", "phases": [{"title": \
"Review"}, {"title": "Verify"}]}
BUGS = {"type": "object", "required": ["bugs"], "properties": {"bugs": {"type": "array", "items": {"type": "object", \
"required": ["title", "why"], "properties": {"title": {"type": "string"}, "why": {"type": "string"}}}}}}
VERDICT = {"type": "object", "required": ["real"], "properties": {"real": {"type": "boolean"}}}
async def review(f):
    return await agent(f"Review {f} for bugs. Read it and its callers.", label=f, phase="Review", schema=BUGS)
async def verify(found, f):
    async def one(b):
        votes = await parallel([lambda i=i: agent(f"Try to refute (angle {i}): {b['title']} in {f}: {b['why']}. Not \
sure = not real.", phase="Verify", schema=VERDICT) for i in range(3)])
        return {**b, "file": f, "real": sum(1 for v in votes if v and v["real"]) >= 2}
    return await parallel([lambda b=b: one(b) for b in found["bugs"]])
done = await pipeline(args["files"], review, verify)
return [b for r in done if r for b in r if b and b["real"]]'''


# ---------------------------------------------------------------------------------------------- runs of a chat
@dataclass
class Entry:
    """A run of this chat: live (run) or from a saved session (snap only)."""

    run: Any = None
    spawn: Any = None
    sid: str = ""
    snap: dict[str, Any] = field(default_factory=dict)
    ready: threading.Event = field(default_factory=threading.Event)  # run is set
    _last: float = 0.0
    _timer: Any = None

    @property
    def status(self) -> str:
        return self.run.status if self.run is not None else str(self.snap.get("status") or "")

    def snapshot(self) -> dict[str, Any]:
        return self.run.snapshot() if self.run is not None else self.snap


# Runs of this process by their script's path. A chat that is closed and opened again (it went to the background, a
# stuck tool made it start over) finds its running workflows here instead of only a snapshot.
_live: dict[str, Entry] = {}
_lock = threading.Lock()


def registry(ctx: ToolContext) -> dict[str, Entry]:
    return ctx.state.setdefault("workflows", {})


def snapshots(ctx: ToolContext) -> list[dict[str, Any]]:
    """Newest first."""
    return [e.snapshot() for e in reversed(list(registry(ctx).values()))]


def active(ctx: ToolContext) -> bool:
    """Does a workflow of this chat still work?"""
    return any(e.status == "running" for e in list((ctx.state.get("workflows") or {}).values()))


def stop_session(sid: str) -> None:
    """A deleted chat: nobody could see or stop its workflows any more."""
    for e in list(_live.values()):
        if sid and e.sid == sid:
            stop(e)


def any_running() -> bool:
    return any(e.status == "running" for e in list(_live.values()))


def stop_runs(ctx: ToolContext) -> int:
    """Stop every running workflow of this chat (its agents stop with it)."""
    return sum(stop(e) for e in list((ctx.state.get("workflows") or {}).values()))


def stop(entry: Entry) -> bool:
    if entry.run is None or entry.run.status != "running":
        return False
    entry.run.stop()
    return True


def live(run_id: str, sid: str) -> Entry | None:
    """A run of a chat that is not open right now but whose workflow goes on."""
    return next((e for e in list(_live.values()) if e.sid == sid and e.run is not None and e.run.id == run_id), None)


def export(ctx: ToolContext) -> list[dict[str, Any]]:
    """Runs to write to the session file, oldest first."""
    out = []
    for e in list((ctx.state.get("workflows") or {}).values())[-KEEP:]:
        s = e.snapshot()
        out.append({**s, "log": list(s.get("log") or [])[-100:]})
    return out


def restore(ctx: ToolContext, rows: list[dict[str, Any]]) -> None:
    """Runs of a saved session. One that still works in this process is picked up again; one that was running when ALICE
    closed counts as stopped (it can be resumed: finished agents are replayed from its journal)."""
    reg = registry(ctx)
    for row in revived(rows):
        reg[str(row["id"])] = _live.get(str(row.get("script_path") or "")) or Entry(snap=row)


def revived(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Saved snapshots as they are now."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        live = _live.get(str(row.get("script_path") or ""))
        if live is not None and live.run is not None:
            out.append(live.snapshot())
            continue
        row = dict(row)
        script = Path(str(row.get("script_path") or ""))
        if row.get("status") == "running":
            row["status"] = "stopped"
            row["resumable"] = (script.parent / "journal.jsonl").is_file()
            row["phases"] = [
                {**p, "agents": [{**a, "status": "stopped"} if a.get("status") in ("queued", "running") else a
                                 for a in p.get("agents") or []]} for p in row.get("phases") or []]
        row["resumable"] = bool(row.get("resumable")) and script.is_file()  # run folders can be deleted by hand
        out.append(row)
    return out


# ---------------------------------------------------------------------------------------------- scripts
def saved_dirs(cwd: Path) -> list[Path]:
    return [Path(cwd) / ".alice" / "workflows", alice_home() / "workflows" / "saved"]


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_SOURCE:
            raise ToolError(f"too large for a workflow script: {path}")
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ToolError(f"can't read the workflow script {path}: {e}") from e


def saved(cwd: Path) -> dict[str, Path]:
    """Saved workflows by META name; a project's own comes before the user's."""
    from alice.workflow.script import ScriptError, parse

    out: dict[str, Path] = {}
    for d in saved_dirs(cwd):
        for p in sorted(d.glob("*.py")) if d.is_dir() else []:
            try:
                out.setdefault(parse(_read(p)).name, p)
            except (ScriptError, ToolError):
                continue  # not a workflow, or broken: it just can't be called by name
    return out


def _local(name: str) -> Path:
    from alice.workflow.script import ScriptError, local_path

    try:
        return local_path(name)
    except (ScriptError, RuntimeError, ValueError) as e:  # RuntimeError: "~user" without a home folder
        raise ToolError(str(e)) from e


def find(cwd: Path, name: str) -> str:
    """Source of a saved workflow, or of the script at a path."""
    name = name.strip()
    path = _local(name)
    known = saved(cwd)
    if name in known:
        return _read(known[name])
    path = path if path.is_absolute() else Path(cwd) / path
    if name.endswith(".py") and path.is_file():
        return _read(path)
    raise ToolError(f"no saved workflow called {name!r}. Saved ones: {', '.join(sorted(known)) or '(none)'}")


def run_dir(run_id: str) -> Path:
    if not _ID_RX.fullmatch(run_id or ""):
        raise ToolError(f"not a run id: {run_id!r}")
    d = alice_home() / "workflows" / run_id
    if not (d / "script.py").is_file():
        raise ToolError(f"there is no workflow run {run_id}")
    return d


def source_of(ctx: ToolContext, script: str = "", name: str = "", script_path: str = "", resume_from: str = "") -> str:
    given = [k for k, v in (("script", script), ("name", name), ("script_path", script_path)) if str(v or "").strip()]
    if len(given) > 1:
        raise ToolError(f"give exactly one of script, name and script_path (got {', '.join(given)})")
    if script and script.strip():
        return script
    if name and name.strip():
        return find(ctx.cwd, name)
    if script_path and script_path.strip():
        _local(script_path)
        return _read(ctx.resolve(script_path.strip()))
    if resume_from:  # the same script again
        return _read(run_dir(resume_from.strip()) / "script.py")
    raise ToolError("give a script, the name of a saved workflow, or a script_path")


def limits(ctx: ToolContext) -> tuple[int, int | None]:
    """(most agents of a run, agents at the same time or None: the engine's default)."""
    cfg = getattr(getattr(ctx, "config", None), "agent", None) or {}

    def number(key: str, default: int) -> int:
        try:
            return int(cfg.get(key) or default)
        except (TypeError, ValueError):
            return default

    many, at_once = number("workflow_max_agents", MAX_AGENTS), number("workflow_concurrency", 0)
    return max(1, min(many, 1000)), (max(1, min(at_once, 16)) if at_once > 0 else None)


# ---------------------------------------------------------------------------------------------- starting a run
def start(ctx: ToolContext, source: str, args: Any = None, budget: int | None = None, resume_from: str = "",
          background: bool = True) -> Entry:
    """Start a run in this chat. background: its result is delivered to the chat as a message when it ends."""
    from alice.workflow.engine import launch
    from alice.workflow.script import ScriptError
    from alice.workflow.spawn import TAINT_FILE, make_spawn

    parent = ctx.state.get("agent")
    if parent is None or getattr(parent, "is_child", False):
        raise ToolError("workflows can't be started in this context")
    if getattr(getattr(parent, "permissions", None), "mode", "") == "plan":
        raise ToolError("plan mode: a workflow can't run now")
    resume_from = (resume_from or "").strip()
    if resume_from:
        folder = run_dir(resume_from)
        old = _live.get(str(folder / "script.py"))
        if old is not None and old.status == "running":
            raise ToolError(f"{resume_from} is still running; stop it first")
        if args is None:
            args = _stored_args(folder)
        if not budget:
            budget = _left(resume_from, folder)
    many, at_once = limits(ctx)
    cwd = Path(ctx.cwd)
    entry = Entry(spawn=make_spawn(parent, ctx), sid=str(ctx.state.get("session_id") or ""))
    if resume_from:  # answers that came from outside content are replayed as what they are
        entry.spawn.tainted = _text(folder / TAINT_FILE)
    publish = _publisher(parent, entry)
    deliver = _deliverer(ctx, entry.sid) if background else None
    idle = ctx.state.get("procs_idle")  # a chat kept open only for this run can close when it ends
    try:
        run = launch(source, entry.spawn, args=args, budget=budget, resume_from=resume_from,
                     on_change=lambda *_a: publish(), on_done=lambda *_a: _ended(entry, publish, deliver, idle),
                     resolve=lambda name: find(cwd, name), max_agents=many, concurrency=at_once)
    except ScriptError as e:
        raise ToolError(f"the workflow script is not valid: {e}") from e
    entry.run = run
    entry.spawn.run_id, entry.spawn.folder = run.id, run.folder
    try:
        (run.folder / "budget.json").write_text(json.dumps(budget), encoding="utf-8")
        if entry.spawn.tainted:
            (run.folder / TAINT_FILE).write_text(entry.spawn.tainted, encoding="utf-8")
    except OSError:
        pass  # only a later resume needs them
    entry.ready.set()
    reg = registry(ctx)
    reg[run.id] = entry
    with _lock:
        _live[str(run.snapshot()["script_path"])] = entry
        for table, keep in ((_live, KEEP * 5), (reg, KEEP)):
            for key in [k for k, e in list(table.items()) if e.status != "running"][:-keep]:
                table.pop(key, None)
    publish()
    return entry


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def _left(run_id: str, folder: Path) -> int | None:
    """What is left of the ceiling of the run that is resumed: resuming must not be a way around it."""
    try:
        total = json.loads(_text(folder / "budget.json") or "null")
        spent = json.loads(_text(folder / "result.json") or "{}").get("spent") or 0
    except (ValueError, AttributeError):
        return None
    if not isinstance(total, int) or total <= 0:
        return None
    left = total - int(spent)
    if left <= 0:
        raise ToolError(f"{run_id} used up its token budget ({int(spent):,} of {total:,}); to go on, give a new "
                        "budget_tokens")
    return left


def target_left(ctx: ToolContext) -> int | None:
    """What is left of the user's +500k target after the workflows that were already started for this request."""
    total = ctx.state.get("workflow_budget")
    if not total:
        return None
    left = int(total) - sum(int(e.snapshot().get("spent") or 0) for e in ctx.state.get("workflow_budget_runs") or [])
    if left <= 0:
        raise ToolError(f"the user's token target ({int(total):,}) is used up; report what there is, or ask for more")
    return left


def _stored_args(folder: Path) -> Any:
    try:
        return json.loads((folder / "args.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _publisher(parent: Any, entry: Entry) -> Any:
    """Live events to the chat that started the run, at most one per LIVE_EVERY; the last state always goes out."""
    ui = getattr(parent, "ui", None)
    bus = getattr(ui, "bus", None)
    send = (lambda ev: bus.publish({**ev, "sid": entry.sid})) if bus is not None and entry.sid else \
        getattr(ui, "publish", None)
    lock = threading.Lock()

    def now() -> None:
        with lock:
            entry._timer, entry._last = None, time.monotonic()
        try:
            send({"type": "workflow", **entry.snapshot()})
        except Exception:  # noqa: BLE001 - the work goes on without an interface
            pass

    def publish(force: bool = False) -> None:
        if not callable(send) or not entry.ready.is_set():
            return
        with lock:
            wait = LIVE_EVERY - (time.monotonic() - entry._last)
            if not force and wait > 0:
                if entry._timer is None:
                    entry._timer = threading.Timer(wait, now)
                    entry._timer.daemon = True
                    entry._timer.start()
                return
        now()

    return publish


def _deliverer(ctx: ToolContext, sid: str) -> Any:
    to_sid = ctx.state.get("deliver_to")  # (chat, text): to the chat that started it, even if the window moved on
    return (lambda text: to_sid(sid, text)) if callable(to_sid) and sid else ctx.state.get("deliver")


def _ended(entry: Entry, publish: Any, deliver: Any, idle: Any = None) -> None:
    entry.ready.wait(10)  # a very short script can end before launch() has returned
    publish(True)
    if entry.run is not None and entry.run.status != "stopped" and callable(deliver):
        # Its own thread: the chat may be at a point where it takes the message only after some tries.
        threading.Thread(target=lambda: deliver(report(entry)), daemon=True, name="alice-wf-deliver").start()
    elif callable(idle):
        threading.Thread(target=idle, daemon=True, name="alice-wf-idle").start()


def report(entry: Entry) -> str:
    """What the chat reads when a run has ended."""
    snap = entry.snapshot()
    ok = snap.get("status") == "done"
    agents = [a for p in snap.get("phases") or [] for a in p.get("agents") or []]
    bad = sum(1 for a in agents if a.get("status") in ("failed", "stopped"))
    head = (f"[Workflow {snap.get('id')} · {snap.get('name')} {'finished' if ok else snap.get('status')}]\n"
            f"{len(agents)} agents{f' ({bad} without a result)' if bad else ''}, "
            f"{int(snap.get('spent') or 0):,} tokens, {int(snap.get('secs') or 0)} s")
    if ok:
        value = getattr(entry.run, "value", None)
        body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1, default=str)
    else:
        body = f"Error: {snap.get('error') or 'unknown'}"
    folder = Path(str(snap.get("script_path") or ".")).parent
    if len(body) > RESULT_CHARS:
        body = body[:RESULT_CHARS] + f"\n... [{len(body) - RESULT_CHARS} characters more in {folder / 'result.json'}]"
    log = [str(x) for x in snap.get("log") or []][-15:]
    kept = list(getattr(entry.spawn, "kept", ()) or ())
    if log:
        body += "\n\nLog:\n- " + "\n- ".join(log)
    if kept:
        body += "\n\nWorktrees:\n- " + "\n- ".join(kept)
    tainted = str(getattr(entry.spawn, "tainted", "") or "")
    if tainted:  # agents read outside content: what they returned is data too
        body = guard.wrap(f"workflow, {tainted}", body)
    again = f"(To change or continue it: workflow script_path={snap.get('script_path')} resume_from={snap.get('id')})"
    return f"{head}\n\n{body}\n\n{again}"


# ---------------------------------------------------------------------------------------------- the tool
class Workflow(Tool):
    name = "workflow"
    description = DESCRIPTION
    parameters = obj({
        "script": {"type": "string", "description": REFERENCE},
        "name": {"type": "string", "description": "A saved workflow by its META name (<project>/.alice/workflows/*.py "
                                                  "or ~/.alice/workflows/saved/*.py)."},
        "script_path": {"type": "string", "description": "Path of a script file, e.g. one an earlier run returned."},
        "args": {"description": "Any JSON value; the script sees it as args. Pass lists and objects as JSON, not text."},
        "resume_from": {"type": "string", "description": "Id of an earlier run (wfN): its finished agent() calls are "
                                                         "replayed up to the first changed one. Alone: the same script "
                                                         "and args again."},
        "budget_tokens": {"type": "integer", "description": "Hard ceiling for the tokens of all agents. Default: the "
                                                            "user's +500k style target, otherwise none."},
        "stop": {"type": "string", "description": "Id of a running workflow of this chat to stop, instead of starting "
                                                  "one."},
    })
    risk = EXEC  # starting it; each agent's own tool calls go through their own permission checks

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("stop") else EXEC

    def summary(self, args: dict[str, Any]) -> str:
        return self._summary(args, None)

    def summary_for(self, ctx: ToolContext, args: dict[str, Any]) -> str:
        """The permission question: what the script says it does, how many agents it may start, its phases."""
        return self._summary(args, ctx)

    @staticmethod
    def _summary(args: dict[str, Any], ctx: ToolContext | None) -> str:
        if args.get("stop"):
            return f"stop {args['stop']}"
        what = str(args.get("name") or args.get("script_path") or args.get("resume_from") or "")
        try:
            from alice.workflow.script import parse

            meta = parse(source_of(ctx, str(args.get("script") or ""), str(args.get("name") or ""),
                                   str(args.get("script_path") or ""), str(args.get("resume_from") or ""))
                         if ctx is not None else str(args.get("script") or ""))
        except Exception:  # noqa: BLE001 - a broken script: running it reports what is wrong
            return what or pick("workflow script", "iş akışı betiği")
        many = limits(ctx)[0] if ctx is not None else MAX_AGENTS
        text = f"{meta.name}: {meta.description[:300]} · " + pick(f"up to {many} agents", f"en çok {many} ajan")
        phases = [str(p.get("title") or "") for p in meta.phases or []]
        if phases:
            text += " · " + pick("phases", "aşamalar") + ": " + ", ".join(phases)[:200]
        if args.get("resume_from"):
            text += f" · {args['resume_from']} ↻"
        return text

    def run(self, ctx: ToolContext, script: str = "", name: str = "", script_path: str = "", args: Any = None,
            resume_from: str = "", budget_tokens: int | None = None,
            stop: str = "") -> ToolResult:  # noqa: A002 - the names the model sees
        if stop:
            entry = registry(ctx).get(stop.strip())
            if entry is None:
                raise ToolError(f"no such workflow run in this chat: {stop}. "
                                f"Runs: {', '.join(registry(ctx)) or '(none)'}")
            return ToolResult(f"{stop} is stopping" if _stop(entry) else f"{stop} is not running ({entry.status})")
        source = source_of(ctx, script, name, script_path, resume_from)
        if isinstance(args, str) and args.lstrip()[:1] in ("{", "["):  # models like to send the JSON as a string
            try:
                args = json.loads(args)
            except ValueError:
                pass  # it is text after all
        asked = int(budget_tokens or 0)
        target = target_left(ctx)  # an explicit budget may be smaller than what is left of the user's target, not larger
        budget = min(asked, target) if asked and target else asked or target
        background = callable(_deliverer(ctx, str(ctx.state.get("session_id") or "")))
        entry = start(ctx, source, args, budget, resume_from, background)
        if ctx.state.get("workflow_budget"):
            ctx.state.setdefault("workflow_budget_runs", []).append(entry)
        run = entry.run
        where = entry.snapshot().get("script_path")
        if background:
            return ToolResult(
                f"Workflow {run.id} started in the background. Script: {where}\n"
                + (f"Token budget: {budget:,}.\n" if budget else "")
                + "Its result comes to this chat as a message when it ends; don't guess it until then. The user "
                "follows it in the Agents tab.")
        # No way to send a message later (terminal, unattended run): wait here.
        try:
            while not run.wait(0.25):
                if ctx.cancelled():
                    run.stop()
                    run.wait(STOP_WAIT)
                    break
        except BaseException:
            run.stop()
            raise
        if getattr(entry.spawn, "tainted", ""):
            guard.mark(ctx, "workflow")
        return ToolResult(report(entry), is_error=run.status == "failed")


_stop = stop  # run() has a parameter of the same name

TOOLS = [Workflow()]
