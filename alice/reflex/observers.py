"""Reflex döngüsü için gözlemciler: ekranı ID'li aday listesine çevirip aksiyonu uygular."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from alice.reflex.perceive import Item, Screen, build_items, ocr_available, ocr_image
from alice.tools.base import ToolContext

EDITABLE_TAGS = {"input", "textarea"}
EDITABLE_ROLES = {"textbox", "searchbox", "combobox", "Edit", "ComboBox", "Document"}
# Pencere çerçevesi: başlık çubuğunun sistem menüsü ve düğmeleri. Uygulamanın kendi içeriği değil; reflex bunları
# seçenek olarak görünce (ör. içi okunamayan WebView penceresinde) "System" menüsüne tıklıyordu, "Close" da vardı.
WINDOW_CHROME = {"system", "sistem", "restore", "move", "size", "minimize", "maximize", "close", "geri yükle",
                 "taşı", "boyut", "boyutlandır", "simge durumuna küçült", "ekranı kapla", "kapat", "önceki boyut"}
CHROME_GOAL = ("kapat", "küçült", "büyüt", "close", "minimize", "maximize", "ekranı kapla")


def is_window_chrome(role: str, name: str, goal: str = "") -> bool:
    if role == "TitleBar":
        return True
    if role in ("MenuBar", "MenuItem", "Button") and name.strip().lower() in WINDOW_CHROME:
        # Pencereyi kapatmak/küçültmek hedefin kendisiyse düğmeler kalsın; sistem menüsü hiç gerekmez.
        return not (role == "Button" and any(w in goal.lower() for w in CHROME_GOAL))
    return False


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
    # İsteğe bağlı (görsel reflex): snapshot() -> (PIL görüntü, ...) ya da None; click_at(x, y) görüntünün eni/boyunun
    # 0-1 kesriyle tıklar.
    def click(self, element_id: int) -> None: ...
    def type(self, element_id: int, text: str, submit: bool) -> None: ...
    def scroll(self, down: bool) -> None: ...
    def back(self) -> None: ...
    def press_enter(self) -> None: ...


ACTIONS = ("click", "double_click", "right_click", "type_text", "press_key", "drag", "scroll_down", "scroll_up",
           "wait")
# Jev'in seçebileceği tuşlar (kapalı küme: Jev metin üretmez). Hedefte başka bir kısayol yazıyorsa o da eklenir.
KEYS = ("enter", "escape", "tab", "space", "backspace", "delete", "up", "down", "left", "right", "pageup",
        "pagedown", "home", "end", "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+s", "ctrl+f", "f5")
WAIT_SECONDS = 1.0


def _browser_key(keys: str) -> str:
    """'ctrl+s' -> 'Control+s' (Playwright adları)."""
    names = {"ctrl": "Control", "alt": "Alt", "shift": "Shift", "enter": "Enter", "escape": "Escape", "tab": "Tab",
             "space": " ", "backspace": "Backspace", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown",
             "left": "ArrowLeft", "right": "ArrowRight", "pageup": "PageUp", "pagedown": "PageDown", "home": "Home",
             "end": "End", "win": "Meta", "cmd": "Meta"}
    return "+".join(names.get(p, p.upper() if len(p) > 1 and p.startswith("f") else p)
                    for p in keys.lower().replace(" ", "").split("+") if p)


class BrowserObserver:
    kind = "browser"

    def __init__(self, ctx: ToolContext, start_url: str | None = None):
        from alice.tools.browser import get_browser

        self.ctx = ctx
        # Kullanıcının kendi tarayıcısı ve o an baktığı sekme (ayrı pencere sadece ayarlarda izin varsa).
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

    def click(self, element_id: int) -> None:
        loc = self.b.locator(element_id)
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.click(timeout=8000)
        self.b.page.wait_for_timeout(500)

    def type(self, element_id: int, text: str, submit: bool) -> None:
        loc = self.b.locator(element_id)
        loc.fill(text, timeout=8000)
        if submit:
            loc.press("Enter")
            self.b.page.wait_for_timeout(900)

    def scroll(self, down: bool) -> None:
        self.b.page.mouse.wheel(0, 700 if down else -700)
        self.b.page.wait_for_timeout(300)

    def screen(self, goal: str = "") -> Screen:
        """Sayfanın öğeleri (DOM'dan; konumları bilinmez) ve görünen alanın görüntüsü."""
        ob = self.observe()
        ax = [Item("", "dom", e.kind, e.label, None, e.value, e.editable,
                   "" if e.in_view else "below, needs scrolling", ref=e.id) for e in ob.elements]
        try:
            img = self.snapshot()
        except Exception:  # noqa: BLE001 - görüntü yedek içindir
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
        """Görünen sayfa alanının görüntüsü (Jev'e gösterilir)."""
        import io

        from PIL import Image

        return Image.open(io.BytesIO(self.b.page.screenshot(type="jpeg", quality=80)))

    def click_at(self, fx: float, fy: float) -> None:
        size = self.b.page.viewport_size or self.b.page.evaluate("({width: innerWidth, height: innerHeight})")
        self.b.page.mouse.click(fx * size["width"], fy * size["height"])
        self.b.page.wait_for_timeout(500)

    def back(self) -> None:
        self.b.page.go_back(wait_until="domcontentloaded")

    def press_enter(self) -> None:
        self.b.page.keyboard.press("Enter")
        self.b.page.wait_for_timeout(900)


class DesktopObserver:
    kind = "desktop"

    def __init__(self, ctx: ToolContext, window_title: str | None = None, goal: str = ""):
        self.ctx = ctx
        self.title = window_title
        self.goal = goal
        self._rect: tuple[int, int, int, int] | None = None  # son ekran görüntüsünün ekrandaki yeri

    def screen(self, goal: str = "") -> Screen:
        """Pencerenin erişilebilirlik öğeleri + ekrandaki yazılar (OCR), pencereye göre kesirli konumlarıyla."""
        from alice.tools.desktop import collect_ui_elements, focus_window

        self.goal = goal or self.goal
        if self.title:
            try:
                focus_window(self.title)  # görüntü öndeki pencereden alınıyor
            except Exception:  # noqa: BLE001 - başlıkla bulunamazsa öndeki pencereyle devam
                pass
        img = self.snapshot()
        title, raw = collect_ui_elements(self.ctx, self.title, limit=200)
        ax: list[Item] = []
        for i in raw:
            if not i["enabled"] or is_window_chrome(i["role"], i["name"], self.goal):
                continue
            ax.append(Item("", "ax", i["role"], i["name"] or i["auto_id"], self._frac(i.get("rect")), i["value"],
                           i["role"] in EDITABLE_ROLES, ref=i["id"]))
        lines: list[tuple[str, tuple[float, float, float, float]]] = []
        if img is not None and ocr_available()[0]:
            try:
                lines = ocr_image(img)
            except Exception:  # noqa: BLE001 - OCR olmazsa ağaçla devam
                lines = []
        return Screen(title, "desktop", build_items(ax, lines, self.goal), image=img)

    def _frac(self, rect: Any) -> tuple[float, float, float, float] | None:
        """Ekran pikseli (sol, üst, sağ, alt) -> son görüntülenen pencereye göre 0-1."""
        if not rect or self._rect is None:
            return None
        left, top, w, h = self._rect
        l, t, r, b = rect
        fr = ((l - left) / w, (t - top) / h, (r - left) / w, (b - top) / h)
        if fr[2] <= 0 or fr[3] <= 0 or fr[0] >= 1 or fr[1] >= 1:
            return None  # pencerenin dışında
        return tuple(min(1.0, max(0.0, v)) for v in fr)  # type: ignore[return-value]

    def _point(self, item: Item) -> tuple[int, int]:
        if item.center is None or self._rect is None:
            raise ValueError(f"'{item.label}' öğesinin ekrandaki yeri bilinmiyor")
        left, top, w, h = self._rect
        return round(left + item.center[0] * w), round(top + item.center[1] * h)

    def act(self, action: str, item: Item | None = None, text: str = "", keys: str = "",
            drop: Item | None = None) -> None:
        from alice.tools.computer import _pyautogui, press_keys, type_text
        from alice.tools.desktop import ui_act

        if action in ("click", "double_click", "right_click", "type_text", "drag") and item is None:
            raise ValueError(f"{action} için öğe gerekli")
        pg = _pyautogui()
        if action == "click":
            if item.source.startswith("ax") and item.ref is not None:
                ui_act(self.ctx, item.ref, "click")  # erişilebilirlik üzerinden: üstünde açılır pencere olsa da
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
               for i in items if i["enabled"] and not is_window_chrome(i["role"], i["name"], self.goal)]
        return Observation(title, "desktop", els)

    def snapshot(self):
        """Öndeki pencerenin görüntüsü (Windows). Pencere bulunamazsa None: reflex görselsiz devam eder."""
        import sys

        if sys.platform != "win32":
            return None
        import ctypes
        from ctypes import wintypes

        import mss
        from PIL import Image

        from alice.tools.computer import _dpi_aware

        _dpi_aware()
        r = wintypes.RECT()
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd or not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        w, h = r.right - r.left, r.bottom - r.top
        if w < 50 or h < 50:
            return None
        with mss.mss() as sct:
            raw = sct.grab({"left": r.left, "top": r.top, "width": w, "height": h})
        self._rect = (r.left, r.top, w, h)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def click_at(self, fx: float, fy: float) -> None:
        from alice.tools.computer import _pyautogui

        if self._rect is None:
            raise RuntimeError("önce ekran görüntüsü alınmalı")
        left, top, w, h = self._rect
        _pyautogui().click(round(left + fx * w), round(top + fy * h))

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
