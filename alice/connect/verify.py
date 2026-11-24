"""Catch cases where a service reports "ok" but did not actually perform the action.

Spotify: when the desktop app's Connect session becomes stale, the device appears as `is_active=true`, `play` returns 204,
but nothing plays (player state, including timestamp, does not change at all). Transferring playback to the device does not wake it either.
Opening the track locally via a `spotify:` link both plays it and revives the session.

If the app is open but idle for a while (or not open at all), Spotify returns 404 NO_ACTIVE_DEVICE to `play`. This is resolved
the same way; returning an error to the model and having it search for devices and retry was an unnecessary turn and meant "1 error".
"""

from __future__ import annotations

import os
from typing import Any

from alice.connect import runner
from alice.connect.manifest import Action, Manifest

POLL = 0.5          # seconds
API_WAIT = 3.0      # wait this long after play for the API to start playing
LOCAL_WAIT = 12.0   # wait this long for the app to play after opening locally (launching takes time if closed)


def after(m: Manifest, act: Action, args: dict[str, Any], data: Any) -> str:
    """Verify after the action that it actually happened; note to add for the model (empty: nothing to report)."""
    if m.name == "spotify" and act.name == "play":
        return _spotify_play(m, args)
    return ""


def recover(m: Manifest, act: Action, args: dict[str, Any], err: Exception) -> str:
    """If action failed but can be done another way, do it; result for model (empty: keep error as is)."""
    if m.name == "spotify" and act.name == "play" and "NO_ACTIVE_DEVICE" in str(err):
        return _spotify_local(m, args, "Spotify'da etkin cihaz yoktu")
    return ""


def _spotify_playing(m: Manifest) -> bool:
    act = m.actions.get("now_playing")
    if act is None:
        return True  # cannot verify; avoid writing a false negative note
    try:
        data = runner.call(m, act, {}, cache=False)
    except runner.ConnectError:
        return False
    return bool(isinstance(data, dict) and data.get("is_playing"))


def _wait_playing(m: Manifest, seconds: float) -> bool:
    waited = 0.0
    while True:
        if _spotify_playing(m):
            return True
        if waited >= seconds:
            return False
        runner._sleep(POLL)
        waited += POLL


def _open_local(uri: str) -> bool:
    start = getattr(os, "startfile", None)
    if start is None:
        return False
    try:
        start(uri)
    except OSError:
        return False
    return True


def _spotify_play(m: Manifest, args: dict[str, Any]) -> str:
    if _wait_playing(m, API_WAIT):
        return "doğrulandı: çalıyor"
    return _spotify_local(m, args, "API 'tamam' dedi ama çalmadı")


def _spotify_local(m: Manifest, args: dict[str, Any], why: str) -> str:
    """Open track (or app if no uri) locally; launches if app is closed. Then check if it is actually playing."""
    uris = [str(u) for u in (args.get("uris") or []) if str(u).startswith("spotify:")]
    if not _open_local(uris[0] if uris else "spotify:"):
        return (f"UYARI: {why} ve Spotify uygulaması açılamadı. Spotify uygulamasını açıp tekrar dene.")
    if not uris:  # app brought to foreground: resume from where it left off
        runner._sleep(2.0)
        try:
            runner.call(m, m.actions["play"], {}, cache=False)
        except runner.ConnectError:
            pass  # the app is open at least; the warning below covers a failed resume
    if _wait_playing(m, LOCAL_WAIT):
        return f"{why}; parça Spotify uygulamasında açıldı, doğrulandı: çalıyor"
    return (f"UYARI: {why}; Spotify uygulamasında açıldı ama çalmaya başlamadı. "
            "Spotify uygulamasını kapatıp açmak gerekebilir.")
