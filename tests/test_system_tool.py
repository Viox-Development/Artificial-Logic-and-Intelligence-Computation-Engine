"""Unit tests for alice/tools/system.py: psutil, subprocess and platform are replaced with fake objects;
no real process is terminated and no real command is run."""

from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

from alice.tools import system
from alice.tools.base import DANGER, EXEC, ToolContext, ToolError
from tests.conftest import make_config

GB = 2**30
MB = 2**20


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


# ------------------------------------------------------------------ _gpu


def _fake_run(results, calls):
    def run(argv, **kw):
        calls.append(argv)
        return results.pop(0)
    return run


def test_gpu_nvidia_smi(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "shutil", SimpleNamespace(which=lambda n: "/usr/bin/nvidia-smi"))
    monkeypatch.setattr(system, "subprocess", SimpleNamespace(
        run=_fake_run([SimpleNamespace(returncode=0, stdout="RTX 4090, 5 %, 100 MiB\n")], calls)))
    assert system._gpu() == "RTX 4090, 5 %, 100 MiB"
    assert calls[0][0] == "nvidia-smi"


def test_gpu_nvidia_fails_falls_back_to_powershell_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "shutil", SimpleNamespace(which=lambda n: "x"))
    monkeypatch.setattr(system, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(system, "subprocess", SimpleNamespace(run=_fake_run([
        SimpleNamespace(returncode=9, stdout="hata"),
        SimpleNamespace(returncode=0, stdout=" Intel UHD, AMD Radeon \n")], calls)))
    assert system._gpu() == "Intel UHD, AMD Radeon"
    assert [c[0] for c in calls] == ["nvidia-smi", "powershell"]


def test_gpu_unknown_on_linux_without_nvidia(monkeypatch):
    monkeypatch.setattr(system, "shutil", SimpleNamespace(which=lambda n: None))
    monkeypatch.setattr(system, "sys", SimpleNamespace(platform="linux"))

    def boom(*a, **k):
        raise AssertionError("komut çalıştırılmamalı")

    monkeypatch.setattr(system, "subprocess", SimpleNamespace(run=boom))
    assert system._gpu() == "bilinmiyor"


# ------------------------------------------------------------------ SystemInfo


def _fake_psutil(**over):
    parts = [SimpleNamespace(mountpoint="/", fstype="ext4"), SimpleNamespace(mountpoint="/locked", fstype="ntfs")]

    def disk_usage(mp):
        if mp == "/locked":
            raise PermissionError("erişim yok")
        return SimpleNamespace(used=100 * GB, total=500 * GB, percent=20.0)

    base = dict(
        virtual_memory=lambda: SimpleNamespace(used=8 * GB, total=16 * GB, percent=50.0),
        cpu_count=lambda logical=True: 16 if logical else 8,
        cpu_percent=lambda interval=None: 12.5,
        disk_partitions=lambda all=False: parts,
        disk_usage=disk_usage,
        sensors_battery=lambda: SimpleNamespace(percent=77.4, power_plugged=False),
        net_io_counters=lambda: SimpleNamespace(bytes_sent=300 * MB, bytes_recv=1200 * MB),
        Error=psutil.Error,
        NoSuchProcess=psutil.NoSuchProcess,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patch_platform(monkeypatch, processor="Intel i9"):
    monkeypatch.setattr(system, "platform", SimpleNamespace(platform=lambda: "TestOS-1.0", machine=lambda: "x86_64",
                                                            processor=lambda: processor))
    monkeypatch.setattr(system, "_gpu", lambda: "FakeGPU")


def test_system_info_rows(ctx, monkeypatch):
    monkeypatch.setattr(system, "psutil", _fake_psutil())
    _patch_platform(monkeypatch)
    out = system.SystemInfo().run(ctx).text.splitlines()
    assert out[0] == "OS: TestOS-1.0 (x86_64)"
    assert out[1] == "CPU: Intel i9 | 8C/16T | usage 12.5%"
    assert out[2] == "RAM: 8.0/16.0 GB (%50.0)"
    assert out[3] == "GPU: FakeGPU"
    assert out[4] == "Disk /: 100/500 GB (20.0%) ext4"
    assert not any("/locked" in r for r in out)  # permission errors are skipped
    assert out[5] == "Battery: 77% on battery"
    assert out[6] == "Network: sent 300 MB, received 1200 MB"


def test_system_info_no_battery_no_processor(ctx, monkeypatch):
    monkeypatch.setattr(system, "psutil", _fake_psutil(sensors_battery=lambda: None))
    _patch_platform(monkeypatch, processor="")
    out = system.SystemInfo().run(ctx).text
    assert "CPU: unknown" in out and "Pil" not in out


def test_system_info_without_sensors_battery_attr(ctx, monkeypatch):
    fake = _fake_psutil()
    del fake.sensors_battery
    monkeypatch.setattr(system, "psutil", fake)
    _patch_platform(monkeypatch)
    assert "Pil" not in system.SystemInfo().run(ctx).text


def test_system_info_charging(ctx, monkeypatch):
    monkeypatch.setattr(system, "psutil", _fake_psutil(
        sensors_battery=lambda: SimpleNamespace(percent=99.6, power_plugged=True)))
    _patch_platform(monkeypatch)
    assert "Battery: 100% charging" in system.SystemInfo().run(ctx).text


# ------------------------------------------------------------------ ProcessList


class FakeProc:
    def __init__(self, pid, name, rss_mb=None, cpu=None, terminate_error=None):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cpu_percent": cpu,
                     "memory_info": SimpleNamespace(rss=rss_mb * MB) if rss_mb is not None else None}
        self.terminate_error = terminate_error
        self.terminated = False
        self.stubborn = False  # ignores terminate, closes only via kill
        self.killed = False

    def kill(self):
        self.killed = True
        self.stubborn = False

    def terminate(self):
        if self.terminate_error:
            raise self.terminate_error
        self.terminated = True


PROCS = [FakeProc(1, "python.exe", 100, 5.0), FakeProc(2, "chrome.exe", 900, 1.0),
         FakeProc(3, "Python", None, None), FakeProc(4, None, 50, 80.0)]


def _patch_procs(monkeypatch, procs, waited=None):
    waited = waited if waited is not None else []
    monkeypatch.setattr(system, "psutil", _fake_psutil(
        process_iter=lambda attrs=None: iter(procs),
        Process=lambda pid: next(p for p in procs if p.pid == pid),
        wait_procs=lambda targets, timeout=None: (waited.append((list(targets), timeout)),
                                                    ([p for p in targets if not p.stubborn],
                                                     [p for p in targets if p.stubborn]))[1]))
    return waited


def _pids(text):
    return [int(line.split()[0]) for line in text.splitlines()[1:]]


def test_process_list_sorted_by_memory_default(ctx, monkeypatch):
    _patch_procs(monkeypatch, PROCS)
    out = system.ProcessList().run(ctx).text
    assert out.startswith("    PID       RAM     CPU  NAME\n")
    assert _pids(out) == [2, 1, 4, 3]  # memory_info None -> 0, placed last
    assert "     900 MB" in out


def test_process_list_sorted_by_cpu(ctx, monkeypatch):
    _patch_procs(monkeypatch, PROCS)
    assert _pids(system.ProcessList().run(ctx, sort="cpu").text) == [4, 1, 2, 3]


def test_process_list_name_filter_case_insensitive_and_limit(ctx, monkeypatch):
    _patch_procs(monkeypatch, PROCS)
    out = system.ProcessList().run(ctx, name="PYTHON").text
    assert _pids(out) == [1, 3]
    assert _pids(system.ProcessList().run(ctx, limit=1).text) == [2]


def test_process_list_no_match(ctx, monkeypatch):
    _patch_procs(monkeypatch, PROCS)
    assert system.ProcessList().run(ctx, name="yokboyle").text == "no process found"


# ------------------------------------------------------------------ ProcessKill


def test_process_kill_risk(monkeypatch):
    # The pid's name is read from the real psutil: if 1234 happened to map to a protected process (csrss, svchost),
    # the test randomly failed on the Windows runner. Pin the process table.
    names = {1234: "notepad.exe", 4321: "csrss.exe"}
    monkeypatch.setattr(system.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: names[pid]))
    # ALICE's own pid and its parent count as protected; on the macOS runner the parent was pid 1234.
    import os

    monkeypatch.setattr(os, "getpid", lambda: 999_001)
    monkeypatch.setattr(os, "getppid", lambda: 999_002)
    k = system.ProcessKill()
    assert k.risk_for({"pid": 1234}) == EXEC
    assert k.risk_for({"pid": 4321}) == DANGER     # a protected process given by pid also requires confirmation
    assert k.risk_for({"name": "notepad.exe"}) == EXEC
    assert k.risk_for({"name": "LSASS.EXE"}) == DANGER
    assert k.risk_for({"name": "explorer.exe"}) == DANGER
    assert k.risk_for({}) == DANGER  # call without a target
    assert k.risk_for({"pid": 0}) == DANGER


