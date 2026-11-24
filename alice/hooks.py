"""Hooks: run your own command at certain moments (like hooks in Claude Code).

config.toml (or ~/.alice/hooks.toml):

  [[hooks.PostToolUse]]
  matcher = "edit_file|write_file|multi_edit"     # tool name (regex); empty = all
  command = "ruff format \"$ALICE_TOOL_PATH\""
  timeout = 30

Events:
  SessionStart      before the first message; its output (stdout) is added to the first message as context
  UserPromptSubmit  every user message; stdout is added to it; exit code 2 = block the message
                    (stderr = reason)
  PreToolUse        before a tool runs; exit code 2 = block the tool, stderr goes to the model
  PostToolUse       after a tool finishes; exit code 2 = stderr goes back to the model as feedback
                    (e.g. lint error)
  Stop              agent finished its reply; exit code 2 = stderr to the model, agent continues
                    (e.g. broken tests)

If a PreToolUse hook exits 0 and writes JSON to stdout (as in Claude Code), it can do more:
  {"updatedInput": {...}}                              change the tool's arguments (e.g. add a flag to a command)
  {"permissionDecision": "allow" | "deny" | "ask", "reason": "..."}   permission decision (allow runs without asking)
  {"additionalContext": "..."}                         add context to the tool result (also valid in PostToolUse)
A PostToolUse hook that writes {"updatedToolOutput": "..."} replaces the tool's output
sent to the model with it (e.g. hiding secrets).

If the hook itself breaks (timeout, could not be run, exit code other than 0 or 2), it is ignored by default.
For a checking hook, set `on_failure = "block"`: when it breaks, it counts as a block,
so the tool does not pass unchecked.

The command runs in the shell; event info comes on stdin as JSON ({"event", "tool", "args", "result", "prompt",
"cwd"}); the environment has ALICE_EVENT, ALICE_TOOL, ALICE_TOOL_PATH (the tool's path/command/url
argument) and ALICE_CWD.
Security: hooks are read only from your own config; a file in a cloned repo cannot run commands.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
BLOCK = 2  # Same as Claude Code: 2 = block / give feedback


@dataclass
class Hook:
    event: str
    command: str
    matcher: str = ""
    timeout: float = 30.0
    # If the hook itself crashes or times out (exit code other than 0 or 2), "ignore" skips it (old behavior),
    # "block" counts it as a block. If a security-checking PreToolUse hook breaks, the tool must not pass unchecked.
    on_failure: str = "ignore"

    def matches(self, tool: str) -> bool:
        if not self.matcher or self.matcher == "*":
            return True
        try:
            return re.fullmatch(self.matcher, tool) is not None
        except re.error:
            return self.matcher == tool


@dataclass
class HookResult:
    hook: Hook
    code: int
    stdout: str
    stderr: str

    @property
    def failed(self) -> bool:
        """The hook itself broke (timeout, could not be run, unexpected exit code)."""
        return self.code not in (0, BLOCK)

    @property
    def blocked(self) -> bool:
        return self.code == BLOCK or (self.failed and self.hook.on_failure == "block")


def _read_hooks_file() -> dict[str, Any]:
    from alice.config import alice_home

    path = alice_home() / "hooks.toml"
    if not path.is_file():
        return {}
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            import tomli as tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - a broken file must not stop ALICE
        return {}
    return data.get("hooks", data) if isinstance(data, dict) else {}


def hooks_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "hooks.toml"


def _stamp() -> tuple[float, int]:
    try:
        st = hooks_path().stat()
        return st.st_mtime, st.st_size
    except OSError:
        return 0.0, 0


def _parse(data: dict[str, Any]) -> list[Hook]:
    hooks = []
    for event in EVENTS:
        for h in data.get(event, []) or []:
            if isinstance(h, dict) and str(h.get("command", "")).strip():
                on_failure = "block" if str(h.get("on_failure", "")).lower() == "block" else "ignore"
                try:  # A value like timeout = "30s" must not crash all hooks (and every tool call)
                    timeout = float(h.get("timeout", 30))
                except (TypeError, ValueError):
                    timeout = 30.0
                hooks.append(Hook(event, str(h["command"]), str(h.get("matcher", "") or ""), timeout, on_failure))
    return hooks


def file_hooks() -> list[Hook]:
    """Only the hooks in ~/.alice/hooks.toml (the ones that can be added or removed from the UI or chat)."""
    return _parse(_read_hooks_file())


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # A TOML basic string is compatible with JSON escapes


def _save(hooks: list[Hook]) -> None:
    lines = ["# ALICE hook'ları (Otomasyon sayfasından ya da sohbetten yönetilir). Biçim: alice/hooks.py", ""]
    for h in hooks:
        lines += [f"[[hooks.{h.event}]]", f"command = {_q(h.command)}"]
        if h.matcher:
            lines.append(f"matcher = {_q(h.matcher)}")
        if h.on_failure == "block":
            lines.append('on_failure = "block"')
        lines += [f"timeout = {h.timeout:g}", ""]
    p = hooks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(p)


def add_hook(event: str, command: str, matcher: str = "", timeout: float = 30.0, on_failure: str = "ignore") -> Hook:
    if event not in EVENTS:
        raise ValueError(f"bilinmeyen olay: {event} (olaylar: {', '.join(EVENTS)})")
    if not command.strip():
        raise ValueError("komut boş")
    if matcher:
        try:
            re.compile(matcher)
        except re.error as e:
            raise ValueError(f"matcher geçerli bir regex değil: {e}") from e
    h = Hook(event, command.strip(), matcher.strip(), max(1.0, min(float(timeout), 600.0)),
             "block" if on_failure == "block" else "ignore")
    _save(file_hooks() + [h])
    return h


def remove_hook(index: int) -> Hook:
    rows = file_hooks()
    if not 0 <= index < len(rows):
        raise ValueError(f"böyle bir hook yok: {index}")
    gone = rows.pop(index)
    _save(rows)
    return gone


class Hooks:
    def __init__(self, hooks: list[Hook], cwd: Path | None = None):
        self.hooks = hooks
        self.cwd = cwd
        self._cfg_hooks: list[Hook] | None = None  # Set up via from_config: refreshed when hooks.toml changes
        self._stamp = (0.0, 0)

    @classmethod
    def from_config(cls, cfg, cwd: Path | None = None) -> "Hooks":
        self = cls([], cwd)
        self._cfg_hooks = _parse(dict(((getattr(cfg, "data", None) or {}).get("hooks")) or {}))
        self._reload()
        return self

    def _reload(self) -> None:
        if self._cfg_hooks is None:
            return
        stamp = _stamp()
        if stamp == self._stamp and self.hooks:
            return
        self._stamp = stamp
        self.hooks = self._cfg_hooks + file_hooks()

    def has(self, event: str) -> bool:
        self._reload()  # A hook added from the UI or chat takes effect right away in the open chat too
        return any(h.event == event for h in self.hooks)

    def run(self, event: str, payload: dict[str, Any], tool: str = "") -> list[HookResult]:
        self._reload()
        out = []
        for h in self.hooks:
            if h.event != event or (tool and not h.matches(tool)):
                continue
            out.append(self._run_one(h, payload, tool))
            if out[-1].blocked and event in ("PreToolUse", "UserPromptSubmit"):
                break  # Blocked: no need to run the next hooks
        return out

    def _run_one(self, h: Hook, payload: dict[str, Any], tool: str) -> HookResult:
        from alice.permissions import primary_arg

        args = payload.get("args") or {}
        env = {**os.environ, "ALICE_EVENT": h.event, "ALICE_TOOL": tool,
               "ALICE_TOOL_PATH": primary_arg(args) if isinstance(args, dict) else "",
               "ALICE_CWD": str(self.cwd or Path.cwd()),
               # Output is read as UTF-8; on Windows, Python scripts used to write cp1254 to the pipe ("kullan�c�").
               "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING", "utf-8")}
        data = json.dumps({"event": h.event, "tool": tool, "cwd": str(self.cwd or ""), **payload},
                          ensure_ascii=False, default=str)
        try:
            p = subprocess.Popen(h.command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                 cwd=str(self.cwd) if self.cwd else None, env=env,
                                 creationflags=0x08000000 if sys.platform == "win32" else 0)
        except OSError as e:
            return HookResult(h, 1, "", f"hook çalıştırılamadı: {e}")
        try:
            out, err = p.communicate(data, timeout=h.timeout)
            return HookResult(h, p.returncode, (out or "").strip()[:4000], (err or "").strip()[:4000])
        except subprocess.TimeoutExpired:
            # On timeout, subprocess.run killed only the shell and waited forever for the pipes to close:
            # if the program the shell started (cmd /c python ...) held the pipes, the hook agent locked up forever.
            _kill(p)
            return HookResult(h, 1, "", f"hook {h.timeout:.0f} sn içinde bitmedi: {h.command}")
        except OSError as e:
            _kill(p)
            return HookResult(h, 1, "", f"hook çalıştırılamadı: {e}")


def _kill(p: subprocess.Popen) -> None:
    """Kill the hook process and its child processes, then wait briefly (let the pipes close, leave no zombies)."""
    try:
        from alice.tools.shell import kill_tree

        kill_tree(p.pid)
    except Exception:  # noqa: BLE001 - if psutil is missing, at least kill the shell
        try:
            p.kill()
        except OSError:
            pass  # already exited
    try:
        p.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass  # the process is already gone or its pipes are closed


def decision(results: list[HookResult]) -> dict[str, Any]:
    """Merge the JSON decisions in the stdout of successful (exit 0) hooks (a later hook overrides an earlier one):
      updatedInput        PreToolUse: replace the tool's arguments with this (object)
      permissionDecision  PreToolUse: "allow" (run without asking; forbidden rules still apply), "deny", "ask"
      reason              reason for the decision (on deny it goes to the model)
      additionalContext   context text to add to the tool result
      updatedToolOutput   PostToolUse: replace the tool's output sent to the model with this (text)
    Non-JSON stdout is ignored (old hooks keep working as they are)."""
    out: dict[str, Any] = {}
    notes: list[str] = []
    for r in results:
        if r.code != 0 or not r.stdout.lstrip().startswith("{"):
            continue
        try:
            data = json.loads(r.stdout)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        data = data.get("hookSpecificOutput", data) if isinstance(data.get("hookSpecificOutput"), dict) else data
        if isinstance(data.get("updatedInput"), dict):
            out["updatedInput"] = data["updatedInput"]
        if data.get("permissionDecision") in ("allow", "deny", "ask"):
            out["permissionDecision"] = data["permissionDecision"]
            out["reason"] = str(data.get("permissionDecisionReason") or data.get("reason") or "")[:1000]
        if isinstance(data.get("updatedToolOutput"), str):
            out["updatedToolOutput"] = data["updatedToolOutput"]
        if data.get("additionalContext"):
            notes.append(str(data["additionalContext"])[:2000])
    if notes:
        out["additionalContext"] = "\n".join(notes)
    return out


def feedback(results: list[HookResult]) -> str:
    """stderr of blocking hooks (text sent to the model): exit code 2, or a broken hook with on_failure="block"."""
    return "\n".join((f"hook başarısız oldu, güvenlik için engellendi (on_failure=block): " if r.failed else "")
                     + (r.stderr or r.stdout or f"hook engelledi: {r.hook.command}") for r in results if r.blocked)


def context(results: list[HookResult]) -> str:
    """stdout of successful hooks (context to add to the message)."""
    return "\n".join(r.stdout for r in results if r.code == 0 and r.stdout)
