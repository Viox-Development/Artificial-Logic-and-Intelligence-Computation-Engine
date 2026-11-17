"""Screen state: is the foreground game in exclusive fullscreen? Then no window (including HUD) can be drawn over the game."""

from __future__ import annotations

import sys

QUNS_RUNNING_D3D_FULL_SCREEN = 3  # borderless window returns QUNS_BUSY (2), which is not a problem for HUD


def exclusive_fullscreen() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        state = ctypes.c_int()
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value == QUNS_RUNNING_D3D_FULL_SCREEN
    except Exception:  # noqa: BLE001 - do not warn if unknown
        return False
