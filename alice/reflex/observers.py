"""Reflex döngüsü için gözlemciler: ekranı ID'li aday listesine çevirip aksiyonu uygular."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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
