"""App lives inside the core: the window attaches to the running core and closes when the core exits."""

from __future__ import annotations

import json
import os

from alice.app import host


def write_info(url: str, pid: int) -> None:
    host.info_path().parent.mkdir(parents=True, exist_ok=True)
    host.info_path().write_text(json.dumps({"url": url, "pid": pid}), encoding="utf-8")


def test_read_url_requires_live_core():
    assert host.read_url() == ""
    write_info("http://127.0.0.1:1/?t=a", os.getpid())
    assert host.read_url() == "http://127.0.0.1:1/?t=a"
    write_info("http://127.0.0.1:1/?t=a", 2**22 + 12345)  # dead process
    assert host.read_url() == ""


def test_follow_closes_when_core_gone_and_switches_on_restart():
    write_info("http://x/?t=1", os.getpid())
    check = host.follow("http://x/?t=1")
    assert check() == ""
    host.info_path().unlink()
    host.mark_restarting()
    assert check() == ""  # restarting: wait
    write_info("http://x/?t=2", os.getpid())
    assert check() == "http://x/?t=2"
    assert check() == ""
    host.info_path().unlink()
    host.restarting_path().unlink()
    assert check() is None  # core shut down: the window closes too


def test_revive_restarts_core_only_after_unclean_death():
    from alice.core import service

    host._revived.clear()  # noqa: SLF001
    started = []
    start = lambda: started.append(1) or True  # noqa: E731
    assert host.revive_crashed(start) is False  # no state file: clean shutdown, leave it alone
    service.state_path().parent.mkdir(parents=True, exist_ok=True)
    service.state_path().write_text(json.dumps({"pid": os.getpid(), "ts": 0}), encoding="utf-8")
    assert host.revive_crashed(start) is False  # process is alive
    for _ in range(host.REVIVE_MAX):
        service.state_path().write_text(json.dumps({"pid": 2**22 + 12345, "ts": 0}), encoding="utf-8")
        assert host.revive_crashed(start) is True  # crashed: restart it
        assert not service.state_path().exists() and host.restarting()
    service.state_path().write_text(json.dumps({"pid": 2**22 + 12345, "ts": 0}), encoding="utf-8")
    assert host.revive_crashed(start) is False  # crash loop: limit reached
    assert len(started) == host.REVIVE_MAX
    host._revived.clear()  # noqa: SLF001


def test_attach_starts_core_and_waits(monkeypatch):
    started = []

    def start() -> bool:
        started.append(1)
        write_info("http://y/?t=3", os.getpid())
        return True

    assert host.attach(start=start, wait=2) == "http://y/?t=3"
    assert started == [1]
    assert host.attach(start=start, wait=2) == "http://y/?t=3" and started == [1]  # already running: don't start it


def test_attach_gives_up_without_core():
    assert host.attach(start=lambda: False, wait=1) == ""


def test_app_cmd_attaches_to_core(monkeypatch):
    from alice import cli
    from alice.app import window

    monkeypatch.setattr(host, "attach", lambda: "http://z/?t=4")
    seen = []
    monkeypatch.setattr(window, "open_window", lambda url, watch=None, **k: seen.append((url, watch is not None)))
    assert cli.app_cmd([]) == 0 and seen == [("http://z/?t=4", True)]


def test_app_cmd_falls_back_when_core_has_no_app(monkeypatch):
    from alice import cli

    monkeypatch.setattr(host, "attach", lambda: "")
    assert cli._attach_to_core() is None  # opens in its own process the old way


def test_host_builds_state_and_cleans_up(tmp_path):
    from tests.conftest import make_config

    cfg = make_config({"brain": ["a:m"]})
    calls = []
    h = host.AppHost(cfg, cwd=tmp_path, restart=lambda: calls.append(1)).start(background=False)
    try:
        assert not h.error, h.error
        url = host.read_url()
        assert url.startswith("http://127.0.0.1:") and h.state.token in url
        assert h.state.restart_hook is not None and not h.busy
    finally:
        h.stop()
    assert host.read_url() == "" and not host.info_path().exists()
