"""Render a page in a real, hard-to-detect Chromium when a plain HTTP fetch is blocked or comes back empty.

What crawl4ai gets from patchright + playwright-stealth, reimplemented on raw DevTools (no extra packages, the same
Edge/Chrome ALICE already uses):

patchright (driver-level leaks)
* No `Runtime.enable` / `Console.enable`: anti-bot scripts detect the DevTools runtime domain being on (the "console
  serialization" trick). Page scripts are run through `Page.createIsolatedWorld` instead, an isolated execution
  context the page cannot see.
* Launch flags: no `--enable-automation`; `--disable-blink-features=AutomationControlled` so `navigator.webdriver`
  is false at the engine level; popups, extensions and component updates are left at their defaults (removing them
  is itself a fingerprint).
* Headless gives itself away in the user agent ("HeadlessChrome"): the real UA is read from the browser and sent
  without that word, both as the User-Agent header and in `navigator.userAgentData` / Sec-CH-UA brands.

playwright-stealth (page-level evasions, injected before any page script runs)
* navigator.webdriver, plugins, mimeTypes, languages, vendor, platform, hardwareConcurrency, deviceMemory,
  permissions.query (notifications), window.chrome (app, csi, loadTimes, runtime), outer window size, WebGL
  vendor/renderer, media codecs, iframe.contentWindow, and Function.prototype.toString so the patched getters look
  native.

Behaviour (crawl4ai's "simulate_user" / "magic")
* Wait for network quiet instead of a fixed sleep, scroll the page in steps so lazy content loads, wait out a
  Cloudflare/JS challenge (it usually clears itself in a real browser within a few seconds), then read the DOM.

The renderer gets its own profile and process, so it never touches the user's panel browser or their logins.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from alice.config import alice_home

VIEWPORT = (1366, 900)
_lock = threading.Lock()
# One page at a time in the renderer (web_crawl fetches in parallel): pages share a process and the CDP target
# list, and a stealth browser opening many tabs at once is itself a bot signal.
_render_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_port = 0
_ua = ""
_last_used = 0.0
IDLE_SHUTDOWN = 300.0  # seconds: the renderer closes itself after this long without a page


class RenderError(Exception):
    pass


# --------------------------------------------------------------------------------------------- evasions
# One script, run in every frame before the page's own scripts (Page.addScriptToEvaluateOnNewDocument).
EVASIONS = r"""
(() => {
  const native = new WeakMap();
  const nativeToString = Function.prototype.toString;
  const toStr = function toString() {
    if (native.has(this)) return native.get(this);
    return nativeToString.call(this);
  };
  native.set(toStr, 'function toString() { [native code] }');
  Object.defineProperty(Function.prototype, 'toString', {value: toStr, configurable: true, writable: true});
  const mark = (fn, name) => { native.set(fn, `function ${name}() { [native code] }`); return fn; };
  const getter = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, {get: mark(() => value, 'get ' + prop), configurable: true, enumerable: true});
    } catch (e) {}
  };

  // navigator.webdriver: false and absent from the prototype like a normal browser.
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
  getter(Navigator.prototype, 'webdriver', false);

  getter(Navigator.prototype, 'languages', __LANGS__);
  getter(Navigator.prototype, 'vendor', 'Google Inc.');
  getter(Navigator.prototype, 'platform', __PLATFORM__);
  getter(Navigator.prototype, 'hardwareConcurrency', 8);
  getter(Navigator.prototype, 'deviceMemory', 8);
  getter(Navigator.prototype, 'maxTouchPoints', 0);

  // plugins / mimeTypes: headless has none, every desktop Chrome has the PDF viewer entries.
  try {
    const mime = {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'};
    const names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer', 'Microsoft Edge PDF Viewer',
                   'WebKit built-in PDF'];
    const plugins = names.map(name => {
      const p = Object.create(Plugin.prototype);
      Object.defineProperties(p, {name: {value: name}, filename: {value: 'internal-pdf-viewer'},
        description: {value: 'Portable Document Format'}, length: {value: 1}, 0: {value: mime}});
      return p;
    });
    const arr = Object.create(PluginArray.prototype);
    plugins.forEach((p, i) => Object.defineProperty(arr, i, {value: p, enumerable: true}));
    Object.defineProperties(arr, {length: {value: plugins.length},
      item: {value: mark(i => plugins[i] || null, 'item')},
      namedItem: {value: mark(n => plugins.find(p => p.name === n) || null, 'namedItem')},
      refresh: {value: mark(() => undefined, 'refresh')}});
    getter(Navigator.prototype, 'plugins', arr);
    const mimes = Object.create(MimeTypeArray.prototype);
    Object.defineProperties(mimes, {0: {value: mime, enumerable: true}, length: {value: 1},
      item: {value: mark(i => i === 0 ? mime : null, 'item')},
      namedItem: {value: mark(n => n === mime.type ? mime : null, 'namedItem')}});
    getter(Navigator.prototype, 'mimeTypes', mimes);
    getter(Navigator.prototype, 'pdfViewerEnabled', true);
  } catch (e) {}

  // Notification permission: headless answers "denied" from permissions.query while Notification says "default".
  try {
    const q = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = mark(p => p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission, onchange: null}) : q(p), 'query');
  } catch (e) {}

  // window.chrome: present in every desktop Chrome, missing in headless.
  try {
    if (!window.chrome) Object.defineProperty(window, 'chrome', {value: {}, configurable: true, writable: true});
    const c = window.chrome;
    c.app = c.app || {isInstalled: false, InstallState: {DISABLED: 'disabled', INSTALLED: 'installed',
      NOT_INSTALLED: 'not_installed'}, RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run',
      RUNNING: 'running'}, getDetails: mark(() => null, 'getDetails'), getIsInstalled: mark(() => false, 'getIsInstalled')};
    c.runtime = c.runtime || {OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {}, PlatformOs: {},
      RequestUpdateCheckStatus: {}, connect: mark(() => {}, 'connect'), sendMessage: mark(() => {}, 'sendMessage')};
    const t0 = performance.timeOrigin || Date.now();
    c.csi = c.csi || mark(() => ({onloadT: Date.now(), startE: t0, pageT: performance.now(), tran: 15}), 'csi');
    c.loadTimes = c.loadTimes || mark(() => ({requestTime: t0 / 1000, startLoadTime: t0 / 1000,
      commitLoadTime: t0 / 1000 + 0.1, finishDocumentLoadTime: t0 / 1000 + 0.3, finishLoadTime: t0 / 1000 + 0.4,
      firstPaintTime: t0 / 1000 + 0.2, navigationType: 'Other', wasFetchedViaSpdy: true, wasNpnNegotiated: true,
      npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false, connectionInfo: 'h2'}), 'loadTimes');
  } catch (e) {}

  // Window chrome size: headless reports outer == inner (no toolbars).
  try {
    if (window.outerWidth === 0 || window.outerWidth === window.innerWidth) {
      getter(window, 'outerWidth', window.innerWidth + 16);
      getter(window, 'outerHeight', window.innerHeight + 88);
    }
  } catch (e) {}

  // WebGL: headless shows SwiftShader/"Google Inc." as the unmasked renderer.
  try {
    const patch = proto => {
      const gp = proto.getParameter;
      proto.getParameter = mark(function (p) {
        if (p === 37445) return 'Google Inc. (Intel)';
        if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return gp.call(this, p);
      }, 'getParameter');
    };
    patch(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  } catch (e) {}

  // Media codecs that headless Chromium builds report as unsupported.
  try {
    const cpt = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = mark(function (t) {
      if (/mp4a\.40|avc1|mp4/.test(t || '')) return 'probably';
      return cpt.call(this, t);
    }, 'canPlayType');
  } catch (e) {}

  // iframe.contentWindow of srcdoc frames must look like a real window.
  try {
    const desc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {configurable: true, enumerable: true,
      get: mark(function () { const w = desc.get.call(this); return w || window; }, 'get contentWindow')});
  } catch (e) {}
})();
"""

# Text extraction run in the isolated world (patchright: the page never sees our evaluation).
READ_JS = "(() => ({html: document.documentElement ? document.documentElement.outerHTML : '', " \
          "title: document.title, url: location.href, text: (document.body && document.body.innerText || '').length}))()"
SCROLL_JS = "(() => { const h = Math.max(document.body ? document.body.scrollHeight : 0, " \
            "document.documentElement.scrollHeight); window.scrollBy(0, Math.round(innerHeight * 0.85)); " \
            "return [scrollY + innerHeight, h]; })()"
CHALLENGE = re.compile(r"Just a moment|Checking your browser|cf-browser-verification|challenge-platform|"
                       r"Attention Required|Please wait while we verify|DDoS protection by|px-captcha|"
                       r"captcha-delivery|Pardon Our Interruption", re.I)


# ----------------------------------------------------------------------------------------------- browser
def _launch() -> None:
    global _proc, _port, _ua
    from alice.app.builtin_browser import _free_port, find_chromium

    exe = find_chromium()
    if not exe:
        raise RenderError("no Chromium-based browser found (Edge/Chrome/Chromium)")
    profile = alice_home() / "render-profile"
    profile.mkdir(parents=True, exist_ok=True)
    _port = _free_port()
    args = [exe, f"--remote-debugging-port={_port}", f"--user-data-dir={profile}", "--headless=new",
            f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}", "--no-first-run", "--no-default-browser-check",
            # patchright: hide the automation switch at the engine level; never pass --enable-automation.
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,MediaRouter,OptimizationHints", "--mute-audio",
            "--password-store=basic", "--use-mock-keychain", "about:blank"]
    if os.environ.get("ALICE_RENDER_NO_SANDBOX"):
        args[1:1] = ["--no-sandbox", "--disable-gpu"]
    _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 15
    info: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            info = _http("/json/version")
            break
        except OSError:
            time.sleep(0.2)
    else:
        _shutdown()
        raise RenderError("the hidden browser did not open")
    # patchright: the real UA minus "HeadlessChrome" (a dead giveaway on every request header).
    _ua = (info.get("User-Agent") or "").replace("HeadlessChrome", "Chrome")
    threading.Thread(target=_idle_watch, daemon=True, name="alice-render-idle").start()


def _http(path: str, method: str = "GET") -> Any:
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{_port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - our own local port
        body = r.read()
    return json.loads(body) if body.strip().startswith((b"{", b"[")) else {}


def _running() -> bool:
    return _proc is not None and _proc.poll() is None


def _shutdown() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(5)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None


def _idle_watch() -> None:
    while _running():
        time.sleep(30)
        with _lock:
            if _running() and time.time() - _last_used > IDLE_SHUTDOWN:
                _shutdown()
                return


class _Tab:
    """A fresh target with only the domains a normal page load needs; Runtime is never enabled."""

    def __init__(self) -> None:
        from websockets.sync.client import connect

        target = _http("/json/new?about:blank", "PUT")
        self.id = target["id"]
        self.ws = connect(target["webSocketDebuggerUrl"], max_size=64 * 2**20, open_timeout=5)
        self._n = 0
        self.events: list[dict[str, Any]] = []
        self.inflight: set[str] = set()
        self.last_net = time.monotonic()
        self.status = 0
        self.doc_id = ""
        self.main_frame = ""

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 20) -> dict[str, Any]:
        self._n += 1
        mid = self._n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise RenderError(f"{method} cevap vermedi")
            try:
                raw = self.ws.recv(timeout=left)
            except TimeoutError as e:
                raise RenderError(f"{method} cevap vermedi") from e
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RenderError(str(msg["error"].get("message")))
                return msg.get("result") or {}
            self._event(msg)

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                self._event(json.loads(self.ws.recv(timeout=left)))
            except TimeoutError:
                return

    def _event(self, msg: dict[str, Any]) -> None:
        m, p = msg.get("method", ""), msg.get("params") or {}
        if m == "Network.requestWillBeSent":
            self.inflight.add(p.get("requestId", ""))
            self.last_net = time.monotonic()
            # Track the LATEST main document: after a Cloudflare challenge the page reloads itself, and the first
            # response (403) is not what we end up reading.
            if p.get("type") == "Document" and p.get("frameId") == self.main_frame:
                self.doc_id = p.get("requestId", "")
        elif m in ("Network.loadingFinished", "Network.loadingFailed"):
            self.inflight.discard(p.get("requestId", ""))
            self.last_net = time.monotonic()
        elif m == "Network.responseReceived" and p.get("requestId") == self.doc_id:
            self.status = int((p.get("response") or {}).get("status") or 0)

    def evaluate(self, expression: str, world: int) -> Any:
        r = self.send("Runtime.evaluate", {"expression": expression, "contextId": world, "returnByValue": True,
                                           "awaitPromise": True}, timeout=20)
        if r.get("exceptionDetails"):
            raise RenderError("the page script failed")
        return (r.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            try:
                _http(f"/json/close/{self.id}")
            except OSError:
                # Tab or renderer process may already be terminated; closing is best-effort.
                pass


def _platform(ua: str) -> str:
    if "Windows" in ua:
        return "Win32"
    if "Mac OS" in ua:
        return "MacIntel"
    return "Linux x86_64"


def render(url: str, timeout: float = 35.0, scroll: bool = True) -> dict[str, Any]:
    """Load url in the stealth renderer and return {status, url, title, html}. Raises RenderError."""
    with _render_lock:
        return _render(url, timeout, scroll)


def _render(url: str, timeout: float, scroll: bool) -> dict[str, Any]:
    global _last_used
    with _lock:
        if not _running():
            _launch()
        _last_used = time.time()
        tab = _Tab()
    try:
        major = (re.search(r"Chrome/(\d+)", _ua) or [None, "131"])[1]
        brand = "Microsoft Edge" if "Edg/" in _ua else "Google Chrome"
        meta = {"brands": [{"brand": "Chromium", "version": major}, {"brand": brand, "version": major},
                           {"brand": "Not.A/Brand", "version": "99"}],
                "fullVersionList": [], "platform": {"Win32": "Windows", "MacIntel": "macOS"}.get(_platform(_ua), "Linux"),
                "platformVersion": "", "architecture": "x86", "model": "", "mobile": False}
        langs = ["tr-TR", "tr", "en-US", "en"]
        tab.send("Network.enable")
        tab.send("Network.setUserAgentOverride", {"userAgent": _ua, "acceptLanguage": ",".join(langs),
                                                  "platform": _platform(_ua), "userAgentMetadata": meta})
        tab.send("Page.enable")
        script = EVASIONS.replace("__LANGS__", json.dumps(langs)).replace("__PLATFORM__", json.dumps(_platform(_ua)))
        tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        tab.send("Emulation.setDeviceMetricsOverride", {"width": VIEWPORT[0], "height": VIEWPORT[1],
                                                        "deviceScaleFactor": 1, "mobile": False})
        frame = tab.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        tab.main_frame = frame
        tab.send("Page.navigate", {"url": url}, timeout=timeout)
        end = time.monotonic() + timeout
        _settle(tab, end)
        world = tab.send("Page.createIsolatedWorld", {"frameId": frame, "worldName": "alice",
                                                      "grantUniveralAccess": False})["executionContextId"]
        # A JS challenge (Cloudflare "Just a moment") usually clears itself in a real browser; give it time.
        for _ in range(4):
            data = tab.evaluate(READ_JS, world) or {}
            if not CHALLENGE.search((data.get("title") or "") + (data.get("html") or "")[:6000]):
                break
            if time.monotonic() > end:
                break
            tab.pump(4)
            _settle(tab, end)
            frame = tab.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
            world = tab.send("Page.createIsolatedWorld", {"frameId": frame, "worldName": "alice",
                                                          "grantUniveralAccess": False})["executionContextId"]
        if scroll:
            # Lazy-loaded feeds and "load more on scroll" lists: scroll in steps like a reader would.
            for _ in range(8):
                if time.monotonic() > end:
                    break
                pos = tab.evaluate(SCROLL_JS, world) or [0, 0]
                tab.pump(0.4)
                if pos[0] >= pos[1] - 5:
                    break
            _settle(tab, min(end, time.monotonic() + 3))
        data = tab.evaluate(READ_JS, world) or {}
        html = data.get("html") or ""
        status = tab.status or 200
        # A challenge page that cleared itself in place (no new document request) keeps the first 403 status;
        # if the content is now real, report it as such.
        if status in (403, 503) and not CHALLENGE.search((data.get("title") or "") + html[:6000]) \
                and int(data.get("text") or 0) > 500:
            status = 200
        return {"status": status, "url": data.get("url") or url, "title": data.get("title") or "", "html": html}
    finally:
        tab.close()
        with _lock:
            _last_used = time.time()


def _settle(tab: _Tab, end: float, quiet: float = 0.8) -> None:
    """Wait until no request has been in flight for `quiet` seconds (crawl4ai's networkidle), or until end."""
    tab.pump(0.5)
    while time.monotonic() < end:
        if not tab.inflight and time.monotonic() - tab.last_net > quiet:
            return
        if time.monotonic() - tab.last_net > 6:  # long-poll or websocket keeping one request open forever
            return
        tab.pump(0.25)


def available() -> bool:
    try:
        from alice.app.builtin_browser import find_chromium

        return bool(find_chromium())
    except Exception:  # noqa: BLE001
        return False


def shutdown() -> None:
    with _lock:
        _shutdown()


def profile_dir() -> Path:
    return alice_home() / "render-profile"
