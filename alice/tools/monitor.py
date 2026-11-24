"""monitor: watch something in the background; when an event happens, return to the chat (Claude Code's Monitor).

process_wait blocks the turn: the user cannot type meanwhile. Here the watch runs in a separate thread; when the
wanted pattern is seen (or the process ends, or time runs out), a "[Watch]" message lands in the chat. If the
chat is busy, the message goes to the running task; if idle, it starts a new turn. The delivery hook is
supplied by the chat side of the app as ctx.state["deliver"]; without it (terminal) only an info line is written.
"""

from __future__ import annotations

import itertools
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.tools.base import EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj

MAX_MINUTES = 360
_ids = itertools.count(1)


@dataclass
class Watcher:
    id: int
    kind: str
    target: str
    pattern: str
    note: str
    deadline: float
    every: float = 5.0
    once: bool = True
    stop: threading.Event = field(default_factory=threading.Event)
    hits: int = 0
    state: str = "izliyor"

    def describe(self) -> str:
        left = max(0, int((self.deadline - time.time()) / 60))
        return f"#{self.id} {self.kind} {self.target[:60]} /{self.pattern}/ [{self.state}, {self.hits} matches, {left} min left]"


def _registry(ctx: ToolContext) -> dict[int, Watcher]:
    return ctx.state.setdefault("monitors", {})


def _deliver(ctx: ToolContext) -> Callable[[str], Any]:
    fn = ctx.state.get("deliver")
    return fn if callable(fn) else ctx.emit


class Monitor(Tool):
    name = "monitor"
    description = (
        "Watches something in the background and comes back to this conversation when an event happens; it does not "
        "hold the turn, and the user can keep typing meanwhile. For jobs like 'tell me/continue when the build "
        "finishes', 'if the server errors', 'when the deploy completes', 'if ERROR shows up in the log'. Use this "
        "instead of process_wait, which blocks the turn.\n"
        "action=start: kind=process (pid from run_command background; a pattern in its output or the process ending), "
        "kind=file (a pattern in lines appended to a log file), kind=command (runs the command every `every` seconds, "
        "until its output matches the pattern). once=false: notifies on every match (frequent matches are merged).\n"
        "action=list / stop (id).\n"
        "When the event happens you get a message starting with '[Watch #id]': continue the work from there.")
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "list", "stop"], "description": "What to do."},
        "kind": {"type": "string", "enum": ["process", "file", "command"], "description": "start: what to watch."},
        "target": {"type": "string", "description": "start: the pid for process, the file path for file, the command "
                                                    "for command."},
        "pattern": {"type": "string", "description": "start: regular expression to look for (case-insensitive). For "
                                                     "process, if left empty, notifies when the process ends."},
        "note": {"type": "string", "description": "start: a short note reminding you what to do when the event "
                                                  "happens (it comes back to you in the message), e.g. 'summarize the "
                                                  "results when the tests finish'."},
        "minutes": {"type": "integer", "description": "start: maximum minutes to watch. Default 30, at most 360."},
        "every": {"type": "integer", "description": "command: how many seconds between runs. Default 30."},
        "once": {"type": "boolean", "description": "start: stop at the first match (default true)."},
        "id": {"type": "integer", "description": "stop: the watch number."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        return EXEC if args.get("action") == "start" and args.get("kind") == "command" else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('kind') or ''} {str(args.get('target') or '')[:60]}".strip()

    def run(self, ctx: ToolContext, action: str, kind: str = "", target: str = "", pattern: str = "", note: str = "",
            minutes: int = 30, every: int = 30, once: bool = True, id: int = 0) -> ToolResult:  # noqa: A002
        reg = _registry(ctx)
        if action == "list":
            live = [w for w in reg.values() if not w.stop.is_set()]
            return ToolResult("\n".join(w.describe() for w in live) or "no watches running")
        if action == "stop":
            w = reg.get(int(id or 0))
            if w is None:
                raise ToolError(f"no watch #{id}")
            w.stop.set()
            w.state = "durduruldu"
            return ToolResult(f"#{w.id} durduruldu")
        if action != "start":
            raise ToolError(f"unknown action: {action}")
        if kind not in ("process", "file", "command"):
            raise ToolError("kind: process | file | command")
        try:
            rx = re.compile(pattern, re.I | re.M) if pattern else None
        except re.error as e:
            raise ToolError(f"invalid pattern: {e}") from e
        if rx is None and kind != "process":
            raise ToolError("file and command need pattern")
        minutes = max(1, min(int(minutes or 30), MAX_MINUTES))
        w = Watcher(next(_ids), kind, str(target), pattern, note, time.time() + minutes * 60,
                    every=max(5.0, float(every or 30)), once=bool(once))
        src = self._source(ctx, w)
        reg[w.id] = w
        threading.Thread(target=_loop, args=(w, src, rx, _deliver(ctx)), daemon=True,
                         name=f"alice-monitor-{w.id}").start()
        what = f"when /{pattern}/ shows up" if pattern else "when the process ends"
        return ToolResult(f"Watch #{w.id} started: {kind} {target[:80]} — a message comes to this chat {what} "
                          f"(at most {minutes} min). You can move on to other work without waiting.")

    @staticmethod
    def _source(ctx: ToolContext, w: Watcher) -> Callable[[], tuple[str, bool]]:
        """Reader that returns (new text, whether the source has ended)."""
        if w.kind == "process":
            try:
                proc = ctx.state.get("bg_procs", {})[int(w.target)]
            except (KeyError, ValueError) as e:
                raise ToolError(f"there is no background process {w.target} (start it with run_command background=true)") from e
            log: Path = proc.log
            pos = [log.stat().st_size if log.exists() else 0]

            def read_proc() -> tuple[str, bool]:
                return _tail(log, pos), proc.popen.poll() is not None

            return read_proc
        if w.kind == "file":
            path = ctx.resolve(w.target)
            if not path.parent.exists():
                raise ToolError(f"folder not found: {path.parent}")
            pos = [path.stat().st_size if path.exists() else 0]  # only what is written from now on

            def read_file() -> tuple[str, bool]:
                return _tail(path, pos), False

            return read_file
        from alice.tools.shell import shell_argv

        cwd = str(ctx.cwd)
        last = [0.0]

        def run_cmd() -> tuple[str, bool]:
            if time.monotonic() - last[0] < w.every:
                return "", False
            last[0] = time.monotonic()
            try:
                r = subprocess.run(shell_argv(w.target), cwd=cwd, capture_output=True, timeout=min(120, w.every * 4),
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired):
                return "", False
            return (r.stdout + r.stderr).decode("utf-8", errors="replace"), False

        return run_cmd


