"""Agent evaluation: run ALICE on small real tasks with real models and score the result automatically.

Each task in evals/tasks/<id>/ is a tiny project (repo/), a goal (task.toml) and automatic checks. The runner copies
repo/ to a temporary folder, gives ALICE the goal, lets it work with its normal tools, then copies hidden/ over the
result (original tests, check scripts the agent never saw) and runs the checks. It records pass/fail, steps, tokens,
time and model switches, and writes results.json plus a Markdown scoreboard.

With --council both (the default) every task runs twice: once plain, once with the review council looking at the
result (and one fix turn if the council finds a blocking problem), so the scoreboard shows whether the council helps.

    python -m evals --models gemini:gemini-3.5-flash,groq:openai/gpt-oss-120b
    python -m evals --only log --council off
    python -m evals --list

See evals/README.md for costs and how to add a task.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

HERE = Path(__file__).resolve().parent
TASKS_DIR = HERE / "tasks"
RESULTS_DIR = HERE / "results"

# Tools that reach outside the throwaway task folder (the user's desktop, accounts, memory, GitHub, schedules) or
# need a person to answer. The agent keeps its file, shell, search, web and planning tools.
EXCLUDED_TOOLS = {
    "memory", "ask_user", "send_file", "share", "hooks", "permissions", "notify", "process_kill",
    "app", "clipboard", "computer", "computer_task", "game", "reflex", "ui_act", "ui_elements", "windows",
    "browser", "preview", "buddy", "kairos", "action_log", "feeds", "rules", "schedule", "blender", "make_tool",
    "now", "timeline", "dcs_wingman", "flight", "council", "team", "feedback", "media", "notes", "phone_call",
    "report", "weather", "youtube", "projects", "gmail", "android", "pc", "tv", "wifi", "github", "code_review",
}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".alice", "node_modules", ".venv"}

PREFACE = """[Evaluation task. You are working alone in the project folder {cwd}; nobody will answer questions, so do
not ask any. Work only inside this folder and do not change global settings. Finish the task completely, verify
it (for example by running the tests), then give a short final reply.]

"""

FIX_PROMPT = """The review council examined your work on this task and found problems that need fixing:

{fixes}

