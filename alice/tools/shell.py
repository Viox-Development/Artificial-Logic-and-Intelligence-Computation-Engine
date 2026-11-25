"""Shell commands and background processes.

On Windows, PowerShell (pwsh if available) is used and output is forced to UTF-8; otherwise Turkish
characters come out garbled because of cp857.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.tools.base import DANGER, EXEC, Tool, ToolContext, ToolError, ToolResult, obj, smart_truncate, truncate

IS_WIN = sys.platform == "win32"
# Commands run from the windowless core (pythonw) must not each open a black console window. Windowed
# programs started by a command (Blender, Notepad...) are still visible.
NO_WINDOW = 0x08000000 if IS_WIN else 0


def hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    """Startup info forcing hidden window on Windows so child processes don't spawn black console windows."""
    if not IS_WIN:
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si

# Patterns that are hard to undo or could break the system. In "auto" mode these ask for approval.
DANGEROUS = [
    r"\brm\s+(-\w+\s+)*-\w*[rR]",  # recursive delete
    r"\bmkfs\b", r"\bdd\s+if=", r"\bfdisk\b", r"\bdiskpart\b", r"\bformat\s+[a-z]:",
    r"\b(shutdown|reboot|halt|poweroff)\b", r"Stop-Computer", r"Restart-Computer",
    r"Remove-Item\b.*-Recurse", r"\brd\s+/s\b", r"\brmdir\s+/s\b", r"\bdel\s+/[sq]",
    # PowerShell aliases, with the flag after the path: "rm .\build -Recurse -Force", "ri C:\x -r"
    r"\b(rm|ri|del|erase|rd|rmdir)\b[^\n|;&]*\s-r(ecurse)?\b",
    r"\|\s*(Remove-Item|ri|rm|del|erase)\b",  # Get-ChildItem ... | Remove-Item
    r"\breg\s+(delete|add)\b", r"Remove-ItemProperty", r"Set-ItemProperty\s+.*HKLM",
    r"\bbcdedit\b", r"\bvssadmin\b.*delete", r"\bcipher\s+/w", r"Clear-Disk", r"Format-Volume",
    r"\bchmod\s+-R\s+[0-7]{3}\s+/", r"\bchown\s+-R\b.*\s/(\s|$)",
    r"git\s+push\s+.*--force", r"git\s+reset\s+--hard", r"git\s+clean\s+-[a-z]*f",
    r":\(\)\s*\{\s*:\|:&\s*\};:",  # fork bomb
    r"Set-ExecutionPolicy\s+Unrestricted", r"netsh\s+advfirewall\s+set\s+.*off",
    r"\bcurl\b.*\|\s*(sh|bash|iex)", r"\biwr\b.*\|\s*iex", r"Invoke-Expression.*DownloadString",
]
_DANGER_RX = [re.compile(p, re.I) for p in DANGEROUS]
# Programs that lock a file or leave an install half-done when two copies run at once: in parallel
# runs, these wait for each other (see RunCommand.exclusive_key).
_GIT_RX = re.compile(r"(?:^|[;&|({]|\s)git(?:\.exe)?\s", re.I)
_INSTALLER_RX = re.compile(r"(?:^|[;&|({]|\s)(?:npm|pnpm|yarn|npx|pip3?|uv|poetry|conda|winget|choco|scoop|apt|apt-get|"
                           r"brew|cargo|dotnet|msiexec)(?:\.exe|\.cmd)?\s|-m\s+pip\s", re.I)
_sandbox_lock = threading.Lock()  # the folder is labeled on first use: parallel commands must not label it at the same time
READ_ONLY = re.compile(r"^\s*(get-\w+|dir|ls|type|cat|where(\.exe)?|test-path|select-string)(\s|$)", re.I)
ACCESS_DENIED = re.compile(r"access (is )?denied|access to the path .* is denied|erişim (engellendi|reddedildi)|UnauthorizedAccess|permission denied|"
                           r"EACCES|EPERM", re.I)


def is_dangerous(command: str) -> bool:
    return any(rx.search(command) for rx in _DANGER_RX)


