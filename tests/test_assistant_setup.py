"""New alice doctor checks, the alice setup google wizard, and the Asistan (Assistant) tab in the app."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest

from alice import doctor, setup_google
from alice.app.server import AppServer, AppState
from alice.connect import store
from tests.conftest import FakeProvider, make_config


# ----------------------------------------------------------------------------------------- doctor
def test_doctor_checks_run_offline(workdir):
    checks = doctor.run_all(make_config({"brain": ["a:m"]}), workdir, network=False)
    names = [c.name for c in checks]
    assert len(checks) == 11 and "Gmail" in names and "Command sandbox" in names
    assert all(c.status in ("ok", "warn", "off", "fail") for c in checks)
    gm = next(c for c in checks if c.name == "Gmail")
    assert gm.status == "off" and "alice setup google" in gm.fix
    assert next(c for c in checks if c.name == "Google search").status == "off"  # no key


def test_doctor_one_broken_check_does_not_stop_others(monkeypatch, workdir):
    monkeypatch.setattr(doctor, "check_memory", lambda cfg: 1 / 0)
    checks = doctor.run_all(make_config({"brain": ["a:m"]}), workdir, network=False)
    mem = next(c for c in checks if c.name == "Memory")
    assert mem.status == "fail" and "ZeroDivisionError" in mem.detail and len(checks) == 11


def test_doctor_tests_providers_even_with_auto_roles(monkeypatch):
    from alice.providers import ranking

    monkeypatch.setattr(ranking, "load_cache", lambda: {"results": {
        "groq:a": {"ref": "groq:a", "ok": True, "quality": 50}, "groq:b": {"ref": "groq:b", "ok": True, "quality": 80},
        "groq:c": {"ref": "groq:c", "ok": False, "quality": 99}}})
    assert doctor.test_ref(None, "groq", SimpleNamespace(models=[])) == "groq:b"
    assert doctor.test_ref(None, "zai", SimpleNamespace(models=["glm-4.7-flash"])) == "zai:glm-4.7-flash"
    assert doctor.test_ref(None, "x", SimpleNamespace(models=[]), "x:elle") == "x:elle"


# ----------------------------------------------------------------------------------------- setup google
CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-xyz"}}


def test_set_env_replaces_and_appends(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# yorum\nGOOGLE_CLIENT_ID=\nGEMINI_API_KEY=k\n", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    setup_google.set_env(env, "GOOGLE_CLIENT_ID", "id1")
    setup_google.set_env(env, "GOOGLE_CLIENT_SECRET", "s1")
    assert env.read_text(encoding="utf-8").splitlines() == ["# yorum", "GOOGLE_CLIENT_ID=id1", "GEMINI_API_KEY=k", "GOOGLE_CLIENT_SECRET=s1"]
    assert os.environ["GOOGLE_CLIENT_ID"] == "id1"
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")


def test_read_client_json_rejects_other_files():
    assert setup_google.read_client_json(json.dumps(CLIENT)) == ("123-abc.apps.googleusercontent.com", "GOCSPX-xyz")
    with pytest.raises(ValueError):
        setup_google.read_client_json('{"foo": 1}')


def test_wizard_full_flow_with_downloaded_json(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "client_secret_123.json").write_text(json.dumps(CLIENT), encoding="utf-8")
    answers = iter([""] * len(setup_google.STEPS) + ["", "", "okul", "Okul!", "kisisel", ""])
    opened, said, gmail = [], [], []
    rc = setup_google.run(lambda q: next(answers), said.append, opened.append, connect_calendar=lambda: True,
                          connect_gmail=lambda acc: gmail.append(acc) or f"{acc}@x.com",
                          env_path=tmp_path / ".env", downloads=downloads)
    assert rc == 0
    assert opened == [url for _, url, _ in setup_google.STEPS]
    assert "GOOGLE_CLIENT_ID=123-abc.apps.googleusercontent.com" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert gmail == ["okul", "kisisel"]  # "Okul!" is an invalid name: asked again
    assert any("Calendar connected" in s for s in said) and "okul (okul@x.com)" in said[-1]
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")


def test_wizard_skips_cloud_steps_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "zaten.apps.googleusercontent.com")
    opened = []
    answers = iter(["h", ""])
    assert setup_google.run(lambda q: next(answers), lambda s: None, opened.append, env_path=tmp_path / ".env") == 0
    assert opened == []


def test_wizard_quit_midway(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert setup_google.run(lambda q: "q", lambda s: None, lambda u: None, env_path=tmp_path / ".env") == 1
    assert not (tmp_path / ".env").exists()


# ----------------------------------------------------------------------------------------- Assistant (Asistan) tab
@pytest.fixture
def app(workdir):
    state = AppState(make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}), workdir, "auto",
                     provider_factory=lambda pc: FakeProvider("a", []))
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client
    srv.stop()


def test_assistant_tab_status_settings_rules_and_gmail(app, workdir, isolated_home):
    from alice.config import load_config
    from alice.permissions import persist_allow

    state, client = app
    persist_allow(workdir, "run_command(git status*)")
    store.save("gmail:okul", access_token="t", expires_at=time.time() + 3600, email="h@okul.edu")
    d = client.get("/api/assistant").json()
    assert len(d["checks"]) == 11 and d["settings"]["agent.sandbox"] == "auto"
    assert {"kind": "allow", "rule": "run_command(git status*)", "source": "project", "untrusted": False} in d["rules"]
    assert d["gmail"] == [{"account": "okul", "email": "h@okul.edu"}] and d["google_ready"] is False

    assert client.post("/api/assistant/set", json={"section": "agent", "key": "sandbox", "value": "on"}).json()["ok"]
    assert state.cfg.data["agent"]["sandbox"] == "on"
    assert load_config().agent["sandbox"] == "on"  # written to config.toml
    assert client.post("/api/assistant/set", json={"section": "greeting", "key": "enabled", "value": False}).json()["ok"]
    bad = client.post("/api/assistant/set", json={"section": "agent", "key": "permission_mode", "value": "yolo"})
    assert bad.status_code == 400  # only allowed settings
    assert client.post("/api/assistant/set", json={"section": "agent", "key": "sandbox", "value": "yok"}).status_code == 400

    r = client.post("/api/assistant/action", json={"action": "rule_delete", "kind": "allow",
                                                   "rule": "run_command(git status*)"}).json()
    assert r["ok"] and "git status" not in (workdir / ".alice" / "permissions.toml").read_text(encoding="utf-8")
    assert client.post("/api/assistant/action", json={"action": "gmail_logout", "account": "okul"}).json()["ok"]
    assert client.get("/api/assistant").json()["gmail"] == []
    r = client.post("/api/assistant/action", json={"action": "gmail_connect", "account": "iş"}).json()
    assert not r["ok"] and "letters" in r["error"]
    r = client.post("/api/assistant/action", json={"action": "gmail_connect", "account": "is"}).json()
    assert not r["ok"] and "Setup" in r["error"]

