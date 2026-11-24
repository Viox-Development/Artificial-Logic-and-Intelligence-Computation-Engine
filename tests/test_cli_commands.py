"""alice/core/cli.py and alice/extras/cli.py: core, rule, task, note, brief and report commands."""

from __future__ import annotations

import io
import json
import os
import time
from types import SimpleNamespace

import pytest
from rich.console import Console

from alice.config import alice_home
from alice.core import cli as core_cli
from alice.core import service
from alice.extras import cli as extras_cli


def _capture(monkeypatch, module):
    buf = io.StringIO()
    monkeypatch.setattr(module, "console", Console(file=buf, width=200, highlight=False, color_system=None))
    return buf.getvalue


@pytest.fixture
def out(monkeypatch):
    return _capture(monkeypatch, core_cli)


@pytest.fixture
def xout(monkeypatch):
    return _capture(monkeypatch, extras_cli)


def _answer(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(core_cli.console, "input", lambda prompt="": next(it))


# ------------------------------------------------------------------- _confirm
@pytest.mark.parametrize("ans,ok", [("", True), ("e", True), ("Evet", True), ("y", True), (" yes ", True),
                                    ("h", False), ("hayır", False), ("n", False)])
def test_confirm(monkeypatch, out, ans, ok):
    _answer(monkeypatch, ans)
    assert core_cli._confirm("Kurulsun mu?") is ok


# ------------------------------------------------------------------ core_cmd
def test_core_unknown_prints_usage(out):
    assert core_cli.core_cmd(["zzz"]) == 2
    assert "alice core run" in out()


def test_core_mode_validates_and_forces(out):
    assert core_cli.core_cmd(["mode"]) == 2
    assert core_cli.core_cmd(["mode", "turbo"]) == 2
    assert core_cli.core_cmd(["mode", "flight"]) == 0
    assert service.force_path().read_text(encoding="utf-8") == "flight"
    assert core_cli.core_cmd(["mode", "auto"]) == 0
    assert not service.force_path().exists()
    assert "mod: flight" in out() and "mod: auto" in out()


def test_core_install_uninstall(monkeypatch, out, tmp_path):
    p = tmp_path / "ALICE Core.desktop"
    monkeypatch.setattr(service, "install_autostart", lambda: p)
    removed = iter([True, False])
    monkeypatch.setattr(service, "uninstall_autostart", lambda: next(removed))
    assert core_cli.core_cmd(["install"]) == 0
    assert core_cli.core_cmd(["uninstall"]) == 0
    assert core_cli.core_cmd(["uninstall"]) == 0
    text = out()
    assert "otomatik başlatma kuruldu" in text and "ALICE Core.desktop" in text
    assert "kaldırıldı" in text and "zaten kurulu değil" in text


def test_core_stats_single_and_glob(out):
    from alice.core.store import Store

    assert core_cli.core_cmd(["stats"]) == 2
    st = Store()
    now = time.time()
    st.add("pc.gpu.temp", 70, ts=now - 60)
    st.add("pc.gpu.temp", 80, ts=now - 30)
    st.add("pc.cpu.temp", 50, ts=now - 30)
    st.close()
    assert core_cli.core_cmd(["stats", "pc.gpu.temp", "1h"]) == 0
    assert "pc.gpu.temp: ort 75.0, en düşük 70.0, en yüksek 80.0" in out()
    assert core_cli.core_cmd(["stats", "pc.*.temp"]) == 0
    assert "pc.cpu.temp: ort 50.0" in out()
    assert core_cli.core_cmd(["stats", "watch.hr"]) == 0
    assert "watch.hr: veri yok" in out()


def test_core_status_not_running_lists_events(out):
    from alice.core.store import Store

    Store().add_event("alert", "GPU sıcak", "85 derece", severity="warn")
    assert core_cli.core_cmd([]) == 0
    text = out()
    assert "çalışmıyor" in text and "Son olaylar" in text and "GPU sıcak: 85 derece" in text


def test_core_status_running(out):
    alice_home().mkdir(parents=True, exist_ok=True)
    service.state_path().write_text(json.dumps({"pid": os.getpid(), "ts": time.time(), "mode": "dev", "forced": True,
                                                "policy": {"min_severity": "warn", "local_gpu": False}}),
                                    encoding="utf-8")
    assert core_cli.core_cmd(["status"]) == 0
    text = out()
    assert f"çalışıyor (pid {os.getpid()}), mod: dev (elle sabit), en düşük bildirim: warn, yerel GPU: False" in text


class _FakeCore:
    instances: list = []

    def __init__(self, cfg, decider=None, run_agent=None):
        from alice.core.store import Store

        self.store = Store(":memory:")
        self.interval = 15.0
        self.calls: list[str] = []
        self.greeter = SimpleNamespace(voice=None)  # the greeter in the real Core
        _FakeCore.instances.append(self)

    def prime(self):
        self.calls.append("prime")

    def tick(self):
        self.calls.append("tick")
        self.store.add_event("info", "tur", ts=1_760_000_000)

    def run_forever(self):
        self.calls.append("forever")
        raise KeyboardInterrupt

    def stop(self):
        self.calls.append("stop")


@pytest.fixture
def fake_core(monkeypatch):
    from alice import decider

    _FakeCore.instances = []
    monkeypatch.setattr(service, "Core", _FakeCore)
    monkeypatch.setattr(service, "make_agent_runner", lambda cfg: (lambda p: p))
    monkeypatch.setattr(decider.Decider, "from_config", staticmethod(lambda cfg, router, **kw: None))
    released = []
    monkeypatch.setattr(service, "acquire_lock", lambda: True)
    monkeypatch.setattr(service, "release_lock", lambda: released.append(True))
    return released


def test_core_run_refuses_second_instance(monkeypatch, out):
    monkeypatch.setattr(service, "acquire_lock", lambda: False)
    assert core_cli.core_cmd(["run"]) == 1
    assert "zaten çalışıyor" in out()


def test_core_run_once_ticks_and_releases_lock(monkeypatch, out, fake_core):
    for name in ("_start_voice", "_start_sims", "_start_phone", "_start_quick", "_start_app", "_start_hud"):
        monkeypatch.setattr(core_cli, name, lambda *a, n=name, **kw: pytest.fail(f"--once ile {n} başlamamalı"))
    assert core_cli.core_cmd(["run", "--once"]) == 0
    core = _FakeCore.instances[0]
    assert core.calls == ["prime", "tick"] and fake_core == [True]
    assert "info/info tur" in out()


def test_core_run_forever_stops_helpers_on_ctrl_c(monkeypatch, out, fake_core):
    from alice.config import init_home, set_config_value

    init_home()
    set_config_value("voice", "enabled", True)
    set_config_value("phone", "enabled", True)
    set_config_value("hud", "enabled", True)
    stopped, sims_show = [], []

    def helper(name):
        def start(*a, **kw):
            if name == "sims":
                sims_show.append(kw.get("show"))
            return SimpleNamespace(stop=lambda: stopped.append(name))

        return start

    for name in ("voice", "sims", "phone", "quick", "app", "hud"):
        monkeypatch.setattr(core_cli, f"_start_{name}", helper(name))
    assert core_cli.core_cmd(["run"]) == 0
    assert _FakeCore.instances[0].calls == ["prime", "forever", "stop"]
    assert sorted(stopped) == ["app", "hud", "phone", "quick", "sims", "voice"] and fake_core == [True]
    from alice.hud import client

    assert sims_show == [client.show]  # flight threats also go to the HUD while it is open
    assert "her 15 sn" in out()


def test_core_run_tray(monkeypatch, out, fake_core):
    for name in ("_start_sims", "_start_quick", "_start_app"):
        monkeypatch.setattr(core_cli, name, lambda *a, **kw: None)
    monkeypatch.setattr(service, "run_tray", lambda core: True)
    assert core_cli.core_cmd(["run", "--tray"]) == 0
    assert "forever" not in _FakeCore.instances[0].calls and fake_core == [True]


def test_start_quick_skips_without_pynput(monkeypatch):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    monkeypatch.setattr("alice.hotkeys.IS_WIN", True)  # Windows: skipped when the voice extra is missing
    assert core_cli._start_quick(None) is None
    monkeypatch.setattr("alice.hotkeys.IS_WIN", False)  # elsewhere: skipped when pynput can't work here
    monkeypatch.setattr("alice.hotkeys.pynput_problem", lambda: "pynput yok")
    monkeypatch.setattr("alice.hotkeys.wayland_session", lambda: False)
    assert core_cli._start_quick(None) is None


def test_start_phone_handles_port_error(monkeypatch, out):
    from alice.app import phone

    class Boom:
        def __init__(self, cfg, store, state=None):
            raise OSError("adres kullanımda")

    monkeypatch.setattr(phone, "PhoneServer", Boom)
    assert core_cli._start_phone(None, SimpleNamespace(store=None)) is None
    assert "adres kullanımda" in out()

    class Ok:
        url = "http://192.168.1.2:8765/"

        def __init__(self, cfg, store, state=None):
            pass

    monkeypatch.setattr(phone, "PhoneServer", Ok)
    assert isinstance(core_cli._start_phone(None, SimpleNamespace(store=None)), Ok)
    assert "telefon: http://192.168.1.2:8765/" in out()


# ----------------------------------------------------------------- rules_cmd
def test_rules_list_defaults_and_toggle(out):
    assert core_cli.rules_cmd([]) == 0
    assert "gpu-hot: pc.gpu.temp > 85 3 dk boyunca (warn)" in out()
    assert core_cli.rules_cmd(["off", "gpu-hot"]) == 0
    from alice.core.rules import RuleSet

    assert RuleSet().get("gpu-hot").enabled is False
    assert core_cli.rules_cmd(["on", "gpu-hot"]) == 0
    assert RuleSet().get("gpu-hot").enabled is True
    assert core_cli.rules_cmd(["rm", "gpu-hot"]) == 0
    assert RuleSet().get("gpu-hot") is None
    assert core_cli.rules_cmd(["rm", "gpu-hot"]) == 1
    assert "kural yok: gpu-hot" in out()
    assert core_cli.rules_cmd(["add"]) == 2  # add without a sentence: usage


def test_rules_list_empty(out):
    from alice.core.rules import RuleSet

    rs = RuleSet()
    rs.rules = []
    rs.save()
    assert core_cli.rules_cmd(["list"]) == 0
    assert "kural yok" in out()


def test_rules_add_confirms_before_saving(monkeypatch, out):
    from alice.core import rules
    from alice.core.rules import Rule, RuleSet

    rule = Rule("watch.hr", ">", 120, "Nabız {value}", confirm="Spor mu yapıyor?", action="agent",
                prompt="nabzı incele", id="hr2")
    seen = []
    monkeypatch.setattr(rules, "parse_rule", lambda text, router, store: seen.append(text) or rule)
    _answer(monkeypatch, "h")
    assert core_cli.rules_cmd(["add", "nabzım", "yüksekse", "söyle"]) == 1
    assert seen == ["nabzım yüksekse söyle"] and RuleSet().get("hr2") is None
    _answer(monkeypatch, "e")
    assert core_cli.rules_cmd(["add", "nabız"]) == 0
    assert RuleSet().get("hr2") is not None
    text = out()
    assert "Jev'e sorulacak: Spor mu yapıyor?" in text and "tetiklenince ALICE: nabzı incele" in text
    assert "kuruldu" in text


def test_rules_add_reports_parse_errors(monkeypatch, out):
    from alice.core import rules

    def bad(text, router, store):
        raise ValueError("eşik yok")

    monkeypatch.setattr(rules, "parse_rule", bad)
    assert core_cli.rules_cmd(["add", "saçma"]) == 1
    assert "anlaşılamadı: eşik yok" in out()


# ----------------------------------------------------------------- tasks_cmd
def test_tasks_list_empty_and_unknown(out):
    assert core_cli.tasks_cmd([]) == 0
    assert "zamanlanmış görev yok" in out()
    assert core_cli.tasks_cmd(["rm", "yok"]) == 1
    assert "görev yok: yok" in out()
    assert core_cli.tasks_cmd(["bilinmeyen"]) == 2


def test_tasks_add_run_rm(monkeypatch, out):
    from alice.core import schedule
    from alice.core.schedule import ScheduledTask, TaskStore

    task = ScheduledTask("0 23 * * 0", "projeleri yedekle", description="yedek", catch_up=False, id="t1")
    monkeypatch.setattr(schedule, "parse_task", lambda text, router: task)
    _answer(monkeypatch, "hayır")
    assert core_cli.tasks_cmd(["add", "her", "pazar"]) == 1
    assert TaskStore().get("t1") is None
    _answer(monkeypatch, "")
    assert core_cli.tasks_cmd(["add", "her", "pazar"]) == 0
    text = out()
    assert "Ne zaman: pazar 23:00" in text and "Kaçırılırsa: atlanır" in text
    assert "kuruldu (t1)" in text and "çekirdek çalışmıyor" in text

    ts = TaskStore()
    ts.get("t1").last_result = "yedek tamam " * 30
    ts.save()
    assert core_cli.tasks_cmd(["list"]) == 0
    assert "son sonuç: yedek tamam" in out()

    assert core_cli.tasks_cmd(["run", "t1"]) == 0
    assert TaskStore().get("t1").pending is True
    assert "bir sonraki çekirdek turunda" in out()
    assert core_cli.tasks_cmd(["rm", "t1"]) == 0
    assert TaskStore().tasks == []


def test_tasks_add_parse_error(monkeypatch, out):
    from alice.core import schedule

    def bad(text, router):
        raise ValueError("cron yok")

    monkeypatch.setattr(schedule, "parse_task", bad)
    assert core_cli.tasks_cmd(["add", "bir", "ara"]) == 1
    assert "anlaşılamadı: cron yok" in out()


# ---------------------------------------------------------------- extras/cli
def test_notes_add_list_search_undo(xout):
    from alice.extras import notes

    assert extras_cli.notes_cmd(["list"]) == 0
    assert "no notes" in xout()
    assert extras_cli.notes_cmd(["add", "Süt", "al"]) == 0
    notes.add("eski not", ts=time.time() - 10 * 86400)
    assert extras_cli.notes_cmd([]) == 0
    text = xout()
    assert "note saved" in text and "Süt al" in text and "eski not" in text

    assert extras_cli.notes_cmd(["search", "sut"]) == 0  # Turkish letter folding
    assert extras_cli.notes_cmd(["search", "not", "1d"]) == 0  # a note from 10 days ago falls outside the period
    tail = xout().split("eski not")[-1]
    assert "no notes" in tail

    assert extras_cli.notes_cmd(["undo"]) == 0
    assert "deleted: eski not" in xout()
    extras_cli.notes_cmd(["undo"])
    assert extras_cli.notes_cmd(["undo"]) == 0
    assert notes.all_notes() == [] and xout().rstrip().endswith("no notes")


def test_brief_install_and_render(monkeypatch, xout):
    from alice.core.schedule import TaskStore
    from alice.extras import brief

    assert extras_cli.brief_cmd(["--install", "07:30", "--weekdays"]) == 0
    t = TaskStore().get("brief")
    assert t.cron == "30 7 * * 1-5" and "set up" in xout()
    assert extras_cli.brief_cmd(["--install", "--weekdays"]) == 0  # no time given: 08:00
    assert TaskStore().get("brief").cron == "0 8 * * 1-5"

    real = brief.gather
    monkeypatch.setattr(brief, "gather", lambda cfg, store, tasks: real(cfg, store, tasks,
                                                                        weather_fn=lambda city: "güneşli [18°]"))
    assert extras_cli.brief_cmd([]) == 0
    text = xout()
    assert "hava: güneşli [18°]" in text and "bugün:" in text


def test_report_text_html_install(monkeypatch, xout, tmp_path):
    import webbrowser

    from alice.core.schedule import TaskStore
    from alice.core.store import Store

    st = Store()
    now = time.time()
    for i, v in enumerate((60, 70, 80)):
        st.add("pc.gpu.temp", v, ts=now - 3600 * (i + 1))
    st.add_event("alert", "GPU", "GPU sıcak", ts=now - 100)
    st.close()
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    assert extras_cli.report_cmd(["--days", "3", "--open"]) == 0
    text = xout()
    assert "Son 3 gün:" in text and "Ekran kartı sıcaklığı: ort 70°C, en yüksek 80°C" in text
    assert "GPU sıcak ×1" in text and ".html" in text
    assert len(opened) == 1 and opened[0].startswith("file:")

    assert extras_cli.report_cmd(["--install", "21:15"]) == 0
    assert TaskStore().get("weekly-report").cron == "15 21 * * 0"
    assert extras_cli.report_cmd(["--install"]) == 0
    assert TaskStore().get("weekly-report").cron == "0 20 * * 0"