def shell_argv(command: str) -> list[str]:
    if IS_WIN:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        # Put the encoding setup on its own LINE (it used to share one via "; "). That way the position in
        # PowerShell's parse errors ("At line:N char:M") points at the user's command (line 2);
        # before, the ~110-character prefix shifted the char position, so the model looked for the error in
        # the wrong place (seen often with gh/jq commands in the 2026-11-13 session). -EncodedCommand was tried,
        # but Windows PowerShell 5.1 wraps stderr in CLIXML that can't be read with it; -Command keeps plain text.
        # PSDefaultParameterValues: in Windows PowerShell 5.1 `>`, Out-File and Set-Content wrote UTF-16 LE
        # (bytes FF FE); every JSON or text file a command produced then failed to load in Python and git saw
        # binary. With this they write UTF-8 (5.1 adds a BOM, which utf-8-sig readers and git handle).
        setup = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; "
                 "$PSDefaultParameterValues['Out-File:Encoding']='utf8'; "
                 "$PSDefaultParameterValues['Set-Content:Encoding']='utf8'; "
                 "$PSDefaultParameterValues['Add-Content:Encoding']='utf8'")
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", setup + "\n" + command]
    sh = shutil.which("bash") or "/bin/sh"
    return [sh, "-lc", command]


def shell_name() -> str:
    if IS_WIN:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return "bash" if shutil.which("bash") else "sh"


# GitHub tokens that ALICE itself loaded from its .env: gh prefers GITHUB_TOKEN / GH_TOKEN over the user's own
# `gh auth login` session, so a narrow token kept in .env for the github tool turned every `gh pr create` in a
# command into "Resource not accessible by personal access token". Commands get the user's real gh login instead.
_GH_ENV = ("GITHUB_TOKEN", "GH_TOKEN")


