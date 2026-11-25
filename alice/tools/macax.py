"""macOS: UI elements by ID through the Accessibility API (System Events), the counterpart of Windows UI Automation
and Linux AT-SPI for ui_elements / ui_act.

System Events lists the buttons, fields and menus of the front window with their role, name, value and screen
position. Elements are returned in the same format as the other platforms, and each one is wrapped in MacElement,
which offers the methods ui_act calls (invoke, click_input, set_focus, set_edit_text...). Clicks go to the element's
center; text is typed by ui_act's fallback after a click.

The app running ALICE (Terminal, the ALICE app) needs the Accessibility permission: System Settings → Privacy &
Security → Accessibility.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

# AX role -> the UI Automation control type the rest of ALICE already understands.
ROLES = {
    "AXButton": "Button", "AXMenuButton": "Button", "AXCheckBox": "CheckBox", "AXRadioButton": "RadioButton",
    "AXMenuItem": "MenuItem", "AXMenuBarItem": "MenuItem", "AXPopUpButton": "ComboBox", "AXComboBox": "ComboBox",
    "AXTextField": "Edit", "AXTextArea": "Edit", "AXSearchField": "Edit", "AXLink": "Hyperlink", "AXRow": "ListItem",
    "AXCell": "DataItem", "AXTab": "TabItem", "AXDisclosureTriangle": "TreeItem", "AXSlider": "Slider",
    "AXIncrementor": "Spinner", "AXWebArea": "Document",
}
EDITABLE = {"Edit", "ComboBox"}
SEP = "\x1f"

# Lists the front window (or the first window whose title contains the given text) and its elements, one per line:
# role, name, value, x, y, width, height separated by SEP. The first line is the window title.
SCRIPT = '''on run argv
  set want to item 1 of argv
  set sep to character id 31
  tell application "System Events"
    set w to missing value
    if want is "" then
      set p to first application process whose frontmost is true
      set w to front window of p
    else
      repeat with p in (application processes whose visible is true)
        repeat with cand in windows of p
          if (name of cand as text) contains want then
            set w to cand
            exit repeat
          end if
        end repeat
        if w is not missing value then exit repeat
      end repeat
      if w is missing value then error "no window titled " & want
    end if
    set out to (name of w as text) & linefeed
    set n to 0
    repeat with e in (entire contents of w)
      try
        set r to role of e as text
        set nm to ""
        try
          set nm to name of e as text
        end try
        if nm is "missing value" then set nm to ""
        set v to ""
        try
          set v to value of e as text
        end try
        if v is "missing value" then set v to ""
        set {px, py} to position of e
        set {sx, sy} to size of e
        set out to out & r & sep & nm & sep & v & sep & px & sep & py & sep & sx & sep & sy & linefeed
        set n to n + 1
        if n > LIMIT then exit repeat
      end try
    end repeat
    return out
  end tell
end run'''


def parse(text: str, limit: int = 150) -> tuple[str, list[dict[str, Any]]]:
    """osascript output -> (window title, items in collect_ui_elements format)."""
    lines = (text or "").splitlines()
    title = lines[0].strip() if lines else ""
    items: list[dict[str, Any]] = []
    for ln in lines[1:]:
        parts = ln.split(SEP)
        if len(parts) != 7:
            continue
        role = ROLES.get(parts[0].strip())
        if role is None:
            continue
        try:
            x, y, w, h = (int(float(v)) for v in parts[3:7])
        except ValueError:
            continue
        if w <= 1 or h <= 1:
            continue
        name, value = parts[1].strip(), parts[2].strip() if role in EDITABLE else ""
        if not name and not value and role not in EDITABLE and role != "Document":
            continue
        rect = (x, y, x + w, y + h)
        items.append({"id": len(items) + 1, "role": role, "name": name[:80], "value": value[:80], "auto_id": "",
                      "enabled": True, "center": (x + w // 2, y + h // 2), "rect": rect})
        if len(items) >= limit:
            break
    return title, items


def collect(title: str | None = None, limit: int = 150, budget: float = 5.0,
            run: Any = None) -> tuple[str, list[dict[str, Any]], dict[int, "MacElement"]]:
    """(window title, items, {id: MacElement}). The scan stops at `budget` seconds (System Events can be slow)."""
    exe = shutil.which("osascript")
    if not exe:
        raise RuntimeError("osascript not found")
    script = SCRIPT.replace("LIMIT", str(max(limit * 4, 200)))  # many elements are skipped (labels, groups)
    try:
        r = (run or subprocess.run)([exe, "-e", script, (title or "").strip()], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=max(3.0, budget + 2))
    except subprocess.TimeoutExpired:
        raise RuntimeError("the window has too many elements to list in time; use the computer tool") from None
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "assistive access" in err or "-1719" in err or "-25211" in err:
            raise RuntimeError("ui_elements on macOS needs the Accessibility permission for the app running ALICE: "
                               "System Settings → Privacy & Security → Accessibility") from None
        raise RuntimeError(err or "could not read the window's elements")
    win, items = parse(r.stdout, limit)
    return win, items, {i["id"]: MacElement(i["rect"]) for i in items}


class MacElement:
    """A listed element; actions go through the mouse at its center (ui_act types text after a click)."""

    def __init__(self, rect: tuple[int, int, int, int]):
        self.rect = rect

    def _click_at(self, button: str = "left", clicks: int = 1) -> None:
        import pyautogui

        x, y = (self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2
        pyautogui.click(x, y, clicks=clicks, button=button)

    def invoke(self) -> None:
        self._click_at()

    click_input = set_focus = toggle = expand = collapse = select = invoke

    def double_click_input(self) -> None:
        self._click_at(clicks=2)

    def right_click_input(self) -> None:
        self._click_at(button="right")

    def set_edit_text(self, text: str) -> None:
        raise RuntimeError("typed through the keyboard instead")  # ui_act then clicks and types
