"""Timeline memory: text you see on screen is written locally, encrypted, into a searchable timeline.

"Find the link someone shared on Discord last week." It stays only on this computer; you can delete it at any time.
Off by default (turn it on with [timeline] enabled = true).

How it works:
  * Every 'every' seconds the foreground window is captured; if it differs noticeably from the previous one, it is
    turned into text with Windows' built-in OCR. Images are not stored, only the text, program name and window title.
  * Text and title are encrypted with Windows DPAPI (only your Windows account can decrypt them), in
    ~/.alice/timeline.db.
  * Nothing is recorded in games (flight mode, War Thunder/DCS in front), in password managers, in private/incognito
    browser windows, or in windows whose title contains "şifre/password/banka". The list is extended with
    [timeline] exclude.
  * Records older than keep_days are deleted automatically.

  alice timeline search "aranan"   search (last 30 days)
  alice timeline status            how many records, how much space
  alice timeline wipe              delete everything
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

DEFAULTS: dict[str, Any] = {"enabled": False, "every": 30, "keep_days": 30, "exclude": []}
EXCLUDE_APPS = {"keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "lastpass.exe", "dashlane.exe",
                "aces.exe", "dcs.exe", "credentialuibroker.exe", "consent.exe", "lockapp.exe"}
EXCLUDE_TITLES = ("inprivate", "incognito", "gizli pencere", "özel pencere", "private browsing", "şifre", "parola",
                  "password", "banka", "bank", "kredi kartı", "1password", "bitwarden")
SAME = 0.03          # on the small image: if fewer pixels than this ratio changed, it's the same screen, not re-read
MAX_TEXT = 20_000


# ----------------------------------------------------------------------- DPAPI
def _blob_cls():
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    return BLOB


def protect(data: bytes) -> bytes:
    """Windows DPAPI (CryptProtectData): only this Windows account can decrypt it. Nothing is recorded
    outside Windows."""
    if sys.platform != "win32":
        raise RuntimeError("zaman çizgisi şimdilik sadece Windows'ta (DPAPI şifrelemesi)")
    import ctypes

    blob = _blob_cls()
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):
        raise OSError("CryptProtectData başarısız")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("zaman çizgisi şimdilik sadece Windows'ta (DPAPI şifrelemesi)")
    import ctypes

    blob = _blob_cls()
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0x1,
                                                    ctypes.byref(dst)):
        raise OSError("CryptUnprotectData başarısız")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


# ---------------------------------------------------------------------- record
def db_path() -> Path:
    return alice_home() / "timeline.db"


class Timeline:
    def __init__(self, path: Path | None = None, enc: Callable[[bytes], bytes] = protect,
                 dec: Callable[[bytes], bytes] = unprotect):
        self.path = path or db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enc, self.dec = enc, dec
        self._lock = threading.Lock()
        self._closed = False
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS shots (ts REAL, app TEXT, title BLOB, text BLOB)")
        self._db.execute("CREATE INDEX IF NOT EXISTS shots_ts ON shots(ts)")
        self._db.commit()

    def add(self, app: str, title: str, text: str, ts: float | None = None) -> None:
        with self._lock:
            if self._closed:  # a final frame arriving after closing (from the recorder thread) is silently dropped
                return
            self._db.execute("INSERT INTO shots VALUES (?, ?, ?, ?)",
                             (time.time() if ts is None else ts, app, self.enc(title.encode("utf-8")),
                              self.enc(text[:MAX_TEXT].encode("utf-8"))))
            self._db.commit()

    def search(self, query: str, days: float = 30, limit: int = 10, now: float | None = None) -> list[dict[str, Any]]:
        """Case-insensitive; all words must match. Newest first."""
        now = time.time() if now is None else now
        words = [w for w in query.casefold().split() if w]
        with self._lock:
            rows = self._db.execute("SELECT ts, app, title, text FROM shots WHERE ts >= ? ORDER BY ts DESC",
                                    (now - days * 86400,)).fetchall()
        out = []
        for ts, app, title_b, text_b in rows:
            try:
                title, text = self.dec(title_b).decode("utf-8"), self.dec(text_b).decode("utf-8")
            except Exception:  # noqa: BLE001 - row written under another account, or corrupted
                continue
            hay = f"{title}\n{text}".casefold()
            if all(w in hay for w in words):
                out.append({"ts": ts, "app": app, "title": title, "snippet": snippet(text, words)})
                if len(out) >= limit:
                    break
        return out

    def prune(self, keep_days: float, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            if self._closed:
                return 0
            n = self._db.execute("DELETE FROM shots WHERE ts < ?", (now - keep_days * 86400,)).rowcount
            self._db.commit()
        return n

    def wipe(self) -> int:
        with self._lock:
            n = self._db.execute("DELETE FROM shots").rowcount
            self._db.commit()
            self._db.execute("VACUUM")
        return n

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM shots").fetchone()[0])

    def close(self) -> None:
        """Under the lock: if the recorder thread is mid-write, wait for it to finish (closing without the lock
        would leave a half-done operation or ProgrammingError there); later writes are silently dropped."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()