def _env() -> dict[str, str]:
    from alice.config import LOADED_ENV_KEYS

    env = dict(os.environ)
    for k in _GH_ENV:
        if k in LOADED_ENV_KEYS:
            env.pop(k, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


# Windows PowerShell 5.1 wraps every stderr line of a native program (git's "Switched to branch", gh progress,
# pip warnings) in a red ErrorRecord block when stderr is merged with 2>&1:
#   git : Switched to a new branch 'x'
#   At line:2 char:81
#   + ... git checkout -b x 2>&1 ...
#   +     ~~~~~~~~~~~~~~~~
#       + CategoryInfo          : NotSpecified: (...) [], RemoteException
#       + FullyQualifiedErrorId : NativeCommandError
# The message is kept; the five lines of noise that made a normal git command look like a failure are dropped.
_NATIVE_NOISE = re.compile(
    r"^(?P<msg>[^\r\n]*)\r?\n(?:At line:\d+ char:\d+\r?\n)(?:\+[^\r\n]*\r?\n){1,3}"
    r"(?:[ \t]+\+ (?:CategoryInfo|FullyQualifiedErrorId)[^\r\n]*\r?\n?){1,2}", re.M)


def clean_ps_noise(text: str) -> str:
    if "NativeCommandError" not in text:
        return text

    def keep(m: re.Match) -> str:
        msg = m.group("msg")
        # "git : message" -> "message" (the program name prefix PowerShell adds)
        return re.sub(r"^[\w.-]+(?:\.exe)? : ", "", msg) + "\n"

    return _NATIVE_NOISE.sub(keep, text)


@dataclass
class BgProc:
    pid: int
    command: str
    popen: subprocess.Popen
    log: Path
    started: float
    err: Path | None = None  # stderr of a command that was moved here while it ran (a background one has a single log)
    notify: bool = True  # tell the chat when it ends (not if it was stopped, or the model already saw the end)
    waiting: int = 0  # process_wait calls watching it right now
    reason: str = ""  # why it was moved to the background: user | message | stop | timeout ("": started there)

    def text(self) -> str:
        parts = [p.read_bytes().decode("utf-8", errors="replace") for p in (self.log, self.err) if p and p.exists()]
        out = parts[0] if parts else ""
        if len(parts) > 1 and parts[1].strip():
            out += ("\n" if out and not out.endswith("\n") else "") + "[stderr]\n" + parts[1]
        return clean_ps_noise(out) if IS_WIN else out


@dataclass
class FgJob:
    """A command the turn is waiting for. `request` (set from another thread) moves it to the background."""

    pop: subprocess.Popen
    command: str
    log: Path
    err: Path
    started: float = field(default_factory=time.time)
    request: str = ""
    request_at: float = 0.0


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Runs a shell command (Windows: PowerShell 5.1, others: bash) and returns the exit code and stdout+stderr. "
        "Runs without a window, in the background.\n"
        "- To read/search/edit files use read_file, grep, glob, edit_file; for a program with a window use windows "
        "action=open; for git, network and UI work use the matching dedicated tools.\n"
        "- For long-running work (servers, watchers, builds, long tests): background=true and go on working; a message "
        "comes to this chat when it ends, so don't wait for it or poll it. The user can also move a running command "
        "there; a message they send does it too.\n"
        "- Ask for independent commands as separate calls in the same reply (they run in parallel); chain dependent "
        "ones in one command with ';' (5.1 has no '&&').\n"
        "- For Python code pass python instead of command: the code goes through as is, no quote escaping needed. "
        "Use stdin for input.\n"
        "- In long output repeated lines are merged and error lines are kept even from the middle; the full output "
        "is written to a file."
    )
    parameters = obj({
        "command": {"type": "string", "description": "Command to run, e.g. 'git status' or 'python -m pytest -q'. "
                    "Leave empty when python is given."},
        "python": {"type": "string", "description": "Python code to run (can be multi-line). It does not go through "
                   "PowerShell, so quotes stay intact. E.g. \"import json; print(json.load(open('a.json'))['x'])\"."},
        "stdin": {"type": "string", "description": "Text to pass to the standard input of the command or Python code."},
        "cwd": {"type": "string", "description": "Folder the command runs in. Default: the working folder."},
        "timeout": {"type": "integer", "description": "Seconds to wait. After that the command goes on in the background (where that isn't possible it is killed). Default 120, at most 1800."},
        "background": {"type": "boolean", "description": "true: start in the background without waiting and return the pid. "
                       "Default false."},
        "unsandboxed": {"type": "boolean", "description": "true only if the command failed because of the sandbox and "
                        "really needs to write outside the project (the user is always asked)."},
    }, [])
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("unsandboxed"):
            return DANGER  # leaving the sandbox always requires approval
        text = str(args.get("command") or "") + "\n" + str(args.get("python") or "")
        return DANGER if is_dangerous(text) else EXEC

    def summary(self, args: dict[str, Any]) -> str:
        if args.get("python") and not args.get("command"):
            first = next((ln for ln in str(args["python"]).splitlines() if ln.strip()), "")
            return f"python: {first[:280]}"
        return str(args.get("command", ""))[:300]

    def parallel_for(self, ctx: ToolContext, args: dict[str, Any]) -> bool:
        # Commands that need approval (dangerous, outside the sandbox) are already asked about one by one;
        # the rest can run side by side.
        return (bool(ctx.config.agent.get("parallel_commands", True)) and not args.get("unsandboxed")
                and self.risk_for(args) != DANGER)

    def exclusive_key(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", ""))
        if _INSTALLER_RX.search(command):
            return "kurulum"
        return "git" if _GIT_RX.search(command) else ""

    def run(self, ctx: ToolContext, command: str = "", cwd: str | None = None, timeout: int = 120,
            background: bool = False, unsandboxed: bool = False, python: str = "", stdin: str = "") -> ToolResult:
        wd = ctx.resolve(cwd) if cwd else ctx.cwd
        if not wd.is_dir():
            raise ToolError(f"folder not found: {wd}")
        if (command is not None and not isinstance(command, str)) or (python is not None and not isinstance(python, str)):
            raise ToolError("invalid parameter: command and python must be text")
        command, python = command or "", python or ""
        if python and command:
            raise ToolError("give only one of command or python")
        if not python and not command.strip():
            raise ToolError("missing required parameter: command (or python)")
        if python:
            # The code goes to the interpreter on stdin, never through a shell: no quoting, no $ or backslash
            # mangling, multi-line works. `py -c "..."` through PowerShell broke on nearly every non-trivial snippet.
            from alice import runtime

            if stdin:
                raise ToolError("python and stdin can't be given together: the code is already read from stdin; put the input inside the "
                                "code or read it from a file")
            argv = [str(runtime.python()), "-X", "utf8", "-"]
            stdin = python
            command = "python -"
        else:
            argv = shell_argv(command)
        env = _env()
        from alice import sandbox

        boxed = sandbox.applies(ctx, {"command": command, "unsandboxed": unsandboxed})
        if boxed:
            try:
                with _sandbox_lock:
                    argv, extra = sandbox.wrap(argv, wd, ctx)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                raise ToolError(f"the sandbox could not be set up, the command was not run: {e}. If needed, run it with unsandboxed=true "
                                "(with the user's approval).") from e
            env = sandbox.scrub_env(env, ctx)  # keep API keys from .env out of the sandbox
            env.update(extra)
        from alice import gitsafe

        # The sandbox can write to .git: if a hook or config changes (ALICE or the user later runs git outside
        # the sandbox it kicks in), a warning is added to the result.
        git_before = gitsafe.snapshot(wd) if boxed and not background else None
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        if background and stdin:
            raise ToolError("a background process can't take stdin/python; write the code to a file and run the file")
        if background:
            # The name used to be a timestamp: two processes started in the same millisecond wrote to one file
            # and their output got mixed.
            log = Path(tempfile.gettempdir()) / f"alice-bg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.log"
            kwargs: dict[str, Any] = {}
            if IS_WIN:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW  # type: ignore[attr-defined]
                kwargs["startupinfo"] = hidden_startupinfo()
            else:
                kwargs["start_new_session"] = True
            with open(log, "wb") as fh:  # the child process gets its own copy; our handle must not leak
                pop = subprocess.Popen(argv, cwd=wd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       env=env, **kwargs)
            procs: dict[int, BgProc] = ctx.state.setdefault("bg_procs", {})
            proc = procs[pop.pid] = BgProc(pop.pid, command, pop, log, time.time(), notify=can_background(ctx))
            time.sleep(1.5)
            head = log.read_bytes().decode("utf-8", errors="replace")[-2000:]
            state = "running" if pop.poll() is None else f"finished (code {pop.returncode})"
            if pop.returncode is not None:
                proc.notify = False  # the model sees the end right here
            elif proc.notify:
                _watch(ctx, proc)
            return ToolResult(f"started in the background pid={pop.pid}, state: {state}\nlog: {log}\n{head}")
        timeout = max(1, min(int(timeout), 1800))
        # Output goes to a temp file instead of a pipe: if a windowed program the command started (e.g. Blender)
        # inherits the pipes, subprocess.run kept waiting forever even after the timeout.
        # Named files: if the command is moved to the background, it goes on writing to them.
        log, errlog = _log_files()
        with open(log, "wb") as fo, open(errlog, "wb") as fe, tempfile.TemporaryFile() as fi:
            fg_kw: dict[str, Any] = {"creationflags": NO_WINDOW}
            if IS_WIN:
                fg_kw["startupinfo"] = hidden_startupinfo()
            if stdin:
                # A file, not a pipe, for the same reason as stdout: nothing can block on a full pipe buffer.
                fi.write(stdin.encode("utf-8"))
                fi.seek(0)
            pop = subprocess.Popen(argv, cwd=wd, stdout=fo, stderr=fe, stdin=fi if stdin else subprocess.DEVNULL,
                                   env=env, **fg_kw)
        job = FgJob(pop, command if command != "python -" else "python: " + next(
            (ln.strip() for ln in python.splitlines() if ln.strip()), ""), log, errlog)
        with track_foreground(ctx, pop, job):
            how = _wait_foreground(job, timeout)
            if how == "timeout" and can_background(ctx) and not ctx.cancelled():
                job.request = "timeout"
            if how not in ("done", "timeout") or job.request == "timeout":
                return _to_background(ctx, job, limit)
            timed_out = how == "timeout"
            if timed_out:
                # The shell and console child processes (a stuck pytest, node...) are killed; the windowed
                # program the command opened (Notepad, a Blender window) and its child processes stay open.
                # Previously only the shell
                # was killed, and stuck child processes kept running in the background.
                kill_console_tree(pop.pid)
                try:
                    pop.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pop.kill()
                    pop.wait()
        returncode = -1 if timed_out else pop.returncode
        out = log.read_bytes().decode("utf-8", errors="replace")
        err = errlog.read_bytes().decode("utf-8", errors="replace")
        for p in (log, errlog):
            try:
                p.unlink()
            except OSError:
                pass  # a windowed program the command opened still holds it
        if IS_WIN:
            out, err = clean_ps_noise(out), clean_ps_noise(err)
        if timed_out:
            return ToolResult(f"TIMEOUT ({timeout} s). Use background=true for long jobs. If you are opening a windowed "
                              "program, use windows action=open instead of run_command; for Blender use the blender tool."
                              f"\n{truncate(out + err, limit)}"
                              + (gitsafe.warning(gitsafe.changes(git_before, gitsafe.snapshot(wd)))
                                 if git_before is not None else ""), is_error=True)
        body = out
        if err.strip():
            body += ("\n[stderr]\n" if out.strip() else "[stderr]\n") + err
        body = smart_truncate(body.strip(), limit, failed=returncode != 0) or "(no output)"
        if len(out) + len(err) > limit:
            # So truncated output is not lost: the full output is written to a temp file the model can read.
            # mkstemp opens the file as 0600 on POSIX; write_bytes would leave it 0644 and expose the output to
            # other users.
            try:
                fd, name = tempfile.mkstemp(prefix="alice-out-", suffix=".log")
                with os.fdopen(fd, "wb") as fh:
                    fh.write((out + ("\n" + err if err.strip() else "")).encode("utf-8", errors="replace"))
                body += f"\n[full output in file: {name} - read the cut part with read_file/grep]"
            except OSError:
                pass  # no file: the truncated output is returned as is
        if IS_WIN and "is not a valid statement separator" in err:
            body += ("\n[hint] This is Windows PowerShell 5.1: there is no '&&' or '||'. Separate commands with ';', "
                     "and use 'if ($?) { ... }' or 'if (-not $?) { ... }' for conditions.")
        body += _ps_hint(command, err) if IS_WIN else ""
        # Only on a real access error, or when a command that tries to write fails (a write blocked at low integrity
        # sometimes gives no output at all). An error from a read-only command ("dosya yok") is not from the sandbox.
        if boxed and (ACCESS_DENIED.search(out + err) or (returncode != 0 and not READ_ONLY.match(command))):
            # Only when there really is an access error: once this was added to every failing command,
            # the model also took "dosya yok" as "access
            # blocked" (a Get-Item looking for the nonexistent Sonora-Setup.exe, 2026-11-02).
            body += (f"\n[sandbox: the command could write only to {wd} and the temp folder. If this access error is "
                     "why, and writing outside the project is really needed, try again with unsandboxed=true.]")
        if git_before is not None:
            body += gitsafe.warning(gitsafe.changes(git_before, gitsafe.snapshot(wd)))
        return ToolResult(f"exit code {returncode}\n{body}", is_error=returncode != 0)


_INLINE_CODE_RX = re.compile(r"(python(\.exe)?\"?|py|node(\.exe)?)\s+-[ce]\s", re.I)


def _ps_hint(command: str, err: str) -> str:
    """One-line fix for common PowerShell syntax errors (the ones that recur in session logs):
    the model spent several turns working out the error."""
    if "was not followed by a valid variable name character" in err:
        return ("\n[hint] PowerShell takes the ':' in \"$name: ...\" as part of the variable name. "
                "Write \"${name}: ...\" or \"$($name): ...\".")
    if "Unrecognized token" in err and "@@" in command:
        return "\n[hint] A hashtable is written '@{ ... }'; '@@{' is invalid."
    if _INLINE_CODE_RX.search(command) and ("SyntaxError" in err or "unexpected EOF" in err or "Unexpected token" in err):
        return ("\n[hint] PowerShell mangles double quotes and backslashes inside code given with -c. "
                "Write the code to a temporary file with write_file and run the file; use only single quotes in inline code.")
    return ""


class ProcessOutput(Tool):
    name = "process_output"
    parallel_safe = True
    description = ("Shows the status (running / finished + exit code) and the end of the output of background "
                   "processes (run_command background=true); returns at once, does not wait.\n"
                   "- Without a pid, lists every background process ALICE started.\n"
                   "- Don't call this over and over to wait for a process to finish or for a line (\"ready\", "
                   "\"error\") to appear in its output; use process_wait.\n"
                   "- For programs ALICE did not start, use process_list.")
    parameters = obj({
        "pid": {"type": "integer", "description": "pid returned by run_command. Empty: all background processes."},
        "tail_chars": {"type": "integer", "description": "How many characters from the end of the output. Default 4000; "
                       "0: no output, status only."},
    })

    def run(self, ctx: ToolContext, pid: int | None = None, tail_chars: int = 4000) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        if pid is None:
            if not procs:
                return ToolResult("no background processes")
            rows = [f"pid={p.pid} {'running' if p.popen.poll() is None else f'finished({p.popen.returncode})'} :: {p.command[:100]}"
                    for p in procs.values()]
            return ToolResult("\n".join(rows))
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} was not started by ALICE")
        done = p.popen.poll() is not None  # before reading: the last line must not be missed
        data = p.text()
        state = f"finished (code {p.popen.returncode})" if done else "running"
        if done:
            p.notify = False
        # data[-0:] returned the whole log: 0 and negative values mean "no output, status only".
        n = int(tail_chars or 0)
        if n <= 0:
            return ToolResult(f"pid={pid} {state}")
        return ToolResult(f"pid={pid} {state}\n{data[-n:]}")


