"""Phone app (/m): status, live screen and controls for managing the computer from the phone.

Runs on the same server and key as the phone bridge (alice phone); reachable only from the local network.
  GET  /api/remote/status   -> mode, current state, temperature/usage, foreground program, recent events, anticipation
  GET  /api/remote/screen   -> downscaled JPEG of the screen (for live viewing on the phone)
  GET  /api/remote/apps     -> apps in the Start menu (to open)
  GET  /api/remote/windows  -> open windows, foreground one marked (to bring to front: action "focus")
  POST /api/remote/action   -> {"action": ...}: volume, media, lock, sleep, shutdown timer, open app/site,
                               write to clipboard, type text, hide/show HUD

Shutdown and sleep come after phone confirmation (the UI asks); shutdown is always delayed 60 s and can be cancelled.
"""

from __future__ import annotations

import io
import sys
import time
from typing import Any

IS_WIN = sys.platform == "win32"
SCREEN_W = 1280          # screen width sent to the phone
SCREEN_QUALITY = 62
SCREEN_MIN_GAP = 0.35    # s: the screen is captured at most this often (if several phones ask at once)
_screen_cache: dict[str, Any] = {"at": 0.0, "jpg": b""}
PHONE_SKIP_KINDS = ("mode", "flight", "sam")  # events not sent to the phone (mode changes, in-game warnings)
NOW_CACHE = 30.0         # s: the "şu an" (now) state computed from flight history (~1 s to compute) is at most this old
_now_cache: dict[str, Any] = {"at": -1e9, "now": {}}


def _last(store, key: str, max_age: float = 120.0) -> float | None:
    row = store.last(key)
    if row and row[1] is not None and time.time() - row[0] < max_age:
        return round(float(row[1]), 1)
    return None


def status() -> dict[str, Any]:
    from alice.core import service
    from alice.core.store import Store

    st = service.read_state()
    import socket

    out: dict[str, Any] = {"core": bool(st.get("running")), "mode": st.get("mode") or "", "voice": st.get("voice") or "",
                           "forced": st.get("forced") or "", "host": socket.gethostname(),
                           "time": time.time()}
    try:
        with Store() as store:
            for name, key in (("cpu", "pc.cpu.percent"), ("ram", "pc.mem.percent"), ("gpu", "pc.gpu.util"),
                              ("gpu_temp", "pc.gpu.temp"), ("cpu_temp", "pc.cpu.temp"), ("vram", "pc.gpu.mem_percent")):
                out[name] = _last(store, key)
            fg = store.last("pc.foreground")
            if fg and time.time() - fg[0] < 120 and fg[2]:
                from alice.now import APP_NAMES

                out["app"] = APP_NAMES.get(fg[2].lower(), fg[2].rsplit(".", 1)[0])
            # In-game flight warnings (SAM, missile, lock) don't go to the phone: the game already speaks them aloud,
            # and the phone app used to notify for each one (7 Nov: dozens of "Füze atıldı!" in one fight). So during
            # a fight these warnings are looked up more widely and filtered afterwards, so they don't fill recent events.
            out["events"] = [{"ts": e.ts, "kind": e.kind, "title": e.title, "body": (e.body or "")[:240],
                              "severity": e.severity} for e in store.events(limit=300)
                             if e.kind not in PHONE_SKIP_KINDS and e.delivered][:15]
            out["series"] = series(store)
            midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            today = [e for e in store.events(since=midnight, limit=500) if e.kind not in ("mode",)]
            out["today"] = {"events": len(today),
                            "warnings": sum(1 for e in today if e.severity in ("warn", "critical"))}
            if time.monotonic() - _now_cache["at"] > NOW_CACHE:
                from alice.now import snapshot

                snap = snapshot(store, state=st)
                _now_cache.update(at=time.monotonic(), now={
                    "flights_today": snap.flights_today, "avg_min_today": snap.avg_min_today,
                    "next_game": snap.next_game, "advice": snap.advice, "in_game": snap.in_game})
            out["now"] = _now_cache["now"]
    except Exception:  # noqa: BLE001 - database busy: the card still opens
        out.setdefault("events", [])
    try:  # what is playing in the media card (Spotify, Sonora, browser...)
        from alice.extras import nowplaying

        n = nowplaying.now()
        if n and (n.get("title") or n.get("artist")):
            out["playing"] = {k: n.get(k, "") for k in ("title", "artist", "app", "status")}
    except Exception:  # noqa: BLE001
        pass
    try:
        from alice.anticipate import ready_line

        out["ready"] = ready_line()
    except Exception:  # noqa: BLE001
        out["ready"] = ""
    return out