Fix what is really wrong (verify each point yourself; ignore a point if it is mistaken), check the result again,
and give a short final reply."""


# --------------------------------------------------------------------------------------------------------- tasks
@dataclass
class Task:
    id: str
    category: str
    goal: str
    checks: list[dict[str, Any]]
    root: Path
    needs: list[str] = field(default_factory=list)
    max_steps: int = 40
    timeout: float = 600.0


def load_tasks(root: Path = TASKS_DIR, only: list[str] | None = None) -> list[Task]:
    tasks = []
    for d in sorted(p for p in root.iterdir() if (p / "task.toml").is_file()):
        data = tomllib.loads((d / "task.toml").read_text(encoding="utf-8"))
        t = Task(id=d.name, category=str(data.get("category", "other")), goal=str(data["goal"]).strip(),
                 checks=list(data.get("check", [])), root=d, needs=list(data.get("needs", [])),
                 max_steps=int(data.get("max_steps", 40)), timeout=float(data.get("timeout", 600)))
        if not t.checks:
            raise ValueError(f"{d.name}: task.toml has no [[check]]")
        if only and not any(o.lower() in f"{t.id} {t.category}".lower() for o in only):
            continue
        tasks.append(t)
    return tasks


# -------------------------------------------------------------------------------------------------------- checks
# Checks start pytest many times; loading every installed plugin made each start take seconds.
def _check_env() -> dict[str, str]:
    return {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _argv(cmd: str) -> list[str]:
    argv = shlex.split(cmd, posix=True)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable  # the same interpreter as the runner (has pytest), not whatever is on PATH
    return argv


def _files(work: Path, pattern: str) -> list[Path]:
    if not any(c in pattern for c in "*?["):
        p = work / pattern
        return [p] if p.is_file() else []
    out = []
    for p in sorted(work.rglob("*")):
        rel = p.relative_to(work).as_posix()
        if p.is_file() and not (set(p.relative_to(work).parts) & SKIP_DIRS) and (
                fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.removeprefix("**/"))):
            out.append(p)
    return out


def check_command(chk: dict[str, Any], work: Path, reply: str, tools: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(_argv(chk["run"]), cwd=work, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=float(chk.get("timeout", 180)), env=_check_env())
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except FileNotFoundError as e:
        return False, f"cannot run: {e}"
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    if r.returncode != int(chk.get("exit", 0)):
        return False, f"exit {r.returncode}: {out.strip()[-400:]}"
    pat = chk.get("output")
    if pat and not re.search(pat, r.stdout or "", re.M):
        return False, f"output does not match /{pat}/: {(r.stdout or '').strip()[-300:]}"
    return True, (r.stdout or "").strip()[-200:]


def check_file(chk: dict[str, Any], work: Path, reply: str, tools: list[str]) -> tuple[bool, str]:
    files = _files(work, chk["path"])
    rx = re.compile(chk["pattern"], re.M)
    hits = {}
    for p in files:
        n = len(rx.findall(p.read_text(encoding="utf-8", errors="replace")))
        if n:
            hits[p.relative_to(work).as_posix()] = n
    total = sum(hits.values())
    if chk.get("absent"):
        return (not hits), (f"/{chk['pattern']}/ still found in: {hits}" if hits else "absent")
    if not files:
        return False, f"{chk['path']}: no such file"
    if total < int(chk.get("min_count", 1)):
        return False, f"/{chk['pattern']}/ not found in {chk['path']}"
    if "max_count" in chk and total > int(chk["max_count"]):
        return False, f"/{chk['pattern']}/ found {total} times (max {chk['max_count']})"
    return True, f"{total} match(es)"


def check_reply(chk: dict[str, Any], work: Path, reply: str, tools: list[str]) -> tuple[bool, str]:
    found = re.search(chk["pattern"], reply or "", re.I | re.M) is not None
    if chk.get("absent"):
        return not found, "absent" if not found else f"reply contains /{chk['pattern']}/"
    return found, "found" if found else f"reply lacks /{chk['pattern']}/"


def check_tool(chk: dict[str, Any], work: Path, reply: str, tools: list[str]) -> tuple[bool, str]:
    want = list(chk.get("any", []))
    used = [t for t in want if t in tools]
    return bool(used), f"used {used}" if used else f"none of {want} was called"


CHECKS: dict[str, Callable[..., tuple[bool, str]]] = {
    "command": check_command, "file_contains": check_file, "reply": check_reply, "tool_used": check_tool}


def run_checks(task: Task, work: Path, reply: str, tools: list[str]) -> list[dict[str, Any]]:
    hidden = task.root / "hidden"
    if hidden.is_dir():
        shutil.copytree(hidden, work, dirs_exist_ok=True)
    out = []
    for chk in task.checks:
        kind = chk.get("type", "")
        label = chk.get("run") or chk.get("path") or chk.get("pattern") or ",".join(chk.get("any", []))
        try:
            ok, detail = CHECKS[kind](chk, work, reply, tools)
        except Exception as e:  # noqa: BLE001 - a broken check fails the task, not the run
            ok, detail = False, f"check error {type(e).__name__}: {e}"
        out.append({"type": kind, "what": str(label)[:80], "ok": ok, "detail": detail[:500]})
    return out


# ------------------------------------------------------------------------------------------------------ metering
class Meter:
    """Counts every model request the router makes (agent and council members), by phase."""

    def __init__(self, router: Any):
        self.phase = "agent"
        self.calls: list[dict[str, Any]] = []
        self.events: list[str] = []
        self._lock = threading.Lock()
        orig = router.usage.record

        def record(ref: str, usage: dict[str, Any], ok: bool, _orig=orig) -> None:
            u = usage or {}
            with self._lock:
                self.calls.append({"phase": self.phase, "ref": ref, "ok": ok,
                                   "in": int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
                                   "out": int(u.get("completion_tokens") or u.get("output_tokens") or 0)})
            _orig(ref, usage, ok)

        router.usage.record = record
        prev = router.on_event
        router.on_event = lambda s, _p=prev: (self.events.append(s), _p(s))

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
            self.events.clear()
        self.phase = "agent"

    def summary(self) -> dict[str, Any]:
        agent = [c for c in self.calls if c["phase"] != "council"]
        council = [c for c in self.calls if c["phase"] == "council"]
        served = [c["ref"] for c in agent if c["ok"]]
        switches = sum(1 for a, b in zip(served, served[1:]) if a != b)
        return {
            "model_calls": len([c for c in agent if c["ok"]]),
            "failed_calls": len([c for c in self.calls if not c["ok"]]),
            "tokens_in": sum(c["in"] for c in agent), "tokens_out": sum(c["out"] for c in agent),
            "council_calls": len([c for c in council if c["ok"]]),
            "council_tokens": sum(c["in"] + c["out"] for c in council),
            "models": dict(Counter(c["ref"] for c in self.calls if c["ok"])),
            "model_switches": switches,
            "failovers": sum(1 for e in self.events if "modeline geçildi" in e),
        }


class RecUI:
    """Headless UI: records tool calls and answers permission questions (yes, except sending messages)."""

    def __init__(self) -> None:
        self.tools: list[str] = []
        self.asked: list[str] = []
        self.denied: list[str] = []

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.tools.append(name)

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        self.asked.append(name)
        if risk == "send":
            self.denied.append(name)
            return "n"
        return "y"


def restrict_models(router: Any, chains: dict[str, list[str]]) -> None:
    """Every role the run uses (agent roles and council members) gets its model order from `chains`:
    {"*": [...]} for all, or per role ("brain", "fast", "vision", "council"). Roles without an entry keep the
    user's configuration. This is what keeps an eval from wandering onto paid or subscription models."""
    if not chains:
        return
    orig = router.chain

    def chain(role: str, _orig=orig) -> list[str]:
        key = "council" if role.startswith("council:") else role
        pick = chains.get(key) or chains.get("*")
        return list(pick) if pick else _orig(role)

    router.chain = chain