def test_process_kill_by_pid(ctx, monkeypatch):
    procs = [FakeProc(10, "a"), FakeProc(11, "b")]
    waited = _patch_procs(monkeypatch, procs)
    r = system.ProcessKill().run(ctx, pid=11)
    assert r.text == "terminated: 11"
    assert procs[1].terminated and not procs[0].terminated
    assert waited == [([procs[1]], 3)]


def test_process_kill_by_name_matches_all_exactly(ctx, monkeypatch):
    procs = [FakeProc(1, "Notepad.exe"), FakeProc(2, "notepad.exe"), FakeProc(3, "notepad.exe.bak"), FakeProc(4, None)]
    _patch_procs(monkeypatch, procs)
    r = system.ProcessKill().run(ctx, name="NOTEPAD.EXE")
    assert r.text == "terminated: 1, 2"
    assert not procs[2].terminated


def test_process_kill_reports_terminate_errors(ctx, monkeypatch):
    procs = [FakeProc(5, "x", terminate_error=psutil.AccessDenied(5)), FakeProc(6, "x")]
    _patch_procs(monkeypatch, procs)
    r = system.ProcessKill().run(ctx, name="x")
    # a process that could not be closed is not reported as "sonlandırıldı"
    assert r.text.startswith("terminated: 6\n5: could not close it (") and not r.is_error
    assert procs[1].terminated


