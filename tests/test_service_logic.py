"""alice/core/service.py: ticks, task execution, state file, lock and autostart (no subprocesses)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from alice.config import Config, alice_home, default_config_dict
from alice.core import service
from alice.core.modes import ModeManager
from alice.core.notify import Notifier
from alice.core.rules import RuleSet
from alice.core.schedule import ScheduledTask, TaskStore
from alice.core.service import (Core, acquire_lock, autostart_path, force_mode, force_path, install_autostart,
                                read_state, release_lock, state_path, uninstall_autostart)
from alice.core.store import MODE_KEY, Store


class Mon:
    def __init__(self, name="m", rows=None, procs=(), error=None):
        self.name = name
        self.rows = rows or []
        self.processes = set(procs)
        self.error = error

    def collect(self):
        if self.error:
            raise self.error
        return list(self.rows)


def make_core(tmp_path, monitors=None, run_agent=None, **core_cfg):
    cfg = Config(default_config_dict())
    cfg.data["core"]["quiet_hours"] = ""
    cfg.data["core"].update(core_cfg)
    st = Store(":memory:")
    sent = []
    mm = ModeManager()
    n = Notifier(st, lambda: mm.policy, desktop=False)
    n.channels.append(lambda note, pol: sent.append(note))
    core = Core(cfg, store=st, monitors=monitors if monitors is not None else [Mon()], notifier=n,
                rules=RuleSet(tmp_path / "rules.json"), tasks=TaskStore(tmp_path / "tasks.json"), run_agent=run_agent)
    return core, sent


# --------------------------------------------------------------- state file
def test_force_mode_roundtrip():
    force_mode("flight")
    assert force_path().read_text(encoding="utf-8") == "flight"
    force_mode(None)
    assert not force_path().exists()
    force_mode(None)  # also fine when the file doesn't exist
    force_mode("")
    assert not force_path().exists()


def test_read_state_missing_or_corrupt():
    assert read_state() == {"running": False}
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text("{bozuk", encoding="utf-8")
    assert read_state() == {"running": False}


def test_read_state_running_requires_live_pid_and_fresh_ts(monkeypatch):
    state_path().parent.mkdir(parents=True, exist_ok=True)
    now = time.time()

    def write(**d):
        state_path().write_text(json.dumps(d), encoding="utf-8")

    write(pid=os.getpid(), ts=now, mode="dev")
    s = read_state()
    assert s["running"] is True and s["mode"] == "dev"
    write(pid=os.getpid(), ts=now - 301)
    assert read_state()["running"] is False
    write(pid=0, ts=now)
    assert read_state()["running"] is False
    monkeypatch.setattr(service.psutil, "pid_exists", lambda pid: False)
    write(pid=12345, ts=now)
    assert read_state()["running"] is False


def test_write_state_is_atomic_and_complete(tmp_path):
    core, _ = make_core(tmp_path)
    core.modes.forced = "quiet"
    core.write_state(1234.5)
    data = json.loads(state_path().read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and data["ts"] == 1234.5 and data["forced"] == "quiet"
    assert data["interval"] == 15.0 and set(data["policy"]) >= {"mode", "min_severity", "local_gpu", "voice"}
    assert not state_path().with_suffix(".tmp").exists()


def test_write_state_retries_brief_lock_and_prime_survives_failure(tmp_path, monkeypatch):
    core, _ = make_core(tmp_path)
    real, fails = os.replace, [2]

    def flaky(src, dst):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError("kilitli")
        return real(src, dst)

    monkeypatch.setattr(service.os, "replace", flaky)
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    core.write_state(1.0)
    assert json.loads(state_path().read_text(encoding="utf-8"))["ts"] == 1.0
    fails[0] = 99  # never releases: startup (prime) must still not crash
    core.prime()
    with pytest.raises(PermissionError):
        core.write_state(2.0)


# ----------------------------------------------------------------------- ticks
def test_core_config_values(tmp_path):
    core, _ = make_core(tmp_path, interval=5, retention_days=2)
    assert core.interval == 5.0 and core.retention_days == 2.0


def test_core_default_monitors_include_watch(monkeypatch, tmp_path):
    cfg = Config(default_config_dict())
    cfg.data.setdefault("watch", {})["export_path"] = str(tmp_path / "gb")
    core = Core(cfg, store=Store(":memory:"), rules=RuleSet(tmp_path / "r.json"), tasks=TaskStore(tmp_path / "t.json"))
    # The feeds monitor is always attached: a feed added from chat runs without a core restart (empty = no-op).
    assert [getattr(m, "name", "") for m in core.monitors][1:] == ["watch", "feeds", "internet", "telefon"] and core.monitors[0] is core.pc
    cfg.data["watch"]["export_path"] = ""
    core2 = Core(cfg, store=Store(":memory:"), rules=RuleSet(tmp_path / "r.json"),
                 tasks=TaskStore(tmp_path / "t.json"))
    assert core2.monitors[0] is core2.pc and [getattr(m, "name", "") for m in core2.monitors][1:] == ["feeds", "internet", "telefon"]


def test_tick_survives_broken_monitor(tmp_path):
    good = Mon("iyi", rows=[("pc.cpu", 42.0, None)])
    bad = Mon("bozuk", error=RuntimeError("sensör " + "x" * 400))
    core, _ = make_core(tmp_path, monitors=[bad, good])
    core.tick(1000)
    assert core.store.last("pc.cpu")[1] == 42.0
    errs = core.store.events(kind="error")
    assert len(errs) == 1 and "bozuk gözlemcisi hata verdi" in errs[0].title and len(errs[0].body) <= 300


def test_mode_event_only_on_change_and_invalid_force_ignored(tmp_path):
    mon = Mon(procs={"DCS.exe"})
    core, _ = make_core(tmp_path, monitors=[mon])
    core.tick(1000)
    core.tick(1015)
    mode_events = core.store.events(kind="mode")
    assert [e.title for e in mode_events] == ["Mod: uçuş"]
    force_mode("uzay")  # undefined mode: ignored
    core.tick(1030)
    assert core.modes.forced is None and core.modes.current == "flight"
    # the mode is written on every tick
    assert len(core.store.texts(MODE_KEY)) == 3


def test_procs_merged_from_all_monitors(tmp_path):
    core, _ = make_core(tmp_path, monitors=[Mon("a", procs={"notepad.exe"}), Mon("b", procs={"Unity.exe"})])
    core.tick(1000)
    assert core.modes.current == "dev"


def test_prune_runs_at_most_hourly(tmp_path, monkeypatch):
    core, _ = make_core(tmp_path)
    calls = []
    monkeypatch.setattr(core.store, "prune", lambda days, now: calls.append(now))
    core.tick(10_000)
    core.tick(10_100)
    core.tick(13_601)
    assert calls == [10_000, 13_601]


def test_reload_when_files_change_externally(tmp_path):
    core, _ = make_core(tmp_path)
    core.tasks.add(ScheduledTask("0 3 * * *", "ilk"))
    core.tick(1000)  # on the first look only the timestamp is recorded
    other = TaskStore(tmp_path / "tasks.json")
    other.add(ScheduledTask("0 4 * * *", "başka süreçten"))
    st = (tmp_path / "tasks.json").stat()
    os.utime(tmp_path / "tasks.json", (st.st_atime, st.st_mtime + 10))
    core.tick(1015)
    assert sorted(t.prompt for t in core.tasks.tasks) == ["başka süreçten", "ilk"]


def test_reload_picks_up_file_created_after_start(tmp_path):
    core, _ = make_core(tmp_path)
    core.tick(1000)  # no file yet
    TaskStore(tmp_path / "tasks.json").add(ScheduledTask("0 4 * * *", "yeni görev"))
    core.tick(1015)
    assert [t.prompt for t in core.tasks.tasks] == ["yeni görev"]


# ----------------------------------------------------------------- tasks
# On Windows, times near 1970 raise OSError from datetime.timestamp(); use a realistic moment.
NOW = 1_760_000_000


def test_run_due_without_agent_does_nothing(tmp_path):
    core, _ = make_core(tmp_path)
    core.tasks.add(ScheduledTask("* * * * *", "x", created=NOW - 100))
    core._run_due(NOW)
    assert core.tasks.tasks[0].last_run is None and not core.running_tasks


def test_run_due_skips_already_running(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(service.threading, "Thread",
                        lambda target, args, daemon: SimpleNamespace(start=lambda: started.append(args)))
    core, _ = make_core(tmp_path, run_agent=lambda p: "ok")
    t = core.tasks.add(ScheduledTask("* * * * *", "x", created=NOW - 100))
    core.running_tasks.add(t.id)
    core._run_due(NOW)
    assert started == []
    core.running_tasks.clear()
    core._run_due(NOW)
    assert len(started) == 1 and t.id in core.running_tasks
    assert core.tasks.get(t.id).last_result == "(çalışıyor)" and core.tasks.get(t.id).last_run == NOW


def test_run_task_success_notifies(tmp_path):
    core, sent = make_core(tmp_path, run_agent=lambda p: f"bitti: {p}")
    t = core.tasks.add(ScheduledTask("0 3 * * *", "yedekle", description="gece yedeği", last_run=500))
    core.running_tasks.add(t.id)
    core._run_task(t, late=True)
    assert core.tasks.get(t.id).last_result == "bitti: yedekle" and core.tasks.get(t.id).last_run == 500
    assert sent[0].title == "Görev (telafi): gece yedeği" and sent[0].severity == "info" and sent[0].kind == "task"
    assert t.id not in core.running_tasks


def test_run_task_failure_without_notify_logs_event(tmp_path):
    def boom(prompt):
        raise RuntimeError("kota doldu")

    core, sent = make_core(tmp_path, run_agent=boom)
    t = core.tasks.add(ScheduledTask("0 3 * * *", "p" * 50, notify=False))
    core.running_tasks.add(t.id)
    core._run_task(t, late=False)
    assert sent == []
    ev = core.store.events(kind="task")[0]
    assert ev.title == "Görev: " + "p" * 40 and ev.body == "görev başarısız: kota doldu" and ev.severity == "warn"
    assert core.tasks.get(t.id).last_result == "görev başarısız: kota doldu"
    assert not core.running_tasks


def test_run_task_uses_original_when_removed_meanwhile(tmp_path):
    core, _ = make_core(tmp_path, run_agent=lambda p: "ok")
    t = core.tasks.add(ScheduledTask("0 3 * * *", "x", notify=False))
    core.tasks.remove(t.id)
    core._run_task(t, late=False)
    assert t.last_result == "ok"


def test_failing_rule_step_does_not_skip_tasks_or_state(tmp_path, monkeypatch):
    """Even if rule evaluation fails on every tick, tasks must still run and the state file must be written (otherwise
    the app thought the core was off); the same error is logged once a day."""
    core, _ = make_core(tmp_path, run_agent=lambda p: "ok")
    ran = []

    def boom(now):
        raise RuntimeError("bozuk kural")

    monkeypatch.setattr(core.engine, "evaluate", boom)
    monkeypatch.setattr(core, "_run_due", lambda now: ran.append(now))
    core.tick(NOW)
    core.tick(NOW + 15)
    assert ran == [NOW, NOW + 15]
    assert json.loads(state_path().read_text(encoding="utf-8"))["ts"] == NOW + 15
    errs = [e for e in core.store.events(kind="error") if "kural değerlendirme" in e.title]
    assert len(errs) == 1 and "bozuk kural" in errs[0].body


# -------------------------------------------------------------------- loop
def test_run_forever_records_start_stop_and_errors(tmp_path, monkeypatch):
    core, _ = make_core(tmp_path)
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("tur patladı")
        core.write_state(time.time())
        core.stop()

    monkeypatch.setattr(core, "tick", tick)
    monkeypatch.setattr(core.stop_event, "wait", lambda t: None)
    core.run_forever()
    assert len(calls) == 2
    titles = [e.title for e in core.store.events(kind="core")]
    assert "Çekirdek başladı" in titles and "Çekirdek durdu" in titles
    err = core.store.events(kind="error")[0]
    assert "tur patladı" in err.body and err.severity == "warn"
    assert core.store.last(MODE_KEY)[2] == "off"
    assert not state_path().exists()


# --------------------------------------------------------------- agent runner
def test_make_agent_runner_uses_unattended_agent(monkeypatch, tmp_path):
    from tests.conftest import make_config

    made, cleaned = [], []

    class FakeAgent:
        def __init__(self, cfg, router, tools, ctx, perms, ui, decider):
            self.ctx, self.perms, self.ui = ctx, perms, ui
            made.append(self)

        def run_turn(self, prompt):
            self.prompt = prompt
            return self.reply

    monkeypatch.setattr("alice.agent.Agent", FakeAgent)
    monkeypatch.setattr("alice.tools.cleanup", lambda ctx: cleaned.append(ctx))
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"]})
    cfg.data.setdefault("core", {})["task_permission_mode"] = "plan"
    run = service.make_agent_runner(cfg, tmp_path)
    FakeAgent.reply = ""
    assert run("raporu yaz") == "(sonuç yok)"
    a = made[0]
    assert a.prompt.startswith("raporu yaz\n\n") and "Soru sorma" in a.prompt
    assert a.ctx.cwd == tmp_path and cleaned == [a.ctx]
    FakeAgent.reply = "tamam"
    assert run("x") == "tamam" and len(made) == 2 and made[0] is not made[1]  # a clean agent on every call

    def broken(self, prompt):
        raise RuntimeError("x")

    monkeypatch.setattr(FakeAgent, "run_turn", broken)
    with pytest.raises(RuntimeError):
        run("y")
    assert len(cleaned) == 3  # cleaned up even on error


# --------------------------------------------------------------------- lock
def test_lock_acquire_and_release():
    assert acquire_lock() is True
    lock = alice_home() / "core.pid"
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
    assert acquire_lock() is True  # our own pid
    release_lock()
    assert not lock.exists()
    release_lock()  # fine when the file doesn't exist


def test_lock_blocked_by_live_alice_core(monkeypatch):
    lock = alice_home() / "core.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999", encoding="utf-8")
    monkeypatch.setattr(service.psutil, "pid_exists", lambda pid: pid == 99999)
    cmd = ["python", "-m", "alice", "core", "run"]
    monkeypatch.setattr(service.psutil, "Process", lambda pid: SimpleNamespace(cmdline=lambda: cmd))
    assert acquire_lock() is False
    release_lock()
    assert lock.exists()  # someone else's lock is not deleted
    cmd[:] = ["notepad.exe"]  # the pid now belongs to another program
    assert acquire_lock() is True


def test_lock_stale_or_garbage(monkeypatch):
    lock = alice_home() / "core.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("çöp", encoding="utf-8")
    assert acquire_lock() is True
    lock.write_text("99999", encoding="utf-8")

    def denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(service.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(service.psutil, "Process", denied)
    assert acquire_lock() is True  # if the command line can't be read, it counts as not alice


# -------------------------------------------------------------- tray/autostart
def test_run_tray_without_pystray(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pystray", None)
    core, _ = make_core(tmp_path)
    assert service.run_tray(core) is False


def test_start_detached_skips_when_running(monkeypatch):
    import subprocess

    monkeypatch.setattr(service, "read_state", lambda: {"running": True})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("başlatmamalı"))
    assert service.start_detached() is False


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_start_detached_spawns_core(monkeypatch, platform):
    import subprocess

    calls = []
    monkeypatch.setattr(service, "read_state", lambda: {"running": False})
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(service.sys, "platform", platform)
    assert service.start_detached() is True
    args, kw = calls[0]
    assert args[1:] == ["-m", "alice", "core", "run", "--tray"]
    assert kw["stdin"] == subprocess.DEVNULL and kw["stdout"] == subprocess.DEVNULL
    if platform == "win32":
        assert kw["creationflags"] == 0x00000208 and "start_new_session" not in kw
    else:
        assert kw["start_new_session"] is True and "creationflags" not in kw


def test_autostart_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert autostart_path() == tmp_path / "xdg" / "autostart" / "alice-core.desktop"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert autostart_path() == tmp_path / "home" / ".config" / "autostart" / "alice-core.desktop"
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert autostart_path() == tmp_path / "home" / "Library" / "LaunchAgents" / "ai.alice.core.plist"
    monkeypatch.setattr(service.sys, "platform", "win32")
    p = autostart_path()
    assert str(p).startswith(str(tmp_path / "appdata")) and p.name == "ALICE Core.lnk"


def test_install_and_uninstall_autostart_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert uninstall_autostart() is False
    p = install_autostart()
    text = p.read_text(encoding="utf-8")
    assert "Type=Application" in text and f"Exec={Path(sys.executable)} -m alice core run --tray" in text
    assert uninstall_autostart() is True and not p.exists()


def test_install_autostart_darwin(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = install_autostart()
    text = p.read_text(encoding="utf-8")
    assert "<string>ai.alice.core</string>" in text and "<key>RunAtLoad</key><true/>" in text
    assert f"<string>{Path(sys.executable)}</string>" in text


def test_install_autostart_windows_uses_powershell(monkeypatch, tmp_path):
    import subprocess

    calls = []
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "it's"))  # quote escaping
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    p = install_autostart()
    args, kw = calls[0]
    assert args[:3] == ["powershell", "-NoProfile", "-Command"] and kw["check"] is True
    ps = args[3]
    assert "it''s" in ps and "$s.Arguments='-m alice core run --tray'" in ps and p.parent.is_dir()


def test_same_step_error_is_logged_once_a_day_even_across_restarts():
    # When calendar permission ran out, the same error was written on every restart (13 times in 2 days).
    events = []
    store = SimpleNamespace(add_event=lambda *a, **k: events.append(a))
    err = ConnectionError("token yenilenemedi")
    service.Core._step_error(SimpleNamespace(store=store), "takvim takibi", err)
    service.Core._step_error(SimpleNamespace(store=store), "takvim takibi", err)  # like a new process: empty memory
    service.Core._step_error(SimpleNamespace(store=store), "takvim takibi", ConnectionError("başka hata"))
    assert len(events) == 2
