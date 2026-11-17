"""Optional features: catalog, dependency closure, background install, wizard state and the /api/features panel."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from alice import features
from alice.app.features_panel import FeaturesPanel
from tests.conftest import make_config


def test_with_needs_adds_dependencies_in_catalog_order():
    assert features.with_needs(["phone"]) == ["core", "phone"]
    assert features.with_needs(["voice_local", "computer"]) == ["voice", "voice_local", "computer"]
    assert features.with_needs(["nope", "browser", "browser"]) == ["browser"]
    assert features.with_needs([]) == []


def test_pip_command_installs_the_repo_editable_with_extras():
    cmd = features.pip_command(["voice", "computer", "voice"])
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert cmd[-2] == "-e" and cmd[-1].endswith("[computer,voice]")


def test_catalog_marks_installed_features(monkeypatch):
    monkeypatch.setattr(features, "installed", lambda f, cfg=None: f.id == "computer")
    cat = {c["id"]: c for c in features.catalog("tr")}
    assert cat["computer"]["installed"] and not cat["voice"]["installed"]
    assert cat["voice"]["title"] == "Ses" and cat["phone"]["needs"] == ["core"]
    assert features.catalog("xx")[0]["title"] == "Voice"  # unknown language falls back to English


def test_wizard_state_round_trip():
    assert features.wizard_state() == {}
    features.set_wizard_state(wizard="pending")
    features.set_wizard_state(lang="tr")
    assert features.wizard_state() == {"wizard": "pending", "lang": "tr"}


def _installer(monkeypatch, ok=True, have=()):
    """An installer whose pip "installs" by marking modules importable."""
    have = set(have)
    monkeypatch.setattr(features, "installed", lambda f, cfg=None: f.id in have)
    monkeypatch.setattr(features, "has_system_browser", lambda: True)
    calls, turned_on = [], []

    def runner(cmd, say):
        calls.append(cmd)
        say("Collecting something")
        if ok:
            have.update(i for i in ("voice", "voice_local", "computer", "browser", "core", "phone")
                        if any(f"{e}" in cmd[-1] for e in features.BY_ID[i].extras))
        return 0 if ok else 1

    inst = features.Installer(runner=runner, after=lambda ids, opts: turned_on.append((ids, opts)) or ["on"])
    return inst, calls, turned_on


def test_installer_runs_pip_once_then_turns_features_on(monkeypatch):
    inst, calls, turned_on = _installer(monkeypatch)
    assert inst.start(["phone"], {"autostart": False}) == {"ok": True, "ids": ["core", "phone"]}
    inst._thread.join(5)
    v = inst.view()
    assert v["state"] == "done" and v["done"] == ["core", "phone"], v
    assert len(calls) == 1 and calls[0][-1].endswith("[core,home]")
    assert turned_on == [(["core", "phone"], {"autostart": False})]
    assert "Collecting something" in v["lines"] and "on" in v["lines"]


def test_installer_skips_installed_and_reports_pip_failure(monkeypatch):
    inst, calls, turned_on = _installer(monkeypatch, have={"computer"})
    inst.start(["computer"])
    inst._thread.join(5)
    assert inst.view()["state"] == "done" and calls == []  # nothing to download

    inst, calls, turned_on = _installer(monkeypatch, ok=False)
    inst.start(["voice"])
    inst._thread.join(5)
    v = inst.view()
    assert v["state"] == "error" and "pip exited" in v["error"] and turned_on == []
    assert inst.start([])["ok"] is False


class FakeSystem:
    def __init__(self):
        self.calls = []

    def action(self, a, d):
        self.calls.append((a, d))
        return {"ok": True}


def _panel(monkeypatch):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    pc = SimpleNamespace(api_key_env="GEMINI_API_KEY", enabled=False, needs_key=True, api_key="")
    state = SimpleNamespace(cfg=cfg, router=SimpleNamespace(providers_cfg={"gemini": pc}), system=FakeSystem(),
                            set_key=lambda k, v: {"ok": True})
    monkeypatch.setattr("alice.config.set_config_value", lambda *a: None)
    return FeaturesPanel(state), state


def test_panel_language_wizard_and_bad_actions(monkeypatch):
    p, _ = _panel(monkeypatch)
    assert p.view()["wizard"] == {"pending": False, "lang": "en"}
    assert p.action("open", {})["ok"] and p.view()["wizard"]["pending"]
    assert p.action("lang", {"lang": "tr"})["ok"] and p.lang() == "tr"
    assert not p.action("lang", {"lang": "xx"})["ok"]
    assert p.action("done", {})["ok"] and not p.view()["wizard"]["pending"]
    assert not p.action("nope", {})["ok"]
    assert not p.action("key", {"key": "NOT_A_KEY", "value": "x"})["ok"]
    assert [k["key"] for k in p.view()["keys"]] == ["GEMINI_API_KEY"]


def test_panel_key_runs_a_probe_of_that_provider_only(monkeypatch):
    p, _ = _panel(monkeypatch)
    seen = {}

    def probe(cfg, emit=None, only=None, **kw):
        seen["only"] = only
        return [{"name": "gemini", "status": "ok", "model": "gemini:x", "detail": ""}]

    monkeypatch.setattr("alice.doctor.probe_providers", probe)
    assert p.action("key", {"key": "GEMINI_API_KEY", "value": "abc"})["ok"]
    p.key_test._thread.join(5)
    assert seen["only"] == {"gemini"}
    assert p.view()["key_test"]["result"]["status"] == "ok"


def test_turn_on_enables_voice_phone_and_starts_core(monkeypatch):
    p, state = _panel(monkeypatch)
    monkeypatch.setattr("alice.core.service.read_state", lambda: {"running": False})
    lines = p._turn_on(["voice", "core", "phone"], {"autostart": True})
    assert p.cfg.data["voice"]["enabled"] and p.cfg.data["phone"]["enabled"]
    assert [a for a, _ in state.system.calls] == ["autostart", "start"]
    assert any("core" in line for line in lines)
    state.system.calls.clear()
    p._turn_on(["core"], {"autostart": False})
    assert [a for a, _ in state.system.calls] == ["start"]


def test_python_prefers_console_python_over_pythonw(monkeypatch, tmp_path):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert features._python() == str(tmp_path / "python.exe")


def test_phone_counts_as_installed_only_when_switched_on(monkeypatch):
    monkeypatch.setattr(features.importlib.util, "find_spec", lambda m: object())
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    phone = features.BY_ID["phone"]
    assert features.installed(phone) and not features.installed(phone, cfg)
    cfg.data.setdefault("phone", {})["enabled"] = True
    assert features.installed(phone, cfg)
