"""Browser control with Playwright.

The profile in ~/.alice/browser-profile is persistent, so sites you logged into once stay open in later sessions.
The page is converted into a list of interactive elements with IDs (DOM snapshot) instead of pixels; both the
brain and the reflex loop use it.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from alice.config import alice_home
from alice.tools.base import COMPUTER, NETWORK, READ, Tool, ToolContext, ToolError, ToolResult, obj, truncate

SNAPSHOT_JS = r"""
(limit) => {
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],' +
              '[role=tab],[role=menuitem],[role=checkbox],[role=option],[role=combobox],[role=textbox],' +
              '[role=searchbox],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"]),[draggable=true],[ondrop]';
  document.querySelectorAll('[data-alice-id]').forEach(e => e.removeAttribute('data-alice-id'));
  const out = [];
  const vh = window.innerHeight, vw = window.innerWidth;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (r.bottom < 0 || r.top > vh * 2.5 || r.right < 0 || r.left > vw) continue;
    const tag = el.tagName.toLowerCase();
    let text = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                el.title || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ');
    if (!text && tag === 'a') text = el.getAttribute('href') || '';
    if (!text && tag !== 'input' && tag !== 'textarea' && tag !== 'select') continue;
    const id = out.length + 1;
    el.setAttribute('data-alice-id', String(id));
    out.push({
      id, tag, role: el.getAttribute('role') || '', type: el.type || '',
      text: text.slice(0, 90),
      placeholder: (el.placeholder || '').slice(0, 40),
      value: (tag === 'input' || tag === 'textarea') && el.type !== 'password' ? (el.value || '').slice(0, 60) : '',
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 80) : '',
      inView: r.top >= 0 && r.bottom <= vh,
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""

TEXT_JS = "() => document.body ? document.body.innerText : ''"

# Element box within the page + window position on screen, for framing on the HUD (screen_box maps it to screen).
PLACE_JS = """el => { const r = el.getBoundingClientRect(); return {
  left: r.left, top: r.top, width: r.width, height: r.height, sx: window.screenX, sy: window.screenY,
  ow: window.outerWidth, oh: window.outerHeight, iw: window.innerWidth, ih: window.innerHeight,
  dpr: window.devicePixelRatio, visible: document.visibilityState === 'visible' }; }"""
# Page-level outline of the shown element (highlight when the HUD is off); restored to its old style after 3 s.
OUTLINE_JS = """el => { const o = el.style.outline, f = el.style.outlineOffset;
  el.style.outline = '3px solid #ffb000'; el.style.outlineOffset = '2px';
  setTimeout(() => { el.style.outline = o; el.style.outlineOffset = f; }, 3000); }"""
MAX_TOOLBAR = 400  # CSS px: if the tab+address bar is taller, the measurement is nonsense (zoom, fullscreen game)
PAD = 4  # CSS px: keep the outline from touching the element's edge


def screen_box(info: Any) -> tuple[int, int, int, int] | None:
    """Convert PLACE_JS output to physical screen pixels (x, y, w, h); return None if it cannot be computed.

    In Chromium the window measurements (screenX/Y, outer*/inner*) and the element box come in the same CSS pixels;
    multiplying by devicePixelRatio gives physical pixels. The page's top-left on screen is the window's top-left +
    side border + the tab/address bar above it (outerHeight - innerHeight, minus the bottom border). If the tab is in
    the background, or the element is outside the visible area, there is nothing to draw."""
    if not isinstance(info, dict) or not info.get("visible"):
        return None
    try:
        v = {k: float(info[k]) for k in ("left", "top", "width", "height", "sx", "sy", "ow", "oh", "iw", "ih", "dpr")}
    except (KeyError, TypeError, ValueError):
        return None
    if any(x != x or x in (float("inf"), float("-inf")) for x in v.values()) or v["dpr"] <= 0:
        return None
    if v["iw"] <= 0 or v["ih"] <= 0 or v["ow"] < v["iw"] or v["oh"] < v["ih"]:
        return None
    side = (v["ow"] - v["iw"]) / 2
    toolbar = v["oh"] - v["ih"] - side
    if not 0 <= toolbar <= MAX_TOOLBAR:
        return None
    # Clip to the visible area: for an element half below the fold, only its visible part is framed.
    left, top = max(0.0, v["left"] - PAD), max(0.0, v["top"] - PAD)
    right, bottom = min(v["iw"], v["left"] + v["width"] + PAD), min(v["ih"], v["top"] + v["height"] + PAD)
    if right - left < 2 or bottom - top < 2:
        return None
    ox, oy, k = v["sx"] + side, v["sy"] + toolbar, v["dpr"]
    return (round((ox + left) * k), round((oy + top) * k), round((right - left) * k), round((bottom - top) * k))


def mark_on_hud(b: Any, loc: Any, label: str, ttl: float | None = None, wait: bool = False) -> bool:
    """Frame the element on the HUD. If the HUD is off, the browser is invisible (headless), or the position cannot be
    computed, it does nothing; the browser action is never affected by this."""
    from alice.hud import client, views

    if not client.active or getattr(b, "headless", False):
        return False
    try:
        box = screen_box(loc.evaluate(PLACE_JS, timeout=2000))
    except Exception:  # noqa: BLE001 - decorative frame: if it cannot be measured, skip silently
        return False
    if box is None:
        return False
    return views.page_target(*box, label=label, ttl=views.PAGE_TTL if ttl is None else ttl, wait=wait)

# Start of a function: "function", "async function", "(a, b) =>", "x =>", "async () =>". Expressions like
# "(() => 1)()" (immediately invoked) or ones that merely contain an arrow function don't match this; they get wrapped.
_JS_FN_HEAD = re.compile(r"^(?:async\b\s*)?(?:function\b|(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>)")


def js_function(js: str) -> str:
    """Turn model-written JS into a callable function: keep it if it is one already, else wrap it as an expression."""
    body = js.strip()
    if _JS_FN_HEAD.match(body):
        return body
    return f"() => ({body.rstrip(';').rstrip()})"


CHROMIUM_FAMILY = ("opera", "chrome", "msedge", "brave", "vivaldi", "chromium", "yandex", "arc")


def default_browser_exe() -> str | None:
    """Exe path of the default browser on Windows (e.g. Opera GX). Returns None if not found."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    return exe_from_command(cmd)


def exe_from_command(cmd: str) -> str | None:
    m = re.match(r'\s*"([^"]+\.exe)"', cmd, re.I) or re.match(r"\s*(\S+\.exe)", cmd, re.I)
    return m.group(1) if m else None


def is_chromium(exe: str) -> bool:
    name = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return any(n in name for n in CHROMIUM_FAMILY)


def cdp_alive(endpoint: str, timeout: float = 0.4) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local port, do not go through the proxy
        with opener.open(endpoint.rstrip("/") + "/json/version", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


# When the browser's "Allow remote debugging for this browser instance" setting (opera://inspect, chrome://inspect)
# is on, it writes the port and path to the DevToolsActivePort file in the user data folder.
USER_DATA_DIRS = [
    ("APPDATA", r"Opera Software\Opera GX Stable"), ("APPDATA", r"Opera Software\Opera Stable"),
    ("LOCALAPPDATA", r"Google\Chrome\User Data"), ("LOCALAPPDATA", r"Microsoft\Edge\User Data"),
    ("LOCALAPPDATA", r"BraveSoftware\Brave-Browser\User Data"), ("LOCALAPPDATA", r"Vivaldi\User Data"),
]
BLANK_URLS = ("about:blank", "chrome://newtab", "opera://startpage", "chrome://new-tab-page", "edge://newtab",
              "opera://speeddial", "chrome-search://")


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def devtools_active_endpoint(dirs: list[Path] | None = None) -> str | None:
    """ws:// address if remote debugging is enabled in the user's open browser."""
    if dirs is None:
        dirs = [Path(os.environ[env]) / sub for env, sub in USER_DATA_DIRS if os.environ.get(env)]
    for d in dirs:
        f = d / "DevToolsActivePort"
        try:
            lines = f.read_text(encoding="utf-8").split()
            port = int(lines[0])
        except (OSError, ValueError, IndexError):
            continue
        if _port_open(port):
            path = lines[1] if len(lines) > 1 else ""
            return f"ws://127.0.0.1:{port}{path}" if path else f"http://127.0.0.1:{port}"
    return None


def bridge_alive(port: int, timeout: float = 0.4) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/alice-bridge", timeout=timeout) as r:
            return r.read().startswith(b"alice-bridge")
    except Exception:  # noqa: BLE001
        return False


def ensure_bridge(port: int, wait: float = 8.0) -> bool:
    """If the browser bridge is not running, start it as a process independent of the core (detached)."""
    if bridge_alive(port):
        return True
    import subprocess
    import time

    exe = Path(sys.executable)
    if sys.platform == "win32" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"  # do not open a console window
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen([str(exe), "-m", "alice.tools.browser_bridge", "--port", str(port)], creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=sys.platform != "win32")
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if bridge_alive(port):
            return True
        time.sleep(0.2)
    return False


def user_browser_endpoint(cdp: str | None, bridge_port: int | None = None) -> str | None:
    if cdp and cdp_alive(cdp):
        return cdp
    found = devtools_active_endpoint()
    if found and bridge_port:
        # Through the bridge, not a direct connection: the browser won't ask permission again if the core restarts.
        from alice.tools.browser_bridge import endpoint

        if ensure_bridge(bridge_port):
            return endpoint(bridge_port)
    return found


def host_of(url: str) -> str:
    h = urlparse(url if "://" in url else "https://" + url).hostname or ""
    return h[4:] if h.startswith("www.") else h


NO_SEPARATE = ("Kullanıcının açık tarayıcısına bağlanılamıyor ve ayrı pencere açılmıyor (kullanıcı istemiyor). "
               "Bunun yerine kullanıcının kendi tarayıcısında çalış: windows action=switch_tab (açık sekmeye geç), "
               "windows action=navigate (sekmede adres/arama), ui_elements + ui_act (sayfada tıklama/yazma). "
               "Ayrı pencereyi sadece kullanıcı açıkça isterse separate=true ile aç.")


class BrowserSession:
    """First connects to the user's open browser (if its remote debugging port is on); otherwise launches the default
    browser (if Chromium-based, e.g. Opera GX) with the ALICE profile."""

    def __init__(self, headless: bool = False, executable: str | None = None, cdp: str | None = None,
                 allow_launch: bool = True, bridge_port: int | None = None):
        endpoint = user_browser_endpoint(cdp, bridge_port)
        if not allow_launch and not endpoint:
            raise ToolError(NO_SEPARATE)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ToolError("playwright kurulu değil: pip install 'alice-harness[browser]' && playwright install chromium") from e
        self._pw = sync_playwright().start()
        _register_exit_cleanup()
        self.driver_pid = _driver_pid(self._pw)
        self.aborted = ""  # "timeout" | "stop": driver killed, this session can no longer be used
        self.headless = bool(headless and not endpoint)  # not visible on screen: nothing to frame on the HUD
        self._bridge_port = None
        if bridge_port and endpoint:
            from alice.tools.browser_bridge import endpoint as bridge_endpoint

            if endpoint == bridge_endpoint(bridge_port):
                self._bridge_port = bridge_port
        try:
            self._start(endpoint, headless, executable)
        except BaseException:
            # Connection/launch failed: if the Playwright driver stays open, the next attempt fails too.
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            raise

    def alive(self) -> bool:
        """Is the browser connection still working? (Opera restarted, bridge closed, ALICE window closed)."""
        if getattr(self, "aborted", ""):
            return False
        port = getattr(self, "_bridge_port", None)
        if port and not bridge_alive(port):
            # When the bridge gives up on the browser and closes, Playwright does not notice (is_connected stays True),
            # so the next call went to the dead socket and waited forever.
            return False
        try:
            if self._browser is not None:
                return bool(self._browser.is_connected())
            # ALICE's own window: the context closes when the user closes it. It used to return True, and every next
            # call failed with "Target page, context or browser has been closed" and never reopened.
            return not getattr(self, "_ctx_closed", False)
        except Exception:  # noqa: BLE001
            return False

    def abort(self, reason: str) -> None:
        """Cut a stuck call: kill the Playwright driver, so every pending call returns at once with "Connection closed".
        Can be called from another thread (watchdog, Stop button); does not touch the user's browser."""
        if getattr(self, "aborted", ""):
            return
        self.aborted = reason
        pid = getattr(self, "driver_pid", None)
        if not pid:
            return
        try:
            import psutil

            psutil.Process(pid).kill()
        except Exception:  # noqa: BLE001 - the process may already have exited
            pass

    def _mark_closed(self, *_: Any) -> None:
        self._ctx_closed = True

    def ensure_page(self):
        """If the active tab was closed (by the user, or the site closed itself), switch to an open tab, or to a new
        one if there is none. Otherwise every action failed on a closed page with a "Target closed" error."""
        page = getattr(self, "page", None)
        if page is not None and not _is_closed(page):
            return page
        open_pages = [p for p in self.ctx.pages if not _is_closed(p)]
        self.page = open_pages[-1] if open_pages else self.ctx.new_page()
        return self.page

    def _start(self, endpoint, headless: bool, executable: str | None) -> None:
        self.attached = False
        self._browser = None
        self._cdp = None
        if endpoint:
            # The browser may ask the user once for permission to connect: give it time.
            headers = None
            if getattr(self, "_bridge_port", None):
                from alice.tools.browser_bridge import TOKEN_HEADER, read_token

                token = read_token()  # an older version wrote no bridge key, so no header (that bridge does not want one)
                headers = {TOKEN_HEADER: token} if token else None
            browser = self._pw.chromium.connect_over_cdp(endpoint, timeout=60000, headers=headers)
            self._browser = browser
            self.ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            self.attached = True
            self.page = self._visible_page() or (self.ctx.pages[-1] if self.ctx.pages else self.ctx.new_page())
            self.ctx.on("page", self._on_page)
            return
        profile = alice_home() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"headless": headless, "viewport": None if not headless else {"width": 1366, "height": 860},
                                  "args": ["--start-maximized"]}
        exe = executable or default_browser_exe()
        self.ctx = None
        if exe and (executable or is_chromium(exe)):
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(str(profile), executable_path=exe, **kwargs)
            except Exception:  # noqa: BLE001
                if executable:
                    raise
        if self.ctx is None:
            self.ctx = self._launch(profile, kwargs)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.ctx.on("page", self._on_page)
        self.ctx.on("close", self._mark_closed)

    def _launch(self, profile, kwargs):
        try:
            # If Chrome/Edge is installed on the system, use it (no separate download needed).
            return self._pw.chromium.launch_persistent_context(str(profile), channel="chrome", **kwargs)
        except Exception:  # noqa: BLE001
            try:
                return self._pw.chromium.launch_persistent_context(str(profile), channel="msedge", **kwargs)
            except Exception:  # noqa: BLE001
                return self._pw.chromium.launch_persistent_context(str(profile), **kwargs)

    def targets(self) -> list[dict[str, Any]]:
        """Tab titles/addresses come from the browser itself (without asking the page: background-suspended tabs
        never answer in-page commands, and ALICE used to freeze)."""
        if self._browser is None:
            return []
        try:
            if self._cdp is None:
                self._cdp = self._browser.new_browser_cdp_session()
            infos = self._cdp.send("Target.getTargets")["targetInfos"]
        except Exception:  # noqa: BLE001
            return []
        return [t for t in infos if t.get("type") == "page"]

    def title_of(self, page) -> str:
        if not self.attached:
            try:
                return page.title()
            except Exception:  # noqa: BLE001
                return ""
        for t in self.targets():
            if t.get("url") == page.url:
                return t.get("title", "")
        return ""

    def _visible_page(self):
        """The tab the user is looking at now: the tab whose title matches the foreground browser window."""
        try:
            from alice.tools.desktop import active_title

            fg = re.sub(r"\s+-\s+[^-]+$", "", active_title()).strip()
        except Exception:  # noqa: BLE001
            fg = ""
        if not fg:
            return None
        urls = [t.get("url") for t in self.targets() if t.get("title", "").strip() == fg]
        for p in self.ctx.pages:
            if p.url in urls:
                return p
        return None

    def evaluate(self, js: str, arg: Any = None, timeout: float = 10000) -> Any:
        """Run JS on the page, but with a time limit (page.evaluate waits forever)."""
        page = self.page
        if self.attached:
            try:
                page.bring_to_front()  # wake the suspended tab
            except Exception:  # noqa: BLE001
                pass
        fn = js_function(js)
        try:
            return page.locator("html").evaluate(f"(_el, arg) => ({fn})(arg)", arg, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            if "Timeout" in type(e).__name__ or "timeout" in str(e).lower():
                raise ToolError(f"sayfa {timeout / 1000:.0f} sn içinde cevap vermedi (sekme donmuş/uyutulmuş olabilir)") from e
            raise

    def tab_for(self, url: str):
        """Tab for goto in the user's browser: the same site's tab if it is open, the front tab if it is blank, otherwise a new one."""
        want = host_of(url)
        for p in self.ctx.pages:
            if want and host_of(p.url) == want:
                return p
        if self.page is not None and (not self.page.url or self.page.url.startswith(BLANK_URLS)):
            return self.page
        return self.ctx.new_page()

    def _on_page(self, page) -> None:
        self.page = page  # when a new tab opens, switch to it

    def snapshot(self, limit: int = 120) -> list[dict[str, Any]]:
        return self.evaluate(SNAPSHOT_JS, limit)

    def locator(self, element_id: int):
        from alice.tools.computer import as_number

        loc = self.page.locator(f'[data-alice-id="{as_number(element_id, "element_id", int)}"]')
        if loc.count() == 0:
            raise ToolError("öğe bulunamadı; sayfa değişmiş olabilir, browser action=snapshot ile yenile")
        return loc.first

    def close(self) -> None:
        try:
            if not self.attached and not getattr(self, "aborted", ""):  # do not close the user's browser
                self.ctx.close()
        finally:
            self._pw.stop()


def _driver_pid(pw: Any) -> int | None:
    try:
        return int(pw._impl_obj._connection._transport._proc.pid)
    except Exception:  # noqa: BLE001 - if Playwright's internals change, the watchdog can only fail to kill
        return None


def _is_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:  # noqa: BLE001
        return True


def kill_playwright_drivers() -> int:
    """Quietly terminate the Playwright drivers (node ... run-driver) started by this process.

    When ALICE shut down or restarted after an update, the driver tried to write to a closed pipe and dumped an
    "Error: EPIPE: broken pipe" stack into core.log. Playwright objects are bound to the thread they were created on,
    so a clean stop() at exit is not always possible; the driver is killed because it would be orphaned anyway.
    The browser bridge (a separate process) and the user's browser are not affected."""
    try:
        import psutil

        kids = psutil.Process().children(recursive=True)
    except Exception:  # noqa: BLE001
        return 0
    found = {}
    for p in kids:
        try:
            cmd = " ".join(p.cmdline()).lower()
            if "playwright" in cmd and "run-driver" in cmd:
                found[p.pid] = p
        except Exception:  # noqa: BLE001 - the process may have exited in the meantime
            continue
    n = 0
    for p in found.values():
        try:
            # On Windows the venv's python.exe launches the real python as a child process: both carry the same command
            # line. If one is the other's child, both are killed but counted as one driver.
            top = p.ppid() not in found
            p.kill()
            n += top
        except Exception:  # noqa: BLE001
            continue
    return n


_ATEXIT = False


def _register_exit_cleanup() -> None:
    global _ATEXIT
    if not _ATEXIT:
        import atexit

        atexit.register(kill_playwright_drivers)
        _ATEXIT = True


# Errors Playwright raises when the connection drops (Opera closed/restarted, the bridge cut a dead connection).
_CONNECTION_LOST = ("has been closed", "target closed", "browser closed", "connection closed", "not connected",
                    "disconnected", "websocket error", "socket closed", "connection refused", "econnrefused")
# Actions that can be retried once on a new connection when it drops: repeating them does not do anything twice.
RETRY_SAFE = ("goto", "snapshot", "text", "screenshot", "tabs", "highlight")
RECONNECT_HINT = ("tarayıcı bağlantısı koptu (Opera kapandı, yeniden başladı ya da cevap vermiyor); bağlantı "
                  "sıfırlandı. İşlem yapılmış olabilir: browser action=snapshot ile sayfaya bak, sonra gerekiyorsa tekrar dene.")


def connection_lost(e: BaseException) -> bool:
    msg = f"{type(e).__name__} {e}".lower()
    return any(k in msg for k in _CONNECTION_LOST)


# Maximum time for one action (s). Some Playwright calls (CDP commands, requests on a dead connection) have no time
# limit; when the time runs out the driver is killed so ALICE does not sit "working" for minutes.
ROOT_AGAIN = 60.0  # s: if the same site's home page is requested again within this time, go to it in the open tab
ACTION_BUDGET = {"goto": 120.0, "back": 60.0, "screenshot": 40.0, "download": 270.0}
DEFAULT_BUDGET = 45.0
CONNECT_BUDGET = 70.0  # new connection: the browser may ask the user for permission (connect_over_cdp waits 60 s)
STOPPED = "durduruldu (kullanıcı kesti)"


def abort_browser(ctx: ToolContext, reason: str = "stop") -> None:
    """Cut the running browser call (Stop button, watchdog). Called from another thread. If there is no call, the
    connection is left alone: cutting it for nothing could make the browser ask for permission again."""
    b = ctx.state.get("browser")
    if b is not None and ctx.state.get("browser_busy"):
        b.abort(reason)


class _Watchdog:
    """Cuts the current session if the action runs over time. The session may be opened inside the action, so it is
    looked up at the moment the timer fires."""

    def __init__(self, ctx: ToolContext, seconds: float):
        self.timer = threading.Timer(seconds, abort_browser, args=(ctx, "timeout"))
        self.timer.daemon = True

    def __enter__(self) -> "_Watchdog":
        self.timer.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.timer.cancel()


def budget_for(ctx: ToolContext, action: str) -> float:
    b = ctx.state.get("browser")
    fresh = b is None or not b.alive()
    return ACTION_BUDGET.get(action, DEFAULT_BUDGET) + (CONNECT_BUDGET if fresh else 0.0)


def _aborted(ctx: ToolContext) -> str:
    return str(getattr(ctx.state.get("browser"), "aborted", "") or "")


def _timeout_hint(action: str) -> str:
    return (f"tarayıcı '{action}' için cevap vermedi (bağlantı takıldı); bağlantı kesildi ve sıfırlandı. İşlem yapılmış "
            "olabilir: browser action=snapshot ile sayfaya bak.")


def drop_browser(ctx: ToolContext) -> None:
    """Drop the session: the next call reconnects (the user's browser is not closed)."""
    b = ctx.state.pop("browser", None)
    if b is not None:
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass


def get_browser(ctx: ToolContext, separate: bool = False) -> BrowserSession:
    b = ctx.state.get("browser")
    if b is not None and not b.alive():  # connection is dead: stop tool calls from saying "browser has been closed"
        drop_browser(ctx)
        b = None
    builtin = ctx.state.get("builtin_browser")
    if (callable(builtin) and not separate
            and str(ctx.config.agent.get("browser", "builtin") or "builtin").lower() == "builtin"):
        # The app's built-in browser: its screen is visible live in the panel, and the user can take over at any time.
        if b is None or not getattr(b, "builtin", False):
            drop_browser(ctx)
            b = BrowserSession(cdp=builtin().endpoint, allow_launch=False)
            b.builtin = True
            b.headless = True  # no window on screen: no framing on the HUD
            ctx.state["browser"] = b
        b.ensure_page()
        return b
    from alice.tools import browser_ext

    hub = None if separate else browser_ext.hub()
    if hub is not None:  # ALICE extension connected: no remote debugging, so Opera does not ask for permission
        if not getattr(b, "extension", False):
            drop_browser(ctx)
            b = ctx.state["browser"] = browser_ext.ExtBrowserSession(hub)
        b.ensure_page()
        return b
    if b is None:
        a = ctx.config.agent
        allow = separate or bool(a.get("browser_headless", False)) or bool(a.get("browser_allow_separate", False))
        b = BrowserSession(headless=bool(a.get("browser_headless", False)), executable=a.get("browser_executable") or None,
                           cdp=a.get("browser_cdp") or None, allow_launch=allow,
                           bridge_port=int(a.get("browser_bridge_port", 9339) or 0) or None)
        ctx.state["browser"] = b
    b.ensure_page()
    return b


def format_snapshot(page_title: str, url: str, items: list[dict[str, Any]]) -> str:
    rows = [f"sayfa: {page_title}", f"url: {url}"]
    for i in items:
        label = i["text"] or i["placeholder"] or i["href"]
        kind = i["role"] or i["tag"]
        if i["tag"] == "input" and i["type"]:
            kind = f"input[{i['type']}]"
        extra = f" değer='{i['value']}'" if i["value"] else ""
        off = "" if i["inView"] else " (aşağıda)"
        rows.append(f"[{i['id']}] {kind} '{label}'{extra}{off}")
    return "\n".join(rows)


class Browser(Tool):
    name = "browser"
    untrusted = True
    description = (
        "Kullanıcının kendi açık tarayıcısında (Opera vb., kendi girişleriyle) web sayfalarıyla çalışır: siteye "
        "gitme, sayfayı okuma, tıklama, form doldurma. Her sitede aynı şekilde çalışır.\n"
        "- Akış: goto ya da snapshot ile öğe listesini al, sonra click/type ile ID üzerinden işlem yap. ID'ler her "
        "snapshot'ta yenilenir; sayfa değişince eski ID'yi kullanma.\n"
        "- goto, site zaten bir sekmede açıksa o sekmeyi kullanır; yeni sekme açmaz.\n"
        "- Sayfanın içeriğini okumak için text, görsel düzen için screenshot.\n"
        "- Kullanıcıya sayfadaki bir yeri göstermek için highlight (ekranda çerçevelenir).\n"
        "- Dosya/kurulum indirmek için download url=<dosya bağlantısı>: kaydetme penceresi çıkmaz, tam yolu döner; "
        "kurulumu o yolla aç. Dosya bağlantısını goto ile açma.\n"
        "- 'Bağlanılamıyor' derse ayrı pencere açma: windows switch_tab/navigate + ui_act ile çalış.\n"
        "- Ekrandaki bir şeye tek tıklama/yazma için reflex daha hızlı; birçok adımlı iş için run_plan."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["goto", "snapshot", "click", "type", "select", "press", "scroll", "back",
                                              "text", "screenshot", "eval", "tabs", "switch_tab", "highlight", "download",
                                              "downloads",
                                              "close"],
                   "description": "goto: url'ye git | snapshot: tıklanabilir öğeleri [ID] ile listele | click: "
                   "element_id'ye tıkla | type: element_id alanına text yaz (submit=true Enter'a basar) | select: "
                   "açılır listede text seçeneğini seç | press: keys tuşuna bas | scroll: amount piksel kaydır | "
                   "back: geri | text: sayfanın düz metni | screenshot: sayfa görüntüsü | eval: js çalıştır | tabs: açık "
                   "sekmeler | switch_tab: index'li sekmeye geç | highlight: element_id'yi kullanıcıya göster (text "
                   "etiket olur) | download: url'deki dosyayı İndirilenler'e indir (kaydetme penceresi açılmaz, "
                   "bitene kadar bekler, dosyanın tam yolunu döner) | downloads: son indirmeler (dosya, durum) | close. "
                   "goto/click/type/press/scroll sonrası güncel "
                   "snapshot otomatik döner."},
        "url": {"type": "string", "description": "goto için adres, ör. 'https://mail.google.com' ya da 'github.com'."},
        "element_id": {"type": "integer", "description": "click/type/select/highlight için son snapshot'taki [ID]."},
        "text": {"type": "string", "description": "type için yazılacak metin; select için seçeneğin görünen adı; "
                 "highlight için çerçevenin kısa etiketi."},
        "submit": {"type": "boolean", "description": "type sonrası Enter'a bas (arama kutuları için). Varsayılan false."},
        "keys": {"type": "string", "description": "press için tuş, ör. 'Enter', 'Escape', 'Control+A'."},
        "amount": {"type": "integer", "description": "scroll için piksel; pozitif aşağı, negatif yukarı. Varsayılan 600."},
        "js": {"type": "string", "description": "eval için JavaScript ifadesi, ör. 'document.title'."},
        "index": {"type": "integer", "description": "switch_tab için tabs çıktısındaki sekme numarası (0'dan başlar)."},
        "separate": {"type": "boolean", "description": "Ayrı ALICE penceresi aç. Sadece kullanıcı açıkça isterse."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("snapshot", "text", "screenshot", "tabs", "highlight", "downloads"):
            return READ
        if a in ("goto", "download"):
            return NETWORK
        return COMPUTER

    def run(self, ctx: ToolContext, action: str, **kw: Any) -> ToolResult:
        try:
            return self._guarded(ctx, action, kw)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if action == "close" or not connection_lost(e):
                raise
            # Connection is dead: drop the session so ALICE does not get stuck saying "browser has been closed".
            reason = _aborted(ctx)
            drop_browser(ctx)
            if reason == "stop":
                raise ToolError(STOPPED) from e
            if action not in RETRY_SAFE:
                raise ToolError(_timeout_hint(action) if reason == "timeout" else RECONNECT_HINT) from e
        try:
            return self._guarded(ctx, action, kw)  # once more, on the new connection
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if not connection_lost(e):
                raise
            reason = _aborted(ctx)
            drop_browser(ctx)
            if reason == "stop":
                raise ToolError(STOPPED) from e
            if reason == "timeout":
                raise ToolError(_timeout_hint(action) + " Yeni bağlantıyla da olmadı: tarayıcıyı kapatıp aç ya da "
                                "windows/ui_act ile kullanıcının sekmelerinde çalış.") from e
            raise ToolError("tarayıcıya yeniden bağlanılamadı: Opera açık mı, opera://inspect'te 'Allow remote "
                            "debugging' açık mı? İzin penceresi çıktıysa onayla.") from e

    def _guarded(self, ctx: ToolContext, action: str, kw: dict[str, Any]) -> ToolResult:
        ctx.state["browser_busy"] = True
        try:
            with _Watchdog(ctx, budget_for(ctx, action)):
                return self._run(ctx, action, **kw)
        finally:
            ctx.state["browser_busy"] = False

    def _run(self, ctx: ToolContext, action: str, url: str | None = None, element_id: int | None = None,
             text: str | None = None, submit: bool = False, keys: str | None = None, amount: int = 600,
             js: str | None = None, index: int | None = None, separate: bool = False) -> ToolResult:
        from alice.tools.computer import as_bool, as_number

        # Weak models send numbers/flags as text: the text "false" was pressing Enter (bool("false") is True).
        submit, separate = as_bool(submit, "submit"), as_bool(separate, "separate")
        amount = as_number(600 if amount is None else amount, "amount", int)
        index = None if index is None else as_number(index, "index", int)
        if element_id is None and action in ("click", "type", "select", "highlight"):
            raise ToolError(f"{action} için element_id gerekli (snapshot çıktısındaki [ID])")
        if action == "close":
            b = ctx.state.get("browser")
            if b is not None and b.attached:
                # The user's own browser: keep the connection open, since reconnecting makes Opera ask for permission again.
                return ToolResult("kullanıcının tarayıcısı açık bırakıldı (ALICE'in ayrı penceresi yok)")
            ctx.state.pop("browser", None)
            if b:
                b.close()
            return ToolResult("tarayıcı kapatıldı")
        b = get_browser(ctx, separate)
        if b.attached and action in ("snapshot", "text", "screenshot"):
            # The user may have switched to another tab in the meantime: look at whatever is "on screen".
            seen = b._visible_page()
            if seen is not None:
                b.page = seen
        page = b.page
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))

        def snap(prefix: str) -> ToolResult:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            p = b.page
            return ToolResult(truncate(f"{prefix}\n{format_snapshot(b.title_of(p), p.url, b.snapshot())}", limit))

        if action == "goto":
            if not url:
                raise ToolError("goto için url gerekli")
            if "://" not in url:
                url = "https://" + url
            if b.attached:  # the user's own browser: use the open site tab, do not open a new one every time
                b.page = page = b.tab_for(url)
                page.bring_to_front()
                root = urlparse(url).path.strip("/") == "" and not urlparse(url).query
                if host_of(page.url) == host_of(url) and root:
                    # On the first request, do not disturb what is inside (e.g. a playing video); if the same home
                    # page is requested again soon ("go to the home page"), really go: the model saw it still playing.
                    last = ctx.state.get("browser_root_switch")
                    ctx.state["browser_root_switch"] = (host_of(url), time.monotonic())
                    if not (last and last[0] == host_of(url) and time.monotonic() - last[1] < ROOT_AGAIN):
                        at = "" if urlparse(page.url).path.strip("/") == "" else (
                            " (sekme sitenin içinde; ana sayfası isteniyorsa aynı goto'yu bir kez daha çağır)")
                        return snap(f"zaten açık sekmeye geçildi: {b.title_of(page)}{at}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:  # noqa: BLE001
                # A newly opened browser (e.g. Opera GX's GX Corner) gets ERR_ABORTED on the first navigation while it
                # loads its own start page; retry after one second.
                if "ERR_ABORTED" not in str(e):
                    raise
                page.wait_for_timeout(1000)
                page = b.page
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return snap(f"açıldı: {url}")
        if action == "snapshot":
            return snap("snapshot")
        if action == "click":
            loc = b.locator(element_id)
            loc.scroll_into_view_if_needed(timeout=5000)
            mark_on_hud(b, loc, "tıklıyorum", wait=True)  # if the HUD is on, show where it will click first
            loc.click(timeout=8000)
            page.wait_for_timeout(600)
            return snap(f"tıklandı: [{element_id}]")
        if action == "highlight":
            from alice.hud import views

            loc = b.locator(element_id)
            loc.scroll_into_view_if_needed(timeout=5000)
            label = str(text or "burada").strip()[:40] or "burada"
            if mark_on_hud(b, loc, label, ttl=views.SHOW_TTL):
                return ToolResult(f"gösterildi: [{element_id}] ekranda çerçevelendi")
            if getattr(b, "headless", False):
                return ToolResult("tarayıcı görünmez (headless) çalışıyor; kullanıcıya gösterilemez")
            loc.evaluate(OUTLINE_JS, timeout=2000)  # no HUD: briefly frame it inside the page
            return ToolResult(f"gösterildi: [{element_id}] sayfada çerçevelendi")
        if action == "type":
            loc = b.locator(element_id)
            mark_on_hud(b, loc, "yazıyorum")
            loc.fill("" if text is None else str(text), timeout=8000)
            if submit:
                loc.press("Enter")
                page.wait_for_timeout(1000)
            return snap(f"yazıldı: [{element_id}]")
        if action == "select":
            if text is None or not str(text).strip():
                raise ToolError("select için text (seçeneğin görünen adı) gerekli")
            loc = b.locator(element_id)
            mark_on_hud(b, loc, "seçiyorum")
            loc.select_option(label=str(text), timeout=8000)
            return snap(f"seçildi: {text}")
        if action == "press":
            keys = str(keys or "Enter")
            page.keyboard.press(keys)
            page.wait_for_timeout(500)
            return snap(f"tuş: {keys}")
        if action == "scroll":
            page.mouse.wheel(0, amount)
            page.wait_for_timeout(400)
            return snap(f"kaydırıldı: {amount}")
        if action == "back":
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            return snap("geri")
        if action == "text":
            return ToolResult(truncate(f"{b.title_of(page)}\n{page.url}\n\n{b.evaluate(TEXT_JS)}", limit))
        if action == "screenshot":
            png = page.screenshot(type="png", timeout=15000)  # do not wait forever on a frozen tab
            from io import BytesIO

            from PIL import Image

            from alice.tools.imaging import image_to_png_b64

            b64, _ = image_to_png_b64(Image.open(BytesIO(png)), int(ctx.config.agent.get("screenshot_max_width", 1366)))
            return ToolResult(f"tarayıcı görüntüsü: {page.url}", images=[b64])
        if action == "eval":
            if not js:
                raise ToolError("eval için js gerekli")
            return ToolResult(truncate(repr(b.evaluate(js)), limit))
        if action == "tabs":
            rows = [f"{i}: {b.title_of(p)} - {p.url}{'  <- aktif' if p is b.page else ''}" for i, p in enumerate(b.ctx.pages)]
            return ToolResult("\n".join(rows))
        if action == "download":
            if not url:
                raise ToolError("download için url gerekli (dosyanın bağlantısı)")
            from alice.tools import fetch_file

            return ToolResult(fetch_file.describe(fetch_file.download(url)))
        if action == "downloads":
            from alice.tools import fetch_file

            rows = []
            for d in fetch_file.jobs() + (b.downloads() if getattr(b, "extension", False) else []):
                row = f"{d.get('file') or d.get('url')} | {d.get('state')}"
                src = fetch_file.source_name(str(d.get("url") or ""))
                name = str(d.get("file") or "").replace("\\", "/").rsplit("/", 1)[-1]
                if name and src and "." in src and name.lower() != src.lower():
                    row += f" | DİKKAT: bağlantıdaki ad {src}, dosya {name} adıyla kaydedilmiş"
                if not d.get("file") and d.get("state") == "in_progress":
                    row += " | kaydetme penceresi bekliyor olabilir (download action'ı bu pencereyi açmaz)"
                if d.get("error"):
                    row += f" | hata: {d['error']}"
                elif d.get("state") != "complete" and d.get("total"):
                    row += f" | %{100 * int(d.get('bytes') or 0) // int(d['total'])}"
                rows.append(row)
            return ToolResult("\n".join(rows) or "indirme yok")
        if action == "switch_tab":
            pages = b.ctx.pages
            if index is None or not 0 <= index < len(pages):
                raise ToolError(f"geçersiz sekme index: {index}; 0 ile {len(pages) - 1} arası olmalı (action=tabs ile bak)")
            b.page = pages[index]
            b.page.bring_to_front()
            return snap(f"sekme {index}")
        raise ToolError(f"bilinmeyen action: {action}. Geçerli: {', '.join(self.parameters['properties']['action']['enum'])}")


def cleanup(ctx: ToolContext) -> None:
    b = ctx.state.pop("browser", None)
    if b:
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass


TOOLS = [Browser()]
