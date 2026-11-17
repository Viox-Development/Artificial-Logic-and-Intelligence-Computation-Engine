"""Browser through the Opera/Chrome extension: no remote debugging (CDP), no permission window.

The extension (alice/extension) connects to the local WebSocket the core listens on and performs the incoming
commands in the user's tabs. We imitate the small part of Playwright we use (page, element, keyboard, mouse):
the browser tool and reflex work with the extension through the same code.

When connected via CDP, Opera asked "allow remote debugging?" on every launch; Playwright also redirects downloads
to its own temporary folder under a GUID name and forces sites into the light theme. The extension does none of this.

Clicks and keys use the real mouse/keyboard when the window is in front (sites see "human" input, the HUD cursor
moves); when the window is in the background they are done with events inside the page. Typing is always inside the
page (like Playwright's fill).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import itertools
import json
import threading
import time
from typing import Any

from alice.tools.base import ToolError

EXT_PORT = 9340
PATH = "/alice-ext"
CALL_TIMEOUT = 15.0
DOWNLOAD_WAIT = 240.0  # s: the download waits at most this long (then the status "in_progress" is returned)
EXT_ORIGINS = ("chrome-extension://",)  # web pages must not connect to the local server: extensions only
# Playwright key name -> pyautogui/press_keys name
_OS_KEYS = {"control": "ctrl", "meta": "win", "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
            "arrowright": "right", "escape": "esc", " ": "space"}


class ExtHub:
    """Local server the extension connects to (in the core, on its own thread). One extension at a time."""

    def __init__(self, port: int = EXT_PORT, log=lambda s: None, allowed_ids: list[str] | tuple[str, ...] = ()):
        self.port = port
        self.allowed_ids = tuple(str(i).strip() for i in allowed_ids or () if str(i).strip())
        self.log = log
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ws: Any = None
        self.info: dict[str, Any] = {}
        self.pending: dict[int, concurrent.futures.Future] = {}
        self._ids = itertools.count(1)
        self._ready = threading.Event()
        self.error: str = ""

    @property
    def connected(self) -> bool:
        return self.ws is not None

    # ------------------------------------------------------------ server
    def start(self) -> "ExtHub":
        threading.Thread(target=self._thread, daemon=True, name="alice-browser-ext").start()
        self._ready.wait(5)
        return self

    def _thread(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as e:  # noqa: BLE001 - port busy etc.: the non-extension (CDP) path keeps working
            self.error = str(e)
            self.log(f"eklenti sunucusu başlatılamadı: {e}")
            self._ready.set()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self.loop = asyncio.get_running_loop()
        async with serve(self._handle, "127.0.0.1", self.port, max_size=None, ping_interval=None,
                         process_request=self._check):
            self._ready.set()
            await asyncio.Future()

    def _check(self, connection, request):
        if request.path != PATH:
            return connection.respond(404, "not found\n")
        origin = request.headers.get("Origin", "")
        if not origin.startswith(EXT_ORIGINS):
            return connection.respond(403, "forbidden\n")
        if not self._ext_allowed(origin.split("://", 1)[1].strip("/")):
            return connection.respond(403, "forbidden\n")
        return None

    def _ext_allowed(self, ext_id: str) -> bool:
        """Only the ALICE extension may connect. Any local program (even a command in the sandbox) can send an
        Origin `chrome-extension://...`; it could kick out the real extension and feed ALICE fake page content.
        Accepted: [agent] browser_ext_ids, plus the ID of the first extension that connected (~/.alice/browser-ext-id,
        hidden from the sandbox). If the ID changes (extension loaded from another folder), it is re-pinned once that
        file is deleted."""
        if not ext_id or not ext_id.isalnum():
            return False
        allowed = set(self.allowed_ids)
        pinned = read_pinned_id()
        if pinned:
            allowed.add(pinned)
        if ext_id in allowed:
            return True
        if not allowed:
            pin_id(ext_id)
            self.log(f"tarayıcı eklentisi eşlendi: {ext_id}")
            return True
        self.log(f"tanınmayan eklenti kimliği reddedildi: {ext_id} (yeniden eşlemek için {pin_path()} dosyasını sil)")
        return False

    async def _handle(self, ws) -> None:
        old, self.ws = self.ws, ws
        if old is not None:
            await old.close()
        self.log("eklenti bağlandı")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "hello" in msg:
                    self.info = msg["hello"] or {}
                    continue
                fut = self.pending.pop(msg.get("id"), None) if "id" in msg else None
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(ToolError(f"tarayıcı eklentisi: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result"))
        except Exception:  # noqa: BLE001 - extension disconnected
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                self.fail_all("tarayıcı eklentisi bağlantısı koptu (connection closed)")
            self.log("eklenti ayrıldı")

    def fail_all(self, reason: str) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self.pending.clear()

    # ------------------------------------------------------------ call
    def call(self, method: str, _timeout: float = CALL_TIMEOUT, **params: Any) -> Any:
        timeout = _timeout
        ws, loop = self.ws, self.loop
        if ws is None or loop is None:
            raise ConnectionError("tarayıcı eklentisi bağlı değil (not connected)")
        cid = next(self._ids)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.pending[cid] = fut
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps({"id": cid, "method": method, "params": params})), loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            raise ToolError(f"tarayıcı {timeout:.0f} sn içinde cevap vermedi ({method}); sekme donmuş olabilir") from None
        finally:
            self.pending.pop(cid, None)


def pin_path():
    from alice.config import alice_home

    return alice_home() / "browser-ext-id"


def read_pinned_id() -> str:
    try:
        return pin_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pin_id(ext_id: str) -> None:
    path = pin_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ext_id, encoding="utf-8")
        from alice.sandbox import hide_from_sandbox

        hide_from_sandbox(path)  # so a command in the sandbox cannot change the pinned ID
    except Exception:  # noqa: BLE001 - if it cannot be written, this session is not paired; the next connection retries
        pass


_HUB: ExtHub | None = None


def start_hub(port: int = EXT_PORT, log=lambda s: None, allowed_ids: list[str] | tuple[str, ...] = ()) -> ExtHub:
    global _HUB
    if _HUB is None:
        _HUB = ExtHub(port, log, allowed_ids).start()
    return _HUB


def hub() -> ExtHub | None:
    return _HUB if _HUB is not None and _HUB.connected else None


# ------------------------------------------------------------------ Playwright imitation
def _os_ok() -> bool:
    import sys

    return sys.platform == "win32"


def _os_keys(keys: str) -> str:
    """'Control+A' -> 'ctrl+a'."""
    return "+".join(_OS_KEYS.get(p.lower(), p.lower()) for p in keys.split("+") if p)


def _center(info: Any) -> tuple[int, int] | None:
    """Center of the element on screen (physical px); None if the window is in the background or unmeasurable."""
    from alice.tools.browser import screen_box

    box = screen_box(info)
    if box is None:
        return None
    x, y, w, h = box
    return round(x + w / 2), round(y + h / 2)


def _viewport_point(info: Any, x: float, y: float) -> tuple[int, int] | None:
    """Point inside the page (CSS px) -> screen (physical px)."""
    if not isinstance(info, dict) or not info.get("visible"):
        return None
    try:
        side = (info["ow"] - info["iw"]) / 2
        toolbar = info["oh"] - info["ih"] - side
        k = float(info["dpr"])
        return round((info["sx"] + side + x) * k), round((info["sy"] + toolbar + y) * k)
    except (KeyError, TypeError, ValueError):
        return None


class ExtKeyboard:
    def __init__(self, page: "ExtPage"):
        self.page = page

    def press(self, keys: str) -> None:
        if self.page.os_focus():
            from alice.tools.computer import press_keys

            press_keys(_os_keys(keys))
        else:
            self.page.run("key", keys.split("+")[-1])

    def type(self, text: str, delay: float = 0) -> None:
        if not self.page.os_focus():
            raise ToolError("tarayıcı penceresi öne alınamadı; yazmak için type action'ını kullan")
        from alice.tools.computer import type_text

        type_text(text)

    def down(self, key: str) -> None:
        if self.page.os_focus():
            from alice.tools.computer import _pyautogui

            _pyautogui().keyDown(_os_keys(key))

    def up(self, key: str) -> None:
        from alice.tools.computer import _pyautogui

        _pyautogui().keyUp(_os_keys(key))


class ExtMouse:
    def __init__(self, page: "ExtPage"):
        self.page = page
        self._at: tuple[float, float] = (0.0, 0.0)

    def wheel(self, dx: float, dy: float) -> None:
        self.page.run("scroll", dy)

    def _screen(self, x: float, y: float) -> tuple[int, int]:
        if not self.page.os_focus():
            raise ToolError("tarayıcı penceresi öne alınamadı; noktaya tıklanamıyor")
        pt = _viewport_point(self.page.run("place", None), x, y)
        if pt is None:
            raise ToolError("tarayıcı penceresi görünmüyor; noktaya tıklanamıyor")
        return pt

    def click(self, x: float, y: float, button: str = "left", click_count: int = 1) -> None:
        from alice.hud import views
        from alice.tools.computer import _dpi_aware, _pyautogui

        _dpi_aware()
        sx, sy = self._screen(x, y)
        views.click_target(sx, sy, "click")
        _pyautogui().click(sx, sy, clicks=click_count, button=button, interval=0.08)

    def move(self, x: float, y: float, steps: int = 1) -> None:
        from alice.tools.computer import _dpi_aware, _pyautogui

        _dpi_aware()
        sx, sy = self._screen(x, y)
        _pyautogui().moveTo(sx, sy, duration=0.05 * max(1, steps) / 4)
        self._at = (x, y)

    def down(self) -> None:
        from alice.tools.computer import _pyautogui

        _pyautogui().mouseDown()

    def up(self) -> None:
        from alice.tools.computer import _pyautogui

        _pyautogui().mouseUp()


class ExtLocator:
    def __init__(self, page: "ExtPage", element_id: int):
        self.page, self.id = page, int(element_id)

    @property
    def first(self) -> "ExtLocator":
        return self

    def count(self) -> int:
        try:
            self.page.run("place", self.id)
            return 1
        except ToolError:
            return 0

    def scroll_into_view_if_needed(self, timeout: float = 5000) -> None:
        self.page.run("reveal", self.id)

    def evaluate(self, js: str, arg: Any = None, timeout: float = 2000) -> Any:
        from alice.tools.browser import OUTLINE_JS, PLACE_JS

        if js == PLACE_JS:
            return self.page.run("place", self.id)
        if js == OUTLINE_JS:
            return self.page.run("outline", self.id)
        raise ToolError("eklentiyle öğe üzerinde serbest JS çalıştırılamıyor")

    def _os_point(self) -> tuple[int, int] | None:
        if not self.page.os_focus():
            return None
        self.page.run("reveal", self.id)
        return _center(self.page.run("place", self.id))

    def click(self, timeout: float = 8000, button: str = "left", click_count: int = 1) -> None:
        pt = self._os_point()
        if pt is not None:
            from alice.tools.computer import _dpi_aware, _pyautogui

            _dpi_aware()
            _pyautogui().click(*pt, clicks=click_count, button=button, interval=0.08)
        else:  # window in the background or too small: use page events
            self.page.run("click", self.id, button, click_count)

    def dblclick(self, timeout: float = 8000) -> None:
        self.click(timeout, click_count=2)

    def fill(self, text: str, timeout: float = 8000) -> None:
        self.page.run("fill", self.id, str(text))

    def press(self, key: str) -> None:
        self.page.run("focus", self.id)
        self.page.keyboard.press(key)

    def select_option(self, label: str, timeout: float = 8000) -> Any:
        return self.page.run("select", self.id, str(label))

    def drag_to(self, other: "ExtLocator", timeout: float = 8000) -> None:
        a, b = self._os_point(), other._os_point()
        if a is None or b is None:
            raise ToolError("sürüklemek için tarayıcı penceresi önde ve görünür olmalı")
        from alice.tools.computer import _dpi_aware, _pyautogui

        _dpi_aware()
        pg = _pyautogui()
        pg.moveTo(*a)
        pg.dragTo(*b, duration=0.5, button="left")


class ExtPage:
    """One tab: the part of Playwright's Page that we use."""

    def __init__(self, session: "ExtBrowserSession", tab: dict[str, Any]):
        self.s, self.tab_id = session, int(tab["id"])
        self._tab = tab
        self.keyboard = ExtKeyboard(self)
        self.mouse = ExtMouse(self)
        self.viewport_size = None

    def call(self, method: str, _timeout: float = CALL_TIMEOUT, **params: Any) -> Any:
        return self.s.hub.call(method, _timeout, tabId=self.tab_id, **params)

    def run(self, fn: str, *args: Any, _timeout: float = CALL_TIMEOUT) -> Any:
        try:
            return self.call("run", _timeout, fn=fn, args=list(args))
        except ToolError as e:
            if "Cannot access" in str(e) or "cannot be scripted" in str(e):
                # opera://, chrome://, extension store: the browser does not let extensions into these pages
                raise ToolError("bu tarayıcının kendi sayfası (ayarlar, eklentiler, mağaza gibi); içi okunamaz ve "
                                "tıklanamaz. Başka sekmeye geç ya da windows/ui_act ile çalış.") from e
            raise

    def _refresh(self) -> dict[str, Any]:
        try:
            self._tab = self.s.hub.call("tab", tabId=self.tab_id)
        except ToolError:
            pass
        return self._tab

    @property
    def url(self) -> str:
        return str(self._refresh().get("url") or "")

    def title(self) -> str:
        return str(self._refresh().get("title") or "")

    def is_closed(self) -> bool:
        try:
            self.s.hub.call("tab", tabId=self.tab_id)
            return False
        except ToolError:
            return True

    def bring_to_front(self) -> None:
        self._tab = self.call("focus") or self._tab

    def os_focus(self) -> bool:
        """Bring the tab and window to the front for real mouse/keyboard. False if that fails (in-page events used)."""
        if not _os_ok():
            return False
        try:
            self.bring_to_front()
        except ToolError:
            return False
        time.sleep(0.12)
        try:
            info = self.run("place", None)
        except ToolError:
            return False
        return bool(isinstance(info, dict) and info.get("visible"))

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: float = 45000) -> None:
        self._tab = self.call("navigate", timeout / 1000 + 2, url=url, timeout=timeout) or self._tab

    def go_back(self, wait_until: str = "domcontentloaded", timeout: float = 30000) -> None:
        self._tab = self.call("back", timeout / 1000 + 2, timeout=timeout) or self._tab

    def wait_for_load_state(self, state: str = "load", timeout: float = 8000) -> None:
        self.call("wait", timeout / 1000 + 2, timeout=timeout)

    def wait_for_timeout(self, ms: float) -> None:
        time.sleep(ms / 1000)

    def evaluate(self, js: str, arg: Any = None) -> Any:
        from alice.tools.browser import SNAPSHOT_JS, TEXT_JS

        if js == TEXT_JS:
            return self.run("text")
        if js == SNAPSHOT_JS:
            return self.run("snapshot", arg if arg is not None else 120)
        if "innerWidth" in js and "innerHeight" in js:
            return self.run("viewport")
        if "document.title" == js.strip():
            return self.run("title")
        return self.call("eval", code=js if arg is None else f"({js})({json.dumps(arg)})")

    def screenshot(self, type: str = "png", quality: int | None = None, timeout: float = 15000) -> bytes:  # noqa: A002
        data = self.call("capture", timeout / 1000 + 2)
        raw = base64.b64decode(str(data).split(",", 1)[-1])
        if type == "jpeg":
            import io

            from PIL import Image

            buf = io.BytesIO()
            Image.open(io.BytesIO(raw)).convert("RGB").save(buf, "JPEG", quality=quality or 80)
            return buf.getvalue()
        return raw

    def locator(self, selector: str) -> ExtLocator:
        import re

        m = re.search(r'data-alice-id="(\d+)"', selector)
        if not m:
            raise ToolError("eklentiyle yalnız snapshot ID'leriyle çalışılır")
        return ExtLocator(self, int(m.group(1)))


