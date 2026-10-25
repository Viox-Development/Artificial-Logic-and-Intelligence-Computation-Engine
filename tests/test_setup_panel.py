"""Ayarlar → Kurulum: doctor, modelleri ölç, Google, telefon, DCS, eklentiler, kısayol uygulamadan."""

from __future__ import annotations

import json
import os

from alice.app.setup_panel import Job, SetupPanel, qr_svg
from alice.config import alice_home
from tests.conftest import make_config


class FakeRouter:
    def __init__(self):
        self.reloaded = []

    def reload_auto(self, cache):
        self.reloaded.append(cache)


class FakeSystem:
    def __init__(self):
        self.calls = []

    def action(self, a, d):
        self.calls.append(a)
        return {"ok": True}


def panel():
    return SetupPanel(make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}), FakeRouter(), FakeSystem())


def test_job_reports_progress_result_and_error():
    j = Job()

    def work(job):
        job.say("bir")
        job.say("iki")
        return {"x": 1}

    assert j.start(work)["ok"]
    j._thread.join(5)
    assert j.view()["state"] == "done" and j.view()["lines"] == ["bir", "iki"] and j.view()["result"] == {"x": 1}
    j.start(lambda job: 1 / 0)
    j._thread.join(5)
    assert j.view()["state"] == "error" and "division" in j.view()["error"]


def test_view_and_unknown_actions():
    p = panel()
    v = p.view()
    assert v["google"]["steps"][0]["title"] == "Proje" and not v["google"]["client"]
    assert v["phone"]["enabled"] is False and v["plugins"]["items"] == [] and v["models"]["state"] == "idle"
    assert not p.action("uç", {})["ok"] and not p.action("_run_doctor", {})["ok"]


def test_models_refresh_and_auto(monkeypatch):
    from alice.providers import ranking

    p = panel()

    def fake_refresh(router, emit=lambda s: None, **k):
        emit("3 aday ölçülüyor")
        return {"chains": {"brain": ["groq:x", "gemini:y"]}, "probed_at": "bugün",
                "results": {"a": {"ok": True}, "b": {"ok": False}}}

    monkeypatch.setattr(ranking, "refresh", fake_refresh)
    assert p.action("models_refresh", {})["ok"]
    p.models._thread.join(5)
    m = p.view()["models"]
    assert m["state"] == "done" and m["result"]["working"] == 1 and m["lines"] == ["3 aday ölçülüyor"]
    assert p.router.reloaded
    assert p.action("models_auto", {})["ok"]
    text = (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert 'brain = ["auto", "ollama:qwen3:8b"]' in text and p.cfg.roles["brain"][0] == "auto"


def test_google_client_from_paste_and_download(tmp_path, monkeypatch):
    from alice import setup_google

    p = panel()
    assert not p.action("google_client", {"text": "rastgele"})["ok"]
    assert not p.action("google_client", {"text": "123.apps.googleusercontent.com"})["ok"]  # secret eksik
    assert p.action("google_client", {"text": "123.apps.googleusercontent.com", "secret": "s3"})["ok"]
    assert os.environ["GOOGLE_CLIENT_ID"] == "123.apps.googleusercontent.com" and p.view()["google"]["client"]
    dl = tmp_path / "client_secret_abc.json"
    dl.write_text(json.dumps({"installed": {"client_id": "9.apps.googleusercontent.com", "client_secret": "q"}}))
    monkeypatch.setattr(setup_google, "find_downloaded_json", lambda folder=None: dl)
    assert p.view()["google"]["found_json"] == "client_secret_abc.json"
    assert p.action("google_client", {"use_download": True})["ok"] and os.environ["GOOGLE_CLIENT_SECRET"] == "q"
    opened = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert p.action("google_open", {"step": 1})["ok"] and "calendar" in opened[0]
    assert not p.action("google_open", {"step": 99})["ok"]


def test_phone_toggle_restarts_core_and_shows_qr(monkeypatch):
    from alice.core import service

    p = panel()
    monkeypatch.setattr(service, "read_state", lambda: {"running": True})
    assert p.action("phone_enable", {"on": True})["ok"] and p.system.calls == ["restart"]
    ph = p.view()["phone"]
    assert ph["enabled"] and "/quick?t=" in ph["url"]
    tok = ph["url"].split("t=")[1]
    assert p.action("phone_new_token", {})["ok"] and p.view()["phone"]["url"].split("t=")[1] != tok
    assert p.system.calls == ["restart", "restart"]


def test_qr_svg_when_library_present():
    import importlib.util

    svg = qr_svg("http://192.168.1.5:8765/quick?t=abc")
    if importlib.util.find_spec("qrcode") is None:
        assert svg == ""
    else:
        assert svg.startswith("<svg") and "<rect" in svg


def test_dcs_install_and_kneeboard(tmp_path, monkeypatch):
    from alice.sims import dcs
    from alice.sims import kneeboard as kb

    p = panel()
    monkeypatch.setattr(dcs, "saved_games_dirs", lambda: [])
    assert not p.action("dcs_install", {"on": True})["ok"]
    sg = tmp_path / "DCS"
    sg.mkdir()
    monkeypatch.setattr(dcs, "saved_games_dirs", lambda: [sg])
    assert p.action("dcs_install", {"on": True})["ok"] and p.view()["dcs"]["dirs"][0]["installed"]
    assert p.action("dcs_install", {"on": False})["ok"] and not p.view()["dcs"]["dirs"][0]["installed"]
    monkeypatch.setattr(kb, "render", lambda info, notes: ["sayfa"])
    monkeypatch.setattr(kb, "write", lambda pages, dirs: [tmp_path / "1.png"])
    assert p.action("dcs_kneeboard", {})["pages"] == 1


def test_plugin_template_and_shortcut(monkeypatch):
    from alice import plugins
    from alice.app import setup_panel

    opened = []
    monkeypatch.setattr(setup_panel, "open_file", opened.append)
    p = panel()
    assert not p.action("plugin_new", {"name": "Kötü Ad"})["ok"]
    r = p.action("plugin_new", {"name": "hava-durumu"})
    assert r["ok"] and r["path"].endswith("hava_durumu.py") and opened
    assert "hava_durumu" in (plugins.plugin_dir() / "hava_durumu.py").read_text(encoding="utf-8")
    assert not p.action("plugin_new", {"name": "hava-durumu"})["ok"]  # zaten var
    from alice.app import window

    monkeypatch.setattr(window, "create_shortcut", lambda: (_ for _ in ()).throw(RuntimeError("sadece Windows")))
    assert p.action("shortcut", {})["error"] == "sadece Windows"


def test_doctor_runs_in_background(workdir):
    import httpx

    from alice.app.server import AppServer, AppState
    from tests.conftest import FakeProvider

    c = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(c, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    srv = AppServer(state).start()
    try:
        client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        assert client.get("/api/setup").json()["doctor"]["state"] == "idle"
        assert client.post("/api/setup/action", json={"action": "doctor_run"}).json()["ok"]
        state.setup.doctor._thread.join(60)
        doc = client.get("/api/setup").json()["doctor"]
        assert doc["state"] in ("done", "error"), doc
        if doc["state"] == "done":
            assert isinstance(doc["result"]["providers"], list) and doc["result"]["checks"]
        assert client.post("/api/setup/action", json={"action": "nope"}).status_code == 400
    finally:
        srv.stop()
