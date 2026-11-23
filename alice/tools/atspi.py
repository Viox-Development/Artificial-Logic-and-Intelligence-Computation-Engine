"""Linux: UI elements by ID through AT-SPI, the counterpart of Windows UI Automation (ui_elements / ui_act).

The accessibility bus lists the buttons, fields and menus of the focused window with their role, name and screen
position, and lets us press, focus or fill them without guessing coordinates. Elements are returned in the same
format as the Windows path (collect_ui_elements), and each one is wrapped in AtspiElement, which offers the methods
ui_act calls on a pywinauto wrapper (invoke, click_input, set_focus, set_edit_text, toggle...).

Needs PyGObject with the Atspi typelib (Debian/Ubuntu: `sudo apt install python3-gi gir1.2-atspi-2.0`, and inside a
venv `pip install PyGObject`) and a running accessibility bus (at-spi2-core; on by default in GNOME and KDE). Qt apps
need QT_ACCESSIBILITY=1, Chromium/Electron apps --force-renderer-accessibility.
"""

from __future__ import annotations

import time
from typing import Any

# AT-SPI role name -> the UI Automation control type the rest of ALICE already understands.
ROLES = {
    "push button": "Button", "toggle button": "Button", "button": "Button",
    "check box": "CheckBox", "check menu item": "MenuItem", "radio button": "RadioButton",
    "radio menu item": "MenuItem", "menu item": "MenuItem", "menu": "MenuItem", "menu bar": "MenuBar",
    "combo box": "ComboBox", "text": "Edit", "entry": "Edit", "password text": "Edit", "editbar": "Edit",
    "link": "Hyperlink", "list item": "ListItem", "page tab": "TabItem", "tree item": "TreeItem",
    "table cell": "DataItem", "slider": "Slider", "spin button": "Spinner", "document web": "Document",
    "document frame": "Document",
}
EDITABLE = {"Edit", "ComboBox"}
CLICK_ACTIONS = ("click", "press", "activate", "jump", "open", "toggle", "select")


def load() -> Any:
    """The Atspi module (gi.repository.Atspi), or RuntimeError saying what to install."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as e:
        raise RuntimeError("ui_elements Linux'ta AT-SPI ister: PyGObject ve Atspi 2.0 (Debian/Ubuntu: sudo apt install "
                           "python3-gi gir1.2-atspi-2.0; venv içinde pip install PyGObject). Yoksa computer aracını "
                           f"kullan. ({e})") from None
    return Atspi


def _children(acc: Any) -> list[Any]:
    try:
        return [c for c in (acc.get_child_at_index(i) for i in range(acc.get_child_count())) if c is not None]
    except Exception:  # noqa: BLE001 - an app can go away while it is being read
        return []


def _has_state(atspi: Any, acc: Any, name: str) -> bool:
    try:
        return bool(acc.get_state_set().contains(getattr(atspi.StateType, name)))
    except Exception:  # noqa: BLE001
        return False


def find_window(atspi: Any, title: str | None = None) -> Any:
    """The active top-level window, or the first one whose name contains title (case-insensitive)."""
    windows = [w for app in _children(atspi.get_desktop(0)) for w in _children(app)]
    if title and title.strip():
        want = title.strip().casefold()
        for w in windows:
            if want in (w.get_name() or "").casefold():
                return w
        raise RuntimeError(f"'{title}' başlıklı pencere bulunamadı")
    for w in windows:
        if _has_state(atspi, w, "ACTIVE"):
            return w
    raise RuntimeError("öndeki pencere bulunamadı (erişilebilirlik veriyolu çalışıyor mu? at-spi2-core)")


def _extents(atspi: Any, acc: Any) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) on screen, or None."""
    try:
        comp = acc.get_component_iface()
        if comp is None:
            return None
        r = comp.get_extents(atspi.CoordType.SCREEN)
        return (int(r.x), int(r.y), int(r.x + r.width), int(r.y + r.height))
    except Exception:  # noqa: BLE001
        return None


