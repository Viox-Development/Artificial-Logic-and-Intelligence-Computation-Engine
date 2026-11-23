"""Data-collecting observers.

Each observer returns (key, number, text) rows via `collect()`; the service writes them to the
store. Currently, there is a computer observer; DCS, War Thunder, and watch will be added
using the same interface.

Temperatures: psutil on Linux, nvidia-smi on NVIDIA cards. For CPU temperature on Windows,
if LibreHardwareMonitor is running, its WMI record is read (psutil does not provide temperatures on Windows).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Protocol

import psutil

Row = tuple[str, float | None, str | None]


class Monitor(Protocol):
    name: str

    def collect(self) -> list[Row]: ...


def _key_part(s: str) -> str:
    """Safe segment within a key: 'C:\\' -> 'C', 'Unity Hub.exe' -> 'unity_hub.exe'."""
    return re.sub(r"[^\w.-]+", "_", s.strip().rstrip(":\\/")).strip("_").lower() or "root"


def _run(cmd: list[str], timeout: float = 5) -> str | None:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # On Turkish Windows, PowerShell error text is cp857/cp1254: decoding with the native code page threw
        # UnicodeDecodeError and crashed the observer on every cycle.
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=flags)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _pid(text: str | None) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m and int(m.group()) > 0 else None


def _sway_focused_pid(tree: Any) -> int | None:
    """swaymsg -t get_tree: the focused node's pid (depth-first over nodes and floating_nodes)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("focused") and node.get("pid"):
            return int(node["pid"])
        stack.extend(node.get("nodes") or [])
        stack.extend(node.get("floating_nodes") or [])
    return None


def _json(text: str | None) -> Any:
    import json

    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _x11_pid() -> int | None:
    if shutil.which("xdotool"):
        if pid := _pid(_run(["xdotool", "getactivewindow", "getwindowpid"])):
            return pid
    if shutil.which("xprop"):
        m = re.search(r"0x[0-9a-fA-F]+", _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"]) or "")
        if m and int(m.group(), 16):
            out = _run(["xprop", "-id", m.group(), "_NET_WM_PID"]) or ""
            return _pid(out.partition("=")[2])
    return None


def _gnome_pid() -> int | None:
    """GNOME Shell's Eval; it only answers when unsafe mode or an extension allows it, otherwise (false, '')."""
    if not shutil.which("gdbus"):
        return None
    out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell",
                "--method", "org.gnome.Shell.Eval", "global.display.focus_window.get_pid()"])
    return _pid(out) if out and out.lstrip("( ").startswith("true") else None


def linux_foreground_pid() -> int | None:
    """PID of the focused window on Linux: Sway, Hyprland, KDE (kdotool), GNOME (Shell Eval) or X11 (xdotool/xprop).
    None when the desktop is not supported or the tool is missing."""
    env = os.environ
    desktop = (env.get("XDG_CURRENT_DESKTOP") or "").upper()
    wayland = bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"
    if env.get("SWAYSOCK") and shutil.which("swaymsg"):
        if pid := _sway_focused_pid(_json(_run(["swaymsg", "-t", "get_tree"]))):
            return pid
    if env.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        win = _json(_run(["hyprctl", "activewindow", "-j"]))
        if isinstance(win, dict) and isinstance(win.get("pid"), int) and win["pid"] > 0:
            return win["pid"]
    if "KDE" in desktop and shutil.which("kdotool"):
        if pid := _pid(_run(["kdotool", "getactivewindow", "getwindowpid"])):
            return pid
    if "GNOME" in desktop and wayland:
        if pid := _gnome_pid():
            return pid
    if env.get("DISPLAY") and not wayland:
        return _x11_pid()  # not on Wayland: XWayland's active window can be stale while a native window has focus
    return None


def foreground_process() -> str | None:
    """Program name of the foreground window (Windows; Linux on X11 and the common Wayland compositors)."""
    if sys.platform.startswith("linux"):
        pid = linux_foreground_pid()
        try:
            return psutil.Process(pid).name() if pid else None
        except psutil.Error:
            return None
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name() if pid.value else None
    except (OSError, AttributeError, psutil.Error):
        return None