def _tail(path: Path, pos: list[int]) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size < pos[0]:  # file was rewritten from the beginning
        pos[0] = 0
    if size == pos[0]:
        return ""
    with open(path, "rb") as f:
        f.seek(pos[0])
        data = f.read(min(size - pos[0], 1_000_000))
    pos[0] += len(data)
    return data.decode("utf-8", errors="replace")


def _loop(w: Watcher, read: Callable[[], tuple[str, bool]], rx: re.Pattern[str] | None,
          deliver: Callable[[str], Any]) -> None:
    head = f"[Watch #{w.id}] {w.kind} {w.target[:80]}"
    tail = f"\nNot: {w.note}" if w.note else ""
    buffer = ""
    last_sent = 0.0
    pending: list[str] = []
    while not w.stop.is_set():
        text, ended = read()
        if text:
            buffer = (buffer + text)[-20_000:]
            if rx is not None:
                lines = [ln for ln in buffer.splitlines() if rx.search(ln)]
                if lines:
                    w.hits += len(lines)
                    pending += lines[-10:]
                    buffer = ""
        if pending and (w.once or time.monotonic() - last_sent > 30):  # frequent matches are batched together
            deliver(f"{head}: /{w.pattern}/ seen.\n" + "\n".join(pending[-10:]) + tail)
            pending, last_sent = [], time.monotonic()
            if w.once:
                w.state = "matched"
                w.stop.set()
                return
        if ended:
            w.state = "process ended"
            out = "\n".join(buffer.splitlines()[-15:])
            deliver(f"{head}: process ended" + (" (pattern not seen)" if rx is not None else "") + f".\n{out}{tail}")
            w.stop.set()
            return
        if time.time() > w.deadline:
            w.state = "time ran out"
            deliver(f"{head}: time ran out, /{w.pattern}/ not seen.{tail}")
            w.stop.set()
            return
        w.stop.wait(1.0)


TOOLS: list[Tool] = [Monitor()]
