"""Screen, mouse and keyboard control (pixel-level computer use).

Coordinates are always in the pixel space of the LAST screenshot (downscaled); the tool converts them to real
screen coordinates itself. pyautogui FAILSAFE is on: moving the mouse to the top-left corner of the screen
immediately aborts ALICE's mouse/keyboard actions.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj

_DPI_SET = False

_TRUE = {"true", "1", "yes", "on", "evet", "e"}
_FALSE = {"false", "0", "no", "off", "hayır", "hayir", "h", "", "none", "null"}


def as_bool(value: Any, name: str = "değer") -> bool:
    """Convert a flag sent by the model to bool: the text "false" must not count as True (bool("false") is True)."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    raise ToolError(f"{name} true ya da false olmalı, '{value}' geldi")


def as_number(value: Any, name: str, kind: type = float) -> Any:
    """"120", 120.0 and similar values to a number; if that fails, raise a ToolError the model can fix."""
    if isinstance(value, bool):
        raise ToolError(f"{name} sayı olmalı, {value} geldi")
    try:
        num = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} sayı olmalı, '{value}' geldi") from None
    if num != num or num in (float("inf"), float("-inf")):
        raise ToolError(f"{name} sonlu bir sayı olmalı, '{value}' geldi")
    return int(num) if kind is int else num


def _dpi_aware() -> None:
    """On Windows with display scaling (125%/150%), so that mss and pyautogui see the same pixels."""
    global _DPI_SET
    if _DPI_SET or sys.platform != "win32":
        return
    _DPI_SET = True
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _pyautogui():
    _dpi_aware()
    try:
        import pyautogui
    except ImportError as e:
        raise ToolError("pyautogui kurulu değil: pip install 'alice-harness[computer]'") from e
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    corners = _desktop_corners()
    if corners:
        pyautogui.FAILSAFE_POINTS = corners
    return pyautogui


def _unpark_mouse(x: int, y: int) -> bool:
    """If the mouse is parked in an emergency-stop corner when a task starts, move it to (x, y); return True
    if it did. This is not a "stop" request: in the general exam the mouse sat in a corner, so no key could
    be pressed during the first two tasks. A mouse moved into a corner during a task still stops it, as before."""
    pg = _pyautogui()
    px, py = pg.position()
    if not any(abs(px - cx) <= 1 and abs(py - cy) <= 1 for cx, cy in pg.FAILSAFE_POINTS):
        return False
    pg.FAILSAFE = False
    try:
        pg.moveTo(x, y)
    finally:
        pg.FAILSAFE = True
    return True


unpark_mouse = _unpark_mouse  # tests replace this with a fake (so the real mouse doesn't move)


def _desktop_corners() -> list[tuple[int, int]]:
    """Make the emergency-stop corners the corners of the whole desktop (all monitors). pyautogui uses the main
    monitor's corners: with a second monitor on the left, (0,0) falls between the two screens, and when the mouse
    rested there, typing got cancelled. Outside Windows this stays empty (pyautogui's own corners remain)."""
    if sys.platform != "win32":
        return []
    import ctypes

    windll = getattr(ctypes, "windll", None)  # missing when a test fakes the platform as win32
    if windll is None:
        return []
    m = windll.user32.GetSystemMetrics
    left, top, w, h = m(76), m(77), m(78), m(79)  # SM_X/YVIRTUALSCREEN, SM_CX/CYVIRTUALSCREEN
    if w <= 0 or h <= 0:
        return []
    right, bottom = left + w - 1, top + h - 1
    return [(left, top), (left, bottom), (right, top), (right, bottom)]