_PRIVATE_READER: Any = False  # False: not attempted yet, None: unavailable


def _private_ws_reader() -> Callable[[int], int | None] | None:
    """Windows: process -> private working set (Task Manager's "Memory" column). None if unavailable."""
    global _PRIVATE_READER
    if _PRIVATE_READER is not False:
        return _PRIVATE_READER
    _PRIVATE_READER = None
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX2 (Windows 10 1809+)
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                    "PrivateUsage", "PrivateWorkingSetSize", "SharedCommitUsage")]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue] - Windows
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION

        def read(pid: int) -> int | None:
            h = k32.OpenProcess(query, False, pid)
            if not h:
                return None
            try:
                c = Counters()
                c.cb = ctypes.sizeof(Counters)
                if not k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                    return None
                return int(c.PrivateWorkingSetSize) or None
            finally:
                k32.CloseHandle(h)

        if read(os.getpid()):  # unsupported on older Windows (returns zero/failure): fall back to rss
            _PRIVATE_READER = read
    except (OSError, AttributeError, ImportError):
        pass
    return _PRIVATE_READER


def _win_proc_stats() -> list[tuple[int, str, int, float]] | None:
    """Windows: (pid, name, rss bytes, user+system cpu sec); inaccessible processes are skipped. None on another
    platform or if psutil internal structure changed (slow generic path is used)."""
    if sys.platform != "win32":
        return None
    try:
        from psutil import _psplatform

        mem_info, times = _psplatform.cext.proc_memory_info, _psplatform.cext.proc_times
    except (ImportError, AttributeError):
        return None
    out = []
    private = _private_ws_reader()
    for p in psutil.process_iter(["name"]):  # names come from a single snapshot: fast
        name = p.info.get("name") or ""
        if not name:
            continue
        try:
            # In multi-process browsers (Opera GX: 61 procs), WorkingSet counts shared pages repeatedly in each process:
            # totaling 9.6 GB ("Opera uses 10 GB"), while Task Manager showed 2.4 GB. Only private portion is counted.
            rss = (private(p.pid) if private else None) or mem_info(p.pid)[2]  # psutil rss = WorkingSetSize
            user, system, _created = times(p.pid)
        except Exception:  # noqa: BLE001 - protected process (no access) or process exited: skip
            continue
        out.append((p.pid, name, int(rss), float(user) + float(system)))
    return out