class ProcessStop(Tool):
    name = "process_stop"
    description = ("Stops a process ALICE started with run_command(background=true), together with its child "
                   "processes, and returns its last output.\n"
                   "- Don't leave finished server/watcher processes running; close them with this when done.\n"
                   "- For programs ALICE did not start (apps the user opened), use process_kill.")
    parameters = obj({"pid": {"type": "integer", "description": "pid returned by run_command."}}, ["pid"])

    risk = EXEC

    def run(self, ctx: ToolContext, pid: int) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} was not started by ALICE; use process_kill for other processes")
        p.notify = False
        kill_tree(pid)
        _reap(p.popen)
        return ToolResult(f"durduruldu: {pid}")


@contextlib.contextmanager
def track_foreground(ctx: ToolContext, pop: subprocess.Popen, job: FgJob | None = None):
    """Record the foreground process so the Stop button can kill it (and its child processes). With a job it can be
    moved to the background instead."""
    procs: set = ctx.state.setdefault("fg_procs", set())
    jobs: dict[int, FgJob] = ctx.state.setdefault("fg_jobs", {})
    procs.add(pop)
    if job is not None:
        jobs[pop.pid] = job
    try:
        yield pop
    finally:
        procs.discard(pop)
        jobs.pop(pop.pid, None)


