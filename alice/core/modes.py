"""Environment-based mode.

Selects the mode based on running programs and time of day. The mode determines which notifications pass,
whether voice is enabled, and whether ALICE can use the local graphics card:

  flight : DCS / War Thunder running. Only flight-related or critical things are spoken,
           local GPU is not used (heavy tasks go to the cloud).
  dev    : Unity / editor running. Normal notifications, development-focused.
  quiet  : night hours. Only warning and above, no voice speech.
  normal : everything else.

Program lists and policies can be changed via config ([core.modes]).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Iterable

SEVERITIES = ("info", "warn", "critical")

DEFAULT_PROGRAMS = {
    "flight": ["dcs.exe", "dcs_server.exe", "aces.exe"],  # aces.exe = War Thunder
    "dev": ["unity.exe", "unity hub.exe", "code.exe", "devenv.exe", "rider64.exe"],
}

DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
    "normal": {"min_severity": "info", "topics": [], "voice": True, "local_gpu": True},
    "dev": {"min_severity": "info", "topics": [], "voice": True, "local_gpu": True},
    # also speak phone messages during games (WhatsApp/SMS); bulk notification digest after the game ends
    "flight": {"min_severity": "critical", "topics": ["flight", "phone"], "voice": True, "local_gpu": False},
    "quiet": {"min_severity": "warn", "topics": [], "voice": False, "local_gpu": True},
}

PRIORITY = ("flight", "dev", "quiet", "normal")


@dataclass
class Policy:
    mode: str
    min_severity: str = "info"
    topics: list[str] = field(default_factory=list)  # if non-empty, these topics pass regardless of min_severity
    voice: bool = True
    local_gpu: bool = True

    def allows(self, severity: str, topic: str = "") -> bool:
        if topic and topic in self.topics:
            return True
        rank = SEVERITIES.index(severity) if severity in SEVERITIES else 0
        # Prevent config typos ("warning") from dropping every notification with ValueError: unknown level = info.
        floor = SEVERITIES.index(self.min_severity) if self.min_severity in SEVERITIES else 0
        return rank >= floor


def parse_hours(spec: str) -> tuple[dtime, dtime] | None:
    """'00:30-08:00' -> (00:30, 08:00). None if empty or invalid."""
    try:
        a, b = (s.strip() for s in spec.split("-", 1))
        return dtime.fromisoformat(a), dtime.fromisoformat(b)
    except (ValueError, AttributeError):
        return None


def in_hours(now: datetime, span: tuple[dtime, dtime]) -> bool:
    a, b = span
    t = now.time()
    return a <= t < b if a <= b else (t >= a or t < b)


class ModeManager:
    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        modes = cfg.get("modes", {})
        self.programs = {m: [p.lower() for p in modes.get(m, {}).get("programs", DEFAULT_PROGRAMS[m])]
                         for m in DEFAULT_PROGRAMS}
        self.quiet_hours = parse_hours(str(cfg.get("quiet_hours", "00:30-08:00")))
        self.policies = {}
        for m, base in DEFAULT_POLICIES.items():
            over = {k: v for k, v in modes.get(m, {}).items() if k in base}
            self.policies[m] = Policy(m, **{**base, **over})
        self.forced: str | None = None
        self.current = "normal"

    def detect(self, processes: Iterable[str], now: datetime | None = None) -> str:
        if self.forced:
            return self.forced
        names = {p.lower() for p in processes}
        for mode in PRIORITY:
            if mode == "quiet":
                if self.quiet_hours and in_hours(now or datetime.now(), self.quiet_hours):
                    return "quiet"
            elif mode in self.programs and names & set(self.programs[mode]):
                return mode
        return "normal"

    def update(self, processes: Iterable[str], now: datetime | None = None) -> tuple[str, bool]:
        """Recalculate the mode. Returns (mode, changed)."""
        mode = self.detect(processes, now)
        changed = mode != self.current
        self.current = mode
        return mode, changed

    @property
    def policy(self) -> Policy:
        return self.policies.get(self.current, self.policies["normal"])
