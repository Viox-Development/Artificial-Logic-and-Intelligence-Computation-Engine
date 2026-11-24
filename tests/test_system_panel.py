"""Settings → Core: manages background ALICE and voice assistant from the app (alice core / alice voice on|off|test)."""

from __future__ import annotations

import json
import os
import threading
import time

from alice.app import system
from alice.config import Config, alice_home, default_config_dict
from alice.core import service
from alice.voice.audio import Audio
from tests.test_core import make_core


def cfg():
    return Config(default_config_dict())


def wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_stop_request_ends_running_core_within_a_second(tmp_path):
    core, _ = make_core(tmp_path)
    core.interval = 60  # long interval between turns: the request must still be noticed within a second
    service.request_stop()  # a leftover request for the previous core must not stop the new one
    t = threading.Thread(target=core.run_forever, daemon=True)
    t.start()
    assert wait(lambda: core.store.events(limit=5) and not service.stop_path().exists())
    time.sleep(0.2)
    assert t.is_alive()
    service.request_stop()
    t.join(3)
    assert not t.is_alive() and any("stopped from the app" in e.title for e in core.store.events(limit=5))


def test_state_reports_voice(tmp_path):
    core, _ = make_core(tmp_path)
    core.voice = "listening"
    core.tick(1000)
    assert json.loads(service.state_path().read_text(encoding="utf-8"))["voice"] == "listening"


def test_stop_core_terminates_old_core_that_ignores_request(monkeypatch):
    killed = []

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def terminate(self):
            killed.append(self.pid)

        def wait(self, t):
            pass

    import psutil

    state = {"running": True, "pid": 4242}
    monkeypatch.setattr(service, "read_state", lambda: dict(state))
    monkeypatch.setattr(psutil, "Process", Proc)
    (alice_home()).mkdir(parents=True, exist_ok=True)
    (alice_home() / "core.pid").write_text("4242", encoding="utf-8")
    assert system.stop_core(wait=0.05, sleep=lambda s: None)
    assert killed == [4242] and not (alice_home() / "core.pid").exists() and not service.stop_path().exists()
    killed.clear()
    state["running"] = False
    assert system.stop_core() and killed == []


def test_stop_core_waits_until_old_core_releases_lock(monkeypatch):
    """State file was removed but the process lock is still held: wait before starting a new core."""
    import os

    import psutil

    from alice.config import alice_home

    calls = {"n": 0}

    def read_state():
        calls["n"] += 1
        return {"running": calls["n"] == 1, "pid": os.getpid()}

    monkeypatch.setattr(service, "read_state", read_state)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    lock = alice_home() / "core.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    slept = []

    def sleep(s):
        slept.append(s)
        if len(slept) == 3:
            lock.unlink()  # old core shut down and released the lock

    assert system.stop_core(wait=5, sleep=sleep)
    assert len(slept) == 3 and not lock.exists()


def test_panel_actions(monkeypatch):
    from alice.config import load_config

    calls = []
    monkeypatch.setattr(service, "install_autostart", lambda: calls.append("install"))
    monkeypatch.setattr(service, "uninstall_autostart", lambda: calls.append("uninstall"))
    monkeypatch.setattr(service, "start_detached", lambda: calls.append("start"))
    monkeypatch.setattr(system, "stop_core", lambda: calls.append("stop"))
    monkeypatch.setattr(system.time, "sleep", lambda s: None)
    p = system.SystemPanel(cfg())
    v = p.view()
    assert v["core"]["running"] is False and "auto" in v["modes"] and any(x["name"] == "sherpa_onnx" for x in v["packages"])
    assert p.action("mode", {"mode": "flight"})["ok"] and service.force_path().read_text(encoding="utf-8") == "flight"
    assert p.action("mode", {"mode": "auto"})["ok"] and not service.force_path().exists()
    assert not p.action("mode", {"mode": "uzay"})["ok"]
    p.action("autostart", {"on": True})
    p.action("autostart", {"on": False})
    assert calls == ["install", "uninstall"]
    assert p.action("voice", {"on": False})["ok"] and calls == ["install", "uninstall"]  # core is off: no start
    assert p.action("voice", {"on": True})["ok"]
    assert wait(lambda: "start" in calls and not p._busy)
    assert load_config().data["voice"]["enabled"] is True and p.cfg.data["voice"]["enabled"] is True
    calls.clear()
    assert p.action("restart", {})["ok"]
    assert wait(lambda: calls == ["stop", "start"] and not p._busy)
    assert not p.action("uç", {})["ok"]


def test_restart_inside_core_uses_core_restart_not_stop_start(monkeypatch):
    """If the app runs inside the core, stop_core would kill this process and start_detached would never be reached."""
    calls = []
    monkeypatch.setattr(service, "start_detached", lambda: calls.append("start"))
    monkeypatch.setattr(system, "stop_core", lambda: calls.append("stop"))
    monkeypatch.setattr(system.time, "sleep", lambda s: None)
    monkeypatch.setattr(service, "read_state", lambda: {"running": True, "pid": os.getpid()})
    p = system.SystemPanel(cfg(), restart_core=lambda: calls.append("core") or True)
    assert p.action("restart", {}) == {"ok": True, "restarting": True}
    assert p.action("voice", {"on": True})["ok"]
    assert calls == ["core", "core"] and not p._busy
    # Core in another process (standalone app): the old path.
    calls.clear()
    monkeypatch.setattr(service, "read_state", lambda: {"running": True, "pid": os.getpid() + 1})
    assert p.action("restart", {})["ok"]
    assert wait(lambda: calls == ["stop", "start"] and not p._busy)


class FakeMic:
    def __init__(self, loud=True):
        self.loud = loud

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        time.sleep(0.003)
        return (b"\x00\x20" if self.loud else b"\0\0") * 480


class FakeSTT:
    last = "groq"

    def __init__(self):
        self.got = []

    def transcribe(self, audio: Audio):
        self.got.append(audio)
        return "merhaba alice"


def test_mic_test_shows_level_and_what_was_heard():
    stt = FakeSTT()
    t = system.MicTest(cfg(), mic=FakeMic, stt=lambda c: stt, seconds=0.2)
    assert t.start()["ok"] and not t.start()["ok"]
    t._thread.join(5)
    v = t.view()
    assert v["state"] == "done" and v["text"] == "merhaba alice" and v["engine"] == "groq" and v["peak"] > 0.1
    assert stt.got and stt.got[0].rate == 16000
    t = system.MicTest(cfg(), mic=lambda: FakeMic(loud=False), stt=lambda c: stt, seconds=0.1)
    t.start()
    t._thread.join(5)
    assert "ses gelmiyor" in t.view()["message"] and len(stt.got) == 1  # if silent, it does not bother transcribing


def test_app_endpoints(workdir, monkeypatch):
    import httpx

    from alice.app.server import AppServer, AppState
    from tests.conftest import FakeProvider, make_config

    c = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(c, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    srv = AppServer(state).start()
    try:
        client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        assert client.get("/api/system").json()["core"]["running"] is False
        assert client.get("/api/system/mictest").json()["state"] == "idle"
        assert client.post("/api/system/action", json={"action": "mode", "mode": "quiet"}).json()["ok"]
        assert client.post("/api/system/action", json={"action": "x"}).status_code == 400
        assert os.path.exists(service.force_path())
    finally:
        srv.stop()