SERIES_KEYS = {"cpu": "pc.cpu.percent", "ram": "pc.mem.percent", "gpu": "pc.gpu.util", "gpu_temp": "pc.gpu.temp"}
SERIES_MINUTES = 10
SERIES_POINTS = 20


def series(store, minutes: float = SERIES_MINUTES, points: int = SERIES_POINTS) -> dict[str, list[float]]:
    """Small charts on the phone: the last few minutes of each measurement, at most `points` evenly spaced values.
    If a bucket has no sample, the previous value is repeated; the list is empty if there are no samples at all."""
    now = time.time()
    since = now - minutes * 60
    step = (now - since) / points
    out: dict[str, list[float]] = {}
    for name, key in SERIES_KEYS.items():
        rows = store.series(key, since=since)
        if not rows:
            out[name] = []
            continue
        vals: list[float] = []
        i, last = 0, rows[0][1]
        for b in range(points):
            end = since + (b + 1) * step
            bucket = []
            while i < len(rows) and rows[i][0] <= end:
                bucket.append(rows[i][1])
                i += 1
            if bucket:
                last = sum(bucket) / len(bucket)
            vals.append(round(last, 1))
        out[name] = vals
    return out


def windows() -> list[dict[str, Any]]:
    """Open windows (titled, incl. non-minimized), foreground one marked. To bring to front from the phone."""
    rows: list[dict[str, Any]] = []
    if IS_WIN:
        try:
            import pygetwindow as gw
        except ImportError:
            return []
        try:
            active = gw.getActiveWindow()
        except Exception:  # noqa: BLE001
            active = None
        seen = set()
        for w in gw.getAllWindows():
            title = (w.title or "").strip()
            if not title or title in seen or title in ("Program Manager", "Windows Input Experience", "Settings"):
                continue
            try:
                if w.width < 120 or w.height < 80:
                    continue
            except Exception:  # noqa: BLE001
                pass
            seen.add(title)
            app = title.rsplit(" - ", 1)[-1].strip() if " - " in title else title
            rows.append({"title": title, "app": app[:40],
                         "active": bool(active is not None and getattr(active, "_hWnd", None) == getattr(w, "_hWnd", 0)),
                         "minimized": bool(getattr(w, "isMinimized", False))})
    else:
        try:
            from alice.tools.desktop import list_windows

            for title in list_windows():
                app = title.rsplit(" - ", 1)[-1].strip() if " - " in title else title
                rows.append({"title": title, "app": app[:40], "active": False, "minimized": False})
        except Exception:  # noqa: BLE001 - wmctrl not installed
            return []
    rows.sort(key=lambda r: (not r["active"], r["minimized"]))
    return rows[:20]