def can_background(ctx: ToolContext) -> bool:
    """Only in a chat that can be told when the command ends (the app), and not for a subagent: its result would come
    back after the subagent is gone."""
    return (callable(ctx.state.get("deliver")) and not ctx.state.get("procs_closed")
            and not getattr(ctx.state.get("agent"), "is_child", False)
            and bool(ctx.config.agent.get("background_commands", True)))


def background_foreground(ctx: ToolContext, reason: str, grace: float = 0.0) -> list[int]:
    """Move the commands the turn is waiting for to the background; their pids. grace: seconds a command still gets to
    finish by itself (a message must not turn every half-second command into a background one)."""
    if not can_background(ctx):
        return []
    moved = []
    for job in list(ctx.state.get("fg_jobs", {}).values()):
        if job.pop.poll() is None:
            if not job.request:
                job.request_at = time.monotonic() + grace
                job.request = reason
            elif grace == 0:
                job.request, job.request_at = reason, 0.0
            moved.append(job.pop.pid)
    return moved


def _log_files() -> tuple[Path, Path]:
    paths = []
    for part in ("out", "err"):
        fd, name = tempfile.mkstemp(prefix="alice-cmd-", suffix=f".{part}.log")  # 0600 on POSIX
        os.close(fd)
        paths.append(Path(name))
    return paths[0], paths[1]