class PCMonitor:
    name = "pc"

    def __init__(self, top_n: int = 5):
        self.top_n = top_n
        self.processes: set[str] = set()  # process names in the last collection (for mode detection)
        self._nvidia = shutil.which("nvidia-smi")
        self._lhm = sys.platform == "win32"  # disabled on first failure
        # -inf: first measurement is taken immediately (monotonic() can be < 60 in the first 60s after boot)
        self._disk_at = float("-inf")
        self._temps_at = float("-inf")
        self._temps_last: list[Row] = []
        psutil.cpu_percent(interval=None)  # first call always returns 0, warm it up

    def collect(self) -> list[Row]:
        rows: list[Row] = [("pc.cpu.percent", psutil.cpu_percent(interval=None), None)]
        vm = psutil.virtual_memory()
        rows.append(("pc.mem.percent", vm.percent, None))
        rows.append(("pc.mem.used_gb", vm.used / 2**30, None))
        rows += self._disks()
        rows += self._temps()
        rows += self._gpu()
        rows += self._procs()
        fg = foreground_process()
        if fg:
            rows.append(("pc.foreground", None, fg))
        return rows

    def _disks(self) -> list[Row]:
        # Disk usage changes slowly, once a minute is sufficient.
        if time.monotonic() - self._disk_at < 60:
            return []
        self._disk_at = time.monotonic()
        out: list[Row] = []
        for part in psutil.disk_partitions(all=False):
            opts = part.opts.split(",")
            # CD-ROM and read-only mounts (squashfs, etc.) always appear "100% full", skip.
            if "cdrom" in opts or "ro" in opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            out.append((f"pc.disk.{_key_part(part.mountpoint)}.percent", u.percent, None))
            # Percentage is noisy on large data disks (F: always 98%), too late on small system disks: also write free GB.
            out.append((f"pc.disk.{_key_part(part.mountpoint)}.free_gb", round(u.free / 1024 ** 3, 2), None))
        return out

    def _temps(self) -> list[Row]:
        out: list[Row] = []
        fn = getattr(psutil, "sensors_temperatures", None)
        if fn:
            try:
                temps = fn() or {}
            except (OSError, RuntimeError):
                temps = {}
            cpu = [t.current for name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")
                   for t in temps.get(name, []) if t.current]
            if cpu:
                out.append(("pc.cpu.temp", max(cpu), None))
        if self._lhm and not out:
            # Each query spawns a PowerShell (~0.5s CPU): once a minute is sufficient rather than every 15s.
            if time.monotonic() - self._temps_at >= 60:
                self._temps_at = time.monotonic()
                self._temps_last = self._lhm_temps()
            out += self._temps_last
        return out

    def _lhm_temps(self) -> list[Row]:
        ps = ("Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction Stop | "
              "Where-Object {$_.SensorType -eq 'Temperature'} | ForEach-Object {\"$($_.Identifier)|$($_.Value)\"}")
        text = _run(["powershell", "-NoProfile", "-Command", ps], timeout=8)
        if not text:
            self._lhm = False
            return []
        cpu = []
        for ident, _, v in (ln.partition("|") for ln in text.splitlines()):
            if "cpu" not in ident.lower():
                continue
            try:
                cpu.append(float(v.strip().replace(",", ".")))  # Decimal comma in Turkish locale
            except ValueError:
                continue
        return [("pc.cpu.temp", max(cpu), None)] if cpu else []

    def _gpu(self) -> list[Row]:
        if not self._nvidia:
            return []
        text = _run([self._nvidia, "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"])
        if not text:
            return []
        try:
            temp, util, used, total = (float(x) for x in text.splitlines()[0].split(","))
        except ValueError:
            return []
        return [("pc.gpu.temp", temp, None), ("pc.gpu.util", util, None),
                ("pc.gpu.mem_percent", 100 * used / total if total else 0.0, None)]

    def _procs(self) -> list[Row]:
        mem: dict[str, float] = {}
        cpu: dict[str, float] = {}
        names: set[str] = set()
        fast = _win_proc_stats()
        if fast is not None:
            # Windows: psutil snapshots the whole system and retries on inaccessible (protected) processes,
            # without releasing the GIL. Across 465 processes, scan took 17s (every 15s): the voice assistant's
            # listening loop starved on GIL and dropped mic audio, "Alice naber" was never heard (Oct 30,
            # observed with py-spy). The path skipping protected processes takes 0.02s.
            now = time.monotonic()
            prev, self._cpu_prev = getattr(self, "_cpu_prev", {}), {}
            for pid, name, rss, busy in fast:
                names.add(name)
                mem[name] = mem.get(name, 0.0) + rss / 2**20
                self._cpu_prev[pid] = (busy, now)
                if pid in prev and now > prev[pid][1]:
                    pct = 100 * (busy - prev[pid][0]) / (now - prev[pid][1])
                    cpu[name] = cpu.get(name, 0.0) + max(0.0, pct)
        else:
            for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
                name = p.info.get("name") or ""
                if not name:
                    continue
                names.add(name)
                mi = p.info.get("memory_info")
                mem[name] = mem.get(name, 0.0) + (mi.rss / 2**20 if mi else 0.0)
                cpu[name] = cpu.get(name, 0.0) + (p.info.get("cpu_percent") or 0.0)
        self.processes = names
        out: list[Row] = []
        for name in sorted(mem, key=lambda n: mem[n], reverse=True)[: self.top_n]:
            out.append((f"pc.proc.{_key_part(name)}.mem_mb", mem[name], None))
        for name in sorted(cpu, key=lambda n: cpu[n], reverse=True)[: self.top_n]:
            if cpu[name] > 0:
                out.append((f"pc.proc.{_key_part(name)}.cpu", cpu[name], None))
        return out
