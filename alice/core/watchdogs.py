"""The core's two small watchdogs.

GameHealth: while in game/flight mode, speaks up if the computer really reaches a critical level: GPU or CPU
overheating, GPU memory or RAM filling up (it also names the background program using the most RAM: "Discord
1.8 GB"). In a game the GPU sits at 85-88 degrees for hours (normal): speaking at 85 meant saying "GPU
86 degrees" every 15 minutes (Nov 1, 23 times). Limits now sit where the card starts slowing itself down. A condition
does not speak until it has lasted two minutes; the same warning repeats at most every 30 minutes, and it is re-armed
once the value drops 5 below the limit.

NetWatch: pings the internet connection in the background every 30 sec (TCP to 1.1.1.1 and 8.8.8.8). Reports drops
longer than 45 sec and the return (with how long it was down), and records each outage as a "net" event; counts them
in the weekly summary.

SystemGauges: while the HUD is open, shows a value nearing these limits as a small bar in the corner (it does not
speak). It does not replace the voice alerts, it adds to them; when the HUD is closed it never connects to the core.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from alice.core.notify import Notification

# (key, limit, text) — text takes the value via {v}
GAME_LIMITS = (
    ("pc.gpu.temp", 93.0, "Ekran kartı {v:.0f} derece, kritik: kart kendini yavaşlatmaya başlar."),
    ("pc.cpu.temp", 95.0, "İşlemci {v:.0f} derece, kritik: oyun takılabilir."),
    ("pc.gpu.mem_percent", 98.0, "Ekran kartı belleği yüzde {v:.0f} dolu, takılma başlayabilir."),
    ("pc.mem.percent", 95.0, "RAM yüzde {v:.0f} dolu, oyun takılabilir."),
)
GAME_HOLD = 120.0
GAME_REPEAT = 1800.0
GAME_PROCS = ("aces", "dcs", "robloxplayerbeta")  # don't name the game itself in the RAM suggestion
# HUD bar: (key, name, unit, value where it appears). The limit comes from GAME_LIMITS; temperatures start at 90%
# of the limit (GPU 84°C): normal in-game play is 85-88, so the bar would stay on all the time otherwise.
GAUGES = (
    ("pc.gpu.temp", "GPU", "°C", 88.0),
    ("pc.cpu.temp", "CPU", "°C", 90.0),
    ("pc.gpu.mem_percent", "VRAM", "%", 95.0),
    ("pc.mem.percent", "RAM", "%", 90.0),
)


class GameHealth:
    def __init__(self, store, notify: Callable[[Notification], bool]):
        self.store = store
        self.notify = notify
        self.since: dict[str, float] = {}
        self.said: dict[str, float] = {}
        self.armed: dict[str, bool] = {}

    def check(self, mode: str, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        out: list[str] = []
        if mode != "flight":
            self.since.clear()
            return out
        for key, limit, text in GAME_LIMITS:
            last = self.store.last(key)
            v = last[1] if last and last[1] is not None and now - last[0] < 120 else None
            if v is None:
                continue
            if v < limit - 5:
                self.armed[key] = True
            if v < limit:
                self.since.pop(key, None)
                continue
            start = self.since.setdefault(key, now)
            since_said = now - self.said.get(key, float("-inf"))
            # even if re-armed, no repeat within 10 min: a value bouncing around the limit must not keep speaking
            if now - start < GAME_HOLD or since_said < GAME_REPEAT / 3 or (
                    not self.armed.get(key, True) and since_said < GAME_REPEAT):
                continue
            msg = text.format(v=v)
            if key == "pc.mem.percent":
                hog = self.top_process(now)
                if hog:
                    msg += f" En çok {hog[0]} kullanıyor ({hog[1] / 1024:.1f} GB), kapatabilirsin."
            if self.notify(Notification("Bilgisayar zorlanıyor", msg, "warn", "flight", kind="flight",
                                        emotion="serious")):
                self.said[key] = now
                self.armed[key] = False
                out.append(msg)
        return out

    def top_process(self, now: float) -> tuple[str, float] | None:
        best = None
        for key in self.store.keys("pc.proc.*.mem_mb", since=now - 120):
            name = key[len("pc.proc."):-len(".mem_mb")]
            if name.removesuffix(".exe") in GAME_PROCS or name in ("memcompression", "system"):
                continue
            last = self.store.last(key)
            if last and last[1] is not None and (best is None or last[1] > best[1]):
                best = (name.removesuffix(".exe").capitalize(), float(last[1]))
        return best if best and best[1] >= 700 else None


class SystemGauges:
    """Core hook (called every turn): sends a HUD bar for a value nearing its limit.
    It is re-sent every turn with the same id; once the value drops, sending stops
    and the bar vanishes by itself when its ttl runs out (about 1.5 turns)."""

    def __init__(self, store, interval: float = 15.0, clock: Callable[[], float] = time.time):
        self.store = store
        self.ttl = max(5.0, 1.5 * interval + 5)
        self.clock = clock
        self.limits = {key: limit for key, limit, _ in GAME_LIMITS}

    def __call__(self, policy=None) -> list[str]:
        from alice.hud import client, views

        if not client.active:
            return []
        now = self.clock()
        shown = []
        for key, name, unit, start in GAUGES:
            last = self.store.last(key)
            if not last or last[1] is None or now - last[0] >= 120:  # a stale sample must not open a bar
                continue
            if views.system_gauge(name, float(last[1]), self.limits[key], unit, warn=start, ttl=self.ttl):
                shown.append(name)
        return shown


def _online(hosts=(("1.1.1.1", 443), ("8.8.8.8", 53)), timeout: float = 2.0) -> float | None:
    """Connection time (ms); None if none of the hosts can be reached."""
    for host in hosts:
        t0 = time.monotonic()
        try:
            with socket.create_connection(host, timeout=timeout):
                return (time.monotonic() - t0) * 1000
        except OSError:
            continue
    return None


class NetWatch:
    name = "internet"
    EVERY = 30.0
    DOWN_AFTER = 45.0

    def __init__(self, store, notify: Callable[[Notification], bool], probe: Callable[[], float | None] = _online,
                 clock: Callable[[], float] = time.time, background: bool = True):
        self.store = store
        self.notify = notify
        self.probe = probe
        self.clock = clock
        self.background = background
        self.last_probe = float("-inf")
        self.result: float | None | bool = False  # False: not measured yet
        self.down_since: float | None = None
        self.announced = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _run_probe(self) -> None:
        r = self.probe()
        with self._lock:
            self.result = r

    def collect(self) -> list[tuple[str, float | None, str | None]]:
        now = self.clock()
        if now - self.last_probe >= self.EVERY and (self._thread is None or not self._thread.is_alive()):
            self.last_probe = now
            if self.background:
                self._thread = threading.Thread(target=self._run_probe, daemon=True, name="alice-net-probe")
                self._thread.start()
            else:
                self._run_probe()
        with self._lock:
            r = self.result
        if r is False:
            return []
        self._track(r, now)
        return [("net.online", 0.0 if r is None else 1.0, None)] + ([("net.latency_ms", float(r), None)] if r else [])

    def _track(self, latency: float | None, now: float) -> None:
        if latency is None:
            if self.down_since is None:
                self.down_since = now
            if not self.announced and now - self.down_since >= self.DOWN_AFTER:
                self.announced = True
                self.notify(Notification("İnternet koptu", "İnternet bağlantısı yok, bilgisayar dışarıya ulaşamıyor.",
                                         "warn", "net", kind="alert"))
            return
        if self.down_since is not None:
            mins = (now - self.down_since) / 60
            if self.announced:
                self.notify(Notification("İnternet geri geldi", f"Bağlantı geri geldi, yaklaşık {max(1, round(mins))} "
                                         "dakika kesikti.", "info", "net", kind="alert"))
                self.store.add_event("net", "İnternet kesintisi", f"{mins:.1f} dk", "info", "net",
                                     ts=self.down_since)
            self.down_since = None
            self.announced = False


def outages(store, since: float) -> tuple[int, float]:
    """Outage count and total minutes (for the weekly summary)."""
    evs = [e for e in store.events(since=since, kind="net", limit=500) if e.title == "İnternet kesintisi"]
    total = 0.0
    for e in evs:
        try:
            total += float(e.body.split()[0])
        except (ValueError, IndexError):
            pass
    return len(evs), total

