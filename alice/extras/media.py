"""Music, volume level and shutdown timer.

Windows: media keys (Spotify, YouTube, every player listens to them) and volume keys; for an exact level,
pycaw if it is installed. Linux: playerctl and pactl. macOS: osascript.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time

from alice.i18n import _

VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "mute": 0xAD, "volume_down": 0xAE,
      "volume_up": 0xAF}
PLAYERCTL = {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}

_timers: dict[str, threading.Timer] = {}


def _press(vk: int, times: int = 1) -> None:
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for _press_n in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.01)


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def key(action: str, times: int = 1) -> str:
    if action not in VK:
        raise ValueError(f"unknown media key: {action}")
    if sys.platform == "win32":
        _press(VK[action], times)
        return _("ok")
    if sys.platform == "darwin":
        if action in ("volume_up", "volume_down"):
            delta = 6 * times * (1 if action == "volume_up" else -1)
            ok = _run(["osascript", "-e", f"set volume output volume ((output volume of (get volume settings)) + {delta})"])
        elif action == "mute":
            ok = _run(["osascript", "-e", "set volume output muted not (output muted of (get volume settings))"])
        else:
            ok = _run(["osascript", "-e", 'tell application "Music" to playpause']) if action == "play_pause" else False
        return _("ok") if ok else _("didn't work")
    if action in PLAYERCTL:
        return _("ok") if shutil.which("playerctl") and _run(["playerctl", PLAYERCTL[action]]) else _("playerctl is missing")
    if action == "mute":
        return _("ok") if _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]) else _("pactl is missing")
    sign = "+" if action == "volume_up" else "-"
    return _("ok") if _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{2 * times}%"]) else _("pactl is missing")


def set_volume(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    if sys.platform == "win32":
        try:
            import ctypes

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            dev = AudioUtilities.GetSpeakers()
            vol = ctypes.cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                              ctypes.POINTER(IAudioEndpointVolume))
            vol.SetMasterVolumeLevelScalar(percent / 100, None)
            return _("volume {n}%", n=percent)
        except Exception:
            # without pycaw: first drop to zero, then each press is ~2%.
            _press(VK["volume_down"], 50)
            _press(VK["volume_up"], round(percent / 2))
            return _("volume about {n}%", n=percent)
    if sys.platform == "darwin":
        return _("volume {n}%", n=percent) if _run(["osascript", "-e", f"set volume output volume {percent}"]) else _("didn't work")
    return _("volume {n}%", n=percent) if _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"]) else _("pactl is missing")


def shutdown_in(minutes: float) -> str:
    sec = max(0, int(minutes * 60))
    if sys.platform == "win32":
        ok = _run(["shutdown", "/s", "/t", str(sec)])
    else:
        ok = _run(["shutdown", "-h", f"+{max(1, round(minutes))}"])
    return _("the computer will shut down in {n} min", n=f"{minutes:g}") if ok else _("could not set the shutdown timer")


def cancel_shutdown() -> str:
    ok = _run(["shutdown", "/a"] if sys.platform == "win32" else ["shutdown", "-c"])
    return _("shutdown cancelled") if ok else _("no shutdown to cancel")


def pause_in(minutes: float) -> str:
    """Pause the music after X minutes (sleep timer). Works only while the process stays open."""
    old = _timers.pop("pause", None)
    if old:
        old.cancel()
    t = threading.Timer(minutes * 60, lambda: key("play_pause"))
    t.daemon = True
    t.start()
    _timers["pause"] = t
    return _("music will stop in {n} min", n=f"{minutes:g}")