# ---------------------------------------------------------------------------------------------------- workspace
def snapshot(work: Path) -> dict[str, str]:
    out = {}
    for p in work.rglob("*"):
        if not p.is_file() or set(p.relative_to(work).parts) & SKIP_DIRS or p.stat().st_size > 300_000:
            continue
        try:
            out[p.relative_to(work).as_posix()] = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return out


def diff(before: dict[str, str], after: dict[str, str], limit: int = 30000) -> str:
    parts = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        parts.extend(difflib.unified_diff((a or "").splitlines(True), (b or "").splitlines(True),
                                          f"a/{name}" if a is not None else "/dev/null",
                                          f"b/{name}" if b is not None else "/dev/null"))
    text = "".join(parts)
    return text if len(text) <= limit else text[:limit] + "\n... (diff cut)"


# ---------------------------------------------------------------------------------------------------------- run
@dataclass
class Settings:
    council: bool = False
    mode: str = "auto"
    max_steps: int | None = None
    timeout: float | None = None
    keep: bool = False
    transcripts: Path | None = None


def make_agent(cfg: Any, router: Any, work: Path, mode: str, ui: RecUI):
    from alice.agent import Agent
    from alice.permissions import Permissions
    from alice.tools import ToolContext, all_tools

    ctx = ToolContext(cwd=work, config=cfg, router=router, decider=None, emit=lambda s: None)
    tools = [t for t in all_tools(cfg, ctx.emit) if t.name not in EXCLUDED_TOOLS]
    # Empty rule set: the user's own allow/deny rules must not change the result.
    perms = Permissions(mode, rules={"allow": [], "ask": [], "deny": []})
    agent = Agent(cfg, router, tools, ctx, perms, ui)
    agent.stream = False
    return agent, ctx


def council_review(ctx: Any, task: Task, work: Path, reply: str, before: dict[str, str],
                   on_council: Callable[[Any], None]) -> dict[str, Any]:
    from alice.council import Council

    context = (f"# Goal\n{task.goal}\n\n# The agent's final reply\n{reply[:4000] or '(empty)'}\n\n"
               f"# Changes the agent made (unified diff)\n```diff\n{diff(before, snapshot(work)) or '(no changes)'}\n```")
    c = Council(ctx, "work", task.goal, context, cwd=work)
    on_council(c)
    return c.run()