def _text_value(acc: Any) -> str:
    try:
        t = acc.get_text_iface()
        return (t.get_text(0, -1) or "") if t is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def collect(title: str | None = None, limit: int = 150, budget: float = 5.0,
            atspi: Any = None) -> tuple[str, list[dict[str, Any]], dict[int, "AtspiElement"]]:
    """(window title, items in collect_ui_elements format, {id: AtspiElement}). Breadth-first, stops at the budget."""
    atspi = atspi or load()
    win = find_window(atspi, title)
    deadline = time.monotonic() + budget
    items: list[dict[str, Any]] = []
    wrappers: dict[int, AtspiElement] = {}
    queue = [(c, 1) for c in _children(win)]
    while queue and time.monotonic() < deadline and len(items) < limit:
        acc, d = queue.pop(0)
        if d < 14:
            queue.extend((c, d + 1) for c in _children(acc))
        try:
            role = ROLES.get((acc.get_role_name() or "").lower())
            if role is None or not _has_state(atspi, acc, "SHOWING"):
                continue
            rect = _extents(atspi, acc)
            if rect is None or rect[2] - rect[0] <= 1 or rect[3] - rect[1] <= 1:
                continue
            name = (acc.get_name() or "").strip()
            value = _text_value(acc) if role in EDITABLE else ""
            if not name and not value and role not in EDITABLE and role != "Document":
                continue
        except Exception:  # noqa: BLE001 - elements can disappear while scanning
            continue
        idx = len(items) + 1
        wrappers[idx] = AtspiElement(atspi, acc, rect)
        items.append({
            "id": idx, "role": role, "name": name[:80], "value": value[:80], "auto_id": "",
            "enabled": _has_state(atspi, acc, "SENSITIVE") or _has_state(atspi, acc, "ENABLED"),
            "center": ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), "rect": rect,
        })
    return (win.get_name() or ""), items, wrappers


class AtspiElement:
    """An AT-SPI accessible with the methods ui_act calls on a pywinauto wrapper."""

    def __init__(self, atspi: Any, acc: Any, rect: tuple[int, int, int, int]):
        self.atspi, self.acc, self.rect = atspi, acc, rect

    # -- actions
    def _do(self, *names: str) -> bool:
        """Run the first AT-SPI action whose name matches (in this order). False if the element has none."""
        try:
            act = self.acc.get_action_iface()
            if act is None:
                return False
            have = {(act.get_action_name(i) or "").lower(): i for i in range(act.get_n_actions())}
        except Exception:  # noqa: BLE001
            return False
        for n in names:
            if n in have:
                return bool(act.do_action(have[n]))
        return False

    def _click_at(self, button: str = "left", clicks: int = 1) -> None:
        import pyautogui

        x, y = (self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2
        pyautogui.click(x, y, clicks=clicks, button=button)

    def invoke(self) -> None:
        if not self._do(*CLICK_ACTIONS):
            raise RuntimeError("öğenin tıklama eylemi yok")

    def click_input(self) -> None:
        self._click_at()

    def double_click_input(self) -> None:
        self._click_at(clicks=2)

    def right_click_input(self) -> None:
        self._click_at(button="right")

    def set_focus(self) -> None:
        comp = self.acc.get_component_iface()
        if comp is None or not comp.grab_focus():
            self._click_at()

    def toggle(self) -> None:
        if not self._do("toggle", "click", "press", "activate"):
            raise RuntimeError("öğe açılıp kapatılamıyor")

    def expand(self) -> None:
        if not self._do("expand or contract", "expand", "open", "click"):
            raise RuntimeError("öğe genişletilemiyor")

    collapse = expand  # AT-SPI exposes one "expand or contract" toggle

    def select(self) -> None:
        if self._do("select"):
            return
        try:
            sel = self.acc.get_parent().get_selection_iface()
            if sel is not None and sel.select_child(self.acc.get_index_in_parent()):
                return
        except Exception:  # noqa: BLE001
            pass
        self.invoke()

    # -- text
    def set_edit_text(self, text: str) -> None:
        edit = self.acc.get_editable_text_iface()
        if edit is None or not edit.set_text_contents(text):
            raise RuntimeError("alan yazılabilir değil")

    def get_value(self) -> str:
        return _text_value(self.acc)

    def window_text(self) -> str:
        return self.acc.get_name() or ""
