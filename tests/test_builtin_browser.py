"""Built-in browser: address bar fixing, console/network events, the agent choosing this browser, and the preview tool.
Real browser is not opened (a browser may not exist on CI); DevTools messages and the session are fake."""

from __future__ import annotations

import threading
from collections import deque

import pytest

from alice.app import builtin_browser as bbm
from alice.tools import browser as br
from alice.tools.base import ToolContext, ToolError
from alice.tools.preview import Preview, format_logs
from tests.conftest import make_config


@pytest.mark.parametrize("raw,url", [
    ("example.com", "https://example.com"), ("localhost:5173", "http://localhost:5173"),
    ("http://x.test/a", "http://x.test/a"), ("kedi videoları", "https://www.google.com/search?q=kedi+videolar%C4%B1"),
    ("", "about:blank")])
def test_address_bar_normalization(raw, url):
    assert bbm._normalize(raw) == url


def test_console_and_network_events_are_collected():
    page = object.__new__(bbm.CDPPage)
    page.logs, page._urls = deque(maxlen=50), {}
    page._event("Runtime.consoleAPICalled", {"type": "error", "args": [{"value": "x tanımsız"}]})
    page._event("Runtime.consoleAPICalled", {"type": "log", "args": [{"value": "sıradan"}]})  # not an error: dropped
    page._event("Runtime.exceptionThrown", {"exceptionDetails": {"exception": {"description": "TypeError: a is null"}}})
    page._event("Network.requestWillBeSent", {"requestId": "1", "request": {"url": "http://localhost/api"}})
    page._event("Network.loadingFailed", {"requestId": "1", "errorText": "net::ERR_CONNECTION_REFUSED"})
    page._event("Network.responseReceived", {"response": {"status": 404, "url": "http://localhost/logo.png"}})
    kinds = [(r["kind"], r["text"]) for r in page.logs]
    assert ("console", "x tanımsız") in kinds and not any("sıradan" in t for _, t in kinds)
    assert any("TypeError" in t for _, t in kinds)
    assert any("ERR_CONNECTION_REFUSED http://localhost/api" in t for _, t in kinds)
    assert any("HTTP 404" in t for _, t in kinds)
    out = format_logs(list(page.logs))
    assert out.startswith("4 errors") and "uncaught error" in out


def test_agent_uses_builtin_browser_by_default(monkeypatch, workdir):
    made = []

    class FakeSession:
        def __init__(self, **kw):
            made.append(kw)

        def ensure_page(self):
            pass

        def alive(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(br, "BrowserSession", FakeSession)
    bb = type("BB", (), {"endpoint": "http://127.0.0.1:9"})()
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    ctx.state["builtin_browser"] = lambda: bb
    s = br.get_browser(ctx)
    assert made[-1] == {"cdp": "http://127.0.0.1:9", "allow_launch": False} and s.builtin and s.headless
    assert br.get_browser(ctx) is s  # the same session is reused
    ctx2 = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, browser="user"))
    ctx2.state["builtin_browser"] = lambda: bb
    monkeypatch.setattr(br.browser_ext if hasattr(br, "browser_ext") else __import__("alice.tools.browser_ext",
                        fromlist=["hub"]), "hub", lambda: None)
    br.get_browser(ctx2)
    assert made[-1].get("cdp") != "http://127.0.0.1:9"  # the user chose their own browser


class FakeBB:
    def __init__(self):
        self.calls = []

    def navigate(self, url):
        self.calls.append(("nav", url))
        return url

    def history(self, step):
        self.calls.append(("hist", step))

    def wait_load(self, timeout=15):
        return True

    def screenshot_png(self):
        return "UE5H"

    def current(self):
        return {"title": "Uygulamam", "url": "http://localhost:5173/"}

    def logs(self, since=0.0, errors_only=True):
        return [{"t": 1, "kind": "exception", "level": "error", "text": "ReferenceError: foo is not defined"}]


def test_preview_opens_page_and_reports_errors(monkeypatch, workdir):
    monkeypatch.setattr("time.sleep", lambda s: None)
    bb = FakeBB()
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    ctx.state["builtin_browser"] = lambda: bb
    out = Preview().run(ctx, "open", url="http://localhost:5173")
    assert ("nav", "http://localhost:5173") in bb.calls and out.images == ["UE5H"]
    assert "Uygulamam" in out.text and "ReferenceError" in out.text
    Preview().run(ctx, "check")
    assert ("hist", "reload") in bb.calls
    assert Preview().risk_for({"action": "open", "command": "npm run dev"}) == "exec"
    with pytest.raises(ToolError, match="only in the ALICE app"):
        Preview().run(ToolContext(cwd=workdir, config=ctx.config), "check")


def test_preview_reports_server_that_dies(monkeypatch, workdir):
    import subprocess
    import sys
    import time

    from alice.tools.shell import BgProc

    monkeypatch.setattr("alice.tools.preview._reachable", lambda url: False)
    log = workdir / "srv.log"

    def fake_run(self, ctx, command, background=False, **kw):
        with open(log, "wb") as f:
            p = subprocess.Popen([sys.executable, "-c", "print('port 5173 dolu'); raise SystemExit(1)"], stdout=f,
                                 stderr=subprocess.STDOUT)
        p.wait()
        ctx.state.setdefault("bg_procs", {})[p.pid] = BgProc(p.pid, command, p, log, time.time())
        return type("R", (), {"text": f"started in the background pid={p.pid}"})()

    monkeypatch.setattr("alice.tools.shell.RunCommand.run", fake_run)
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    ctx.state["builtin_browser"] = lambda: FakeBB()
    with pytest.raises(ToolError, match="port 5173 dolu"):
        Preview().run(ctx, "open", url="http://localhost:5173", command="npm run dev", wait=10)


def test_lock_is_reentrant_safe():
    b = bbm.BuiltinBrowser.__new__(bbm.BuiltinBrowser)
    b._lock = threading.Lock()
    b.proc = None
    assert not b.running()


def test_devtools_port_does_not_allow_any_origin(monkeypatch, tmp_path):
    """No --remote-allow-origins=*: a page in the browser can't reach the DevTools port and drive the browser."""
    seen = []
    monkeypatch.setattr(bbm.subprocess, "Popen", lambda args, **kw: seen.append(args) or object())
    b = bbm.BuiltinBrowser(tmp_path / "profil")
    monkeypatch.setattr(b, "_http", lambda path, method="GET": {})
    b._launch("chrome.exe")
    assert seen and f"--remote-debugging-port={b.port}" in seen[0]
    assert not any(a.startswith("--remote-allow-origins") for a in seen[0])
