from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from alice.core import monitors
from alice.core.monitors import PCMonitor, _key_part, _run, foreground_process


# ---------------------------------------------------------------- helpers
@pytest.mark.parametrize("raw, want", [
    ("C:\\", "c"),
    ("/", "root"),
    ("", "root"),
    ("/home/user", "home_user"),
    ("Unity Hub.exe", "unity_hub.exe"),
    ("  D:/Games  ", "d_games"),
    ("my-app_v2.exe", "my-app_v2.exe"),
    ("a$$b", "a_b"),
])
def test_key_part(raw, want):
    assert _key_part(raw) == want


def test_run_returns_stdout_on_success(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return SimpleNamespace(returncode=0, stdout="ok\n")

    monkeypatch.setattr(monitors.subprocess, "run", fake_run)
    assert _run(["tool", "-x"], timeout=3) == "ok\n"
    assert seen["cmd"] == ["tool", "-x"] and seen["timeout"] == 3 and seen["capture_output"] is True


def test_run_none_on_failure_or_error(monkeypatch):
    monkeypatch.setattr(monitors.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="x"))
    assert _run(["t"]) is None

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(monitors.subprocess, "run", boom)
    assert _run(["t"]) is None

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(monitors.subprocess, "run", missing)
    assert _run(["t"]) is None


def test_foreground_process_none_on_unsupported_systems(monkeypatch):
    monkeypatch.setattr(monitors.sys, "platform", "darwin")
    assert foreground_process() is None


# ---------------------------------------------------------------- Linux foreground window
LINUX_ENV = ("SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
             "DISPLAY")


@pytest.fixture
def desktop(monkeypatch):
    """Fake Linux desktop: set env vars, the installed tools and each command's output."""
    monkeypatch.setattr(monitors.sys, "platform", "linux")
    calls = []

    def make(env, outputs):
        for k in LINUX_ENV:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        tools = {cmd.split()[0] for cmd in outputs}
        monkeypatch.setattr(monitors.shutil, "which", lambda n: f"/usr/bin/{n}" if n in tools else None)

        def run(cmd, timeout=5):
            calls.append(" ".join(cmd))
            return next((out for k, out in outputs.items() if " ".join(cmd).startswith(k)), None)

        monkeypatch.setattr(monitors, "_run", run)
        return calls
    return make


SWAY_TREE = json.dumps({"nodes": [{"nodes": [{"focused": False, "pid": 11, "nodes": []},
                                              {"nodes": [], "floating_nodes": [{"focused": True, "pid": 42}]}]}]})


@pytest.mark.parametrize("env,outputs", [
    ({"DISPLAY": ":0"}, {"xdotool getactivewindow": "42\n"}),
    ({"DISPLAY": ":0"}, {"xprop -root": "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007\n",
                         "xprop -id 0x3a00007": "_NET_WM_PID(CARDINAL) = 42\n"}),
    ({"SWAYSOCK": "/run/sway", "WAYLAND_DISPLAY": "wayland-1"}, {"swaymsg -t get_tree": SWAY_TREE}),
    ({"HYPRLAND_INSTANCE_SIGNATURE": "x", "WAYLAND_DISPLAY": "wayland-1"},
     {"hyprctl activewindow": json.dumps({"class": "kitty", "pid": 42})}),
    ({"XDG_CURRENT_DESKTOP": "KDE", "WAYLAND_DISPLAY": "wayland-0"}, {"kdotool getactivewindow": "42\n"}),
    ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "XDG_SESSION_TYPE": "wayland"},
     {"gdbus call": "(true, '42')\n"}),
])
def test_linux_foreground_pid_per_desktop(desktop, env, outputs):
    desktop(env, outputs)
    assert monitors.linux_foreground_pid() == 42


