"""What is playing and control it: Windows' media sessions (the player box next to the volume icon).

Spotify, Sonora, YouTube Music, a video in the browser... every app connected to media control shows up here;
ALICE reads what is playing and stops/skips in that exact app. Media keys (extras.media) used to go to the last
focused app and could not say what was playing. Outside Windows or without the package: None/False, and the caller
falls back to the media keys.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from typing import Any, Callable

STATUS = {4: "playing", 5: "paused", 3: "stopped", 2: "changing", 1: "opened", 0: "closed"}
CONTROL = {"play_pause": "try_toggle_play_pause_async", "next": "try_skip_next_async",
           "previous": "try_skip_previous_async", "stop": "try_stop_async", "play": "try_play_async",
           "pause": "try_pause_async"}
APPS = {"spotify": "Spotify", "sonora": "Sonora", "chrome": "Chrome", "opera": "Opera", "msedge": "Edge",
        "firefox": "Firefox", "zunemusic": "Medya Oynatıcı", "vlc": "VLC", "applemusic": "Apple Music",
        "youtube": "YouTube", "discord": "Discord"}


def app_name(aumid: str) -> str:
    """'SpotifyAB.SpotifyMusic_zpdnek!Spotify' / 'sonora.exe' -> readable name."""
    low = (aumid or "").lower()
    for key, name in APPS.items():
        if key in low:
            return name
    base = re.split(r"[!\\/]", aumid or "")[-1]
    return re.sub(r"\.exe$", "", base, flags=re.I) or "bilinmeyen uygulama"


def _run(coro_fn: Callable[[], Any]) -> Any:
    if sys.platform != "win32":
        return None
    try:
        return asyncio.run(coro_fn())
    except Exception:  # noqa: BLE001 - no package, session closed, COM error: the caller falls back
        return None


async def _session():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    mgr = await Manager.request_async()
    return mgr.get_current_session()


def now() -> dict[str, Any] | None:
    """Current media: {app, title, artist, album, status}, or None (nothing playing)."""

    async def go():
        s = await _session()
        if s is None:
            return None
        p = await s.try_get_media_properties_async()
        info = s.get_playback_info()
        return {"app": app_name(s.source_app_user_model_id), "title": p.title or "", "artist": p.artist or "",
                "album": p.album_title or "", "status": STATUS.get(int(info.playback_status), "unknown")}

    return _run(go)


def control(action: str) -> bool:
    """Send play_pause/next/previous/stop/play/pause to the current session. False if it did not work."""
    method = CONTROL.get(action)
    if method is None:
        return False

    async def go():
        s = await _session()
        if s is None:
            return False
        return bool(await getattr(s, method)())

    return bool(_run(go))


def describe(n: dict[str, Any] | None) -> str:
    if not n or not (n.get("title") or n.get("artist")):
        return "şu an medya kontrolüne bağlı çalan bir şey yok"
    who = f"{n['artist']} – {n['title']}" if n.get("artist") else n["title"]
    state = {"playing": "çalıyor", "paused": "duraklatıldı", "stopped": "durdu"}.get(n.get("status", ""), n.get("status"))
    return f"{who} ({n['app']}, {state})"


class Watcher:
    """On track change, briefly show "♪ şarkı · sanatçı" on the HUD. Own thread; checks every few seconds."""

    def __init__(self, show: Callable[[dict[str, Any]], Any], every: float = 2.5,
                 read: Callable[[], dict[str, Any] | None] = now):
        self.show, self.every, self.read = show, every, read
        self.last: tuple[str, str] | None = None
        self._stop = threading.Event()

    def tick(self) -> bool:
        n = self.read()
        if not n or n.get("status") != "playing" or not n.get("title"):
            return False
        key = (n.get("title", ""), n.get("artist", ""))
        if key == self.last:
            return False
        first = self.last is None
        self.last = key
        if first:
            return False  # do not announce the song already playing while ALICE starts
        self.show(n)
        return True

    def start(self) -> "Watcher":
        def loop():
            while not self._stop.wait(self.every):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    time.sleep(self.every)

        threading.Thread(target=loop, daemon=True, name="alice-nowplaying").start()
        return self

    def stop(self) -> None:
        self._stop.set()
