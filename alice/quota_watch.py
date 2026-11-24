"""Subscription quota warning: when one of the cliproxy accounts (Claude 5-hour / weekly, ChatGPT windows) is about
to run out, tell the user once. The proxy and router already switch between accounts; what was missing was making it
visible before the quota ran out (work was noticed only when it became slow or fell back to a free model).

Each threshold is reported once for each window (account + window + reset time); again after the window resets.
The proxy is asked at most once every `interval` seconds, and this is done in the background at the end of each turn."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

from alice.i18n import _

THRESHOLDS = (80, 95)
INTERVAL = 300.0  # sec


def proxy_accounts() -> list[dict[str, Any]]:
    """cliproxy accounts (same format as the card summary). Empty if the proxy is off; it does not try to start it."""
    from alice.app import cliproxy

    if not cliproxy.settings().get("installed"):
        return []
    data = cliproxy._req("GET", "/v8/management/credentials")  # noqa: SLF001
    files = data.get("files") if isinstance(data, dict) else None
    return [cliproxy._account(f) for f in files or [] if isinstance(f, dict)]  # noqa: SLF001


class QuotaWatch:
    def __init__(self, notify: Callable[[str], None], fetch: Callable[[], list[dict[str, Any]]] = proxy_accounts,
                 interval: float = INTERVAL, thresholds: tuple[int, ...] = THRESHOLDS):
        self.notify, self.fetch, self.interval, self.thresholds = notify, fetch, interval, tuple(sorted(thresholds))
        self._seen: dict[tuple[str, str, int], int] = {}
        self._last = 0.0
        self._lock = threading.Lock()

    def maybe_check(self, now: float | None = None) -> bool:
        """If it is time, check in the background. Returns True if a check was started."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._last and now - self._last < self.interval:
                return False
            self._last = now
        threading.Thread(target=self._run, daemon=True, name="alice-quota-watch").start()
        return True

    def _run(self) -> None:
        try:
            for msg in self.check(self.fetch()):
                self.notify(msg)
        except Exception:  # noqa: BLE001 - proxy off / broken response: no warning, ALICE is not affected
            pass

    def check(self, accounts: list[dict[str, Any]]) -> list[str]:
        """Warning texts for newly crossed thresholds."""
        out: list[str] = []
        for acc in accounts:
            if acc.get("disabled"):
                continue
            for w in (acc.get("quota") or {}).get("windows") or []:
                pct = int(w.get("percent") or 0)
                level = max((t for t in self.thresholds if pct >= t), default=0)
                key = (str(acc.get("name") or acc.get("email")), str(w.get("label")), int(w.get("reset") or 0))
                if not level or self._seen.get(key, 0) >= level:
                    continue
                self._seen[key] = level
                out.append(self._message(acc, w, pct, accounts))
        return out

    @staticmethod
    def _message(acc: dict[str, Any], w: dict[str, Any], pct: int, accounts: list[dict[str, Any]]) -> str:
        service = acc.get("provider_name") or acc.get("provider") or _("account")
        reset = int(w.get("reset") or 0)
        when = _(", resets {when}", when=datetime.fromtimestamp(reset).strftime("%d.%m %H:%M")) if reset else ""
        others = [a for a in accounts if a is not acc and a.get("provider") == acc.get("provider") and a.get("ready")
                  and int((a.get("quota") or {}).get("percent") or 0) < 80]
        tail = (_(" Another {service} account is ready; requests move to it when this one is full.", service=service) if others else
                _(" No other {service} account is ready; when it is full, requests fall back to the next model.", service=service))
        return _("Quota warning: the {service} account {account} used {pct}% of its {window} quota{when}.",
                 service=service, account=acc.get("email") or acc.get("name"), window=w.get("label"), pct=pct, when=when) + tail