def _wait_foreground(job: FgJob, timeout: float) -> str:
    """"done", "timeout", or the reason it has to go to the background."""
    end = time.monotonic() + timeout
    while True:
        left = end - time.monotonic()
        try:
            job.pop.wait(timeout=max(0.01, min(0.1, left)))
            return "done"
        except subprocess.TimeoutExpired:
            pass
        if job.request and time.monotonic() >= job.request_at:
            return job.request
        if left <= 0:
            return "timeout"


_MOVED = {"user": "the user moved it to the background", "message": "the user wrote a message while it was running",
          "timeout": "it did not finish within the timeout", "stop": "the user stopped the turn"}


def _to_background(ctx: ToolContext, job: FgJob, limit: int) -> ToolResult:
    proc = BgProc(job.pop.pid, job.command, job.pop, job.log, job.started, err=job.err, reason=job.request)
    if ctx.state.get("procs_closed"):  # the chat closed meanwhile: nothing may stay behind
        kill_tree(job.pop.pid)
        return ToolResult("stopped: the chat was closed", is_error=True)
    ctx.state.setdefault("bg_procs", {})[proc.pid] = proc
    if job.request == "stop":
        # The user said stop: the end of the command must not start a new turn. It is told with their next message.
        proc.notify = False
        ctx.state.setdefault("bg_notes", []).append(
            f"the command you were running when the user pressed Stop goes on in the background: pid={proc.pid} "
            f"{job.command[:200]} (process_output, process_stop)")
    _watch(ctx, proc)
    tail = truncate(proc.text().strip(), min(limit, 4000)) or "(no output yet)"
    return ToolResult(f"moved to the background, pid={proc.pid}: {_MOVED.get(job.request, job.request)}. It is still running. A message "
                      "comes to this chat when it ends; go on with other work, don't wait for it or poll it "
                      f"(process_output shows the output, process_stop stops it).\noutput so far:\n{tail}")


