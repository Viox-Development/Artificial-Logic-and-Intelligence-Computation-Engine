"""Global shortcuts, without slowing down the keyboard.

pynput on Windows installs a system-wide low-level keyboard hook (WH_KEYBOARD_LL): EVERY key pressed, in whichever
program it is typed in, first passes through ALICE's Python process. When it is busy (audio, model calls, agents)
Python answers the hook late and Windows makes every key wait, so typing shows up seconds late everywhere.
That is why we do not use a hook on Windows:

- Shortcuts (quick bar): RegisterHotKey. Windows only tells us when that key combination is pressed; it
  leaves the other keys alone.
- Push-to-talk: the key's state is checked at short intervals (GetAsyncKeyState); no hook.

On other systems, pynput is still used.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable

IS_WIN = sys.platform == "win32"

MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "cmd": 0x8, "win": 0x8, "super": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK_NAMES = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pause": 0x13, "scroll_lock": 0x91,
    "caps_lock": 0x14, "num_lock": 0x90, "print_screen": 0x2C, "menu": 0x5D,
    "ctrl": 0x11, "ctrl_l": 0xA2, "ctrl_r": 0xA3, "alt": 0x12, "alt_l": 0xA4, "alt_r": 0xA5, "alt_gr": 0xA5,
    "shift": 0x10, "shift_l": 0xA0, "shift_r": 0xA1,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
PUNCT = {";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC,
         "]": 0xDD, "'": 0xDE}


def key_vk(name: str) -> int | None:
    """Windows virtual key code of a single key (by pynput name: 'a', '<f13>', '<scroll_lock>')."""
    name = name.strip().strip("<>").lower()
    if name in VK_NAMES:
        return VK_NAMES[name]
    if len(name) == 1:
        if name.isalnum() and name.isascii():
            return ord(name.upper())
        return PUNCT.get(name)
    return None


def parse_combo(combo: str) -> tuple[int, int] | None:
    """'<ctrl>+<alt>+a' -> (modifiers, virtual key). None if not understood."""
    mods, vk = 0, None
    for part in [p for p in combo.lower().split("+") if p.strip()]:
        bare = part.strip().strip("<>")
        if bare in MODS:
            mods |= MODS[bare]
        elif vk is None:
            vk = key_vk(part)
            if vk is None:
                return None
        else:
            return None  # two main keys
    return (mods, vk) if vk is not None else None


class WinHotkeys:
    """Global shortcuts via RegisterHotKey: runs on a thread with its own message loop."""

    def __init__(self, mapping: dict[str, Callable[[], None]]):
        self.mapping = {k: v for k, v in mapping.items() if k}
        self.failed: list[str] = []  # shortcuts held by another program or not understood
        self._tid = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="alice-hotkeys")

    def start(self) -> bool:
        self._thread.start()
        self._ready.wait(5)
        return len(self.failed) < len(self.mapping)

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        actions: dict[int, Callable[[], None]] = {}
        for i, (combo, fn) in enumerate(self.mapping.items(), start=1):
            parsed = parse_combo(combo)
            if parsed is None or not user32.RegisterHotKey(None, i, parsed[0] | MOD_NOREPEAT, parsed[1]):
                self.failed.append(combo)
                continue
            actions[i] = fn
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in actions:
                    try:
                        actions[msg.wParam]()
                    except Exception:  # noqa: BLE001 - an error in one shortcut must not stop listening
                        pass
        finally:
            for i in actions:
                user32.UnregisterHotKey(None, i)

    def stop(self) -> None:
        if self._tid:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


class KeyPoller:
    """Watches the push-to-talk key without a hook: checks the key state every 25 ms (Windows)."""

    def __init__(self, key: str, on_press: Callable[[], None], on_release: Callable[[], None], every: float = 0.025):
        vk = key_vk(key)
        if vk is None:
            raise ValueError(f"tuş anlaşılmadı: {key}")
        self.vk, self.on_press, self.on_release, self.every = vk, on_press, on_release, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="alice-ptt")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        import ctypes

        state = ctypes.windll.user32.GetAsyncKeyState
        down = False
        while not self._stop.is_set():
            now = bool(state(self.vk) & 0x8000)
            if now != down:
                down = now
                (self.on_press if down else self.on_release)()
            time.sleep(self.every)

    def stop(self) -> None:
        self._stop.set()