class _Pages:
    def __init__(self, session: "ExtBrowserSession"):
        self.s = session

    @property
    def pages(self) -> list[ExtPage]:
        return [self.s.page_for(t) for t in self.s.tabs()]

    def new_page(self) -> ExtPage:
        return self.s.page_for(self.s.hub.call("create", url=""))


class ExtBrowserSession:
    """The extension-backed counterpart of BrowserSession (always the user's own browser)."""

    attached = True
    headless = False
    extension = True

    def __init__(self, hub: ExtHub):
        self.hub = hub
        self._pages: dict[int, ExtPage] = {}
        self.ctx = _Pages(self)
        self.page: ExtPage | None = None

    def tabs(self) -> list[dict[str, Any]]:
        return list((self.hub.call("tabs") or {}).get("tabs") or [])

    def page_for(self, tab: dict[str, Any]) -> ExtPage:
        p = self._pages.get(int(tab["id"]))
        if p is None:
            p = self._pages[int(tab["id"])] = ExtPage(self, tab)
        else:
            p._tab = tab
        return p

    def alive(self) -> bool:
        return self.hub.connected

    def abort(self, reason: str) -> None:
        self.hub.fail_all(f"tarayıcı işlemi kesildi: {reason} (connection closed)")

    def _visible_page(self) -> ExtPage | None:
        """Active tab in the foreground window."""
        data = self.hub.call("tabs") or {}
        tabs, win = data.get("tabs") or [], data.get("focusedWindow")
        for t in tabs:
            if t.get("active") and (win is None or t.get("windowId") == win):
                return self.page_for(t)
        return None

    def ensure_page(self) -> None:
        if self.page is not None and not self.page.is_closed():
            return
        self.page = self._visible_page()
        if self.page is None:
            self.page = self.ctx.new_page()

    def title_of(self, page: ExtPage) -> str:
        return page.title()

    def evaluate(self, js: str, arg: Any = None, timeout: float = 10000) -> Any:
        return self.page.evaluate(js, arg)

    def tab_for(self, url: str) -> ExtPage:
        from alice.tools.browser import BLANK_URLS, host_of

        want = host_of(url)
        tabs = self.tabs()
        cur = self.page.tab_id if self.page is not None else None
        same = [t for t in tabs if want and host_of(t.get("url") or "") == want]
        if same:  # if the current tab is on that site, use it; otherwise the first match
            return self.page_for(next((t for t in same if t["id"] == cur), same[0]))
        if self.page is not None and (not self.page.url or self.page.url.startswith(BLANK_URLS)):
            return self.page
        return self.ctx.new_page()

    def snapshot(self, limit: int = 120) -> list[dict[str, Any]]:
        return self.page.run("snapshot", limit) or []

    def locator(self, element_id: int) -> ExtLocator:
        from alice.tools.computer import as_number

        loc = ExtLocator(self.page, as_number(element_id, "element_id", int))
        if loc.count() == 0:
            raise ToolError("öğe bulunamadı; sayfa değişmiş olabilir, browser action=snapshot ile yenile")
        return loc

    def downloads(self, limit: int = 5) -> list[dict[str, Any]]:
        return list(self.hub.call("downloads", limit=limit) or [])

    def download(self, url: str, wait: float = DOWNLOAD_WAIT) -> dict[str, Any]:
        """Download to Downloads without opening a save dialog; wait until it finishes or `wait` seconds pass."""
        return dict(self.hub.call("download", wait + 10, url=url, timeout=int(wait * 1000)) or {})

    def close(self) -> None:
        pass  # the user's own browser: nothing is closed
