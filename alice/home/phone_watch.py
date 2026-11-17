"""Phone notifications on the computer: reads WhatsApp/SMS/Telegram etc. messages aloud, batches the rest.

If the phone is connected via ADB ([android] phone, the `android` tool's connect), `dumpsys notification`
is read every 20 s.
  * New notification from a messaging app -> spoken right away ("WhatsApp, Ahmet: akşam geliyor musun?"); the same
    app at most once every 30 s (so group chats don't fire back to back; messages arriving in between are counted)
  * others accumulate; in normal mode they are read out as a batch once an hour
    ("Telefonda 5 bildirim: Instagram 3, Gmail 2")
In game/flight mode messages are spoken too (the topic goes through the "phone" flight policy), and the batch
summary is held back.
If the phone is not connected it waits silently; the settings file is re-read every 5 min, and no restart is
needed after pairing.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from alice.core.notify import Notification

MESSAGING = {
    "com.whatsapp": "WhatsApp", "com.whatsapp.w4b": "WhatsApp Business", "org.telegram.messenger": "Telegram",
    "com.google.android.apps.messaging": "Mesajlar", "com.samsung.android.messaging": "Mesajlar",
    "com.android.mms": "Mesajlar", "com.discord": "Discord", "com.instagram.android": "Instagram",
    "com.facebook.orca": "Messenger", "com.google.android.dialer": "Telefon", "com.samsung.android.dialer": "Telefon",
    "com.android.server.telecom": "Telefon",
}
SKIP = ("android", "com.android.systemui", "com.google.android.gms", "com.android.vending", "com.android.providers")
APP_GAP = 30.0
DIGEST_EVERY = 3600.0
DIGEST_MIN = 3


def _app_name(pkg: str) -> str:
    if pkg in MESSAGING:
        return MESSAGING[pkg]
    tail = pkg.rsplit(".", 1)[-1]
    return {"gm": "Gmail", "youtube": "YouTube", "katana": "Facebook"}.get(tail, tail.capitalize())


class PhoneWatch:
    name = "telefon"
    EVERY = 20.0

    def __init__(self, notify: Callable[[Notification], bool], mode: Callable[[], str] = lambda: "normal",
                 read: Callable[[], list[dict[str, str]] | None] | None = None, clock: Callable[[], float] = time.time,
                 background: bool = True):
        self.notify = notify
        self.mode = mode
        self.read = read or self._read_adb
        self.clock = clock
        self.background = background
        self.last_poll = float("-inf")
        self.seen: set[tuple[str, str, str]] | None = None  # None: first read; existing ones are not spoken
        self.app_last: dict[str, float] = {}
        self.pending: dict[str, int] = {}
        self.last_digest = clock()
        self._thread: threading.Thread | None = None
        self._serial = ""
        self._serial_at = float("-inf")

    # ---------------------------------------------------------------- reading
    def _read_adb(self) -> list[dict[str, str]] | None:
        now = time.time()
        if now - self._serial_at > 300:
            self._serial_at = now
            try:
                from alice.config import load_config

                self._conf = (load_config().data.get("android") or {})
            except Exception:  # noqa: BLE001
                self._conf = {}
        conf = getattr(self, "_conf", {})
        if not conf.get("phone"):
            return None
        from alice.home import android as adb

        try:
            d = adb.Device(conf["phone"], "phone", adb.adb_path(conf.get("adb", "")))
            return d.notifications()
        except Exception:  # noqa: BLE001 - phone moved away/locked: try again later
            return None

    def collect(self) -> list[tuple[str, float | None, str | None]]:
        now = self.clock()
        if now - self.last_poll >= self.EVERY and (self._thread is None or not self._thread.is_alive()):
            self.last_poll = now
            if self.background:
                self._thread = threading.Thread(target=self._poll, daemon=True, name="alice-phone")
                self._thread.start()
            else:
                self._poll()
        return []

    def _poll(self) -> None:
        items = self.read()
        if items is None:
            return
        self.handle(items, self.clock())

    # ---------------------------------------------------------------- decision
    def handle(self, items: list[dict[str, Any]], now: float) -> list[str]:
        keys = {(i.get("app", ""), i.get("title", ""), i.get("text", "")) for i in items}
        if self.seen is None:
            self.seen = keys
            return []
        fresh = [k for k in keys if k not in self.seen and not k[0].startswith(SKIP)]
        self.seen = keys
        said = []
        for pkg, title, text in sorted(fresh):
            if pkg in MESSAGING:
                if now - self.app_last.get(pkg, -1e9) < APP_GAP:
                    self.pending[pkg] = self.pending.get(pkg, 0) + 1
                    continue
                self.app_last[pkg] = now
                msg = f"{MESSAGING[pkg]}, {title}: {text}".strip(": ,")[:220]
                if self.notify(Notification("Telefon", msg, "info", "phone", kind="info")):
                    said.append(msg)
            else:
                self.pending[pkg] = self.pending.get(pkg, 0) + 1
        if now - self.last_digest >= DIGEST_EVERY and self.mode() == "normal":
            self.last_digest = now
            total = sum(self.pending.values())
            if total >= DIGEST_MIN:
                parts = ", ".join(f"{_app_name(p)} {n}" for p, n in sorted(self.pending.items(), key=lambda x: -x[1])[:4])
                msg = f"Telefonda {total} bildirim birikti: {parts}."
                if self.notify(Notification("Telefon", msg, "info", "phone", kind="info")):
                    said.append(msg)
                    self.pending.clear()
        return said