def test_process_kill_forces_stubborn_and_accepts_name_without_exe(ctx, monkeypatch):
    procs = [FakeProc(7, "notepad.exe")]
    procs[0].stubborn = True
    monkeypatch.setattr(system, "sys", SimpleNamespace(platform="win32"))
    _patch_procs(monkeypatch, procs)
    r = system.ProcessKill().run(ctx, name="notepad")
    assert r.text == "terminated: 7" and procs[0].killed
    assert system.ProcessKill().risk_for({"name": "explorer"}) == DANGER


def test_process_kill_all_failed_is_error(ctx, monkeypatch):
    procs = [FakeProc(5, "x", terminate_error=psutil.AccessDenied(5))]
    _patch_procs(monkeypatch, procs)
    r = system.ProcessKill().run(ctx, name="x")
    assert r.is_error and "sonlandırıldı" not in r.text


def test_process_kill_no_target(ctx, monkeypatch):
    _patch_procs(monkeypatch, [FakeProc(1, "a")])
    with pytest.raises(ToolError, match="no matching process"):
        system.ProcessKill().run(ctx, name="zzz")
    with pytest.raises(ToolError):
        system.ProcessKill().run(ctx)


def test_tools_exported():
    assert [t.name for t in system.TOOLS] == ["system_info", "process_list", "process_kill"]
    assert system.SystemInfo.parallel_safe and system.ProcessList.parallel_safe
    assert not system.ProcessKill.parallel_safe