def snippet(text: str, words: list[str], width: int = 160) -> str:
    flat = " ".join(text.split())
    i = flat.casefold().find(words[0]) if words else 0
    i = max(0, i)
    start = max(0, i - width // 3)
    out = flat[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


# ------------------------------------------------------------------- capture
def excluded(app: str, title: str, extra: list[str] | None = None) -> bool:
    a, t = (app or "").lower(), (title or "").casefold()
    extra_l = [x.casefold() for x in (extra or [])]
    return a in EXCLUDE_APPS or any(x in t for x in EXCLUDE_TITLES) or any(x in a or x in t for x in extra_l)


def different(prev: Any, cur: Any) -> bool:
    """Are two small grayscale images noticeably different? (So OCR doesn't run needlessly when the screen
    is unchanged.)"""
    if prev is None or cur is None or prev.size != cur.size:
        return True
    from PIL import ImageChops

    hist = ImageChops.difference(prev, cur).histogram()  # grayscale: 256 bins, no numpy needed
    return sum(hist[25:]) / max(1, sum(hist)) >= SAME


def _foreground() -> tuple[str, str, tuple[int, int, int, int] | None]:
    """(program, title, window box)."""
    import ctypes
    from ctypes import wintypes

    from alice.core.monitors import foreground_process

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    r = wintypes.RECT()
    box = None
    if user32.GetWindowRect(hwnd, ctypes.byref(r)) and r.right - r.left > 50 and r.bottom - r.top > 50:
        box = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return foreground_process() or "", buf.value, box


def _grab(box: tuple[int, int, int, int]):
    from alice.tools.imaging import screen_capture
    from PIL import Image

    from alice.tools.computer import _dpi_aware

    _dpi_aware()
    x, y, w, h = box
    with screen_capture() as sct:
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.rgb)


@dataclass
class Recorder:
    timeline: Timeline
    cfg: dict[str, Any]
    mode: Callable[[], str] = lambda: ""
    foreground: Callable[[], tuple[str, str, Any]] = _foreground
    grab: Callable[[Any], Any] = _grab
    ocr: Callable[[Any], list[tuple[str, Any]]] | None = None

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_small = None
        self._last_text = ""
        self._pruned = 0.0
        self.saved = 0

    def step(self, now: float | None = None) -> bool:
        """Look once; return True if something was saved."""
        now = time.time() if now is None else now
        if now - self._pruned > 3600:
            self._pruned = now
            self.timeline.prune(float(self.cfg.get("keep_days", DEFAULTS["keep_days"])), now)
        if self.mode() in ("flight", "quiet"):  # the screen is not read in games (performance and focus)
            return False
        app, title, box = self.foreground()
        if box is None or excluded(app, title, self.cfg.get("exclude")):
            return False
        img = self.grab(box)
        small = img.convert("L").resize((64, 36))
        if not different(self._last_small, small):
            return False
        self._last_small = small
        if self.ocr is not None:
            lines = self.ocr(img)
        else:
            from alice.reflex.perceive import ocr_image

            lines = ocr_image(img)
        text = "\n".join(t for t, _ in lines).strip()
        if not text or text == self._last_text:
            return False
        self._last_text = text
        self.timeline.add(app, title, text, now)
        self.saved += 1
        return True

    def run(self) -> None:
        every = max(5.0, float(self.cfg.get("every", DEFAULTS["every"])))
        while not self._stop.wait(every):
            try:
                self.step()
            except Exception:  # noqa: BLE001 - a frame that can't be read doesn't stop recording
                pass

    def start(self) -> "Recorder":
        self._thread = threading.Thread(target=self.run, daemon=True, name="alice-timeline")
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop recording, wait for the frame in progress, close the database (no half-written data left behind)."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        try:
            self.timeline.close()
        except Exception:  # noqa: BLE001 - an error at shutdown must not keep the core from closing
            pass


def fmt(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Zaman çizgisinde bulunamadı."
    return "\n".join(f"- {datetime.fromtimestamp(r['ts']):%d.%m %H:%M} · {r['app']} · {r['title'][:60]}\n  {r['snippet']}"
                     for r in rows)


def timeline_cmd(argv: list[str]) -> int:
    sub = argv[0] if argv else "status"
    tl = Timeline()
    try:
        if sub == "search" and len(argv) > 1:
            print(fmt(tl.search(" ".join(argv[1:]))))
            return 0
        if sub == "wipe":
            print(f"{tl.wipe()} kayıt silindi.")
            return 0
        if sub == "status":
            from alice.config import load_config

            on = bool((load_config().data.get("timeline") or {}).get("enabled"))
            size = db_path().stat().st_size / 1e6 if db_path().exists() else 0
            print(f"zaman çizgisi: {'açık' if on else 'kapalı'}, {tl.count()} kayıt, {size:.1f} MB ({db_path()})")
            return 0
    finally:
        tl.close()
    print(__doc__)
    return 2
