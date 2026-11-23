from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any

import psutil

from alice.tools.shell import NO_WINDOW
from alice.tools.base import DANGER, EXEC, Tool, ToolContext, ToolError, ToolResult, obj

PROTECTED = {"system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
             "explorer.exe", "init", "systemd", "launchd", "kernel_task", "dwm.exe"}


def _gpu() -> str:
    # Time limit: when WMI is busy or a driver hangs, system_info (and the parallel tool group) must not wait forever.
    try:
        if shutil.which("nvidia-smi"):
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                "--format=csv,noheader"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=5, creationflags=NO_WINDOW)
            if r.returncode == 0:
                return r.stdout.strip()
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_VideoController).Name -join ', '"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=8, creationflags=NO_WINDOW)
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "bilinmiyor"


class SystemInfo(Tool):
    name = "system_info"
    parallel_safe = True
    description = ("Returns the computer's current state in one go: operating system, CPU and RAM use, disks (how "
                   "full), GPU (temperature, use), network and battery.\n"
                   "- For questions about right now like 'why is the computer slow', 'how full is the disk', 'how hot "
                   "is the GPU'.\n"
                   "- For past values ('how hot was the GPU yesterday') use metrics; for which program is the load, "
                   "process_list.\n"
                   "- Don't try to collect the same information with run_command (wmic, Get-CimInstance).")
    parameters = obj({})

    def run(self, ctx: ToolContext) -> ToolResult:
        vm = psutil.virtual_memory()
        rows = [
            f"OS: {platform.platform()} ({platform.machine()})",
            f"CPU: {platform.processor() or 'bilinmiyor'} | {psutil.cpu_count(logical=False)}C/{psutil.cpu_count()}T | "
            f"kullanım %{psutil.cpu_percent(interval=0.5)}",
            f"RAM: {vm.used / 2**30:.1f}/{vm.total / 2**30:.1f} GB (%{vm.percent})",
            f"GPU: {_gpu()}",
        ]
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            rows.append(f"Disk {part.mountpoint}: {u.used / 2**30:.0f}/{u.total / 2**30:.0f} GB (%{u.percent}) {part.fstype}")
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if bat:
            rows.append(f"Pil: %{bat.percent:.0f} {'şarjda' if bat.power_plugged else 'pilde'}")
        net = psutil.net_io_counters()
        rows.append(f"Ağ: gönderilen {net.bytes_sent / 2**20:.0f} MB, alınan {net.bytes_recv / 2**20:.0f} MB")
        return ToolResult("\n".join(rows))


class ProcessList(Tool):
    name = "process_list"
    parallel_safe = True
    description = ("Lists running processes as a table with PID, RAM and CPU use.\n"
                   "- For questions like 'what is eating so much RAM', 'is X open', and to find the right name/PID "
                   "before process_kill.\n"
                   "- For open windows use windows action=list; for background commands ALICE started, "
                   "process_output.\n"
                   "- Don't write a tasklist or Get-Process command; use this.")
    parameters = obj({
        "name": {"type": "string", "description": "Processes whose name contains this, e.g. 'chrome'. Empty: all."},
        "sort": {"type": "string", "enum": ["cpu", "memory"], "description": "Sort order. Default memory."},
        "limit": {"type": "integer", "description": "Maximum rows. Default 40."},
    })

    def run(self, ctx: ToolContext, name: str | None = None, sort: str = "memory", limit: int = 40) -> ToolResult:
        procs: list[dict[str, Any]] = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            info = p.info
            if name and name.lower() not in (info["name"] or "").lower():
                continue
            procs.append(info)
        key = (lambda i: i["cpu_percent"] or 0) if sort == "cpu" else (lambda i: i["memory_info"].rss if i["memory_info"] else 0)
        procs.sort(key=key, reverse=True)
        rows = [f"{i['pid']:>7}  {(i['memory_info'].rss if i['memory_info'] else 0) / 2**20:>8.0f} MB  "
                f"%{i['cpu_percent'] or 0:>5.1f}  {i['name']}" for i in procs[:limit]]
        return ToolResult("    PID       RAM     CPU  İSİM\n" + "\n".join(rows) if rows else "süreç bulunamadı")


