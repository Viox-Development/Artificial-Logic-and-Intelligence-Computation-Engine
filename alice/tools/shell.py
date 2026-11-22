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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Shell komutu çalıştırır (Windows: PowerShell 5.1, diğer: bash); çıkış kodu ve stdout+stderr döner. "
        "Penceresiz, arka planda çalışır.\n"
        "- Dosya okuma/arama/düzenleme için read_file, grep, glob, edit_file; pencereli program için windows "
        "action=open; git, ağ, arayüz işleri için ilgili özel araçlar.\n"
        "- Sunucu/izleme gibi sürekli işler: background=true, sonra process_output.\n"
        "- Bağımsız komutları aynı cevapta ayrı çağrılar olarak iste (paralel çalışır); birbirine bağlı olanları "
        "tek komutta ';' ile sırala (5.1'de '&&' yok).\n"
        "- Python kodu için command yerine python ver: kod olduğu gibi gider, tırnak kaçışı gerekmez. Girdi için stdin.\n"
        "- Uzun çıktıda tekrarlanan satırlar birleşir, hata satırları ortadan da korunur; tamamı bir dosyaya yazılır."
    )
    parameters = obj({
        "command": {"type": "string", "description": "Çalıştırılacak komut, ör. 'git status' ya da 'python -m pytest -q'. "
                    "python verildiyse boş bırak."},
        "python": {"type": "string", "description": "Çalıştırılacak Python kodu (çok satırlı olabilir). PowerShell'den "
                   "geçmez, tırnaklar bozulmaz. Örn: \"import json; print(json.load(open('a.json'))['x'])\"."},
        "stdin": {"type": "string", "description": "Komutun ya da Python kodunun standart girdisine verilecek metin."},
        "cwd": {"type": "string", "description": "Komutun çalışacağı klasör. Varsayılan: çalışma klasörü."},
        "timeout": {"type": "integer", "description": "Saniye; dolunca komut öldürülür. Varsayılan 120, en fazla 1800."},
        "background": {"type": "boolean", "description": "true: beklemeden arka planda başlat ve pid döndür. "
                       "Varsayılan false."},
        "unsandboxed": {"type": "boolean", "description": "Sadece komut sandbox yüzünden başarısız olduysa ve "
                        "proje dışına yazması gerçekten gerekiyorsa true (kullanıcıya her zaman sorulur)."},
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
            raise ToolError(f"klasör yok: {wd}")
        if (command is not None and not isinstance(command, str)) or (python is not None and not isinstance(python, str)):
            raise ToolError("geçersiz parametre: command ve python metin olmalı")
        command, python = command or "", python or ""
        if python and command:
            raise ToolError("command ya da python'dan yalnız birini ver")
        if not python and not command.strip():
            raise ToolError("eksik zorunlu parametre: command (ya da python)")
        if python:
            # The code goes to the interpreter on stdin, never through a shell: no quoting, no $ or backslash
            # mangling, multi-line works. `py -c "..."` through PowerShell broke on nearly every non-trivial snippet.
            from alice import runtime

            if stdin:
                raise ToolError("python ile stdin birlikte verilemez: kod zaten stdin'den okunuyor; girdiyi kodun "
                                "içine yaz ya da dosyadan oku")
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
                raise ToolError(f"sandbox kurulamadı, komut çalıştırılmadı: {e}. Gerekiyorsa unsandboxed=true ile "
                                "(kullanıcı onayıyla) çalıştır.") from e
            env = sandbox.scrub_env(env, ctx)  # keep API keys from .env out of the sandbox
            env.update(extra)
        from alice import gitsafe

        # The sandbox can write to .git: if a hook or config changes (ALICE or the user later runs git outside
        # the sandbox it kicks in), a warning is added to the result.
        git_before = gitsafe.snapshot(wd) if boxed and not background else None
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        if background and stdin:
            raise ToolError("arka plan sürecine stdin/python verilemez; kodu bir dosyaya yazıp dosyayı çalıştır")
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
            procs[pop.pid] = BgProc(pop.pid, command, pop, log, time.time())
            time.sleep(1.5)
            head = log.read_bytes().decode("utf-8", errors="replace")[-2000:]
            state = "çalışıyor" if pop.poll() is None else f"bitti (kod {pop.returncode})"
            return ToolResult(f"arka planda başlatıldı pid={pop.pid}, durum: {state}\nlog: {log}\n{head}")
        timeout = max(1, min(int(timeout), 1800))
        # Output goes to a temp file instead of a pipe: if a windowed program the command started (e.g. Blender)
        # inherits the pipes, subprocess.run kept waiting forever even after the timeout.
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe, tempfile.TemporaryFile() as fi:
            fg_kw: dict[str, Any] = {"creationflags": NO_WINDOW}
            if IS_WIN:
                fg_kw["startupinfo"] = hidden_startupinfo()
            if stdin:
                # A file, not a pipe, for the same reason as stdout: nothing can block on a full pipe buffer.
                fi.write(stdin.encode("utf-8"))
                fi.seek(0)
            pop = subprocess.Popen(argv, cwd=wd, stdout=fo, stderr=fe, stdin=fi if stdin else subprocess.DEVNULL,
                                   env=env, **fg_kw)
            with track_foreground(ctx, pop):
                try:
                    returncode = pop.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
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
                    returncode, timed_out = -1, True
            fo.seek(0)
            fe.seek(0)
            out = fo.read().decode("utf-8", errors="replace")
            err = fe.read().decode("utf-8", errors="replace")
        if IS_WIN:
            out, err = clean_ps_noise(out), clean_ps_noise(err)
        if timed_out:
            return ToolResult(f"ZAMAN AŞIMI ({timeout} sn). Uzun işler için background=true kullan. Pencereli bir "
                              "program açıyorsan run_command yerine windows action=open kullan; Blender için blender aracını "
                              f"kullan.\n{truncate(out + err, limit)}"
                              + (gitsafe.warning(gitsafe.changes(git_before, gitsafe.snapshot(wd)))
                                 if git_before is not None else ""), is_error=True)
        body = out
        if err.strip():
            body += ("\n[stderr]\n" if out.strip() else "[stderr]\n") + err
        body = smart_truncate(body.strip(), limit, failed=returncode != 0) or "(çıktı yok)"
        if len(out) + len(err) > limit:
            # So truncated output is not lost: the full output is written to a temp file the model can read.
            # mkstemp opens the file as 0600 on POSIX; write_bytes would leave it 0644 and expose the output to
            # other users.
            try:
                fd, name = tempfile.mkstemp(prefix="alice-out-", suffix=".log")
                with os.fdopen(fd, "wb") as fh:
                    fh.write((out + ("\n" + err if err.strip() else "")).encode("utf-8", errors="replace"))
                body += f"\n[tam çıktı dosyada: {name} - kesilen kısmı read_file/grep ile oku]"
            except OSError:
                pass
        if IS_WIN and "is not a valid statement separator" in err:
            body += ("\n[ipucu] Bu Windows PowerShell 5.1: '&&' ve '||' yok. Komutları ';' ile ayır, "
                     "koşul için 'if ($?) { ... }' ya da 'if (-not $?) { ... }' kullan.")
        body += _ps_hint(command, err) if IS_WIN else ""
        # Only on a real access error, or when a command that tries to write fails (a write blocked at low integrity
        # sometimes gives no output at all). An error from a read-only command ("dosya yok") is not from the sandbox.
        if boxed and (ACCESS_DENIED.search(out + err) or (returncode != 0 and not READ_ONLY.match(command))):
            # Only when there really is an access error: once this was added to every failing command,
            # the model also took "dosya yok" as "access
            # blocked" (a Get-Item looking for the nonexistent Sonora-Setup.exe, 2026-11-02).
            body += (f"\n[sandbox: komut sadece {wd} ve geçici klasöre yazabildi. Bu erişim hatası bu yüzdense ve proje "
                     "dışına yazmak gerçekten gerekiyorsa unsandboxed=true ile tekrar dene.]")
        if git_before is not None:
            body += gitsafe.warning(gitsafe.changes(git_before, gitsafe.snapshot(wd)))
        return ToolResult(f"çıkış kodu {returncode}\n{body}", is_error=returncode != 0)


_INLINE_CODE_RX = re.compile(r"(python(\.exe)?\"?|py|node(\.exe)?)\s+-[ce]\s", re.I)


def _ps_hint(command: str, err: str) -> str:
    """One-line fix for common PowerShell syntax errors (the ones that recur in session logs):
    the model spent several turns working out the error."""
    if "was not followed by a valid variable name character" in err:
        return ("\n[ipucu] PowerShell \"$ad: ...\" içindeki ':' işaretini değişken adının parçası sanıyor. "
                "\"${ad}: ...\" ya da \"$($ad): ...\" yaz.")
    if "Unrecognized token" in err and "@@" in command:
        return "\n[ipucu] Sözlük '@{ ... }' ile yazılır; '@@{' geçersiz."
    if _INLINE_CODE_RX.search(command) and ("SyntaxError" in err or "unexpected EOF" in err or "Unexpected token" in err):
        return ("\n[ipucu] PowerShell, -c ile verilen kodun içindeki çift tırnakları ve ters eğik çizgileri bozuyor. "
                "Kodu write_file ile geçici bir dosyaya yaz ve dosyayı çalıştır; satır içi kodda sadece tek tırnak kullan.")
    return ""


class ProcessOutput(Tool):
    name = "process_output"
    parallel_safe = True
    description = ("Arka planda çalışan süreçlerin (run_command background=true) durumunu (çalışıyor / bitti + çıkış kodu) ve "
                   "çıktısının sonunu gösterir; hemen döner, beklemez.\n"
                   "- pid verilmezse ALICE'in başlattığı bütün arka plan süreçlerini listeler.\n"
                   "- Bir sürecin bitmesini ya da çıktısında bir satır (\"ready\", \"error\") görünmesini beklemek için bunu "
                   "tekrar tekrar çağırma; process_wait kullan.\n"
                   "- ALICE'in başlatmadığı programlar için process_list.")
    parameters = obj({
        "pid": {"type": "integer", "description": "run_command'ın döndürdüğü pid. Boş: tüm arka plan süreçleri."},
        "tail_chars": {"type": "integer", "description": "Çıktının sonundan kaç karakter. Varsayılan 4000; "
                       "0: çıktı yok, yalnız durum."},
    })

    def run(self, ctx: ToolContext, pid: int | None = None, tail_chars: int = 4000) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        if pid is None:
            if not procs:
                return ToolResult("arka plan süreci yok")
            rows = [f"pid={p.pid} {'çalışıyor' if p.popen.poll() is None else f'bitti({p.popen.returncode})'} :: {p.command[:100]}"
                    for p in procs.values()]
            return ToolResult("\n".join(rows))
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış")
        data = p.log.read_bytes().decode("utf-8", errors="replace") if p.log.exists() else ""
        state = "çalışıyor" if p.popen.poll() is None else f"bitti (kod {p.popen.returncode})"
        # data[-0:] returned the whole log: 0 and negative values mean "no output, status only".
        n = int(tail_chars or 0)
        if n <= 0:
            return ToolResult(f"pid={pid} {state}")
        return ToolResult(f"pid={pid} {state}\n{data[-n:]}")


class ProcessStop(Tool):
    name = "process_stop"
    description = ("ALICE'in run_command(background=true) ile başlattığı bir süreci, alt süreçleriyle birlikte "
                   "durdurur ve son çıktısını döndürür.\n"
                   "- İşi biten sunucu/izleme süreçlerini açık bırakma; bitince bununla kapat.\n"
                   "- ALICE'in başlatmadığı programlar (kullanıcının açtığı uygulamalar) için process_kill kullan.")
    parameters = obj({"pid": {"type": "integer", "description": "run_command'ın döndürdüğü pid."}}, ["pid"])

    risk = EXEC

    def run(self, ctx: ToolContext, pid: int) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış; başka süreçler için process_kill kullan")
        kill_tree(pid)
        _reap(p.popen)
        return ToolResult(f"durduruldu: {pid}")


@contextlib.contextmanager
def track_foreground(ctx: ToolContext, pop: subprocess.Popen):
    """Record the foreground process so the Stop button can kill it (and its child processes)."""
    procs: set = ctx.state.setdefault("fg_procs", set())
    procs.add(pop)
    try:
        yield pop
    finally:
        procs.discard(pop)


def kill_foreground(ctx: ToolContext) -> None:
    for pop in list(ctx.state.get("fg_procs", ())):
        if pop.poll() is None:
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
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass


def _visible_window_pids() -> set[int]:
    """PIDs of processes that have a visible top-level window (Windows only)."""
    if not IS_WIN:
        return set()
    import ctypes
    from ctypes import wintypes

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
        pass
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
                pass
    for child in children:
        if child.pid in keep:
            continue
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass


def _reap(pop: subprocess.Popen) -> None:
    """Wait for the killed process so zombie processes don't pile up in a long session."""
    try:
        pop.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def cleanup(ctx: ToolContext) -> None:
    for p in ctx.state.get("bg_procs", {}).values():
        if p.popen.poll() is None:
            kill_tree(p.pid)
            _reap(p.popen)


class ProcessWait(Tool):
    """The counterpart of the Monitor/until loop in Claude Code: wait in one call, don't waste tool calls."""

    name = "process_wait"
    parallel_safe = True
    description = ("Arka plandaki bir süreç bitene ya da çıktısında aranan desen görünene kadar bekler, sonra "
                   "eşleşen satırları (ya da bitiş kodunu) ve çıktının sonunu döndürür.\n"
                   "- \"Sunucu hazır olunca\", \"derleme bitince\", \"testler bitince\" gibi beklemeler için. "
                   "process_output'u döngüde çağırmak ya da run_command ile sleep yazmak yerine bunu kullan.\n"
                   "- pattern hem başarıyı hem hatayı yakalasın: ör. 'ready|listening|error|Traceback'. Sadece "
                   "başarıyı ararsan süreç çöktüğünde zaman aşımına kadar boşuna beklersin.\n"
                   "- Desen verilmezse süreç bitene kadar bekler. Sadece bu çağrıdan SONRA gelen çıktıya bakılır.\n"
                   "- Zaman aşımında süreç durdurulmaz; durum ve çıktı döner, istersen tekrar bekle.")
    parameters = obj({
        "pid": {"type": "integer", "description": "run_command(background=true)'ın döndürdüğü pid."},
        "pattern": {"type": "string", "description": "Çıktıda beklenen düzenli ifade (büyük/küçük harf duyarsız), "
                    "ör. 'Local:.*http|error'. Boş: süreç bitene kadar bekle."},
        "timeout": {"type": "integer", "description": "En fazla kaç saniye beklensin. Varsayılan 60, en fazla 600."},
    }, ["pid"])

    def summary(self, args: dict[str, Any]) -> str:
        return f"pid {args.get('pid')} {args.get('pattern') or 'bitene kadar'}"

    def run(self, ctx: ToolContext, pid: int, pattern: str = "", timeout: int = 60) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış; listelemek için process_output")
        try:
            rx = re.compile(pattern, re.I) if pattern else None
        except re.error as e:
            raise ToolError(f"geçersiz desen: {e}") from e

        def read() -> str:
            return p.log.read_bytes().decode("utf-8", errors="replace") if p.log.exists() else ""

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
                    return ToolResult(f"pid={pid} desen göründü:\n" + "\n".join(hits[-20:]) +
                                      f"\n--- çıktının sonu ---\n{data[-2000:]}")
            if code is not None:
                return ToolResult(f"pid={pid} bitti (kod {code})" + (" — desen görünmedi" if rx else "") +
                                  f"\n{data[-4000:]}", is_error=bool(rx) or code != 0)
            if ctx.cancelled():
                return ToolResult(f"pid={pid} bekleme durduruldu (süreç çalışmaya devam ediyor)")
            if time.monotonic() >= end:
                return ToolResult(f"pid={pid} zaman aşımı: hâlâ çalışıyor" + (", desen görünmedi" if rx else "") +
                                  f"\n{data[-2000:]}")
            time.sleep(0.5)


TOOLS = [RunCommand(), ProcessOutput(), ProcessStop(), ProcessWait()]
