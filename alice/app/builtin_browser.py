"""Built-in browser: ALICE's own Chromium, shown live in a panel inside the app.

Like the agent browser in ChatGPT/Claude: the browser runs invisibly (headless) and has a persistent profile
(~/.alice/builtin-browser; sites you have logged in to once stay logged in). Both sides connect to the same browser:

* The agent drives the page through the browser tool (Playwright, connect_over_cdp).
* The app connects directly over the DevTools protocol and shows the screen live; the user can take over control
  at any moment by clicking on the image and typing. The image keeps streaming while the agent works too (it is not
  tied to Playwright's single thread).

The page's console errors and failed network requests are also collected, so ALICE can verify a UI change by
"run it, open it, look at the errors" (the preview tool).
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from alice.config import alice_home

VIEWPORT = (1280, 800)
LOG_MAX = 300
KEYS = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46, "ArrowLeft": 37, "ArrowUp": 38,
        "ArrowRight": 39, "ArrowDown": 40, "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34}


class BrowserError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_chromium() -> str | None:
    """The Chromium to use: ALICE_CHROMIUM, Edge/Chrome, and last the default browser (if it is Chromium-based).
    For invisible runs Edge and Chrome are the most reliable (derivatives like Opera GX can open windows/prompts at
    startup)."""
    from alice.tools.browser import default_browser_exe, is_chromium

    cands = [os.environ.get("ALICE_CHROMIUM", "")]
    if sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        cands += [str(Path(b) / rel) for rel in (r"Microsoft\Edge\Application\msedge.exe",
                                                 r"Google\Chrome\Application\chrome.exe") for b in pf if b]
    else:
        import shutil

        cands += [shutil.which(n) or "" for n in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")]
    cands.append(default_browser_exe() or "")
    for c in cands:
        if c and Path(c).is_file() and is_chromium(c):
            return c
    return None


class CDPPage:
    """DevTools connection to one tab: sends commands, collects console and network events."""

    def __init__(self, ws_url: str, enable: bool = True):
        from websockets.sync.client import connect

        self.ws = connect(ws_url, max_size=64 * 2**20, open_timeout=5)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._waiting: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.logs: deque[dict[str, Any]] = deque(maxlen=LOG_MAX)
        self._urls: dict[str, str] = {}
        self.closed = False
        threading.Thread(target=self._read, daemon=True, name="alice-cdp").start()
        for m in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable") if enable else ():
            try:
                self.send(m, timeout=5)
            except BrowserError:
                pass

    def _read(self) -> None:
        try:
            for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    slot = self._waiting.pop(msg["id"], None)
                    if slot is not None:
                        slot[1].update(msg)
                        slot[0].set()
                else:
                    self._event(msg.get("method", ""), msg.get("params") or {})
        except Exception:  # noqa: BLE001 - connection closed
            pass
        self.closed = True
        for ev, box in list(self._waiting.values()):
            box["error"] = {"message": "bağlantı kapandı"}
            ev.set()

    def _event(self, method: str, p: dict[str, Any]) -> None:
        now = time.time()
        if method == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning", "assert"):
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args") or [])
            self.logs.append({"t": now, "kind": "console", "level": p.get("type"), "text": text[:1000]})
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            text = (d.get("exception") or {}).get("description") or d.get("text") or "hata"
            self.logs.append({"t": now, "kind": "exception", "level": "error", "text": str(text)[:1500],
                              "url": d.get("url", ""), "line": d.get("lineNumber")})
        elif method == "Log.entryAdded":
            e = p.get("entry") or {}
            if e.get("level") in ("error", "warning"):
                self.logs.append({"t": now, "kind": "log", "level": e.get("level"), "text": str(e.get("text"))[:1000],
                                  "url": e.get("url", "")})
        elif method == "Network.requestWillBeSent":
            self._urls[p.get("requestId", "")] = (p.get("request") or {}).get("url", "")
            if len(self._urls) > 2000:
                self._urls.clear()
        elif method == "Network.responseReceived":
            r = p.get("response") or {}
            if int(r.get("status") or 0) >= 400:
                self.logs.append({"t": now, "kind": "network", "level": "error",
                                  "text": f"HTTP {r.get('status')} {r.get('url', '')}"[:600]})
        elif method == "Network.loadingFailed" and not p.get("canceled"):
            url = self._urls.get(p.get("requestId", ""), "")
            self.logs.append({"t": now, "kind": "network", "level": "error",
                              "text": f"{p.get('errorText', 'yüklenemedi')} {url}"[:600]})
        elif method == "Page.frameNavigated" and not (p.get("frame") or {}).get("parentId"):
            self.logs.append({"t": now, "kind": "nav", "level": "info", "text": (p.get("frame") or {}).get("url", "")})

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        if self.closed:
            raise BrowserError("sekme bağlantısı kapandı")
        mid = next(self._ids)
        ev, box = threading.Event(), {}
        self._waiting[mid] = (ev, box)
        with self._lock:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not ev.wait(timeout):
            self._waiting.pop(mid, None)
            raise BrowserError(f"{method} cevap vermedi")
        if "error" in box:
            raise BrowserError(str(box["error"].get("message") or box["error"]))
        return box.get("result") or {}

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class BuiltinBrowser:
    def __init__(self, profile: Path | None = None):
        self.profile = profile or alice_home() / "builtin-browser"
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self._pages: dict[str, CDPPage] = {}
        self.target = ""  # tab the user picked in the panel; empty: the most recently active one
        self._lock = threading.Lock()
        # Tab connections: the panel's frame requests, the input endpoint and the agent's tool modify them at once.
        # Separate from start()'s lock so it doesn't deadlock when page() calls start().
        self._pages_lock = threading.Lock()

    # --------------------------------------------------------------------------------------------- lifecycle
    @property
    def endpoint(self) -> str:
        self.start()
        return f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    no_sandbox = False  # if the renderer can't start in the sandbox (restricted environment), retry once without it

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            exe = find_chromium()
            if not exe:
                raise BrowserError("Chromium tabanlı tarayıcı bulunamadı (Edge, Chrome). ALICE_CHROMIUM ile yol ver.")
            self.profile.mkdir(parents=True, exist_ok=True)
            self._launch(exe)
            if not self._renders() and not self.no_sandbox:
                # The browser started but the page-rendering process isn't responding (in some restricted
                # environments the renderer sandbox can't start): restart once without the sandbox.
                self._kill()
                self.no_sandbox = True
                self._launch(exe)
            if not self._renders():
                self._kill()
                raise BrowserError("yerleşik tarayıcı açıldı ama sayfa çizemiyor")

    def _launch(self, exe: str) -> None:
        self.port = _free_port()
        # --remote-allow-origins is not passed: ALICE's CDP clients (websockets, Playwright) don't send an Origin and
        # connections without an Origin are accepted; "*" would have let any page opened in the browser connect to
        # this port and drive the browser.
        args = [exe, f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}", "--headless=new",
                f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,MediaRouter",
                *(["--no-sandbox", "--disable-gpu"] if self.no_sandbox else []),
                *os.environ.get("ALICE_BROWSER_FLAGS", "").split(), "about:blank"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                self._http("/json/version")
                return
            except OSError:
                time.sleep(0.2)
        self._kill()
        raise BrowserError("yerleşik tarayıcı açılmadı")

    def _renders(self) -> bool:
        """Is the page really working: can a small expression be evaluated on a blank page within 5 s."""
        try:
            tabs = [t for t in self._http("/json/list") if t.get("type") == "page"]
            if not tabs:
                return False
            conn = CDPPage(tabs[0]["webSocketDebuggerUrl"], enable=False)
            try:
                r = conn.send("Runtime.evaluate", {"expression": "1+1", "returnByValue": True}, timeout=5)
                return (r.get("result") or {}).get("value") == 2
            finally:
                conn.close()
        except (BrowserError, OSError, KeyError):
            return False
        except Exception:  # noqa: BLE001 - could not connect
            return False

    def _kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None

    def stop(self) -> None:
        with self._pages_lock:
            pages = list(self._pages.values())
            self._pages.clear()
        for p in pages:
            p.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    # ---------------------------------------------------------------------------------------------------- tabs
    def _http(self, path: str, method: str = "GET") -> Any:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310 - only our own local port
            body = r.read()
        return json.loads(body) if body.strip().startswith((b"{", b"[")) else {}

    def tabs(self) -> list[dict[str, Any]]:
        if not self.running():
            return []
        rows = [t for t in self._http("/json/list") if t.get("type") == "page"]
        return [{"id": t["id"], "title": t.get("title") or t.get("url", ""), "url": t.get("url", ""),
                 "ws": t.get("webSocketDebuggerUrl", "")} for t in rows]

    def current(self) -> dict[str, Any]:
        tabs = self.tabs()
        if not tabs:
            self._http("/json/new?about:blank", "PUT")
            tabs = self.tabs()
        for t in tabs:
            if t["id"] == self.target:
                return t
        return tabs[0]  # the most recently active one (the one the agent is working on)

    def page(self) -> tuple[CDPPage, dict[str, Any]]:
        self.start()
        tab = self.current()
        live = {t["id"] for t in self.tabs()}
        with self._pages_lock:
            conn = self._pages.get(tab["id"])
            if conn is None or conn.closed:
                conn = self._pages[tab["id"]] = CDPPage(tab["ws"])
            gone = [self._pages.pop(k) for k in list(self._pages) if k not in live and k != tab["id"]]
        for old in gone:
            old.close()
        return conn, tab

    def select(self, tab_id: str) -> None:
        self.target = tab_id
        try:
            self._http(f"/json/activate/{tab_id}")
        except OSError:
            pass

    def new_tab(self, url: str = "about:blank") -> None:
        from urllib.parse import quote

        t = self._http(f"/json/new?{quote(_normalize(url), safe=':/?&=#%')}", "PUT")
        self.target = t.get("id", "")

    def close_tab(self, tab_id: str) -> None:
        self._http(f"/json/close/{tab_id}")
        if self.target == tab_id:
            self.target = ""

    # ------------------------------------------------------------------------------------------- frame/input
    def frame(self, quality: int = 60) -> dict[str, Any]:
        conn, tab = self.page()
        shot = conn.send("Page.captureScreenshot", {"format": "jpeg", "quality": quality}, timeout=10)
        m = conn.send("Page.getLayoutMetrics")
        vp = m.get("cssVisualViewport") or m.get("cssLayoutViewport") or {}
        return {"ok": True, "b64": shot.get("data", ""), "url": tab["url"], "title": tab["title"], "tab": tab["id"],
                "w": int(vp.get("clientWidth") or VIEWPORT[0]), "h": int(vp.get("clientHeight") or VIEWPORT[1])}

    def screenshot_png(self) -> str:
        conn, _ = self.page()
        return conn.send("Page.captureScreenshot", {"format": "png"}, timeout=15).get("data", "")

    def click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        conn, _ = self.page()
        base = {"x": x, "y": y, "button": button, "clickCount": count}
        conn.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        conn.send("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
        conn.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})

    def scroll(self, x: float, y: float, dy: float) -> None:
        conn, _ = self.page()
        conn.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy})

    def type_text(self, text: str) -> None:
        conn, _ = self.page()
        conn.send("Input.insertText", {"text": text})

    def key(self, name: str, modifiers: int = 0) -> None:
        conn, _ = self.page()
        code = KEYS.get(name)
        if code is None:
            raise BrowserError(f"bilinmeyen tuş: {name}")
        down = {"type": "rawKeyDown", "key": name, "code": name, "windowsVirtualKeyCode": code, "modifiers": modifiers}
        if name == "Enter":
            down = {**down, "type": "keyDown", "text": "\r"}
        conn.send("Input.dispatchKeyEvent", down)
        conn.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": name, "code": name,
                                             "windowsVirtualKeyCode": code, "modifiers": modifiers})

    def navigate(self, url: str) -> str:
        conn, _ = self.page()
        url = _normalize(url)
        conn.send("Page.navigate", {"url": url}, timeout=30)
        return url

    def history(self, step: str) -> None:
        conn, _ = self.page()
        if step == "reload":
            conn.send("Page.reload")
        else:
            conn.send("Runtime.evaluate", {"expression": "history.back()" if step == "back" else "history.forward()"})

    def wait_load(self, timeout: float = 15.0) -> bool:
        conn, _ = self.page()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                state = conn.send("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
                if (state.get("result") or {}).get("value") == "complete":
                    return True
            except BrowserError:
                pass
            time.sleep(0.3)
        return False

    def logs(self, since: float = 0.0, errors_only: bool = True) -> list[dict[str, Any]]:
        conn, _ = self.page()
        return [r for r in list(conn.logs) if r["t"] >= since and (not errors_only or r["level"] in ("error", "warning"))]


def _normalize(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "about:blank"
    if "://" in url or url.startswith(("about:", "data:", "file:")):
        return url
    if url.startswith(("localhost", "127.0.0.1")) or (":" in url.split("/")[0] and "." not in url.split(":")[0]):
        return "http://" + url
    if " " in url or "." not in url:
        from urllib.parse import quote_plus

        return "https://www.google.com/search?q=" + quote_plus(url)
    return "https://" + url


def b64_size(b64: str) -> int:
    return len(base64.b64decode(b64)) if b64 else 0