def _same_name(proc_name: str, wanted: str) -> bool:
    """Exact name, also without '.exe' on Windows: the model often writes 'notepad' (it used to return no match)."""
    a, b = proc_name.lower(), wanted.strip().lower()
    return a == b or (sys.platform == "win32" and not b.endswith(".exe") and a == f"{b}.exe")


class ProcessKill(Tool):
    name = "process_kill"
    description = ("Force-ends a process by PID or exact name; by name, ALL processes with that name are closed.\n"
                   "- Unsaved work can be lost: to close an app gently, try windows action=close first.\n"
                   "- If you don't know the name or PID, use process_list first. Don't close system processes "
                   "(explorer, svchost, csrss).\n"
                   "- For background commands ALICE started itself use process_stop.")
    parameters = obj({
        "pid": {"type": "integer", "description": "PID of the process to end (from process_list)."},
        "name": {"type": "string", "description": "Exact process name, case-insensitive, e.g. 'notepad.exe'. Used "
                                                  "when pid is not given."},
    })

    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        n = (args.get("name") or "").lower()
        if n and not n.endswith(".exe") and f"{n}.exe" in PROTECTED:
            n += ".exe"  # "explorer" also closes explorer.exe (see _same_name)
        pid = args.get("pid")
        if pid:  # when given by PID too, ask before touching protected processes (explorer, winlogon) and ALICE itself
            import os

            try:
                pid = int(pid)
                if pid in (os.getpid(), os.getppid()) or psutil.Process(pid).name().lower() in PROTECTED:
                    return DANGER
            except (ValueError, TypeError, psutil.Error):
                pass
        return DANGER if n in PROTECTED or not (pid or n) else EXEC

    def run(self, ctx: ToolContext, pid: int | None = None, name: str | None = None) -> ToolResult:
        # Also handle ALICE's own background processes smoothly: if the model passed a bg_proc PID,
        # terminate it through process_stop logic rather than failing.
        bg_procs = ctx.state.get("bg_procs") or {}
        if pid and int(pid) in bg_procs:
            from alice.tools.shell import ProcessStop
            return ProcessStop().run(ctx, pid=int(pid))
        targets = []
        if pid:
            try:
                targets = [psutil.Process(pid)]
            except psutil.NoSuchProcess:
                raise ToolError(f"pid {pid} ile çalışan süreç yok (zaten kapanmış olabilir)") from None
        elif name:
            targets = [p for p in psutil.process_iter(["name"]) if _same_name(p.info["name"] or "", name)]
        if not targets:
            raise ToolError(f"eşleşen süreç yok: {name or pid} (adı process_list ile bak)")
        failed: dict[int, str] = {}
        for p in targets:
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass  # it exited on its own in the meantime
            except psutil.Error as e:
                failed[p.pid] = str(e) or type(e).__name__
        _, alive = psutil.wait_procs([p for p in targets if p.pid not in failed], timeout=3)
        for p in alive:  # process that ignores SIGTERM: force it
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as e:
                failed[p.pid] = str(e) or type(e).__name__
        if alive:
            _, alive = psutil.wait_procs(alive, timeout=2)
        for p in alive:
            failed.setdefault(p.pid, "hâlâ çalışıyor")
        # Processes that never exited used to be reported as "sonlandırıldı" too (access denied, exit ignored).
        ok = [str(p.pid) for p in targets if p.pid not in failed]
        parts = [f"sonlandırıldı: {', '.join(ok)}"] if ok else []
        parts += [f"{pid_}: kapatılamadı ({why})" for pid_, why in failed.items()]
        return ToolResult("\n".join(parts), is_error=bool(failed) and not ok)


TOOLS = [SystemInfo(), ProcessList(), ProcessKill()]
