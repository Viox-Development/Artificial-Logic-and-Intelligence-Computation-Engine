"""Observers for the reflex loop: turn the screen into a candidate list with IDs and apply the action."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from alice.reflex.perceive import (Item, Screen, build_items, changed_regions, mark_fresh, ocr_available, ocr_changed,
                                   ocr_image)
from alice.tools.base import ToolContext, ToolError

EDITABLE_TAGS = {"input", "textarea"}
EDITABLE_ROLES = {"textbox", "searchbox", "combobox", "Edit", "ComboBox", "Document"}
# Window frame (chrome): the title bar's system menu and buttons, not the app's own content. When reflex saw them
# as options (e.g. in a WebView window with unreadable content) it clicked the "System" menu, and "Close" was there.
WINDOW_CHROME = {"system", "sistem", "restore", "move", "size", "minimize", "maximize", "close", "geri yükle",
                 "taşı", "boyut", "boyutlandır", "simge durumuna küçült", "ekranı kapla", "kapat", "önceki boyut"}
CHROME_GOAL = ("kapat", "küçült", "büyüt", "close", "minimize", "maximize", "ekranı kapla")


# WebView2 apps (ALICE itself) report an invisible Chromium tab strip in the same place as the content:
# the "ALICE - Memory usage - 31.8 MB" tab, "New Tab", "Close tab". Not on screen; clicking them has unclear effects.
TAB_STRIP = {"new tab", "close tab", "yeni sekme", "sekmeyi kapat"}
# Any language: "X - Memory usage - 31.8 MB", "about:blank - Bellek kullanımı - 17,9 MB" (Solitaire, Turkish Windows).
MEMORY_TAB_RX = re.compile(r" - [^-]+ - [\d.,]+ ?[kmg]b$")


def is_phantom_tab(role: str, name: str, rect: Any, docs: list[Any], anywhere: bool = False) -> bool:
    """Is this a Chromium tab strip item inside the page (Document)? In a real browser the strip is above the page.
    anywhere (game session): the tab strip is never an option; it is discarded wherever it is."""
    n = name.strip().lower()
    if not ((role == "TabItem" and MEMORY_TAB_RX.search(n)) or (role == "Button" and n in TAB_STRIP)):
        return False
    if anywhere:
        return True
    if not rect:
        return False
    t = 8  # pixels: the strip can overflow the page edge by a few pixels
    return any(d[0] - t <= rect[0] and d[1] - t <= rect[1] and rect[2] <= d[2] + t and rect[3] <= d[3] + t
               for d in docs)


def window_union(rects: list[tuple[int, int, int, int]], screen: tuple[int, int, int, int]
                 ) -> tuple[int, int, int, int] | None:
    """Union of window and dialog rects (left, top, right, bottom), clipped to screen -> (left, top, width, height)."""
    if not rects:
        return None
    l, t = max(min(r[0] for r in rects), screen[0]), max(min(r[1] for r in rects), screen[1])
    r, b = min(max(r[2] for r in rects), screen[2]), min(max(r[3] for r in rects), screen[3])
    if r - l < 50 or b - t < 50:
        return None
    return l, t, r - l, b - t


def app_name(title: str) -> str:
    """Window title -> app name: '*sayilar.txt, Not Defteri' -> 'Not Defteri'; 'refleks-test - File Explorer' -> 'File Explorer'."""
    for sep in (" - ", ", ", " — ", " | "):
        if sep in title:
            title = title.rsplit(sep, 1)[1]
    return title.strip(" *")


def named_window(goal: str, front: str, windows: list[str]) -> str:
    """If the goal names the app of another open window (not the foreground one), return that name; otherwise ''."""
    from alice.reflex.perceive import words

    gw = set(words(goal))

    def mentioned(title: str) -> bool:
        ws = [w for w in words(app_name(title)) if len(w) > 2]
        return bool(ws) and all(any(g.startswith(w) for g in gw) for w in ws)  # "Defteri'ne" (dative) -> "defteri"

    if not front.strip() or mentioned(front):
        return ""
    return next((app_name(w) for w in windows if w.strip() and w != front and mentioned(w)), "")


def _open_windows() -> list[str]:
    try:
        from alice.tools.desktop import list_windows

        return list_windows()
    except Exception:  # noqa: BLE001
        return []


def focused_control() -> str:
    """The keyboard-focused control and its window (nearest Window ancestor; the dialog's name if it is one), e.g.
    "Edit 'Dosya adı:' in window 'Farklı kaydet'". Ground truth from code (typesafe-computer-use 'focused field'):
    "1-10" meant for Not Defteri (Notepad) went into the file name field of an open Save dialog. '' if unreadable."""
    import sys

    if sys.platform != "win32":
        return ""
    try:
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        el = UIAElementInfo(IUIA().iuia.GetFocusedElement())
        window, p = "", el.parent
        for _ in range(20):
            if p is None or not p.control_type:
                break
            if p.control_type == "Window":
                window = p.name or ""
                break
            p = p.parent
        return f"{el.control_type} '{(el.name or '')[:60]}'" + (f" in window '{window[:60]}'" if window else "")
    except Exception:  # noqa: BLE001 - if focus can't be read, no fabricated value is returned
        return ""


def foreground_title() -> str:
    """Title of the foreground window (empty outside Windows)."""
    import sys

    if sys.platform != "win32":
        return ""
    import ctypes

    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(ctypes.windll.user32.GetForegroundWindow(), buf, 512)
    return buf.value


def is_window_chrome(role: str, name: str, goal: str = "", title: str = "") -> bool:
    if role == "TitleBar":
        return True
    n = name.strip().lower()
    if title.strip() and n.endswith(" " + title.strip().lower()):
        # UWP windows append the title to the name: "Close Solitaire & Casual Games" was slipping through the filter.
        n = n[: -len(title.strip()) - 1].strip()
    if role in ("MenuBar", "MenuItem", "Button") and n in WINDOW_CHROME:
        # If closing/minimizing the window is the goal itself, keep the buttons; the system menu is never needed.
        return not (role == "Button" and wants_window_chrome(goal, title))
    return False


def wants_window_chrome(goal: str, title: str = "") -> bool:
    """Does the goal close/minimize the window itself? Not 'Ayarlar katmanını X ile kapat' (Unciv),
    where the window's Close button stayed listed and the game closed. Needs a window/app word or the window's name."""
    g = goal.lower()
    if not any(w in g for w in CHROME_GOAL):
        return False
    if any(w in g for w in ("pencere", "window", "uygulama", "program", "app")):
        return True
    from alice.reflex.perceive import words

    gw = set(words(goal))
    return any(w in gw for w in words(title) if len(w) > 2)


@dataclass
class Element:
    id: int
    kind: str
    label: str
    editable: bool
    value: str = ""
    in_view: bool = True

    def describe(self) -> str:
        s = f"{self.kind} '{self.label}'"
        if self.value:
            s += f" (şu an: '{self.value}')"
        if not self.in_view:
            s += " [aşağıda]"
        return s


@dataclass
class Observation:
    title: str
    location: str
    elements: list[Element]


class Observer(Protocol):
    kind: str

    def observe(self) -> Observation: ...
    # Optional (visual reflex): snapshot() -> (PIL image, ...) or None; click_at(x, y) clicks at 0-1 fractions
    # of the image's width/height.
    def click(self, element_id: int) -> None: ...
    def type(self, element_id: int, text: str, submit: bool) -> None: ...
    def scroll(self, down: bool) -> None: ...
    def back(self) -> None: ...
    def press_enter(self) -> None: ...


ACTIONS = ("click", "double_click", "right_click", "type_text", "type_keys", "press_key", "hold_key", "drag",
           "scroll_down", "scroll_up", "wait")
# Keys Jev may pick (a closed set; Jev writes no free text). If the goal names another shortcut, it is added too.
KEYS = ("enter", "escape", "tab", "shift+tab", "space", "backspace", "delete", "up", "down", "left", "right",
        "pageup", "pagedown", "home", "end", "ctrl+home", "ctrl+end", "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z",
        "ctrl+y", "ctrl+s", "ctrl+shift+s", "ctrl+f", "ctrl+n", "ctrl+t", "ctrl+tab", "ctrl+shift+tab", "alt+enter",
        "f2", "f5")
WAIT_SECONDS = 1.0
HOLD_SECONDS = 1.0  # hold_key: the key(s) stay pressed this long (changeable from the tool)


def _browser_key(keys: str) -> str:
    """'ctrl+s' -> 'Control+s' (Playwright names)."""
    names = {"ctrl": "Control", "alt": "Alt", "shift": "Shift", "enter": "Enter", "escape": "Escape", "tab": "Tab",
             "space": " ", "backspace": "Backspace", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown",
             "left": "ArrowLeft", "right": "ArrowRight", "pageup": "PageUp", "pagedown": "PageDown", "home": "Home",
             "end": "End", "win": "Meta", "cmd": "Meta"}
    return "+".join(names.get(p, p.upper() if len(p) > 1 and p.startswith("f") else p)
                    for p in keys.lower().replace(" ", "").split("+") if p)


class BrowserObserver:
    kind = "browser"
    hold_seconds = HOLD_SECONDS

    def __init__(self, ctx: ToolContext, start_url: str | None = None):
        from alice.tools.browser import get_browser

        self.ctx = ctx
        # The user's own browser and the tab they are looking at (a separate window only if settings allow it).
        self.b = get_browser(ctx)
        if self.b.attached:
            seen = self.b._visible_page()
            if seen is not None:
                self.b.page = seen
        if start_url:
            if "://" not in start_url:
                start_url = "https://" + start_url
            if self.b.attached:
                self.b.page = self.b.tab_for(start_url)
                self.b.page.bring_to_front()
            self.b.page.goto(start_url, wait_until="domcontentloaded", timeout=45000)

    def observe(self) -> Observation:
        try:
            self.b.page.wait_for_load_state("domcontentloaded", timeout=6000)
        except Exception:  # noqa: BLE001
            pass
        items = self.b.snapshot(limit=150)
        els = []
        for i in items:
            kind = i["role"] or i["tag"]
            if i["tag"] == "input" and i["type"]:
                kind = f"input[{i['type']}]"
            editable = (i["tag"] in EDITABLE_TAGS and i["type"] not in ("submit", "button", "checkbox", "radio")) \
                or i["role"] in EDITABLE_ROLES
            els.append(Element(i["id"], kind, i["text"] or i["placeholder"] or i["href"], editable, i["value"], i["inView"]))
        return Observation(self.b.page.title(), self.b.page.url, els)

    def _frame(self, loc, label: str) -> None:
        from alice.tools.browser import mark_on_hud

        mark_on_hud(self.b, loc, label)  # does nothing if the HUD is off or cannot be measured

    def click(self, element_id: int) -> None:
        loc = self.b.locator(element_id)
        loc.scroll_into_view_if_needed(timeout=5000)
        self._frame(loc, "tıklıyorum")
        loc.click(timeout=8000)
        self.b.page.wait_for_timeout(500)

    def type(self, element_id: int, text: str, submit: bool) -> None:
        loc = self.b.locator(element_id)
        self._frame(loc, "yazıyorum")
        loc.fill(text, timeout=8000)
        if submit:
            loc.press("Enter")
            self.b.page.wait_for_timeout(900)

    def scroll(self, down: bool) -> None:
        self.b.page.mouse.wheel(0, 700 if down else -700)
        self.b.page.wait_for_timeout(300)

    def screen(self, goal: str = "") -> Screen:
        """The page's elements (from the DOM; positions unknown) and an image of the visible area."""
        ob = self.observe()
        ax = [Item("", "dom", e.kind, e.label, None, e.value, e.editable,
                   "" if e.in_view else "below, needs scrolling", ref=e.id) for e in ob.elements]
        try:
            img = self.snapshot()
        except Exception:  # noqa: BLE001 - the image is only a fallback
            img = None
        return Screen(ob.title, ob.location, build_items(ax, [], goal), image=img)

    def act(self, action: str, item: Item | None = None, text: str = "", keys: str = "",
            drop: Item | None = None) -> None:
        page = self.b.page
        if action in ("click", "double_click", "right_click", "type_text", "drag") and item is None:
            raise ValueError(f"{action} için öğe gerekli")
        if action == "click":
            self.click(item.ref)
        elif action == "double_click":
            self.b.locator(item.ref).dblclick(timeout=8000)
        elif action == "right_click":
            self.b.locator(item.ref).click(button="right", timeout=8000)
        elif action == "type_text":
            self.type(item.ref, text, submit=False)
        elif action == "press_key":
            page.keyboard.press(_browser_key(keys))
        elif action == "type_keys":
            page.keyboard.type(text, delay=10)
        elif action == "hold_key":
            parts = _browser_key(keys).split("+")
            for k in parts:
                page.keyboard.down(k)
            page.wait_for_timeout(int(self.hold_seconds * 1000))
            for k in reversed(parts):
                page.keyboard.up(k)
        elif action == "drag":
            if drop is None:
                raise ValueError("sürüklemek için hedef öğe gerekli")
            self.b.locator(item.ref).drag_to(self.b.locator(drop.ref), timeout=8000)
        elif action in ("scroll_down", "scroll_up"):
            self.scroll(action == "scroll_down")
        elif action == "wait":
            page.wait_for_timeout(int(WAIT_SECONDS * 1000))
        else:
            raise ValueError(f"bilinmeyen eylem: {action}")
        page.wait_for_timeout(300)

    def snapshot(self):
        """Image of the visible page area (shown to Jev)."""
        import io

        from PIL import Image

        return Image.open(io.BytesIO(self.b.page.screenshot(type="jpeg", quality=80)))

    def click_at(self, fx: float, fy: float) -> None:
        size = self.b.page.viewport_size or self.b.page.evaluate("({width: innerWidth, height: innerHeight})")
        self.b.page.mouse.click(fx * size["width"], fy * size["height"])
        self.b.page.wait_for_timeout(500)

    def drag_at(self, fx: float, fy: float, tx: float, ty: float) -> None:
        size = self.b.page.viewport_size or self.b.page.evaluate("({width: innerWidth, height: innerHeight})")
        m = self.b.page.mouse
        m.move(fx * size["width"], fy * size["height"])
        m.down()
        m.move(tx * size["width"], ty * size["height"], steps=12)
        m.up()
        self.b.page.wait_for_timeout(500)

    def back(self) -> None:
        self.b.page.go_back(wait_until="domcontentloaded")

    def press_enter(self) -> None:
        self.b.page.keyboard.press("Enter")
        self.b.page.wait_for_timeout(900)


class DesktopObserver:
    kind = "desktop"
    hold_seconds = HOLD_SECONDS

    def __init__(self, ctx: ToolContext, window_title: str | None = None, goal: str = "",
                 echoes: tuple[str, ...] = (), diff: bool = False):
        self.ctx = ctx
        self.title = window_title
        self.goal = goal
        self.echoes = echoes  # text on screen that must not be mistaken for the target (the user's chat message)
        # diff (game session): the previous frame is kept; only changed regions are re-read, new items become 'new'.
        # The observer lives across turns, so "what changed in this turn" is visible too.
        self.diff = diff
        self._prev_img: Any = None
        self._prev_lines: list[tuple[str, tuple[float, float, float, float]]] = []
        self._seen: set[str] | None = None
        self.changed: list[tuple[float, float, float, float]] | None = None
        self._hwnd = 0
        self._unparked = False  # at task start, a mouse parked in a corner is moved out once
        if not (window_title or "").strip() and goal and "alice" not in goal.lower() \
                and foreground_title().strip() == "ALICE":
            # The foreground window is ALICE's own chat: "open Notepad, press Ctrl+S" was pressing Escape and Ctrl+S
            # here (reflex does not open applications).
            raise ToolError("öndeki pencere ALICE'in kendisi; hedef pencereyi önce app/windows aracıyla açıp öne "
                            "getir ve title ver (ör. title='Not Defteri')")
        if not (window_title or "").strip() and goal:
            other = named_window(goal, foreground_title(), _open_windows())
            if other:
                # No title given, and the goal names another open window: "type 1-10 into Notepad" was typed into the
                # foreground Calculator.
                raise ToolError(f"hedef '{other}' penceresinden söz ediyor ama öndeki pencere "
                                f"'{foreground_title()}'; title='{other}' ver")
        self._rect: tuple[int, int, int, int] | None = None  # position on screen of the last screenshot

    def screen(self, goal: str = "") -> Screen:
        """The window's accessibility items + on-screen text (OCR), with positions as fractions of the window."""
        from alice.tools.desktop import collect_ui_elements, focus_window

        self.goal = goal or self.goal
        if self.title:
            try:
                focus_window(self.title)  # the image is taken from the foreground window
            except ToolError as e:
                # The requested window is missing: continuing with the foreground window sent keys and clicks to
                # another window ('Setup - Sonora' was requested, but the screen was 'War Thunder - In battle', 2 Nov).
                if "pencere yok" in str(e):
                    raise
            except Exception:  # noqa: BLE001 - focus failed (focus lock etc.): window exists, continue with foreground
                pass
        img = self.snapshot(self._target_hwnd())
        if not self._unparked and self._rect is not None:
            self._unparked = True
            try:
                from alice.tools.computer import unpark_mouse

                left, top, w, h = self._rect
                unpark_mouse(left + w // 2, top + h // 2)  # a mouse waiting in the corner must not block typing
            except Exception:  # noqa: BLE001
                pass
        try:
            title, raw = collect_ui_elements(self.ctx, self.title, limit=200, hwnd=self._window())
        except Exception:  # noqa: BLE001 - UIA sometimes raises a COM error (0x80040201, seen on desktop after Win+D);
            # don't drop the whole reflex: wait briefly and retry once; if that fails, continue with OCR only.
            time.sleep(0.3)
            try:
                title, raw = collect_ui_elements(self.ctx, self.title, limit=200, hwnd=self._window())
            except Exception:  # noqa: BLE001
                title, raw = foreground_title(), []
        # In a game the window's Close button is never an option: "close the settings with X" had closed Unciv.
        chrome_goal = "" if self.diff else self.goal
        ax: list[Item] = []
        docs = [i["rect"] for i in raw if i["role"] == "Document" and i.get("rect")]
        for i in raw:
            if not i["enabled"] or is_window_chrome(i["role"], i["name"], chrome_goal, title) \
                    or is_phantom_tab(i["role"], i["name"], i.get("rect"), docs, anywhere=self.diff):
                continue
            if i["role"] == "Document" and i["name"].strip() in ("", title.strip()):
                # A WebView app's page body (unnamed 'view_1055', or the same as the window title): it covers the
                # whole window; a click lands in the middle of the window, and typing goes to an unclear place.
                continue
            ax.append(Item("", "ax", i["role"], i["name"] or i["auto_id"], self._frac(i.get("rect")), i["value"],
                           i["role"] in EDITABLE_ROLES, ref=i["id"]))
        lines: list[tuple[str, tuple[float, float, float, float]]] = []
        regions = changed_regions(self._prev_img, img) if self.diff else None
        if img is not None and ocr_available()[0]:
            try:
                lines = ocr_changed(img, self._prev_lines, regions) if self.diff else ocr_image(img)
            except Exception:  # noqa: BLE001 - if OCR fails, continue with the tree
                lines = []
        # A game window has no chat, so repeated text is not an echo: it is the target itself (a button).
        items = build_items(ax, lines, self.goal, echoes=self.echoes, drop_echo=not self.diff)
        if self.diff:
            self._prev_img, self._prev_lines = img, lines
            self._seen = mark_fresh(items, self._seen)
            self.changed = regions
        return Screen(title, "desktop", items, focused=focused_control(), image=img)

    def _frac(self, rect: Any) -> tuple[float, float, float, float] | None:
        """Screen pixel (left, top, right, bottom) -> 0-1 relative to the last displayed window."""
        if not rect or self._rect is None:
            return None
        left, top, w, h = self._rect
        l, t, r, b = rect
        fr = ((l - left) / w, (t - top) / h, (r - left) / w, (b - top) / h)
        if fr[2] <= 0 or fr[3] <= 0 or fr[0] >= 1 or fr[1] >= 1:
            return None  # outside the window
        return tuple(min(1.0, max(0.0, v)) for v in fr)  # type: ignore[return-value]

    def _frame(self, item: Item, action: str) -> None:
        """Show the click spot on the HUD if it is open. Doesn't wait (keeps reflex fast); skips if spot unknown."""
        from alice.hud import views

        try:
            x, y = self._point(item)
        except ValueError:
            return
        views.click_target(x, y, "type" if action == "type_text" else action, wait=False)

    def _point(self, item: Item) -> tuple[int, int]:
        if item.center is None or self._rect is None:
            raise ValueError(f"'{item.label}' öğesinin ekrandaki yeri bilinmiyor")
        left, top, w, h = self._rect
        return round(left + item.center[0] * w), round(top + item.center[1] * h)

    def act(self, action: str, item: Item | None = None, text: str = "", keys: str = "",
            drop: Item | None = None) -> None:
        from alice.tools.computer import _pyautogui, hold_keys, press_keys, type_text
        from alice.tools.desktop import ui_act

        if action in ("click", "double_click", "right_click", "type_text", "drag") and item is None:
            raise ValueError(f"{action} için öğe gerekli")
        if item is not None and action in ("click", "double_click", "right_click", "type_text", "drag"):
            self._frame(item, action)
        pg = _pyautogui()
        if action == "click":
            if item.source.startswith("ax") and item.ref is not None:
                ui_act(self.ctx, item.ref, "click")  # via accessibility, even if a pop-up window is open on top
            else:
                pg.click(*self._point(item))
        elif action == "double_click":
            pg.doubleClick(*self._point(item))
        elif action == "right_click":
            pg.rightClick(*self._point(item))
        elif action == "type_text":
            if item.source.startswith("ax") and item.editable and item.ref is not None:
                ui_act(self.ctx, item.ref, "set_text", text)
            else:
                pg.click(*self._point(item))
                time.sleep(0.15)
                type_text(text)
        elif action == "press_key":
            press_keys(keys)
        elif action == "type_keys":
            type_text(text)  # to the foreground window (screen() brought it to the front): calculator, terminal, game
        elif action == "hold_key":
            hold_keys(keys, self.hold_seconds)
        elif action == "drag":
            if drop is None:
                raise ValueError("sürüklemek için hedef öğe gerekli")
            x, y = self._point(item)
            tx, ty = self._point(drop)
            pg.moveTo(x, y)
            pg.dragTo(tx, ty, duration=0.5, button="left")
        elif action in ("scroll_down", "scroll_up"):
            if self._rect is not None:
                left, top, w, h = self._rect
                pg.moveTo(left + w // 2, top + h // 2)
            pg.scroll(-600 if action == "scroll_down" else 600)
        elif action == "wait":
            time.sleep(WAIT_SECONDS)
        else:
            raise ValueError(f"bilinmeyen eylem: {action}")

    def observe(self) -> Observation:
        from alice.tools.desktop import collect_ui_elements

        title, items = collect_ui_elements(self.ctx, self.title, limit=150)
        els = [Element(i["id"], i["role"], i["name"] or i["auto_id"], i["role"] in EDITABLE_ROLES, i["value"])
               for i in items if i["enabled"] and not is_window_chrome(i["role"], i["name"], self.goal, title)]
        return Observation(title, "desktop", els)

    def _window(self) -> int:
        """Handle of the window with the given title, cached: a title search scans all windows (~1.5 s), and it
        was done twice per step. If the window was closed, it is searched again; if not found, 0."""
        import sys

        if sys.platform != "win32" or not self.title:
            return 0
        import ctypes

        if self._hwnd and ctypes.windll.user32.IsWindow(self._hwnd):
            return self._hwnd
        from alice.tools.desktop import _foreground_uia

        try:
            self._hwnd = int(_foreground_uia(self.title).handle or 0)
        except Exception:  # noqa: BLE001
            self._hwnd = 0
        return self._hwnd

    def _target_hwnd(self) -> int:
        """Window whose image is captured. If the foreground window is the target or a dialog it opened (Save): 0,
        i.e. the foreground one. If Windows refused to bring the target to the front, the target itself; otherwise the
        items come from the target while the image and positions come from another window, and clicks drift."""
        import sys

        if sys.platform != "win32" or not self.title:
            return 0
        import ctypes

        target = self._window()
        if not target:
            return 0
        fg = ctypes.windll.user32.GetForegroundWindow()
        if not target or fg == target or ctypes.windll.user32.GetAncestor(fg, 3) == target:  # 3: GA_ROOTOWNER
            return 0
        return target

    def snapshot(self, hwnd: int = 0):
        """Image of the foreground (or given) window (Windows). If no window is found, None: the reflex continues
        without an image."""
        import sys

        if sys.platform != "win32":
            return None
        import ctypes
        import ctypes.wintypes as wintypes

        from alice.tools.imaging import screen_capture
        from PIL import Image

        from alice.tools.computer import _dpi_aware

        _dpi_aware()
        u32 = ctypes.windll.user32
        hwnd = hwnd or u32.GetForegroundWindow()
        if not hwnd:
            return None
        # The window and the dialogs it opens (Notepad's Save window sticks out of the main window;
        # the position of its Cancel button, outside it, was unknown).
        root = u32.GetAncestor(hwnd, 3) or hwnd  # 3: GA_ROOTOWNER
        hwnds = [root]

        def owned(h: int, _: int) -> bool:
            if h != root and u32.IsWindowVisible(h) and u32.GetAncestor(h, 3) == root:
                hwnds.append(h)
            return True

        u32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(owned), 0)
        rects = []
        for h in hwnds:
            r = wintypes.RECT()
            if u32.GetWindowRect(h, ctypes.byref(r)) and r.right - r.left >= 50 and r.bottom - r.top >= 50:
                rects.append((r.left, r.top, r.right, r.bottom))
        with screen_capture() as sct:
            v = sct.monitors[0]
            box = window_union(rects, (v["left"], v["top"], v["left"] + v["width"], v["top"] + v["height"]))
            if box is None:
                return None
            left, top, w, h = box
            raw = sct.grab({"left": left, "top": top, "width": w, "height": h})
        self._rect = box
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def click_at(self, fx: float, fy: float) -> None:
        from alice.tools.computer import _pyautogui

        if self._rect is None:
            raise RuntimeError("önce ekran görüntüsü alınmalı")
        left, top, w, h = self._rect
        _pyautogui().click(round(left + fx * w), round(top + fy * h))

    def drag_at(self, fx: float, fy: float, tx: float, ty: float) -> None:
        """Drag from one spot seen in the image to another (0-1 fractions): card, piece, unit."""
        from alice.tools.computer import _pyautogui

        if self._rect is None:
            raise RuntimeError("önce ekran görüntüsü alınmalı")
        left, top, w, h = self._rect
        pg = _pyautogui()
        pg.moveTo(round(left + fx * w), round(top + fy * h))
        pg.dragTo(round(left + tx * w), round(top + ty * h), duration=0.5, button="left")

    def click(self, element_id: int) -> None:
        from alice.tools.desktop import ui_act

        ui_act(self.ctx, element_id, "click")

    def type(self, element_id: int, text: str, submit: bool) -> None:
        from alice.tools.computer import press_keys
        from alice.tools.desktop import ui_act

        ui_act(self.ctx, element_id, "set_text", text)
        if submit:
            press_keys("enter")

    def scroll(self, down: bool) -> None:
        import pyautogui

        pyautogui.scroll(-600 if down else 600)

    def back(self) -> None:
        from alice.tools.computer import press_keys

        press_keys("alt+left")

    def press_enter(self) -> None:
        from alice.tools.computer import press_keys

        press_keys("enter")