def _watch(ctx: ToolContext, proc: BgProc) -> None:
    """Tell the chat that started it when a background command ends."""
    sid = str(ctx.state.get("session_id") or "")
    to_sid = ctx.state.get("deliver_to")
    deliver = (lambda t: to_sid(sid, t)) if callable(to_sid) and sid else ctx.state.get("deliver")
    idle = ctx.state.get("procs_idle")

    def run() -> None:
        code = proc.popen.wait()
        time.sleep(1.0)  # a process_wait/process_output that is just returning tells it first
        if proc.notify and not proc.waiting and not ctx.state.get("procs_closed") and callable(deliver):
            proc.notify = False
            secs = int(time.time() - proc.started)
            deliver(f"[Command pid={proc.pid} finished: exit code {code}, {secs} s]\n{proc.command[:300]}\n"
                    f"--- end of output ---\n{proc.text()[-3000:]}")
        elif callable(idle):
            idle()

    threading.Thread(target=run, daemon=True, name=f"alice-proc-{proc.pid}").start()


def pending(ctx: ToolContext) -> bool:
    """Is there a background command whose end this chat still has to hear about?"""
    return any(p.notify and p.popen.poll() is None for p in list(ctx.state.get("bg_procs", {}).values()))


def procs_view(ctx: ToolContext) -> list[dict[str, Any]]:
    """Commands for the interface: the ones the turn waits for and the ones in the background."""
    now = time.time()
    rows = [{"pid": j.pop.pid, "command": j.command[:300], "state": "foreground", "secs": int(now - j.started),
             "movable": can_background(ctx) and not j.request}
            for j in list(ctx.state.get("fg_jobs", {}).values()) if j.pop.poll() is None]
    for p in list(ctx.state.get("bg_procs", {}).values()):
        code = p.popen.poll()
        rows.append({"pid": p.pid, "command": p.command[:300], "state": "running" if code is None else "done",
                     "code": code, "secs": int(now - p.started), "reason": p.reason})
    return rows


def kill_foreground(ctx: ToolContext, skip: list[int] | tuple[int, ...] = ()) -> None:
    for pop in list(ctx.state.get("fg_procs", ())):
        if pop.pid not in skip and pop.poll() is None:
            try:
                kill_tree(pop.pid)
            except Exception:  # noqa: BLE001 - errors are swallowed while stopping
                pass


def kill_tree(pid: int) -> None:
    import psutil

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.kill()
        except psutil.Error:
            pass  # already gone
    try:
        parent.kill()
    except psutil.Error:
        pass  # already gone


def _visible_window_pids() -> set[int]:
    """PIDs of processes that have a visible top-level window (Windows only)."""
    if not IS_WIN:
        return set()
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    pids: set[int] = set()
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):  # noqa: ANN001
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(int(pid.value))
        return True

    try:
        user32.EnumWindows(proto(cb), 0)
    except OSError:
        pass  # window listing failed: return what was found
    return pids


def _is_gui_exe(path: str) -> bool:
    """Subsystem field in the PE header: 2 = Windows GUI (windowed program), 3 = console."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if head[:2] != b"MZ":
            return False
        pe = int.from_bytes(head[0x3C:0x40], "little")
        if head[pe:pe + 4] != b"PE\0\0":
            return False
        return int.from_bytes(head[pe + 0x5C:pe + 0x5E], "little") == 2
    except (OSError, ValueError):
        return False


def _is_gui_process(proc: Any, window_pids: set[int]) -> bool:
    if not IS_WIN:
        return False  # no reliable distinction on POSIX; the whole tree is killed on timeout
    if proc.pid in window_pids:
        return True
    try:
        return _is_gui_exe(proc.exe())
    except Exception:  # noqa: BLE001 - a process we can't access counts as console
        return False


def kill_console_tree(pid: int) -> None:
    """On timeout: kill the shell and console child processes, but leave windowed programs (and their child
    processes) open. Windowed = has a visible window, or its exe is a GUI subsystem program."""
    import psutil

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.Error:
        return
    window_pids = _visible_window_pids()
    keep: set[int] = set()
    for child in children:
        if child.pid in keep:
            continue
        if _is_gui_process(child, window_pids):
            keep.add(child.pid)
            try:
                keep.update(c.pid for c in child.children(recursive=True))
            except psutil.Error:
                pass  # already gone
    for child in children:
        if child.pid in keep:
            continue
        try:
            child.kill()
        except psutil.Error:
            pass  # already gone
    try:
        parent.kill()
    except psutil.Error:
        pass  # already gone


def _reap(pop: subprocess.Popen) -> None:
    """Wait for the killed process so zombie processes don't pile up in a long session."""
    try:
        pop.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # left to the OS to reap


