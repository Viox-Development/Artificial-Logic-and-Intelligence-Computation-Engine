"""Shell komutları ve arka plan süreçleri.

Windows'ta PowerShell (varsa pwsh) kullanılır ve çıktı UTF-8'e zorlanır; yoksa Türkçe
karakterler cp857 yüzünden bozuk gelir.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, EXEC, Tool, ToolContext, ToolError, ToolResult, obj, truncate

IS_WIN = sys.platform == "win32"
# Konsolsuz çalışan çekirdekten (pythonw) her komut ayrı bir siyah pencere açmasın. Komutun başlattığı
# pencereli programlar (Blender, Not Defteri...) yine görünür.
NO_WINDOW = 0x08000000 if IS_WIN else 0

# Geri dönüşü zor ya da sistemi bozabilecek kalıplar. "auto" modda bunlar için onay istenir.
DANGEROUS = [
    r"\brm\s+(-\w+\s+)*-\w*[rR]",  # özyinelemeli silme
    r"\bmkfs\b", r"\bdd\s+if=", r"\bfdisk\b", r"\bdiskpart\b", r"\bformat\s+[a-z]:",
    r"\b(shutdown|reboot|halt|poweroff)\b", r"Stop-Computer", r"Restart-Computer",
    r"Remove-Item\b.*-Recurse", r"\brd\s+/s\b", r"\brmdir\s+/s\b", r"\bdel\s+/[sq]",
    # PowerShell takma adları, bayrak yoldan sonra: "rm .\build -Recurse -Force", "ri C:\x -r"
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


def is_dangerous(command: str) -> bool:
    return any(rx.search(command) for rx in _DANGER_RX)


def shell_argv(command: str) -> list[str]:
    if IS_WIN:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; "
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prefix + command]
    sh = shutil.which("bash") or "/bin/sh"
    return [sh, "-lc", command]


def shell_name() -> str:
    if IS_WIN:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return "bash" if shutil.which("bash") else "sh"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


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
        "Shell komutu çalıştırır (Windows: PowerShell 5.1, diğer: bash); çıkış kodu ve stdout+stderr döner.\n"
        "- Dosya okumak/aramak/düzenlemek için komut değil read_file, grep, glob, edit_file kullan.\n"
        "- Pencereli program ya da site açmak için windows action=open; Blender için blender aracı.\n"
        "- Sürekli çalışan işler (sunucu, izleme) için background=true, sonra process_output.\n"
        "- PowerShell 5.1'de '&&' yok: komutları ';' ile ayır. Boşluklu yolları tırnak içine al."
    )
    parameters = obj({
        "command": {"type": "string", "description": "Çalıştırılacak komut, ör. 'git status' ya da 'python -m pytest -q'."},
        "cwd": {"type": "string", "description": "Komutun çalışacağı klasör. Varsayılan: çalışma klasörü."},
        "timeout": {"type": "integer", "description": "Saniye; dolunca komut öldürülür. Varsayılan 120, en fazla 1800."},
        "background": {"type": "boolean", "description": "true: beklemeden arka planda başlat ve pid döndür. "
                       "Varsayılan false."},
        "unsandboxed": {"type": "boolean", "description": "Sadece komut sandbox yüzünden başarısız olduysa ve "
                        "proje dışına yazması gerçekten gerekiyorsa true (kullanıcıya her zaman sorulur)."},
    }, ["command"])
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("unsandboxed"):
            return DANGER  # sandbox'tan çıkmak her zaman onay ister
        return DANGER if is_dangerous(args.get("command", "")) else EXEC

    def summary(self, args: dict[str, Any]) -> str:
        return args.get("command", "")[:300]

    def run(self, ctx: ToolContext, command: str, cwd: str | None = None, timeout: int = 120,
            background: bool = False, unsandboxed: bool = False) -> ToolResult:
        wd = ctx.resolve(cwd) if cwd else ctx.cwd
        if not wd.is_dir():
            raise ToolError(f"klasör yok: {wd}")
        argv = shell_argv(command)
        env = _env()
        from alice import sandbox

        boxed = sandbox.applies(ctx, {"command": command, "unsandboxed": unsandboxed})
        if boxed:
            try:
                argv, extra = sandbox.wrap(argv, wd, ctx)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                raise ToolError(f"sandbox kurulamadı, komut çalıştırılmadı: {e}. Gerekiyorsa unsandboxed=true ile "
                                "(kullanıcı onayıyla) çalıştır.") from e
            env.update(extra)
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        if background:
            log = Path(tempfile.gettempdir()) / f"alice-bg-{int(time.time() * 1000)}.log"
            fh = open(log, "wb")
            kwargs: dict[str, Any] = {}
            if IS_WIN:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW  # type: ignore[attr-defined]
            else:
                kwargs["start_new_session"] = True
            pop = subprocess.Popen(argv, cwd=wd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   env=env, **kwargs)
            procs: dict[int, BgProc] = ctx.state.setdefault("bg_procs", {})
            procs[pop.pid] = BgProc(pop.pid, command, pop, log, time.time())
            time.sleep(1.5)
            head = log.read_bytes().decode("utf-8", errors="replace")[-2000:]
            state = "çalışıyor" if pop.poll() is None else f"bitti (kod {pop.returncode})"
            return ToolResult(f"arka planda başlatıldı pid={pop.pid}, durum: {state}\nlog: {log}\n{head}")
        timeout = max(1, min(int(timeout), 1800))
        # Çıktı boru yerine geçici dosyaya yazılır: komutun başlattığı pencereli bir program (ör. Blender)
        # boruları devralırsa subprocess.run zaman aşımından sonra bile sonsuza kadar bekliyordu.
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            pop = subprocess.Popen(argv, cwd=wd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, env=env,
                                   creationflags=NO_WINDOW)
            with track_foreground(ctx, pop):
                try:
                    returncode = pop.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    pop.kill()  # sadece kabuğu öldür; açtığı pencereli program açık kalsın
                    pop.wait()
                    returncode, timed_out = -1, True
            fo.seek(0)
            fe.seek(0)
            out = fo.read().decode("utf-8", errors="replace")
            err = fe.read().decode("utf-8", errors="replace")
        if timed_out:
            return ToolResult(f"ZAMAN AŞIMI ({timeout} sn). Uzun işler için background=true kullan. Pencereli bir "
                              "program açıyorsan run_command yerine windows action=open kullan; Blender için blender aracını "
                              f"kullan.\n{truncate(out + err, limit)}", is_error=True)
        body = out
        if err.strip():
            body += ("\n[stderr]\n" if out.strip() else "[stderr]\n") + err
        body = truncate(body.strip(), limit) or "(çıktı yok)"
        if IS_WIN and "is not a valid statement separator" in err:
            body += ("\n[ipucu] Bu Windows PowerShell 5.1: '&&' ve '||' yok. Komutları ';' ile ayır, "
                     "koşul için 'if ($?) { ... }' ya da 'if (-not $?) { ... }' kullan.")
        if boxed:
            body += (f"\n[sandbox: komut sadece {wd} ve geçici klasöre yazabildi. 'Erişim reddedildi' gibi bir hata bu "
                     "yüzdense ve proje dışına yazmak gerçekten gerekiyorsa unsandboxed=true ile tekrar dene.]")
        return ToolResult(f"çıkış kodu {returncode}\n{body}", is_error=returncode != 0)


class ProcessOutput(Tool):
    name = "process_output"
    parallel_safe = True
    description = ("run_command(background=true) ile başlatılan süreçlerin durumunu ve son çıktısını gösterir. pid "
                   "verilmezse hepsini listeler.")
    parameters = obj({
        "pid": {"type": "integer", "description": "run_command'ın döndürdüğü pid. Boş: tüm arka plan süreçleri."},
        "tail_chars": {"type": "integer", "description": "Çıktının sonundan kaç karakter. Varsayılan 4000."},
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
        return ToolResult(f"pid={pid} {state}\n{data[-tail_chars:]}")


class ProcessStop(Tool):
    name = "process_stop"
    description = ("ALICE'in run_command(background=true) ile başlattığı bir süreci durdurur. Başka programlar için "
                   "process_kill kullan.")
    parameters = obj({"pid": {"type": "integer", "description": "run_command'ın döndürdüğü pid."}}, ["pid"])

    risk = EXEC

    def run(self, ctx: ToolContext, pid: int) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış; başka süreçler için process_kill kullan")
        kill_tree(pid)
        return ToolResult(f"durduruldu: {pid}")


@contextlib.contextmanager
def track_foreground(ctx: ToolContext, pop: subprocess.Popen):
    """Ön planda beklenen süreci kaydet ki Durdur tuşu onu (ve alt süreçlerini) sonlandırabilsin."""
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
            except Exception:  # noqa: BLE001 - durdururken hata yutulur
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


def cleanup(ctx: ToolContext) -> None:
    for p in ctx.state.get("bg_procs", {}).values():
        if p.popen.poll() is None:
            kill_tree(p.pid)


TOOLS = [RunCommand(), ProcessOutput(), ProcessStop()]
