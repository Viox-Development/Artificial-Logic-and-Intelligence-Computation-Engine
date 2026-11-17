"""Starts the core HUD process and restarts it if it crashes. Even if the HUD crashes, ALICE keeps talking."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

CHECK_EVERY = 1.0       # a crashed HUD comes back at the latest after this long + the restart time (target: 5 sec)
BURST, BURST_WINDOW, BURST_WAIT = 5, 60.0, 30.0   # after 5 crashes within a minute, wait 30 sec (so it doesn't loop)
CREATE_NO_WINDOW = 0x08000000


def python_exe() -> str:
    """Python that opens no console window (pythonw.exe); otherwise the running Python."""
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


class HudSupervisor:
    def __init__(self, port: int, spawn: Callable[[list[str]], Any] | None = None, check_every: float = CHECK_EVERY):
        self.port = port
        self.spawn = spawn or self._spawn
        self.check_every = check_every
        self.proc: Any = None
        self.restarts = 0
        self._starts: deque[float] = deque()
        self._wait_until = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="alice-hud-supervisor")

    def argv(self) -> list[str]:
        return [python_exe(), "-m", "alice", "hud", "--port", str(self.port), "--parent", str(os.getpid())]

    @staticmethod
    def _spawn(argv: list[str]):
        root = Path(__file__).resolve().parent.parent.parent  # so `-m alice` finds the core's code
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)

    def start(self) -> "HudSupervisor":
        self.check()
        self._thread.start()
        return self

    def check(self, now: float | None = None) -> None:
        """Start the HUD if it is not running (waiting a little if it is in a crash flood)."""
        now = time.monotonic() if now is None else now
        if self.proc is not None and self.proc.poll() is None:
            return
        if now < self._wait_until:
            return
        while self._starts and now - self._starts[0] > BURST_WINDOW:
            self._starts.popleft()
        if len(self._starts) >= BURST:
            self._starts.clear()
            self._wait_until = now + BURST_WAIT
            return
        if self.proc is not None:
            self.restarts += 1
        self._starts.append(now)
        try:
            self.proc = self.spawn(self.argv())
        except OSError:
            self.proc = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _run(self) -> None:
        while not self._stop.wait(self.check_every):
            self.check()

    def stop(self) -> None:
        self._stop.set()
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(3)
            except Exception:  # noqa: BLE001 - while shutting down, a stubborn HUD must not make the core wait
                pass