def test_linux_foreground_pid_unknown_cases(desktop):
    desktop({"XDG_CURRENT_DESKTOP": "GNOME", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
            {"gdbus call": "(false, '')\n", "xdotool getactivewindow": "99\n"})
    assert monitors.linux_foreground_pid() is None  # GNOME refuses Eval; XWayland's answer is not trusted
    desktop({"DISPLAY": ":0"}, {"xprop -root": "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"})
    assert monitors.linux_foreground_pid() is None
    desktop({"HYPRLAND_INSTANCE_SIGNATURE": "x"}, {"hyprctl activewindow": "not json"})
    assert monitors.linux_foreground_pid() is None


def test_foreground_process_on_linux_names_the_process(desktop, monkeypatch):
    desktop({"DISPLAY": ":0"}, {"xdotool getactivewindow": f"{os.getpid()}\n"})
    assert foreground_process() == psutil.Process(os.getpid()).name()
    desktop({"DISPLAY": ":0"}, {"xdotool getactivewindow": "999999999\n"})
    assert foreground_process() is None  # the process is gone


# ---------------------------------------------------------------- PCMonitor
class Proc:
    def __init__(self, name, rss_mb=None, cpu=None):
        mi = SimpleNamespace(rss=int(rss_mb * 2**20)) if rss_mb is not None else None
        self.info = {"name": name, "memory_info": mi, "cpu_percent": cpu}


def Part(mount, fstype="ext4", opts="rw"):
    return SimpleNamespace(mountpoint=mount, fstype=fstype, opts=opts)


@pytest.fixture
def fake_ps(monkeypatch):
    """Replace psutil with fake values; tests may modify the fields."""
    state = SimpleNamespace(
        cpu=12.5,
        vm=SimpleNamespace(percent=40.0, used=8 * 2**30),
        parts=[Part("/")],
        usage={"/": 55.0},
        temps={},
        procs=[],
        run_out={},
        run_calls=[],
    )
    # On a freshly started CI machine, time.monotonic() can be < 60; then the first disk/temperature read
    # hits the "once a minute" limit. Use a fixed, large clock.
    monkeypatch.setattr(monitors.time, "monotonic", lambda: 1_000_000.0)
    monkeypatch.setattr(monitors.psutil, "cpu_percent", lambda interval=None: state.cpu)
    monkeypatch.setattr(monitors.psutil, "virtual_memory", lambda: state.vm)
    monkeypatch.setattr(monitors.psutil, "disk_partitions", lambda all=False: state.parts)

    def usage(mp):
        v = state.usage[mp]
        if isinstance(v, Exception):
            raise v
        return SimpleNamespace(percent=v, free=(100 - v) * 1024 ** 3)

    monkeypatch.setattr(monitors.psutil, "disk_usage", usage)

    def temps():
        if isinstance(state.temps, Exception):
            raise state.temps
        return state.temps

    monkeypatch.setattr(monitors.psutil, "sensors_temperatures", temps, raising=False)
    monkeypatch.setattr(monitors.psutil, "process_iter", lambda attrs=None: list(state.procs))
    monkeypatch.setattr(monitors, "_win_proc_stats", lambda: None)  # The Windows fast path is tested separately
    monkeypatch.setattr(monitors.shutil, "which", lambda name: None)
    monkeypatch.setattr(monitors, "foreground_process", lambda: None)

    def fake_run(cmd, timeout=5):
        state.run_calls.append(cmd)
        return state.run_out.get(cmd[0])

    monkeypatch.setattr(monitors, "_run", fake_run)
    return state


def make_mon(**kw):
    m = PCMonitor(**kw)
    m._lhm = False
    return m


def keys(rows):
    return [k for k, _, _ in rows]


def test_collect_basic_rows(fake_ps):
    mon = make_mon()
    rows = mon.collect()
    d = {k: v for k, v, _ in rows}
    assert d["pc.cpu.percent"] == 12.5
    assert d["pc.mem.percent"] == 40.0
    assert d["pc.mem.used_gb"] == pytest.approx(8.0)
    assert d["pc.disk.root.percent"] == 55.0
    assert "pc.foreground" not in d


def test_first_disk_reading_right_after_boot(fake_ps, monkeypatch):
    monkeypatch.setattr(monitors.time, "monotonic", lambda: 5.0)  # 5 s after boot
    assert "pc.disk.root.percent" in keys(make_mon().collect())


def test_collect_adds_foreground(fake_ps, monkeypatch):
    monkeypatch.setattr(monitors, "foreground_process", lambda: "DCS.exe")
    rows = make_mon().collect()
    assert ("pc.foreground", None, "DCS.exe") in rows


def test_disks_skip_readonly_cdrom_and_errors_and_throttle(fake_ps):
    fake_ps.parts = [Part("/"), Part("/snap/core", "squashfs", "ro,nodev"), Part("E:\\", "CDFS", "cdrom"),
                     Part("/weird", ""), Part("/denied"), Part("C:\\", "NTFS", "rw,fixed")]
    fake_ps.usage = {"/": 10.0, "/denied": PermissionError("no"), "C:\\": 70.0}
    mon = make_mon()
    rows = mon._disks()
    assert rows == [("pc.disk.root.percent", 10.0, None), ("pc.disk.root.free_gb", 90.0, None),
                    ("pc.disk.c.percent", 70.0, None), ("pc.disk.c.free_gb", 30.0, None)]
    # Once a minute: the immediate next call returns empty
    assert mon._disks() == []
    mon._disk_at -= 61
    assert len(mon._disks()) == 4


def test_temps_from_psutil_takes_max_cpu(fake_ps):
    T = lambda c: SimpleNamespace(current=c)  # noqa: E731
    fake_ps.temps = {"coretemp": [T(55.0), T(71.5), T(0.0)], "k10temp": [T(60.0)], "nvme": [T(99.0)]}
    assert make_mon()._temps() == [("pc.cpu.temp", 71.5, None)]


def test_temps_none_when_no_cpu_sensor(fake_ps):
    fake_ps.temps = {"nvme": [SimpleNamespace(current=40.0)]}
    assert make_mon()._temps() == []


def test_temps_sensor_error_falls_back_to_lhm(fake_ps):
    fake_ps.temps = RuntimeError("no sensors")
    fake_ps.run_out["powershell"] = "/amdcpu/0/temperature/2|61,5\n/gpu-nvidia/0/temperature/0|70\n" \
                                    "/intelcpu/0/temperature/0|58.25\n/lpc/cpu|bad\nnopipe-line\n"
    mon = make_mon()
    mon._lhm = True
    assert mon._temps() == [("pc.cpu.temp", 61.5, None)]
    assert mon._lhm is True


def test_lhm_disables_itself_when_unavailable(fake_ps):
    mon = make_mon()
    mon._lhm = True
    assert mon._temps() == []
    assert mon._lhm is False
    n = len(fake_ps.run_calls)
    mon._temps()
    assert len(fake_ps.run_calls) == n  # powershell is not called again


def test_lhm_no_cpu_lines(fake_ps):
    fake_ps.run_out["powershell"] = "/gpu/0/temperature/0|50\n"
    mon = make_mon()
    mon._lhm = True
    assert mon._lhm_temps() == []
    assert mon._lhm is True  # the command ran, there were just no CPU lines


def test_lhm_not_used_when_psutil_has_temp(fake_ps):
    fake_ps.temps = {"acpitz": [SimpleNamespace(current=45.0)]}
    mon = make_mon()
    mon._lhm = True
    assert mon._temps() == [("pc.cpu.temp", 45.0, None)]
    assert fake_ps.run_calls == []


def test_temps_without_sensors_function(fake_ps, monkeypatch):
    monkeypatch.delattr(monitors.psutil, "sensors_temperatures", raising=False)
    assert make_mon()._temps() == []


def test_gpu_disabled_without_nvidia_smi(fake_ps):
    assert make_mon()._gpu() == []
    assert fake_ps.run_calls == []


def test_gpu_parses_nvidia_smi(fake_ps):
    mon = make_mon()
    mon._nvidia = "nvidia-smi"
    fake_ps.run_out["nvidia-smi"] = "65, 37, 2048, 8192\n50, 1, 1, 2\n"
    assert mon._gpu() == [("pc.gpu.temp", 65.0, None), ("pc.gpu.util", 37.0, None),
                          ("pc.gpu.mem_percent", 25.0, None)]


@pytest.mark.parametrize("out", [None, "", "N/A, 3, 1, 2", "1, 2, 3"])
def test_gpu_bad_output(fake_ps, out):
    mon = make_mon()
    mon._nvidia = "nvidia-smi"
    fake_ps.run_out["nvidia-smi"] = out
    assert mon._gpu() == []


def test_gpu_zero_total_memory(fake_ps):
    mon = make_mon()
    mon._nvidia = "nvidia-smi"
    fake_ps.run_out["nvidia-smi"] = "40, 0, 0, 0"
    assert mon._gpu()[-1] == ("pc.gpu.mem_percent", 0.0, None)


def test_procs_aggregates_by_name_and_limits_top_n(fake_ps):
    fake_ps.procs = [
        Proc("chrome.exe", 100, 5.0), Proc("chrome.exe", 300, 2.5),
        Proc("Unity Hub.exe", 250, 0.0),
        Proc("idle", 10, None),
        Proc("", 999, 99.0),        # unnamed, skipped
        Proc("ghost", None, 1.0),   # no memory info
    ]
    mon = make_mon(top_n=2)
    rows = mon._procs()
    assert mon.processes == {"chrome.exe", "Unity Hub.exe", "idle", "ghost"}
    assert rows == [
        ("pc.proc.chrome.exe.mem_mb", pytest.approx(400.0), None),
        ("pc.proc.unity_hub.exe.mem_mb", pytest.approx(250.0), None),
        ("pc.proc.chrome.exe.cpu", 7.5, None),
        ("pc.proc.ghost.cpu", 1.0, None),
    ]


def test_procs_zero_cpu_not_reported(fake_ps):
    fake_ps.procs = [Proc("a", 1, 0.0), Proc("b", 2, None)]
    rows = make_mon()._procs()
    assert keys(rows) == ["pc.proc.b.mem_mb", "pc.proc.a.mem_mb"]


def test_procs_updates_process_set_each_collect(fake_ps):
    mon = make_mon()
    fake_ps.procs = [Proc("aces.exe", 1, 0)]
    mon.collect()
    assert mon.processes == {"aces.exe"}
    fake_ps.procs = []
    mon.collect()
    assert mon.processes == set()


def test_procs_windows_fast_path_cpu_from_deltas(fake_ps, monkeypatch):
    # psutil's generic path held the GIL for 17 s across 465 processes on Windows; the fast path computes
    # the CPU percent from the difference in processor time between two rounds
    clock = [1000.0]
    monkeypatch.setattr(monitors.time, "monotonic", lambda: clock[0])
    stats = [[(1, "aces.exe", 2 * 2**30, 10.0), (2, "chrome.exe", 2**29, 1.0), (3, "chrome.exe", 2**29, 1.0)]]
    monkeypatch.setattr(monitors, "_win_proc_stats", lambda: stats[0])
    mon = make_mon(top_n=2)
    assert mon._procs() == [("pc.proc.aces.exe.mem_mb", 2048.0, None), ("pc.proc.chrome.exe.mem_mb", 1024.0, None)]
    clock[0] += 2.0
    stats[0] = [(1, "aces.exe", 2 * 2**30, 13.0), (2, "chrome.exe", 2**29, 1.5), (4, "new.exe", 1, 9.0)]
    rows = mon._procs()
    assert ("pc.proc.aces.exe.cpu", pytest.approx(150.0), None) in rows
    assert ("pc.proc.chrome.exe.cpu", pytest.approx(25.0), None) in rows
    assert "pc.proc.new.exe.cpu" not in keys(rows)  # first-seen process: no delta
    assert mon.processes == {"aces.exe", "chrome.exe", "new.exe"}


def test_windows_proc_stats_is_fast():
    if sys.platform != "win32":
        pytest.skip("Windows")
    t = time.monotonic()
    stats = monitors._win_proc_stats()
    assert stats and time.monotonic() - t < 2.0


def test_real_psutil_smoke():
    """With real psutil, one collection must return rows without errors (no external commands)."""
    mon = PCMonitor(top_n=1)
    mon._nvidia = None
    mon._lhm = False
    rows = mon.collect()
    assert rows[0][0] == "pc.cpu.percent"
    assert all(isinstance(k, str) for k, _, _ in rows)
    assert isinstance(psutil.cpu_count(), int)