def grab_screen(ctx: ToolContext, grid: bool = False, monitor: int = 1) -> tuple[str, str]:
    """Take a screenshot and save its scale to ctx.state. Returns (b64 png, description)."""
    _dpi_aware()
    try:
        import mss  # noqa: F401  (only checks that it is installed; screen_capture() opens it)
    except ImportError as e:
        raise ToolError("mss kurulu değil: pip install 'alice-harness[computer]'") from e
    from PIL import Image, ImageDraw

    from alice.tools.imaging import image_to_png_b64, screen_capture

    with screen_capture() as sct:
        mons = sct.monitors
        if monitor >= len(mons):
            raise ToolError(f"monitör {monitor} yok, {len(mons) - 1} monitör var")
        mon = mons[monitor]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    max_w = int(ctx.config.agent.get("screenshot_max_width", 1366))
    scale = img.width / max_w if img.width > max_w else 1.0
    if grid:
        # Draw the grid on the original image, labelled every 100 pixels in the downscaled space.
        d = ImageDraw.Draw(img)
        step = 100 * scale
        x = step
        while x < img.width:
            d.line([(x, 0), (x, img.height)], fill=(255, 0, 255), width=max(1, int(scale)))
            d.text((x + 2, 2), str(int(round(x / scale))), fill=(255, 0, 255))
            x += step
        y = step
        while y < img.height:
            d.line([(0, y), (img.width, y)], fill=(255, 0, 255), width=max(1, int(scale)))
            d.text((2, y + 2), str(int(round(y / scale))), fill=(255, 0, 255))
            y += step
    b64, scale = image_to_png_b64(img, max_w)
    ctx.state["screen"] = {"scale": scale, "left": mon["left"], "top": mon["top"],
                           "w": round(img.width / scale), "h": round(img.height / scale)}
    s = ctx.state["screen"]
    return b64, f"ekran görüntüsü {s['w']}x{s['h']} (gerçek {img.width}x{img.height}, monitör {monitor})"


def to_real(ctx: ToolContext, x: float, y: float) -> tuple[int, int]:
    s = ctx.state.get("screen")
    if not s:
        raise ToolError("önce action=screenshot al; koordinatlar ekran görüntüsüne göre verilir")
    return int(s["left"] + x * s["scale"]), int(s["top"] + y * s["scale"])


PASTE_OVER = 40  # characters: longer or multi-line text is pasted from the clipboard, not typed key by key


def send_unicode(text: str) -> bool:
    """Windows: send the characters as Unicode rather than key codes (SendInput, KEYEVENTF_UNICODE). Independent of
    the keyboard layout: pyautogui pressed keys by the US layout, so on a Turkish Q keyboard typing '1234*5678='
    lost the '*'. Returns False outside Windows (the caller types another way)."""
    if sys.platform != "win32":
        return False
    import ctypes
    import ctypes.wintypes as wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    unicode_flag, keyup = 0x0004, 0x0002
    for ch in text:
        events = (INPUT * 2)()
        for n, flags in enumerate((unicode_flag, unicode_flag | keyup)):
            events[n].type = 1  # INPUT_KEYBOARD
            events[n].ki = KEYBDINPUT(0, ord(ch), flags, 0, 0)
        ctypes.windll.user32.SendInput(2, ctypes.byref(events), ctypes.sizeof(INPUT))
        time.sleep(0.01)
    return True


def type_text(text: str) -> None:
    pg = _pyautogui()
    if text.isascii() and "\n" not in text and len(text) <= PASTE_OVER:
        # Letters, digits and spaces are on the same keys in every layout; symbols ('*', '=', '@') are not: they go as Unicode.
        if all(c.isalnum() or c == " " for c in text) or not send_unicode(text):
            pg.write(text, interval=0.01)
        return
    # Turkish characters can't be typed with pyautogui.write; multi-line/long text also vanishes when typed key by key
    # (typing lines 1-10 one by one into Notepad left only "9, 10" in the file): paste from the clipboard.
    try:
        import pyperclip
    except ImportError as e:
        raise ToolError("ASCII olmayan metin için pyperclip gerekli") from e
    old = None
    try:
        old = pyperclip.paste()
    except Exception:  # noqa: BLE001
        pass
    pyperclip.copy(text)
    pg.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
    # The target app may handle the paste late (a busy tab, WhatsApp): if the clipboard is restored too early, the
    # user's old clipboard (possibly a password) gets pasted.
    time.sleep(0.5)
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:  # noqa: BLE001
            pass


def press_keys(keys: str) -> None:
    pg = _pyautogui()
    parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    alias = {"control": "ctrl", "cmd": "command", "win": "win", "windows": "win", "return": "enter",
             "esc": "escape", "del": "delete", "pgup": "pageup", "pgdn": "pagedown"}
    parts = [alias.get(p, p) for p in parts]
    if len(parts) == 1:
        pg.press(parts[0])
    else:
        pg.hotkey(*parts)


