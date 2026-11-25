from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from alice.config import Config
    from alice.decider import Decider
    from alice.providers.router import Router

# Risk classes are used by the permission system.
READ, WRITE, EXEC, COMPUTER, NETWORK, DANGER = "read", "write", "exec", "computer", "network", "danger"
# Outward action: a message, post or e-mail going to someone else. Unlike file edits it cannot be undone;
# so it is asked about even in auto mode (can be turned off with [agent] confirm_outward = false).
SEND = "send"


@dataclass
class ToolResult:
    text: str
    images: list[str] = field(default_factory=list)  # base64 PNG
    is_error: bool = False
    diff: str = ""  # unified diff shown in the UI (not sent to the model)
    files: list[dict[str, Any]] = field(default_factory=list)  # files shown to the user as cards (send_file)
    card: dict[str, Any] = field(default_factory=dict)  # UI-specific card (e.g. next_step: summary, question, buttons)


class ToolError(Exception):
    """For returning a meaningful error message to the user/model from within a tool."""


@dataclass
class ToolContext:
    cwd: Path
    config: "Config"
    router: "Router | None" = None
    decider: "Decider | None" = None
    state: dict[str, Any] = field(default_factory=dict)  # state shared between tools (browser, processes, todo)
    emit: Callable[[str], None] = lambda s: None

    def cancelled(self) -> bool:
        """Did the user press Stop? Tools with long loops should check this from time to time."""
        agent = self.state.get("agent")
        cancel = getattr(agent, "cancel", None)
        return bool(cancel is not None and cancel.is_set())

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()


class DesktopLock:
    """The screen, mouse/keyboard and the user's browser are a single resource: only one chat uses them at a time. The
    lock is held until the chat's turn ends, so no other chat can step into an unfinished screen action.
    The owner is the ToolContext (sub-agents share the same context and do not make their chats wait).

    When the owner changes, the previous owner's browser connection is dropped: the browser bridge serves a single
    client, so the old connection would stay silently dead and its next call would hang."""

    def __init__(self) -> None:
        import threading

        self._cond = threading.Condition()
        self.owner: ToolContext | None = None
        self.last: ToolContext | None = None

    def held_by(self, ctx: ToolContext) -> bool:
        return self.owner is ctx

    def acquire(self, ctx: ToolContext, cancelled: Callable[[], bool], on_wait: Callable[[], None] | None = None) -> bool:
        """Wait for our turn; return False if cancelled."""
        with self._cond:
            told = False
            while self.owner is not None and self.owner is not ctx:
                if cancelled():
                    return False
                if not told and on_wait is not None:
                    on_wait()
                    told = True
                self._cond.wait(0.25)
            prev, self.owner, self.last = self.last, ctx, ctx
        if prev is not None and prev is not ctx:
            b = prev.state.get("browser")
            if b is not None and hasattr(b, "abort"):
                b.abort("handoff")
        return True

    def release(self, ctx: ToolContext) -> None:
        with self._cond:
            if self.owner is ctx:
                self.owner = None
                self._cond.notify_all()


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk: str = READ
    # True: a tool with no side effects that uses no thread-bound objects; back-to-back calls run in parallel.
    parallel_safe: bool = False
    # True: the result is outside content (web page, message, e-mail, MCP). It reaches the model marked
    # "güvenilmeyen veri" (untrusted data); in that turn, commands and outward messages still need approval in auto mode.
    untrusted: bool = False

    def risk_for(self, args: dict[str, Any]) -> str:
        return self.risk

    def parallel_for(self, ctx: "ToolContext", args: dict[str, Any]) -> bool:
        """Can this call run at the same time as neighbouring calls in the same reply? Default: parallel_safe."""
        return self.parallel_safe

    def exclusive_key(self, args: dict[str, Any]) -> str:
        """If not empty: two calls carrying the same key never run at once (e.g. two git commands in the same repo)."""
        return ""

    def summary(self, args: dict[str, Any]) -> str:
        """Short summary shown on screen."""
        s = json.dumps(args, ensure_ascii=False)
        return s if len(s) < 160 else s[:157] + "..."

    # Every tool defines run(self, ctx, <its schema arguments>) -> ToolResult. It is only declared here as a type: a
    # base method taking **args would make each tool's own argument list an incompatible override.
    run: Callable[..., ToolResult]

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def check_args(tool: Tool, args: dict[str, Any]) -> str:
    """Roughly check the arguments against the tool's schema (required fields, enum). If there is a problem, return
    an error text the model can use to fix itself; otherwise return "". Claude Code and Codex also bounce a faulty call
    without running the tool, telling what is missing or wrong and listing the valid values."""
    schema = tool.parameters if isinstance(tool.parameters, dict) else {}
    props = schema.get("properties") or {}
    missing = [k for k in schema.get("required") or [] if args.get(k) is None]  # "" is valid: new_string="" deletes
    if missing:
        return f"{tool.name}: eksik zorunlu parametre: {', '.join(missing)}. {_param_hint(props, missing)}"
    for k, v in args.items():
        enum = (props.get(k) or {}).get("enum")
        if enum and isinstance(v, str) and v not in enum:
            return f"{tool.name}: {k}='{v}' is invalid. Valid values: {', '.join(map(str, enum))}."
    return ""


