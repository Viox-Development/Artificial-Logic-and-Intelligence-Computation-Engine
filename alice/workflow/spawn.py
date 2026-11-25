"""Runs one agent() call of a workflow as a real ALICE subagent (the engine only knows the Spawn signature).

Each call gets a clean subagent with its own context, model and effort. Its tool calls go through the normal
permission checks under the label of the run, and the run's cancel event stops it. With a schema the subagent returns
its answer through the return_result tool; with isolation="worktree" it works in its own git worktree."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alice import guard
from alice.config import alice_home
from alice.subagents import LIVE_EVERY, Sub, SubUI, build_child, child_context
from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult

if TYPE_CHECKING:
    from alice.workflow.engine import AgentCall, AgentResult

# A workflow's agent starts nothing of its own (the script decides who runs), does not wait for the user, and does not
# change the mode of the chat it works for.
BLOCKED = frozenset({"workflow", "task", "subagents", "team", "council", "run_plan", "task_control", "session",
                     "code_review", "monitor", "schedule", "enter_plan_mode", "exit_plan_mode", "ask_user",
                     "next_step"})
EFFORTS = ("off", "low", "medium", "high", "xhigh", "max")
GENERAL = ("", "general", "general-purpose")
FIXES = 2  # corrective retries: an answer that does not fit the schema, or an empty one
PROVIDER_DELAYS = (10.0, 60.0)  # seconds before another try when no model answered (the router waited already)
RETURN_TOOL = "return_result"
TAINT_FILE = "outside-content.txt"  # in the run's folder: source of outside content its agents read
# Files that come from running things, not from the agent's work: a worktree holding only these counts as unchanged.
_CACHE_RX = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache|node_modules)(/|$)|\.pyc$")

BRIEF = ("\n\n[You are an agent inside a workflow{label}. Your final text IS the return value: a script receives it as "
         "it is, the user does not read it. So return only what was asked for (data, findings, what you changed), "
         "without a greeting, a preface or an offer to continue. Nobody can answer questions: decide yourself, and say "
         "what you could not do. You can't start other agents.]")
SCHEMA_BRIEF = ("\n[Return your answer by calling the {tool} tool once, when the work is finished; its parameters are "
                "the required structure. Plain text is not accepted as the answer.]")
WRAP_UP = ("You have no steps left for this. Don't start anything new: give your final answer now from what you have, "
           "and say what is missing.")
RETRY_PROVIDER = "The model call failed for a moment. Carry on from where you were and finish the task."

_git_lock = threading.Lock()  # git locks the repository's metadata while a worktree is added or removed


@dataclass
class _Box:
    """What return_result received."""

    done: bool = False
    value: Any = None
    problems: list[str] = field(default_factory=list)
    bad: int = 0


class ReturnResult(Tool):
    name = RETURN_TOOL
    description = ("Returns your final answer to the workflow in the required structure. Call it once, when the work "
                   "is finished; if it reports problems, fix the values and call it again.")
    risk = READ
    # Checked here instead of by the generic argument check: that one bounces a required field whose value is null.
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, schema: dict[str, Any], box: _Box):
        self.schema, self.box = schema, box

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.schema}}

    def summary(self, args: dict[str, Any]) -> str:
        return ""

    def run(self, ctx: ToolContext, /, **value: Any) -> ToolResult:  # "/": a field of the schema may be called ctx
        from alice.workflow.schema import check

        agent = ctx.state.get("agent")
        problems = check(self.schema, value)
        if problems:
            self.box.bad += 1
            self.box.problems = problems
            if self.box.bad > FIXES and agent is not None:
                agent.end_turn = True
            return ToolResult("The result does not fit the required structure:\n- " + "\n- ".join(problems[:20])
                              + "\nFix it and call return_result again.", is_error=True)
        self.box.done, self.box.value = True, value
        if agent is not None:
            agent.end_turn = True  # the answer is here: another model call would only say "done"
        return ToolResult("recorded")


class _UI(SubUI):
    """The agent's interface: its live status goes to the engine, its permission questions to the chat."""

    def __init__(self, sub: Sub, parent_ui: Any, progress: Callable[[dict[str, Any]], Any]):
        super().__init__(sub, parent_ui, None)
        self.progress, self.model, self.note = progress, "", ""

    def changed(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < LIVE_EVERY:
            return
        self._last = now
        try:
            self.progress({"last": self.sub.last, "steps": self.sub.steps, "tokens": tokens_of(self.sub.child),
                           "model": self.model})
        except Exception:  # noqa: BLE001 - the status must not break the work
            pass

    def stream_end(self, text: str, source: str) -> None:
        if source:
            self.model = source
        self.changed()

    def info(self, msg: str) -> None:
        self.note = msg  # the last one says why a turn ended early
        super().info(msg)


def tokens_of(child: Any) -> int:
    """Tokens an agent spent: input that did not come from the cache, plus output (like the turn budget)."""
    st = getattr(child, "stats", None) or {}
    return int(max(0, st.get("input", 0) - st.get("cached", 0)) + st.get("output", 0))


def _prompt(call: "AgentCall") -> str:
    text = call.prompt + BRIEF.format(label=f" (your name there: {call.label})" if call.label else "")
    return text + (SCHEMA_BRIEF.format(tool=RETURN_TOOL) if call.schema else "")


def _json_answer(text: str) -> Any:
    """The object in a plain-text answer (a model that wrote the JSON instead of calling return_result), else None."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        value = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class Spawner:
    """The Spawn of one run. Everything that belongs to the chat is fixed when the run starts: the chat may move to
    another folder or another conversation while its workflow goes on."""

    def __init__(self, parent: Any, ctx: ToolContext):
        self.parent = parent
        # Children are built from a frozen copy: after the chat moved to another project, its system prompt (folder,
        # AGENTS.md) must not reach agents whose tools still work in the old one.
        self.frozen = copy.copy(parent)
        self.frozen.messages = [dict(parent.messages[0])]
        self.frozen.tools = dict(parent.tools)
        self.run_id = ""  # set once the engine gave the run its id (only for labels)
        self.folder: Path | None = None  # the run's folder, set with it
        self.owner = ctx.state.get("lock_owner") or ctx
        self.ctx = child_context(ctx)
        for k in ("workflows", "workflow_budget", "set_cwd", "todos"):
            self.ctx.state.pop(k, None)
        self.frozen.ctx = self.ctx
        self.tainted = ""  # source of outside content an agent read: the result carries it on
        self.kept: list[str] = []  # worktrees that were left in place
        self._active = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ the Spawn signature
    def __call__(self, call: "AgentCall", cancel: threading.Event,
                 progress: Callable[[dict[str, Any]], None]) -> "AgentResult":
        from alice.workflow.engine import AgentResult

        if cancel.is_set():
            return AgentResult(False, error="stopped")
        if call.isolation and call.isolation != "worktree":
            return AgentResult(False, error=f"unknown isolation {call.isolation!r}: only 'worktree'")
        label = (call.label or call.prompt.split("\n")[0])[:48].strip() or "agent"
        sub = Sub(id=f"{self.run_id or 'wf'}.{call.n}", label=label, prompt=call.prompt, kind=call.agent_type or "")
        ui = _UI(sub, getattr(self.parent, "ui", None), progress)
        cctx = child_context(self.ctx)
        if self.tainted:  # what an earlier agent read may be in this one's prompt, put there by the script
            guard.mark(cctx, self.tainted)
        cctx.state["tool_groups"] = set(self.ctx.state.get("tool_groups") or ())  # its own: not the chat's tool list
        tree: _Worktree | None = None
        done = threading.Event()
        with self._lock:
            self._active += 1
        try:
            if call.isolation:
                tree = _Worktree.add(self.ctx.cwd, f"{self.run_id}-{call.n}" if self.run_id else str(call.n))
                cctx.cwd = tree.cwd
                cctx.state.pop("checkpoint", None)  # rewinding the chat must not write into the worktree
            child, steps = self._build(call, ui, cctx, tree)
            sub.child = child
            child.stop_check = cancel.is_set  # a stop that lands before the turn begins is not lost
            threading.Thread(target=_watch, args=(cancel, done, child), daemon=True, name="alice-wf-watch").start()
            res = self._work(call, child, ui, steps, cancel)
        except ToolError as e:
            res = AgentResult(False, error=str(e))
        except Exception as e:  # noqa: BLE001 - one agent's error is this call's failure, not the run's
            res = AgentResult(False, error=f"{type(e).__name__}: {e}")
        finally:
            done.set()
            self._finish(cctx)
        res.tokens = tokens_of(sub.child)
        if tree is not None:
            self._settle(tree, res, call)
        ui.changed(force=True)
        return res

    # ------------------------------------------------------------------ building the agent
    def _build(self, call: "AgentCall", ui: _UI, cctx: ToolContext, tree: "_Worktree | None") -> tuple[Any, int]:
        kind = (call.agent_type or "").strip().lower()
        if kind in GENERAL:
            from alice.skills import load_agents

            kind = kind if kind in load_agents(self.ctx.cwd) else ""
        child, steps = build_child(self.frozen, kind, ui, cctx)
        if not kind:
            steps = max(steps, int(getattr(self.parent, "max_steps", 80) or 80))
            child.role = self.parent.role if self.parent.role != "voice" else "brain"
        child.tools = {n: t for n, t in child.tools.items() if n not in BLOCKED}
        self._set_model(call, child)
        if "tool_specs" in child.__dict__:  # a custom agent's fixed tool list
            listed = child.tool_specs
            child.tool_specs = lambda: [s for s in listed() if s["function"]["name"] in child.tools]
        if tree is not None:
            child.messages[0]["content"] += (
                f"\n\n# Your working folder\nYou work in {tree.cwd}, a separate git worktree (branch {tree.branch}); "
                f"this overrides any other working folder named above. Change files only inside {tree.path}, never in "
                f"{tree.root}.")
        return child, steps

    def _set_model(self, call: "AgentCall", child: Any) -> None:
        """Model and effort for this agent alone. The router is copied: health, quota and clients stay shared, but the
        effort and model choices (and the model name shown in the status bar) of the chat are not touched."""
        src = child.router
        level = src.thinking_level(child.role)
        router = copy.copy(src)
        router.ultracode = False  # the standing order to run workflows is not for an agent of one
        router.thinking_overrides = dict(src.thinking_overrides)
        router.role_overrides = dict(src.role_overrides)
        router.last_ref = dict(src.last_ref)
        router.last_settings = dict(src.last_settings)
        router.on_event = lambda s: None
        model = (call.model or "").strip()
        if ":" in model:
            if model.split(":", 1)[0] not in src.providers_cfg:
                raise ToolError(f"unknown provider in model {model!r}")
            try:
                rest = [r for r in src.chain(child.role) if r != model]
            except Exception:  # noqa: BLE001 - no chain to fall back to
                rest = []
            child.role = "workflow"
            router.role_overrides["workflow"] = [model, *rest]
        elif model:
            if model not in child.config.roles and model not in src.role_overrides:
                raise ToolError(f"unknown model {model!r}: use provider:model or one of the roles "
                                f"{', '.join(sorted(child.config.roles))}")
            child.role = model
            level = src.thinking_level(model)
        effort = (call.effort or "").strip().lower()
        if effort and effort not in EFFORTS:
            raise ToolError(f"unknown effort {effort!r}: {', '.join(EFFORTS)}")
        router.thinking_overrides[child.role] = effort or level
        child.router = router

    # ------------------------------------------------------------------ the conversation
    def _work(self, call: "AgentCall", child: Any, ui: _UI, steps: int, cancel: threading.Event) -> "AgentResult":
        from alice.workflow.engine import AgentResult
        from alice.workflow.schema import check

        box = _Box()
        if call.schema:
            tool = ReturnResult(call.schema, box)
            child.tools[tool.name] = tool
            if "tool_specs" in child.__dict__:
                listed = child.tool_specs
                child.tool_specs = lambda: [s for s in listed() if s["function"]["name"] != RETURN_TOOL] + [tool.spec()]
        ask, limit = _prompt(call), steps
        tries = fixes = 0
        why = ""
        wrapped = False
        while True:
            text = child.run_turn(ask, max_steps=limit) or ""
            if box.done:
                return AgentResult(True, text=json.dumps(box.value, ensure_ascii=False), value=box.value)
            if cancel.is_set() or child.cancel.is_set():
                return AgentResult(False, error="stopped")
            if getattr(child, "_turn_error", False):
                # The notice for the user has three paragraphs; the second one names the models and why they failed. The
                # first failure says it best: on the next tries the models are only cooling down.
                why = why or "".join(ui.note.split("\n\n")[1:2]) or ui.note or "no model could answer"
                if tries >= len(PROVIDER_DELAYS):
                    return AgentResult(False, error=f"no model could answer. {why}")
                if cancel.wait(PROVIDER_DELAYS[tries]):
                    return AgentResult(False, error="stopped")
                tries += 1
                ask = RETRY_PROVIDER
                continue
            if call.schema:
                value = _json_answer(text)
                if value is not None:
                    box.problems = check(call.schema, value)
                    if not box.problems:
                        return AgentResult(True, text=json.dumps(value, ensure_ascii=False), value=value)
                fixes += 1
                bad = "; ".join(box.problems[:8]) or f"{RETURN_TOOL} was not called"
                if fixes + box.bad > FIXES:
                    return AgentResult(False, text=text, error=f"no valid result: {bad}")
                ask = (f"Your answer was not accepted ({bad}). Call {RETURN_TOOL} now with the complete result in the "
                       "required structure.")
                limit = 6
                continue
            if child.halted and not wrapped:
                wrapped, ask, limit = True, WRAP_UP, 3
                continue
            if text.strip():
                return AgentResult(True, text=text)
            fixes += 1
            if fixes > FIXES:
                return AgentResult(False, error=ui.note or "the agent returned an empty answer")
            ask = "Your answer was empty. Write the result now."

    # ------------------------------------------------------------------ afterwards
    def _finish(self, cctx: ToolContext) -> None:
        with self._lock:
            self._active -= 1
            idle = self._active == 0
            source = guard.tainted(cctx)
            fresh = bool(source) and not self.tainted
            self.tainted = self.tainted or source
        if fresh and self.folder is not None:
            try:  # a resumed run replays these answers: it has to know where they came from
                (self.folder / TAINT_FILE).write_text(self.tainted, encoding="utf-8")
            except OSError:
                pass
        # It may have taken the screen lock in the chat's name: given back when no agent of the run works any more and
        # the chat itself is idle (a working chat releases it at the end of its turn).
        lock = cctx.state.get("desktop_lock")
        if idle and lock is not None and not getattr(self.parent, "busy", False):
            lock.release(self.owner)

    def _settle(self, tree: "_Worktree", res: "AgentResult", call: "AgentCall") -> None:
        where = tree.finish()
        if not where:
            return
        with self._lock:
            self.kept.append(where)
        res.text = f"{res.text}\n\n[{where}]".strip()
        if isinstance(res.value, dict) and call.schema:
            # The script sees only the value: tell it there too when the schema allows another field.
            if "worktree" not in (call.schema.get("properties") or {}) \
                    and call.schema.get("additionalProperties", True) is True:
                res.value = {**res.value, "worktree": where}
        if res.error:
            res.error += f" [{where}]"


def _watch(cancel: threading.Event, done: threading.Event, child: Any) -> None:
    while not done.wait(0.1):
        if cancel.is_set():
            child.stop()
            return


# ---------------------------------------------------------------------- git worktree
def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    from alice.tools.github import run_git

    return run_git(args, cwd, check=False)


def _stash(root: Path) -> str:
    code, out = _git(["rev-parse", "--verify", "--quiet", "refs/stash"], root)
    return out.strip() if code == 0 else ""


@dataclass
class _Worktree:
    root: Path  # the chat's repository
    path: Path
    cwd: Path  # the folder in the worktree that matches the chat's folder
    branch: str
    base: str  # the commit it starts from
    stash: str = ""  # refs/stash when it was opened

    @classmethod
    def add(cls, cwd: Path, name: str) -> "_Worktree":
        code, out = _git(["rev-parse", "--show-toplevel"], cwd)
        root = Path(out.splitlines()[-1]) if code == 0 and out else None
        if root is None or not root.is_dir():
            raise ToolError(f"isolation='worktree' needs a git repository; {cwd} is not in one")
        code, out = _git(["rev-parse", "--verify", "HEAD"], root)
        if code != 0:
            raise ToolError("isolation='worktree' needs a repository with at least one commit")
        base = out.split()[0]
        path = alice_home() / "worktrees" / f"wf-{name}-{secrets.token_hex(3)}"
        branch = f"alice/{path.name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with _git_lock:
            code, out = _git(["worktree", "add", "-b", branch, str(path), base], root)
        if code != 0:
            raise ToolError(f"could not open a worktree: {out[-300:]}")
        try:
            rel = os.path.relpath(Path(cwd).resolve(), root.resolve())
        except ValueError:  # another drive (subst, junction): the worktree's own root
            rel = "."
        inside = path / rel
        return cls(root, path, inside if not rel.startswith("..") and inside.is_dir() else path, branch, base,
                   _stash(root))

    def changed(self) -> bool:
        """Anything in it that is not in the base commit? When in doubt, yes."""
        code, head = _git(["rev-parse", "HEAD"], self.path)
        if code != 0 or head.split()[:1] != [self.base]:
            return True
        # The branch can hold commits that the agent left by checking out something else.
        code, tip = _git(["rev-parse", f"refs/heads/{self.branch}"], self.root)
        if code == 0 and tip.split()[:1] != [self.base]:
            return True
        # Commits it made and left without a branch (detached HEAD): only the worktree's own reflog still names them.
        code, out = _git(["reflog", "show", "--format=%H", "HEAD"], self.path)
        if code != 0:
            return True
        seen = [h for h in dict.fromkeys(out.split()) if re.fullmatch(r"[0-9a-f]{40,64}", h)][:500]
        if seen:
            code, out = _git(["rev-list", "--max-count=1", *seen, "--not", self.base, "--branches", "--tags", "--remotes"],
                             self.path)
            if code != 0 or out.strip():
                return True
        if _stash(self.root) != self.stash:  # a stash holds changes too (perhaps another agent's: keep it then)
            return True
        code, out = _git(["ls-files", "-v"], self.path)  # S, lower case: edits git was told not to look at
        if code != 0 or any(row[:1] == "S" or row[:1].islower() for row in out.splitlines()):
            return True
        code, out = _git(["-c", "core.quotepath=false", "status", "--porcelain", "--ignored", "--untracked-files=all"],
                         self.path)
        if code != 0:
            return True
        return any(not (row[:2] in ("!!", "??") and _CACHE_RX.search(row[3:].strip('"')))
                   for row in out.splitlines() if row.strip())

    def finish(self) -> str:
        """Remove an unchanged worktree; a changed one stays, and where it is comes back as text."""
        try:
            if not self.changed():
                with _git_lock:
                    # --force only for the caches: git refuses to remove a worktree that holds untracked files.
                    code, _out = _git(["worktree", "remove", "--force", str(self.path)], self.root)
                    if code == 0:  # the branch goes only while it still points at the base commit
                        _git(["update-ref", "-d", f"refs/heads/{self.branch}", self.base], self.root)
                        return ""
                return f"unchanged worktree could not be removed: {self.path}" if self.path.exists() else ""
        except (ToolError, OSError):
            pass  # could not tell: keep it
        return f"changes kept in worktree {self.path}, branch {self.branch}"


def make_spawn(parent_agent: Any, ctx: ToolContext) -> Spawner:
    return Spawner(parent_agent, ctx)