def run_task(task: Task, cfg: Any, router: Any, meter: Meter, s: Settings, log: Callable[[str], None] = print
             ) -> dict[str, Any]:
    from alice.tools import cleanup

    work = Path(tempfile.mkdtemp(prefix=f"alice-eval-{task.id}-")).resolve()
    shutil.copytree(task.root / "repo", work, dirs_exist_ok=True)
    before = snapshot(work)
    meter.reset()
    ui = RecUI()
    agent, ctx = make_agent(cfg, router, work, s.mode, ui)
    steps = s.max_steps or task.max_steps
    limit = s.timeout or task.timeout
    live: dict[str, Any] = {}
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        agent.stop()
        if live.get("council") is not None:
            live["council"].stop()

    timer = threading.Timer(limit, on_timeout)
    timer.daemon = True
    t0 = time.monotonic()
    timer.start()
    reply, error, council = "", "", None
    try:
        reply = agent.run_turn(PREFACE.format(cwd=work) + task.goal, max_steps=steps) or ""
        if s.council and not timed_out.is_set():
            from alice.council import blocking, fix_list

            meter.phase = "council"
            snap = council_review(ctx, task, work, reply, before, lambda c: live.__setitem__("council", c))
            res = snap.get("result") or {}
            council = {"decision": res.get("decision"), "score": round(float(res.get("score") or 0), 2),
                       "findings": len([f for f in snap.get("findings", []) if f.get("status") != "refuted"]),
                       "blocking": blocking(snap), "fix_round": False, "error": res.get("error", "")}
            if council["blocking"] and not timed_out.is_set():
                meter.phase = "fix"
                council["fix_round"] = True
                again = agent.run_turn(FIX_PROMPT.format(fixes=fix_list(snap)), max_steps=steps) or ""
                reply = again or reply
    except Exception as e:  # noqa: BLE001 - one broken run must not stop the set
        error = f"{type(e).__name__}: {e}"
    finally:
        timer.cancel()
        seconds = time.monotonic() - t0
        cleanup(ctx)
    checks = run_checks(task, work, reply, ui.tools)
    passed = all(c["ok"] for c in checks) and not error
    m = meter.summary()
    # No model ever answered the agent (quota gone, every model too small or down): this says nothing about ALICE,
    # so the run is reported apart and left out of the pass rate.
    no_model = m["model_calls"] == 0
    row = {"task": task.id, "category": task.category, "council": s.council, "passed": passed, "no_model": no_model,
           "checks": checks, "seconds": round(seconds, 1), "steps": m["model_calls"], "tool_calls": len(ui.tools),
           "tools": dict(Counter(ui.tools)), "permission_asks": len(ui.asked), "denied": ui.denied,
           "timed_out": timed_out.is_set(), "error": error, "reply": reply[:2000], "council_review": council, **m}
    if s.transcripts is not None:
        s.transcripts.mkdir(parents=True, exist_ok=True)
        name = f"{task.id}-{'council' if s.council else 'plain'}.json"
        msgs = [{k: v for k, v in mm.items() if not k.startswith("_")} for mm in agent.messages[1:]]
        (s.transcripts / name).write_text(json.dumps(msgs, ensure_ascii=False, indent=1, default=str),
                                          encoding="utf-8")
    if s.keep:
        row["workdir"] = str(work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    mark = "PASS" if passed else ("NO MODEL" if no_model else "FAIL")
    log(f"  {mark} {task.id} [{'council' if s.council else 'plain'}] {seconds:.0f}s, {m['model_calls']} steps, "
        f"{m['tokens_in'] + m['tokens_out']} tok"
        + (f", council {council['decision']}{' + fix' if council['fix_round'] else ''}" if council else "")
        + (f" ({error})" if error else "")
        + ("" if passed else " - " + "; ".join(c["detail"][:120] for c in checks if not c["ok"])))
    return row


# ---------------------------------------------------------------------------------------------------- scoreboard
def _ktok(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    skipped = [r for r in rows if r.get("no_model")]
    rows = [r for r in rows if not r.get("no_model")]
    if not rows:
        return {"runs": 0, "no_model": len(skipped)} if skipped else {}
    n = len(rows)
    return {"runs": n, "no_model": len(skipped), "passed": sum(r["passed"] for r in rows), "pass_rate": round(sum(r["passed"] for r in rows) / n, 3),
            "avg_steps": round(sum(r["steps"] for r in rows) / n, 1),
            "avg_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1),
            "tokens": sum(r["tokens_in"] + r["tokens_out"] for r in rows),
            "council_tokens": sum(r["council_tokens"] for r in rows),
            "seconds": round(sum(r["seconds"] for r in rows), 1),
            "model_switches": sum(r["model_switches"] for r in rows),
            "failed_calls": sum(r["failed_calls"] for r in rows),
            "timeouts": sum(r["timed_out"] for r in rows), "errors": sum(bool(r["error"]) for r in rows)}


def scoreboard(result: dict[str, Any]) -> str:
    rows = result["runs"]
    plain = [r for r in rows if not r["council"]]
    with_c = [r for r in rows if r["council"]]
    meta = result["meta"]
    out = [f"# ALICE agent eval: {meta['started']}", "",
           f"- Models: {meta['models']}",
           f"- Tasks: {meta['tasks']} | permission mode: `{meta['mode']}` | ALICE {meta['alice_version']} "
           f"({meta['commit'] or 'no git'}) | Python {meta['python']} on {meta['platform']}", "",
           "## Summary", "",
           ("| Mode | Passed | Pass rate | Avg steps | Avg tool calls | Agent tokens | Council tokens | Time | "
            "Model switches | Failed model calls |"),
           "|---|---|---|---|---|---|---|---|---|---|"]
    for label, rs in (("council off", plain), ("council on", with_c)):
        sm = summarize(rs)
        if not sm.get("runs"):
            if sm:
                out.append(f"| {label} | - | - | - | - | - | - | - | - | - |")
            continue
        out.append(f"| {label} | {sm['passed']}/{sm['runs']} | {sm['pass_rate']:.0%} | {sm['avg_steps']} | "
                   f"{sm['avg_tool_calls']} | {_ktok(sm['tokens'])} | {_ktok(sm['council_tokens'])} | "
                   f"{sm['seconds'] / 60:.1f} min | {sm['model_switches']} | {sm['failed_calls']} |")
    skipped = [r for r in rows if r.get("no_model")]
    if skipped:
        note = (f"**{len(skipped)} run(s) got no answer from any model** (quota used up, requests too large for "
                "every model in the chain, or providers down). They say nothing about ALICE and are not counted "
                "above; rerun them with another chain: ")
        out += ["", note + ", ".join(sorted({r['task'] for r in skipped})) + "."]
    out += ["", "## Per task", ""]
    both = bool(plain and with_c)
    if both:
        out += [("| Task | Category | Council off | Council on | Steps off/on | Tokens off/on | Time off/on | "
                 "Council verdict |"), "|---|---|---|---|---|---|---|---|"]
        on = {r["task"]: r for r in with_c}
        for r in plain:
            c = on.get(r["task"])
            if not c:
                continue
            tok_c = c["tokens_in"] + c["tokens_out"] + c["council_tokens"]
            out.append(f"| {r['task']} | {r['category']} | {_pf(r)} | {_pf(c)} | {r['steps']}/{c['steps']} | "
                       f"{_ktok(r['tokens_in'] + r['tokens_out'])}/{_ktok(tok_c)} | "
                       f"{r['seconds']:.0f}s/{c['seconds']:.0f}s | {_verdict(c)} |")
        pairs = [(r, on[r["task"]]) for r in plain
                 if r["task"] in on and not r.get("no_model") and not on[r["task"]].get("no_model")]
        fixed = [r["task"] for r, c in pairs if not r["passed"] and c["passed"]]
        broke = [r["task"] for r, c in pairs if r["passed"] and not c["passed"]]
        counted = [c for _, c in pairs]
        fix_turns = [c for c in counted if (c.get("council_review") or {}).get("fix_round")]
        useful = [c["task"] for c in fix_turns if c["passed"]]
        failed = [c["task"] for c in counted if (c.get("council_review") or {}).get("error")]
        if pairs:
            so, sw = summarize([r for r, _ in pairs]), summarize(counted)
            out += ["", "## Does the council help?", "",
                    (f"- Over the {len(pairs)} task(s) where both runs got a model: passed {so['passed']} without "
                     f"the council, {sw['passed']} with it."),
                    f"- Passed only with the council: {', '.join(fixed) or 'none'}.",
                    f"- Passed only without the council: {', '.join(broke) or 'none'}.",
                    (f"- The council asked for a fix turn in {len(fix_turns)} of {len(counted)} runs; "
                     f"{len(useful)} of those ended passing ({', '.join(useful) or '-'})."),
                    f"- The council itself could not finish (model errors) in {len(failed)} run(s)"
                    + (f": {', '.join(failed)}." if failed else "."),
                    (f"- Cost: {_ktok(sw['tokens'] + sw['council_tokens'])} tokens with vs {_ktok(so['tokens'])} "
                     f"without; time {sw['seconds'] / 60:.1f} vs {so['seconds'] / 60:.1f} min."),
                    ("- One run per task and mode: a single flip can be model noise, look at the totals over several "
                     "runs.")]
    else:
        out += ["| Task | Category | Result | Steps | Tool calls | Tokens | Time | Models |",
                "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            out.append(f"| {r['task']} | {r['category']} | {_pf(r)} | {r['steps']} | {r['tool_calls']} | "
                       f"{_ktok(r['tokens_in'] + r['tokens_out'] + r['council_tokens'])} | {r['seconds']:.0f}s | "
                       f"{', '.join(r['models']) or '-'} |")
    fails = [r for r in rows if not r["passed"] and not r.get("no_model")]
    if fails:
        out += ["", "## Failures", ""]
        for r in fails:
            why = r["error"] or "; ".join(c["detail"].replace("\n", " ")[:160] for c in r["checks"] if not c["ok"])
            out.append(f"- `{r['task']}` ({'council' if r['council'] else 'plain'}"
                       f"{', timed out' if r['timed_out'] else ''}): {why}")
    return "\n".join(out) + "\n"


def _verdict(c: dict[str, Any]) -> str:
    cr = c.get("council_review") or {}
    if not cr:
        return "-"
    if cr.get("error"):
        return "council failed (model errors)"
    return f"{cr.get('decision')} ({cr.get('findings')} findings{', fix turn' if cr.get('fix_round') else ''})"


def _pf(r: dict[str, Any]) -> str:
    if r.get("no_model"):
        return "no model"
    return "pass" if r["passed"] else ("**timeout**" if r["timed_out"] else "**fail**")



# ---------------------------------------------------------------------------------------------------------- CLI
STOP_AFTER_NO_MODEL = 3  # consecutive runs without any model answer: the chain is used up, stop instead of skimming


class ChainExhausted(Exception):
    pass

def parse_chains(models: str | None, roles: list[str] | None) -> dict[str, list[str]]:
    chains: dict[str, list[str]] = {}
    if models:
        chains["*"] = [m.strip() for m in models.split(",") if m.strip()]
    for spec in roles or []:
        role, _, refs = spec.partition("=")
        if not role or not refs:
            raise SystemExit(f"--role wants ROLE=provider:model[,provider:model...], got {spec!r}")
        chains[role.strip()] = [m.strip() for m in refs.split(",") if m.strip()]
    for refs in chains.values():
        for ref in refs:
            if ":" not in ref:
                raise SystemExit(f"model refs look like provider:model, got {ref!r}")
    return chains


def prepare_env(scratch: Path) -> None:
    """The agent's shell should find the runner's Python (with pytest) as `python`, and `git config --global`
    inside a task must not touch the user's real git config."""
    bindir = Path(sys.executable).parent
    extra = [str(bindir), str(bindir / "Scripts")] if sys.platform == "win32" else [str(bindir)]
    os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
    gitcfg = scratch / "gitconfig"
    gitcfg.touch()
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitcfg)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals", description="Run ALICE on the agent eval tasks.")
    ap.add_argument("--models", help="model chain for every role, e.g. gemini:gemini-3.5-flash,groq:openai/gpt-oss-120b")
    ap.add_argument("--role", action="append", metavar="ROLE=MODELS",
                    help="chain for one role (brain, fast, vision, council); repeatable; overrides --models")
    ap.add_argument("--council", choices=["off", "on", "both"], default="both",
                    help="run without the review council, with it, or both and compare (default: both)")
    ap.add_argument("--only", action="append", help="run tasks whose id or category contains this (repeatable)")
    ap.add_argument("--mode", choices=["auto", "yolo"], default="auto", help="ALICE permission mode (default auto)")
    ap.add_argument("--max-steps", type=int, help="override each task's step limit")
    ap.add_argument("--timeout", type=float, help="override each task's time limit (seconds)")
    ap.add_argument("--out", type=Path, help="result folder (default evals/results/<timestamp>)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary task folders for inspection")
    ap.add_argument("--list", action="store_true", help="list the tasks and exit")
    ap.add_argument("--render", type=Path, metavar="RESULTS_JSON", help="only re-render the scoreboard of a result")
    a = ap.parse_args(argv)

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass  # not a real console (pipe or IDE): keep its encoding, output still works
    if a.render:
        result = json.loads(a.render.read_text(encoding="utf-8"))
        md = a.render.with_name("scoreboard.md")
        md.write_text(scoreboard(result), encoding="utf-8")
        print(md.read_text(encoding="utf-8"))
        return 0
    tasks = load_tasks(only=a.only)
    if a.list:
        for t in tasks:
            print(f"{t.id:26} {t.category:12} {len(t.checks)} checks  {t.goal.splitlines()[0][:70]}")
        return 0
    if not tasks:
        print("no tasks match")
        return 2

    from alice import __version__
    from alice.config import load_config
    from alice.providers.router import Router

    chains = parse_chains(a.models, a.role)
    cfg = load_config(cwd=HERE)
    router = Router(cfg)
    router._probe_checked = True  # no background re-measuring of every model during the eval
    restrict_models(router, chains)
    meter = Meter(router)
    brain = router.chain("brain")
    if not brain:
        print("no brain model available; pass --models or configure ALICE first")
        return 2
    from alice.providers.router import AllProvidersFailed

    try:  # one tiny request first: a dead or empty chain should fail here, not as 28 "failed" tasks
        router.chat("brain", [{"role": "user", "content": "Reply with the word OK."}], max_tokens=20)
    except AllProvidersFailed as e:
        print(f"no model in the chain answered; nothing to evaluate:\n  {e}")
        return 2
    started = time.strftime("%Y-%m-%d %H:%M")
    out = a.out or RESULTS_DIR / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="alice-eval-env-"))
    prepare_env(scratch)
    modes = {"off": [False], "on": [True], "both": [False, True]}[a.council]
    desc = ", ".join(f"{k}={v}" for k, v in chains.items()) if chains else f"user config (brain: {brain[:4]}...)"
    print(f"ALICE eval: {len(tasks)} tasks x {len(modes)} mode(s); models: {desc}")
    if not chains:
        print("  note: without --models the user's role chains are used; they may include paid or subscription models")
    rows: list[dict[str, Any]] = []
    meta = {"started": started, "models": desc, "tasks": len(tasks), "mode": a.mode, "council": a.council,
            "alice_version": __version__, "commit": _commit(), "python": sys.version.split()[0],
            "platform": sys.platform}

    def save() -> None:
        result = {"meta": meta, "summary": {"council_off": summarize([r for r in rows if not r["council"]]),
                                            "council_on": summarize([r for r in rows if r["council"]])},
                  "runs": rows}
        (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "scoreboard.md").write_text(scoreboard(result), encoding="utf-8")

    try:
        for i, t in enumerate(tasks, 1):
            print(f"[{i}/{len(tasks)}] {t.id} ({t.category})")
            for council in modes:
                s = Settings(council=council, mode=a.mode, max_steps=a.max_steps, timeout=a.timeout, keep=a.keep,
                             transcripts=out / "transcripts")
                rows.append(run_task(t, cfg, router, meter, s))
                save()  # partial results survive a crash or Ctrl+C
                if len(rows) >= STOP_AFTER_NO_MODEL and all(r["no_model"] for r in rows[-STOP_AFTER_NO_MODEL:]):
                    raise ChainExhausted
    except ChainExhausted:
        print(f"the last {STOP_AFTER_NO_MODEL} runs got no answer from any model (quota used up?); stopping early")
    except KeyboardInterrupt:
        print("interrupted; writing what finished")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    save()
    print()
    print((out / "scoreboard.md").read_text(encoding="utf-8"))
    print(f"results: {out / 'results.json'}")
    return 0