def hold_keys(keys: str, seconds: float) -> None:
    """Hold keys such as 'shift+w' down together, then release them in reverse order: running, walking in games."""
    pg = _pyautogui()
    alias = {"control": "ctrl", "win": "win", "windows": "win", "return": "enter", "esc": "escape"}
    parts = [alias.get(k.strip().lower(), k.strip().lower()) for k in keys.replace(" ", "").split("+") if k.strip()]
    try:
        for k in parts:
            pg.keyDown(k)
        time.sleep(max(0.05, min(float(seconds), 10.0)))
    finally:
        for k in reversed(parts):
            pg.keyUp(k)


class Computer(Tool):
    name = "computer"
    description = (
        "Takes screenshots and uses the mouse/keyboard one step at a time (low level).\n"
        "- Take action=screenshot first; x,y coordinates are in that image's pixel space. Don't guess coordinates "
        "without looking.\n"
        "- For screen work (single or multi-step) use reflex first (fast); for web pages, browser; if reflex "
        "can't do it, computer_task. Clicking by coordinates is the last resort.\n"
        "- For 'what is on the screen', a screenshot is enough."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["screenshot", "click", "double_click", "right_click", "middle_click",
                                              "move", "drag", "scroll", "type", "key", "wait", "cursor_position"],
                   "description": "screenshot | click/double_click/right_click/middle_click/move: x,y | drag: from x,y "
                   "to to_x,to_y | scroll: amount over x,y | type: type text | key: press the keys combination | "
                   "wait: wait seconds | cursor_position."},
        "x": {"type": "number", "description": "Horizontal pixel in the last screenshot."},
        "y": {"type": "number", "description": "Vertical pixel in the last screenshot."},
        "to_x": {"type": "number", "description": "End x for drag."},
        "to_y": {"type": "number", "description": "End y for drag."},
        "text": {"type": "string", "description": "Text to type for type (non-ASCII characters such as Turkish letters work)."},
        "keys": {"type": "string", "description": "Key or combination for key, e.g. 'enter', 'ctrl+s', 'alt+tab'."},
        "amount": {"type": "integer", "description": "Notches for scroll; positive up, negative down. Default -5."},
        "seconds": {"type": "number", "description": "Seconds for wait, at most 30. Default 1."},
        "grid": {"type": "boolean", "description": "Draw a coordinate grid on the screenshot (for precise clicks)."},
        "monitor": {"type": "integer", "description": "1 main monitor, 2 second, 0 all side by side. Default 1."},
        "screenshot_after": {"type": "boolean", "description": "Return a new screenshot after the action."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") in ("screenshot", "cursor_position", "wait") else COMPUTER

    def run(self, ctx: ToolContext, action: str, x: float | None = None, y: float | None = None,
            to_x: float | None = None, to_y: float | None = None, text: str | None = None,
            keys: str | None = None, amount: int = -5, seconds: float = 1.0, grid: bool = False,
            monitor: int = 1, screenshot_after: bool = False) -> ToolResult:
        monitor = as_number(1 if monitor is None else monitor, "monitor", int)
        if monitor < 0:
            raise ToolError(f"monitor 0 ya da pozitif olmalı, {monitor} geldi")
        screenshot_after = as_bool(screenshot_after, "screenshot_after")
        x, y, to_x, to_y = (None if v is None else as_number(v, n) for v, n in
                            ((x, "x"), (y, "y"), (to_x, "to_x"), (to_y, "to_y")))
        if action == "screenshot":
            b64, desc = grab_screen(ctx, grid=as_bool(grid, "grid"), monitor=monitor)
            return ToolResult(desc, images=[b64])
        if action == "wait":
            seconds = as_number(1.0 if seconds is None else seconds, "seconds")
            time.sleep(max(0.0, min(seconds, 30.0)))
            msg = f"{seconds} sn beklendi"
        elif action == "cursor_position":
            pg = _pyautogui()
            px, py = pg.position()
            s = ctx.state.get("screen") or {"scale": 1.0, "left": 0, "top": 0}
            return ToolResult(f"imleç: ({(px - s['left']) / s['scale']:.0f}, {(py - s['top']) / s['scale']:.0f}) ekran görüntüsü uzayında")
        elif action == "type":
            if text is None:
                raise ToolError("type için text gerekli")
            text = str(text)
            type_text(text)
            msg = f"{len(text)} karakter yazıldı"
        elif action == "key":
            if not keys:
                raise ToolError("key için keys gerekli")
            keys = str(keys)
            press_keys(keys)
            msg = f"tuş: {keys}"
        else:
            pg = _pyautogui()
            if x is None or y is None:
                raise ToolError(f"{action} için x ve y gerekli")
            rx, ry = to_real(ctx, x, y)
            from alice.hud import views

            views.click_target(rx, ry, action)  # if the HUD is open, show where the click will land beforehand
            if action in ("click", "double_click", "right_click", "middle_click"):
                button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
                pg.click(rx, ry, clicks=2 if action == "double_click" else 1, button=button, interval=0.08)
            elif action == "move":
                pg.moveTo(rx, ry, duration=0.1)
            elif action == "drag":
                if to_x is None or to_y is None:
                    raise ToolError("drag için to_x ve to_y gerekli")
                tx, ty = to_real(ctx, to_x, to_y)
                pg.moveTo(rx, ry)
                pg.dragTo(tx, ty, duration=0.4, button="left")
            elif action == "scroll":
                pg.moveTo(rx, ry)
                # Wheel delta unit: 120 = one notch on Windows, one notch on X11, one line on macOS.
                unit = {"win32": 120, "darwin": 5}.get(sys.platform, 1)
                pg.scroll(as_number(-5 if amount is None else amount, "amount", int) * unit)
            msg = f"{action} ({x:.0f},{y:.0f})"
        if screenshot_after:
            time.sleep(0.4)
            b64, desc = grab_screen(ctx, monitor=monitor)
            return ToolResult(f"{msg}\n{desc}", images=[b64])
        return ToolResult(msg)


class ComputerTask(Tool):
    name = "computer_task"
    description = (
        "SLOW fallback: does multi-step screen work with a large model that looks at screenshots (5-15 s per "
        "step, takes minutes). For screen work use reflex first: it is multi-step too (typing, shortcuts, save "
        "dialogs, menus) at ~1 s per step. Typing into Notepad and saving once took 9 minutes with this tool.\n"
        "- Use it: when reflex says needs_help, or for interfaces with no readable elements/text on screen.\n"
        "- Don't use it for: what reflex can do, opening apps (windows open), file or command work (file "
        "tools, run_command). Only one task runs at a time; don't start it again while it runs."
    )
    parameters = obj({
        "goal": {"type": "string", "description": "The job to do, as the user asked it, with names, clear and "
                 "complete. E.g. 'Open the Render settings in Blender's interface'."},
        "monitor": {"type": "integer", "description": "Monitor to work on (1, 2...). Empty: the foreground window's monitor."},
        "max_steps": {"type": "integer", "description": "Maximum steps. Default 30."},
    }, ["goal"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, max_steps: int = 30, monitor: int = 0) -> ToolResult:
        from alice.computer_agent import ComputerAgent, Screen, router_asker

        if ctx.router is None:
            raise ToolError("model yönlendirici yok")
        from alice.computer_agent import _RUNNING, log_line

        from alice.hud import views

        if not _RUNNING.acquire(blocking=False):
            return ToolResult("zaten bir ekran görevi sürüyor; bitmesini bekle, aynı işi tekrar başlatma.", is_error=True)
        try:
            log_line(f"başladı: {goal}")
            views.task_step(1)

            def emit(s: str) -> None:
                ctx.emit(s)
                log_line(s)
                if m := re.match(r"(\d+)\. ", s):  # step finished: the model is now thinking about the next one
                    views.task_step(int(m.group(1)) + 1)

            agent = ComputerAgent(Screen(ctx, monitor=max(0, as_number(monitor or 0, "monitor", int))),
                                  router_asker(ctx.router, str(ctx.config.agent.get("computer_role", "vision"))),
                                  emit=emit, stop=ctx.cancelled)
            res = agent.run(goal, max_steps=max(1, min(as_number(max_steps or 30, "max_steps", int), 60)))
            log_line(f"bitti: {res.status}: {res.summary}")
        finally:
            views.task_end()
            _RUNNING.release()
        return ToolResult(res.report(), is_error=res.status in ("error",))


TOOLS = [Computer(), ComputerTask()]