def cleanup(ctx: ToolContext) -> None:
    ctx.state["procs_closed"] = True
    kill_foreground(ctx)
    for p in list(ctx.state.get("bg_procs", {}).values()):
        p.notify = False
        if p.popen.poll() is None:
            kill_tree(p.pid)
            _reap(p.popen)


class ProcessWait(Tool):
    """The counterpart of the Monitor/until loop in Claude Code: wait in one call, don't waste tool calls."""

    name = "process_wait"
    parallel_safe = True
    description = ("Waits until a background process finishes or a pattern appears in its output, then returns "
                   "the matching lines (or the exit code) and the end of the output.\n"
                   "- For waits like \"when the server is ready\", \"when the build finishes\", \"when the tests "
                   "finish\". Use this instead of calling process_output in a loop or writing sleep with "
                   "run_command.\n"
                   "- Make pattern catch both success and failure: e.g. 'ready|listening|error|Traceback'. If you "
                   "only look for success, you wait in vain until the timeout when the process crashes.\n"
                   "- Without a pattern it waits until the process finishes. Only output that arrives AFTER this call "
                   "is checked.\n"
                   "- On timeout the process is not stopped; the status and output are returned, and you can wait "
                   "again.")
    parameters = obj({
        "pid": {"type": "integer", "description": "pid returned by run_command(background=true)."},
        "pattern": {"type": "string", "description": "Regular expression expected in the output (case-insensitive), "
                    "e.g. 'Local:.*http|error'. Empty: wait until the process finishes."},
        "timeout": {"type": "integer", "description": "Maximum seconds to wait. Default 60, at most 600."},
    }, ["pid"])

    def summary(self, args: dict[str, Any]) -> str:
        return f"pid {args.get('pid')} {args.get('pattern') or 'bitene kadar'}"

    def run(self, ctx: ToolContext, pid: int, pattern: str = "", timeout: int = 60) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} was not started by ALICE; process_output to list them")
        try:
            rx = re.compile(pattern, re.I) if pattern else None
        except re.error as e:
            raise ToolError(f"invalid pattern: {e}") from e

        def read() -> str:
            return p.text()

        p.waiting += 1
        try:
            return self._wait(ctx, p, rx, read, timeout)
        finally:
            p.waiting -= 1

    @staticmethod
    def _wait(ctx: ToolContext, p: BgProc, rx: re.Pattern | None, read: Callable[[], str], timeout: int) -> ToolResult:
        pid = p.pid
        start = len(read())
        end = time.monotonic() + max(1, min(int(timeout or 60), 600))
        while True:
            # Check whether it has finished first, then read. In the reverse order, a process that writes its
            # last line and exits between the read and the poll (often the "error"/"done" being searched for)
            # would be reported as "desen görünmedi" without ever being seen.
            code = p.popen.poll()
            data = read()
            new = data[start:]
            if rx is not None:
                hits = [ln for ln in new.splitlines() if rx.search(ln)]
                if hits:
                    return ToolResult(f"pid={pid} pattern seen:\n" + "\n".join(hits[-20:]) +
                                      f"\n--- end of output ---\n{data[-2000:]}")
            if code is not None:
                p.notify = False
                return ToolResult(f"pid={pid} finished (code {code})" + (" — pattern not seen" if rx else "") +
                                  f"\n{data[-4000:]}", is_error=bool(rx) or code != 0)
            if ctx.cancelled():
                return ToolResult(f"pid={pid} waiting stopped (the process keeps running)")
            if time.monotonic() >= end:
                return ToolResult(f"pid={pid} timeout: still running" + (", pattern not seen" if rx else "") +
                                  f"\n{data[-2000:]}")
            time.sleep(0.5)


TOOLS = [RunCommand(), ProcessOutput(), ProcessStop(), ProcessWait()]