def screen_jpeg(monitor: int = 1) -> bytes:
    """Downscaled screen. The HUD and ALICE's own windows stay out of the image (excluded from the screenshot)."""
    now = time.monotonic()
    if now - _screen_cache["at"] < SCREEN_MIN_GAP and _screen_cache["jpg"]:
        return _screen_cache["jpg"]
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mons = sct.monitors
        shot = sct.grab(mons[monitor] if 0 < monitor < len(mons) else mons[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > SCREEN_W:
        img = img.resize((SCREEN_W, round(img.height * SCREEN_W / img.width)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=SCREEN_QUALITY, optimize=False)
    _screen_cache.update(at=now, jpg=buf.getvalue())
    return _screen_cache["jpg"]


def apps(limit: int = 400) -> list[str]:
    from alice.tools.desktop import start_apps

    seen, out = set(), []
    for name, _id in start_apps():
        low = name.lower()
        if low in seen or any(w in low for w in ("uninstall", "kaldır", "readme", "help", "yardım")):
            continue
        seen.add(low)
        out.append(name)
    return sorted(out, key=str.lower)[:limit]


def _lock() -> str:
    if not IS_WIN:
        raise ValueError("sadece Windows")
    import ctypes

    ctypes.windll.user32.LockWorkStation()
    return "bilgisayar kilitlendi"


def _sleep() -> str:
    if not IS_WIN:
        raise ValueError("sadece Windows")
    import ctypes

    ctypes.windll.powrprof.SetSuspendState(0, 0, 0)
    return "bilgisayar uyku moduna geçiyor"


def _foreground_title() -> str | None:
    """Title of the focused window ("" = desktop/untitled). None outside Windows (unknown)."""
    if not IS_WIN:
        return None
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value.strip()


def _type(text: str, enter: bool = False) -> str:
    """Type into the focused window. The phone user cannot see which window has focus, so the target is reported;
    with no window focused, nothing is typed (it used to go to the desktop and looked like "nothing happened")."""
    from alice.tools.computer import press_keys, type_text

    title = _foreground_title()
    if title == "":
        raise ValueError("bilgisayarda odakta bir pencere yok: önce aşağıdaki listeden bir pencereyi öne getir")
    type_text(text)
    if enter:
        press_keys("enter")
    _screen_cache["at"] = 0.0  # show the result right away
    return f"yazıldı → {title[:60]}" if title else "yazıldı"


def _screen_point(d: dict[str, Any]) -> tuple[int, int]:
    """Point tapped on the phone's screenshot (0-1 fractions) -> pixel on the main screen (as in screen_jpeg)."""
    import mss

    from alice.tools.computer import _pyautogui

    _pyautogui()  # DPI awareness: mss and pyautogui must see the same pixels (at 125%/150% scaling)
    try:
        fx, fy = float(d.get("x")), float(d.get("y"))
    except (TypeError, ValueError):
        raise ValueError("dokunulan yer okunamadı") from None
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        raise ValueError("dokunulan yer ekranın dışında")
    with mss.mss() as sct:
        m = sct.monitors[1]
    return m["left"] + round(fx * (m["width"] - 1)), m["top"] + round(fy * (m["height"] - 1))


def _click(d: dict[str, Any]) -> str:
    from alice.tools.computer import _pyautogui

    pg = _pyautogui()
    x, y = _screen_point(d)
    pg.click(x, y, clicks=2 if d.get("double") else 1, button="right" if d.get("button") == "right" else "left")
    _screen_cache["at"] = 0.0
    return "tamam"


def _scroll(d: dict[str, Any]) -> str:
    """Wheel: dy > 0 down, < 0 up (notches)."""
    from alice.tools.computer import _pyautogui

    pg = _pyautogui()
    x, y = _screen_point(d)
    try:
        dy = max(-15, min(15, int(d.get("dy") or 0)))
    except (TypeError, ValueError):
        dy = 0
    if dy:
        pg.moveTo(x, y)
        pg.scroll(-dy * 120)  # on Windows, one wheel notch is 120 units
        _screen_cache["at"] = 0.0
    return "tamam"


def _clipboard(text: str) -> str:
    import pyperclip

    pyperclip.copy(text)
    return "bilgisayarın panosuna kopyalandı"


def _hud_toggle() -> str:
    from alice.config import load_config
    from alice.hud import PORT, client

    hud = load_config().data.get("hud", {}) or {}
    client.configure(int(hud.get("port") or PORT))
    client.send({"kind": "toggle"})
    return "HUD gizlendi/gösterildi"


def action(d: dict[str, Any]) -> str:
    """A control from the phone. On failure raises ValueError (the message is sent to the phone)."""
    from alice.extras import media

    a = str(d.get("action") or "")
    if a in ("play_pause", "next", "previous"):
        from alice.extras import nowplaying

        if nowplaying.control(a):  # to the app that is actually playing (the media key used to go to the last-focused one)
            return "tamam"
    if a in ("play_pause", "next", "previous", "mute", "volume_up", "volume_down"):
        return media.key(a, int(d.get("times") or (3 if a.startswith("volume") else 1)))
    if a == "set_volume":
        return media.set_volume(int(d.get("percent") or 0))
    if a == "lock":
        return _lock()
    if a == "sleep":
        return _sleep()
    if a == "shutdown":
        return media.shutdown_in(1)  # 60 s: undo from the phone with "İptal" (Cancel)
    if a == "cancel_shutdown":
        return media.cancel_shutdown()
    if a == "open":
        from alice.tools.desktop import open_app

        target = str(d.get("target") or "").strip()
        if not target:
            raise ValueError("açılacak bir şey yazılmadı")
        return open_app(target)
    if a == "type":
        text = str(d.get("text") or "")
        if not text:
            raise ValueError("yazılacak metin boş")
        return _type(text[:2000], enter=bool(d.get("enter")))
    if a == "click":  # tap on the live screen on the phone
        return _click(d)
    if a == "scroll":
        return _scroll(d)
    if a == "clipboard":
        return _clipboard(str(d.get("text") or "")[:20000])
    if a == "hud":
        return _hud_toggle()
    if a == "focus":
        from alice.tools.desktop import focus_window

        title = str(d.get("title") or "").strip()
        if not title:
            raise ValueError("pencere seçilmedi")
        return focus_window(title) + " öne getirildi"
    raise ValueError(f"bilinmeyen işlem: {a}")
