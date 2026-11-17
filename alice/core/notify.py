"""Notification channels.

Notifier first passes each notification through the mode policy: any that do not pass are recorded
as "muted" (so one can ask later, "what happened while I was in-game?"), and those that pass go to each channel.
Channels: desktop notification (Windows toast / macOS / notify-send) and each added
callable (system tray, ALICE's voice in the future).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from alice.core.modes import Policy
from alice.core.store import Store


@dataclass
class Notification:
    title: str
    body: str = ""
    severity: str = "info"  # info | warn | critical
    topic: str = ""         # e.g. "flight", "pc", "task", "health"
    kind: str = "alert"     # alert | task | mode | info | flight
    emotion: str = ""       # if empty, voice chooses the tone itself from severity and topic


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def desktop_notify(n: Notification) -> bool:
    """Show the operating system's notification toast. False if failed."""
    try:
        if sys.platform == "win32":
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]"
                " | Out-Null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$x=$t.GetElementsByTagName('text');$x.Item(0).InnerText={_ps_quote(n.title)};"
                f"$x.Item(1).InnerText={_ps_quote(n.body)};"
                "$app='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
                "[Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=15,
                               creationflags=flags)
            return r.returncode == 0
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e", f"display notification {n.body!r} with title {n.title!r}"],
                               capture_output=True, timeout=10)
            return r.returncode == 0
        if shutil.which("notify-send"):
            urgency = {"critical": "critical", "warn": "normal"}.get(n.severity, "low")
            r = subprocess.run(["notify-send", "-u", urgency, "-a", "ALICE", n.title, n.body], capture_output=True,
                               timeout=10)
            return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    return False


# Notifier of the core running in this process (app runs inside core): notify tool sends from here.
ACTIVE: "Notifier | None" = None


class Notifier:
    def __init__(self, store: Store, policy: Callable[[], Policy], desktop: bool = True):
        self.store = store
        self.policy = policy
        self.channels: list[Callable[[Notification, Policy], None]] = []
        self._toasted: dict[str, float] = {}
        self._muted: dict[str, float] = {}
        if desktop:
            self.channels.append(self._desktop)
        global ACTIVE
        ACTIVE = self

    TOAST_REPEAT = 600.0  # sec: the same notification toast is not shown again within this period
    MUTED_REPEAT = 1800.0  # sec: the same muted notification is not recorded again within this period

    def _desktop(self, n: Notification, pol: Policy) -> None:
        """Windows notification toast. Flight announcements are spoken, toasts spammed during games: do not show.
        Also do not show if the same notification arrives again within 10 min."""
        if n.kind == "flight" or n.topic == "flight":
            return
        key = f"{n.title}|{n.body}"
        now = time.monotonic()
        if now - self._toasted.get(key, -1e9) < self.TOAST_REPEAT:
            return
        self._toasted[key] = now
        desktop_notify(n)

    def send(self, n: Notification) -> bool:
        """Send notification. False if muted by mode policy."""
        pol = self.policy()
        ok = pol.allows(n.severity, n.topic)
        if ok:  # voice first: flight announcements should not wait if the log database is busy
            for ch in self.channels:
                try:
                    ch(n, pol)
                except Exception:  # keep other channels running even if one crashes
                    pass
        if not ok:
            # A muted report is retried on each cycle (health report every 10 min during flight): prevent event log
            # from filling with the same line on each retry (Nov 1: 36 "health report", 24 "weekly summary" in 2 days).
            key, now = f"{n.title}|{n.body}", time.monotonic()
            if now - self._muted.get(key, -1e9) < self.MUTED_REPEAT:
                return ok
            self._muted[key] = now
        try:
            self.store.add_event(n.kind, n.title, n.body, n.severity, n.topic, delivered=ok)
        except Exception:  # noqa: BLE001 - notification was delivered even if log write failed
            pass
        return ok
