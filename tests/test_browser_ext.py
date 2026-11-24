"""ALICE browser extension: works in the user's own tabs without remote debugging."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
from pathlib import Path

import pytest

from alice.tools import browser, browser_ext
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config

EXT = Path(browser_ext.__file__).resolve().parent.parent / "extension"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ extension files
def test_extension_manifest_and_snapshot_match_python():
    m = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3 and m["background"]["service_worker"] == "background.js"
    assert {"tabs", "scripting", "downloads"} <= set(m["permissions"])
    for f in ("background.js", "popup.html", "popup.js", "icon.png"):
        assert (EXT / f).exists()
    js = (EXT / "background.js").read_text(encoding="utf-8")
    assert f"ws://127.0.0.1:{browser_ext.EXT_PORT}{browser_ext.PATH}" in js
    # The element selector and IDs match the Playwright path exactly: the same [ID]s on the same page
    sel = re.compile(r"const sel = ('[^;]+);", re.S)
    norm = lambda s: re.sub(r"'\s*\+\s*'", "", re.sub(r"\s+", " ", s))  # noqa: E731
    assert norm(sel.search(js).group(1)) == norm(sel.search(browser.SNAPSHOT_JS).group(1))
    for fn in ("snapshot", "place", "fill", "click", "select", "key", "scroll", "reveal", "outline", "text"):
        # it is copied into the page as text: the short method form "ad(...) {}" doesn't work
        # there and silently returned nothing
        assert re.search(rf"\n  {fn}: function \(", js), fn
    assert not re.search(r"\n  \w+\([^)]*\) \{", js.split("const PAGE = {", 1)[1])


# ------------------------------------------------------------------ local server
def _fake_extension(port: int, origin: str, handler, ready: threading.Event, stop: threading.Event):
    from websockets.asyncio.client import connect

    async def main():
        async with connect(f"ws://127.0.0.1:{port}{browser_ext.PATH}", origin=origin) as ws:
            await ws.send(json.dumps({"hello": {"version": "1.0.0"}}))
            ready.set()
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), 0.1)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                try:
                    reply = {"id": msg["id"], "result": handler(msg["method"], msg["params"])}
                except Exception as e:  # noqa: BLE001
                    reply = {"id": msg["id"], "error": str(e)}
                await ws.send(json.dumps(reply))

    asyncio.run(main())


def test_hub_talks_to_extension_and_rejects_web_pages():
    pytest.importorskip("websockets")
    port = free_port()
    h = browser_ext.ExtHub(port).start()
    assert not h.connected
    with pytest.raises(ConnectionError):
        h.call("tabs")
    ready, stop = threading.Event(), threading.Event()

    def handler(method, params):
        if method == "boom":
            raise RuntimeError("olmadı")
        return {"echo": method, "params": params}

    t = threading.Thread(target=_fake_extension, args=(port, "chrome-extension://abc", handler, ready, stop),
                         daemon=True)
    t.start()
    assert ready.wait(5)
    for _ in range(100):
        if h.connected and h.info:
            break
        threading.Event().wait(0.02)
    assert h.connected and h.info == {"version": "1.0.0"}
    assert h.call("tabs", tabId=3) == {"echo": "tabs", "params": {"tabId": 3}}
    with pytest.raises(ToolError, match="olmadı"):
        h.call("boom")
    # A web page (http origin) must not be able to connect to the local server and drive the browser
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async def evil():
        async with connect(f"ws://127.0.0.1:{port}{browser_ext.PATH}", origin="https://evil.example"):
            pass

    with pytest.raises(InvalidStatus):
        asyncio.run(evil())
    stop.set()
    t.join(3)
    for _ in range(100):
        if not h.connected:
            break
        threading.Event().wait(0.02)
    assert not h.connected


# ------------------------------------------------------------------ Playwright stand-in
class FakeHub:
    """Stands in for the extension: tabs and page functions kept in memory."""

    connected = True

    def __init__(self):
        self.tabs = [{"id": 1, "windowId": 9, "title": "YouTube", "url": "https://www.youtube.com/watch?v=x",
                      "active": True},
                     {"id": 2, "windowId": 9, "title": "Sonora", "url": "https://sonorahq.org/", "active": False}]
        self.calls: list[tuple[str, dict]] = []
        self.visible = False  # window in the background: falls back to in-page events
        self.elements = {1: {"id": 1, "tag": "a", "role": "", "type": "", "text": "Install", "placeholder": "",
                             "value": "", "href": "#install", "inView": True},
                         2: {"id": 2, "tag": "input", "role": "", "type": "text", "text": "", "placeholder": "Ara",
                             "value": "", "href": "", "inView": True}}

    def tab(self, tid):
        return next(t for t in self.tabs if t["id"] == tid)

    def call(self, method, _timeout=15.0, **p):
        self.calls.append((method, p))
        if method == "tabs":
            return {"tabs": self.tabs, "focusedWindow": 9}
        if method == "tab":
            return self.tab(p["tabId"])
        if method == "focus":
            for t in self.tabs:
                t["active"] = t["id"] == p["tabId"]
            return self.tab(p["tabId"])
        if method == "wait":
            return self.tab(p["tabId"])
        if method == "navigate":
            self.tab(p["tabId"])["url"] = p["url"]
            return self.tab(p["tabId"])
        if method == "create":
            t = {"id": 10 + len(self.tabs), "windowId": 9, "title": "", "url": p.get("url") or "about:blank",
                 "active": True}
            self.tabs.append(t)
            return t
        if method == "downloads":
            return [{"file": "C:\\Users\\k\\Downloads\\Sonora-Setup.exe", "url": "u", "state": "in_progress",
                     "error": "", "bytes": 50, "total": 200}]
        if method == "run":
            fn, args = p["fn"], p["args"]
            if fn == "snapshot":
                return list(self.elements.values())
            if fn == "place":
                if args[0] is not None and args[0] not in self.elements:
                    raise ToolError("öğe yok")
                return {"left": 10, "top": 20, "width": 100, "height": 30, "sx": 0, "sy": 0, "ow": 1000, "oh": 800,
                        "iw": 1000, "ih": 720, "dpr": 1.0, "visible": self.visible}
            if fn == "text":
                return "Sonora metni"
            return True
        raise AssertionError(method)


@pytest.fixture
def ext(monkeypatch, tmp_path):
    fh = FakeHub()
    monkeypatch.setattr(browser_ext, "_HUB", fh)
    monkeypatch.setattr(browser_ext, "_os_ok", lambda: False)  # no real mouse/keyboard in tests
    ctx = ToolContext(cwd=tmp_path, config=make_config({"brain": ["a:m"]}, tool_output_chars=4000))
    return fh, ctx


def test_get_browser_prefers_extension_over_cdp(ext):
    fh, ctx = ext
    b = browser.get_browser(ctx)
    assert isinstance(b, browser_ext.ExtBrowserSession) and b.attached
    assert b.page.tab_id == 1  # the active tab in the front window
    assert browser.get_browser(ctx) is b


def test_browser_tool_works_through_extension(ext):
    fh, ctx = ext
    tool = browser.Browser()
    r = tool.run(ctx, "goto", url="sonorahq.org/download")
    assert "opened" in r.text and "[1] a 'Install'" in r.text
    assert ("focus", {"tabId": 2}) in fh.calls  # the open Sonora tab was used; no new tab was opened
    assert any(c == "navigate" and p["tabId"] == 2 for c, p in fh.calls)
    r = tool.run(ctx, "type", element_id=2, text="nükleer", submit=True)
    assert ("run", {"tabId": 2, "fn": "fill", "args": [2, "nükleer"]}) in fh.calls
    assert ("run", {"tabId": 2, "fn": "key", "args": ["Enter"]}) in fh.calls  # window in background: Enter in page
    tool.run(ctx, "click", element_id=1)
    assert ("run", {"tabId": 2, "fn": "click", "args": [1, "left", 1]}) in fh.calls
    with pytest.raises(ToolError, match="öğe bulunamadı"):
        tool.run(ctx, "click", element_id=99)
    assert "Sonora metni" in tool.run(ctx, "text").text
    tabs = tool.run(ctx, "tabs").text
    assert "1: Sonora - https://sonorahq.org/download  <- aktif" in tabs
    dl = tool.run(ctx, "downloads").text
    assert "Sonora-Setup.exe | in_progress | %25" in dl


def test_extension_clicks_with_real_mouse_when_window_is_in_front(ext, monkeypatch):
    fh, ctx = ext
    fh.visible = True
    monkeypatch.setattr(browser_ext, "_os_ok", lambda: True)
    clicks = []
    from alice.tools import computer

    monkeypatch.setattr(computer, "_dpi_aware", lambda: None)
    monkeypatch.setattr(computer, "_pyautogui", lambda: type("PG", (), {"click": staticmethod(
        lambda *a, **k: clicks.append((a, k)))})())
    monkeypatch.setattr(browser_ext.time, "sleep", lambda s: None)
    b = browser.get_browser(ctx)
    b.locator(1).click()
    assert clicks and clicks[0][0] == (60, 115)  # the element's center on screen (left 10+50, top 80 toolbar+20+15)
    assert not any(c == "run" and p["fn"] == "click" for c, p in fh.calls)


def test_reflex_browser_observer_runs_on_extension(ext):
    from alice.reflex.observers import BrowserObserver

    fh, ctx = ext
    obs = BrowserObserver(ctx)
    ob = obs.observe()
    assert ob.location.startswith("https://www.youtube.com") and [e.label for e in ob.elements] == ["Install", "Ara"]
    obs.type(2, "merhaba", submit=False)
    assert ("run", {"tabId": 1, "fn": "fill", "args": [2, "merhaba"]}) in fh.calls


def test_browser_own_pages_get_a_clear_message(ext):
    fh, ctx = ext
    b = browser.get_browser(ctx)

    def deny(method, _timeout=15.0, **p):
        if method == "run":
            raise ToolError("tarayıcı eklentisi: Cannot access a chrome:// URL")
        return FakeHub.call(fh, method, **p)

    fh.call = deny
    with pytest.raises(ToolError, match="tarayıcının kendi sayfası"):
        b.snapshot()


@pytest.fixture
def files_server(tmp_path):
    import http.server
    import socketserver

    web = tmp_path / "web"
    web.mkdir()
    (web / "Sonora-Setup.exe").write_bytes(b"MZ" + b"\0" * 300_000)
    (web / "login.html").write_text("<html>giriş yap</html>", encoding="utf-8")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(web), **k)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_download_skips_save_as_and_returns_path(ext, files_server, tmp_path, monkeypatch):
    """Live capture: in Opera "her indirmede sor" (ask on every download) was on; when an install link opened in the
    browser it got stuck on Save As. The extension's downloads.download also opens the same window (saveAs=false, even
    when a file name is given). The download action saves the file straight to Downloads without the browser and returns
    the full path."""
    from alice.tools import fetch_file
    from alice.tools.desktop import open_app

    fh, ctx = ext
    dl = tmp_path / "Downloads"
    monkeypatch.setattr(fetch_file, "downloads_dir", lambda: dl)
    url = f"{files_server}/Sonora-Setup.exe"
    r = browser.Browser().run(ctx, "download", url=url)
    assert r.text == f"indirildi: {dl / 'Sonora-Setup.exe'}, 0.3 MB — kurulumu/dosyayı bu yolla aç"
    assert (dl / "Sonora-Setup.exe").read_bytes()[:2] == b"MZ" and not list(dl.glob("*.part"))
    assert browser.Browser().risk_for({"action": "download"}) == "network"
    assert not any(c == "download" for c, _ in fh.calls)  # tarayıcıya hiç gitmedi
    # windows open ile açılan kurulum bağlantısı da tarayıcıya gitmez; ikinci kopya üzerine yazmaz
    assert open_app(url).startswith(f"indirildi (açılmadı): {dl / 'Sonora-Setup (1).exe'}")
    assert "Sonora-Setup (1).exe | complete" in browser.Browser().run(ctx, "downloads").text


def test_download_needing_login_falls_back_to_browser(ext, files_server, tmp_path, monkeypatch):
    from alice.tools import fetch_file

    fh, ctx = ext
    monkeypatch.setattr(fetch_file, "downloads_dir", lambda: tmp_path)
    orig = fh.call
    fh.call = lambda m, _timeout=15.0, **p: ({"file": "", "url": p["url"], "state": "in_progress", "bytes": 0,
                                              "total": 0} if m == "download" else orig(m, _timeout, **p))
    r = browser.Browser().run(ctx, "download", url=f"{files_server}/login.html")  # page: login required
    assert "kaydetme penceresi açık olabilir" in r.text


def test_open_missing_file_says_so_and_lists_lookalikes(tmp_path):
    """Live capture: the install had been saved as e.exe; when ALICE opened the non-existent Sonora-Setup.exe, Windows
    showed its "bulunamadı" (not found) window, and reflex pressed Enter there and stopped. Now it returns an error and
    the similar files in the folder."""
    from alice.tools.desktop import open_app

    (tmp_path / "e.exe").write_bytes(b"MZ")
    with pytest.raises(ToolError) as e:
        open_app(str(tmp_path / "Sonora-Setup.exe"))
    assert "file not found" in str(e.value) and "e.exe" in str(e.value) and "download" in str(e.value)


def test_downloads_warns_when_saved_under_other_name(ext):
    fh, ctx = ext
    url = ("https://release-assets.githubusercontent.com/x?sp=r&rscd=attachment%3B+filename%3DSonora-Setup.exe"
           "&rsct=application%2Foctet-stream")
    orig = fh.call
    fh.call = lambda m, _timeout=15.0, **p: ([{"file": "C:/Users/k/Downloads/e.exe", "url": url, "state": "complete",
                                               "error": "", "bytes": 1, "total": 1}]
                                             if m == "downloads" else orig(m, _timeout, **p))
    out = browser.Browser().run(ctx, "downloads").text
    assert "NOTE: the name in the link is Sonora-Setup.exe; the file was saved as e.exe" in out


def test_sandbox_hint_only_for_access_errors():
    from alice.tools.shell import ACCESS_DENIED

    assert ACCESS_DENIED.search("Remove-Item : Access to the path 'C:/x' is denied.")
    assert ACCESS_DENIED.search("UnauthorizedAccessException")
    assert not ACCESS_DENIED.search("Get-Item : Cannot find path 'C:/Users/k/Downloads/Sonora-Setup.exe'")


def test_hub_pins_first_extension_id_and_rejects_impostors():
    """Any local program can send an Origin of `chrome-extension://...`: anything other than the first pinned extension
    is refused; IDs in the settings are always accepted."""
    h = browser_ext.ExtHub(0)
    assert h._ext_allowed("abc")  # the first one to connect gets pinned
    assert browser_ext.read_pinned_id() == "abc"
    assert h._ext_allowed("abc")
    assert not h._ext_allowed("evil")
    assert not h._ext_allowed("")
    assert not h._ext_allowed("a/b")
    h2 = browser_ext.ExtHub(0, allowed_ids=["cfgid"])
    assert h2._ext_allowed("cfgid") and h2._ext_allowed("abc") and not h2._ext_allowed("evil")
    browser_ext.pin_path().unlink()
    assert not h2._ext_allowed("other")  # if the settings have an ID, the first arrival is not pinned
    assert browser_ext.ExtHub(0)._ext_allowed("other")  # pinned again once the file is deleted