def unknown_params(tool: Tool, args: dict[str, Any]) -> str:
    """Error text for parameters that are not in the schema (to make the TypeError understandable)."""
    props = (tool.parameters or {}).get("properties") or {}
    extra = [k for k in args if k not in props]
    if not extra or not props:
        return ""
    return (f"{tool.name}: unknown parameter: {', '.join(extra)}. Valid parameters: "
            f"{', '.join(props)}.")


def _param_hint(props: dict[str, Any], names: list[str]) -> str:
    hints = [f"{n}: {props[n]['description']}" for n in names if (props.get(n) or {}).get("description")]
    return " ".join(hints)


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def make_diff(path: str, before: str, after: str, context: int = 3, max_lines: int = 400) -> str:
    import difflib

    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      fromfile=f"a/{path}", tofile=f"b/{path}", n=context, lineterm=""))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    return "\n".join(lines)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 0:  # text[-0:] used to return the whole text
        return f"... [{len(text)} characters cut] ..."
    head = int(limit * 0.6)
    tail = limit - head
    return f"{text[:head]}\n\n... [{len(text) - limit} characters cut] ...\n\n{text[-tail:]}"


# Lines worth keeping from the cut middle of a long command output: errors, failures, tracebacks, summaries.
_SIGNAL = re.compile(r"\b(?:error|errors|fail(?:ed|ure|s)?|exception|traceback|fatal|panic|denied|not found|"
                     r"cannot|could not|unable to|warning|assert(?:ion)?|exit code|timed? ?out|hata|başarısız)\b|"
                     r"\b\w*(?:Error|Exception|Warning)\b:|"  # ValueError:, KeyError:, DeprecationWarning:
                     r"^(?:E |FAILED |ERROR |  File \")|^\s*\^+\s*$", re.I)
_REPEAT_MIN = 4


def _collapse_repeats(lines: list[str]) -> list[str]:
    """Runs of near-identical lines (progress bars, "Downloading x...", the same warning 300 times) become one line
    plus a count; that alone usually brings a long install or build log under the limit without losing anything."""
    out: list[str] = []
    i = 0
    # Numbers are ignored (counters, percentages, line numbers), the rest of the line must match exactly; a signal
    # line (an error) never joins a run, so "FAILED" between 700 "PASSED" lines stays visible.
    norm = lambda s: re.sub(r"\d+", "#", s.strip())  # noqa: E731
    while i < len(lines):
        j = i + 1
        if lines[i].strip() and not _SIGNAL.search(lines[i]):
            while j < len(lines) and norm(lines[j]) == norm(lines[i]):
                j += 1
        if j - i >= _REPEAT_MIN:
            out += [lines[i], f"    ... ({j - i - 2} similar lines more) ...", lines[j - 1]]
        else:
            out += lines[i:j]
        i = j
    return out


def smart_truncate(text: str, limit: int, failed: bool = False) -> str:
    """Cut a long command output so the useful parts survive.

    1. Repeated lines are collapsed first (often enough on its own).
    2. Then head + tail are kept as before, but the tail gets more room when the command failed (the error and the
       summary are at the end), and lines from the cut middle that look like errors/failures/warnings are kept with
       their line numbers, so a failing test in the middle of a 3000-line pytest run is not lost.
    The full output is still saved to a file by the caller."""
    if len(text) <= limit:
        return text
    lines = _collapse_repeats(text.splitlines())
    squeezed = "\n".join(lines)
    if len(squeezed) <= limit:
        return squeezed
    if limit <= 0:
        return f"... [{len(text)} characters cut] ..."
    head_n = int(limit * (0.3 if failed else 0.45))
    tail_n = int(limit * (0.45 if failed else 0.35))
    sig_n = limit - head_n - tail_n
    head, tail = squeezed[:head_n], squeezed[-tail_n:]
    # Middle lines with a signal, deduplicated, numbered so the model can read around them in the saved file.
    start_line = head.count("\n")
    middle = squeezed[head_n:len(squeezed) - tail_n].splitlines()
    picked: list[str] = []
    seen: set[str] = set()
    used = 0
    for k, line in enumerate(middle):
        # The line right after a signal line is usually its message ("Traceback" -> the frame/exception line).
        follows = k > 0 and _SIGNAL.search(middle[k - 1]) and line.strip()
        if not _SIGNAL.search(line) and not follows:
            continue
        key = re.sub(r"\d+", "#", line.strip())[:120]
        if key in seen:
            continue
        seen.add(key)
        row = f"{start_line + k + 1:>6}| {line[:300]}"
        if used + len(row) + 1 > sig_n:
            break
        picked.append(row)
        used += len(row) + 1
    cut = len(squeezed) - head_n - tail_n
    mid = f"\n\n... [{cut} characters cut"
    if picked:
        mid += f"; {len(picked)} important lines in between (with line numbers):]\n" + "\n".join(picked) + "\n[...]\n\n"
    else:
        mid += "] ...\n\n"
    return head + mid + tail
