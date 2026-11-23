"""The application lives inside the core.

Previously, `alice app` set up everything from scratch in its own process on every launch (router, tools, MCP servers,
chat): the app felt like reloading from scratch every time, and was unaware of the background core (closing the core
left the window open). Now:

* The core (`alice core run`) sets up the app server (AppState + AppServer) on startup and keeps it running; writes its
  address to ~/.alice/app.json.
* `alice app`, if the core is running, simply opens the window to that address: opens instantly, chat stays where it was.
  If the core is down, it starts it first. Closing the window does not stop the core (it keeps running in the background).
* When the core shuts down, the window closes too. If the core restarts for an update, the window does not close,
  it switches to the new core's address.

`alice app --standalone` (or config [app] in_core = false) is the legacy behavior: the app in its own process.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

RESTART_GRACE = 90.0  # s: the window waits this long for the new address while the core restarts
ATTACH_WAIT = 60.0    # s: timeout waiting for the app server to be ready after launching an inactive core


def info_path() -> Path:
    return alice_home() / "app.json"


def restarting_path() -> Path:
    return alice_home() / "core-restarting"


def _alive(pid: Any) -> bool:
    try:
        import psutil

        return bool(pid) and psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001 - assume alive if unsure: prevent the window from closing needlessly
        return True


def read_url() -> str:
    """Address of the app server in the core; "" if core is absent or server is not yet ready."""
    try:
        data = json.loads(info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or not _alive(data.get("pid")):
        return ""
    return str(data.get("url") or "")


def mark_restarting() -> None:
    """The core is restarting itself: keep open window alive, wait for the new address."""
    try:
        restarting_path().write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass  # the window then waits for the new address without the marker


def restarting() -> bool:
    try:
        return time.time() - float(restarting_path().read_text(encoding="utf-8").strip()) < RESTART_GRACE
    except (OSError, ValueError):
        return False


REVIVE_WINDOW = 600.0  # s
REVIVE_MAX = 3         # at most this many automatic restarts in this window (prevents crash loops)
_revived: list[float] = []


def revive_crashed(start: Callable[[], bool] | None = None) -> bool:
    """Restart if the core died without a clean shutdown. True if restarted.

    On a clean shutdown (tray Exit, app Stop) the core deletes core-state.json. If the file exists but no process
    is found, the core crashed or was killed externally: previously the window closed too ("ALICE keeps crashing
    and closing"), now the core restarts and the window switches to the new address.
    """
    from alice.core import service

    try:
        data = json.loads(service.state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or _alive(data.get("pid")):
        return False
    now = time.time()
    _revived[:] = [t for t in _revived if now - t < REVIVE_WINDOW]
    if len(_revived) >= REVIVE_MAX:
        return False
    _revived.append(now)
    try:
        service.state_path().unlink()
    except OSError:
        pass  # stale state file; overwritten on the next start
    mark_restarting()
    try:
        return bool((start or service.start_detached)())
    except Exception:  # noqa: BLE001 - if it fails to start, the window closes as before
        return False


def follow(url: str) -> Callable[[], str | None]:
    """Watcher for the window: "" = continue as is, new address = switch to it, None = core stopped, close window."""
    cur = [url]

    def check() -> str | None:
        u = read_url()
        if u == cur[0]:
            return ""
        if u:
            cur[0] = u
            return u
        return "" if restarting() or revive_crashed() else None

    return check


def attach(start: Callable[[], bool] | None = None, wait: float = ATTACH_WAIT) -> str:
    """Address of the app in the core. Starts the core if stopped and waits for readiness. "" on failure."""
    url = read_url()
    if url:
        return url
    from alice.core import service

    if service.read_state().get("running") and _alive(service.read_state().get("pid")):
        # Core is running but address is missing: either still setting up server or an older version without app host.
        wait = min(wait, 20.0)
    elif not (start or service.start_detached)():
        return ""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.3)
        url = read_url()
        if url:
            return url
    return ""


class AppHost:
    """App server inside the core. Setup runs in the background: does not block core startup."""

    def __init__(self, cfg: Any, cwd: Path | None = None, restart: Callable[[], None] | None = None):
        self.cfg = cfg
        self.cwd = cwd
        self.restart = restart
        self.state: Any = None
        self.server: Any = None
        self.error = ""
        self._stopped = False
        self._lock = threading.Lock()

    def start(self, background: bool = True) -> "AppHost":
        if background:
            threading.Thread(target=self._build, daemon=True, name="alice-app-host").start()
        else:
            self._build()
        return self

    def _build(self) -> None:
        from alice.app.server import AppServer, AppState

        try:
            if self.cwd is None:
                from alice.cli import default_workspace

                self.cwd = default_workspace()
            state = AppState(self.cfg, Path(self.cwd).resolve(), self.cfg.agent.get("permission_mode", "auto"))
            state.restart_hook = self.restart  # UI "Update": restarts the core, not just the app
            server = AppServer(state).start()
        except Exception as e:  # noqa: BLE001 - if the app fails to initialize, let the core keep running
            self.error = repr(e)[:300]
            return
        with self._lock:
            if self._stopped:
                server.stop()
                return
            self.state, self.server = state, server
        state.watch_updates(None)
        self.publish()

    def publish(self) -> None:
        """Write address to app.json. Called again when the phone bridge replaces the shared state token with its
        own persistent token; open window switches to the new address."""
        if self.server is None:
            return
        try:
            info_path().write_text(json.dumps({"url": self.server.url, "pid": os.getpid()}), encoding="utf-8")
            restarting_path().unlink(missing_ok=True)
        except OSError:
            pass  # the window finds the server by port instead

    @property
    def busy(self) -> bool:
        """Whether a chat is currently running in the app (core should not restart for update during this)."""
        st = self.state
        return bool(st is not None and (st.agent.busy or st._queued or st.running()))

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            server = self.server
        try:
            data = json.loads(info_path().read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                info_path().unlink()
        except (OSError, ValueError, AttributeError):
            pass  # already gone or written by another process
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001 - shutdown errors should not prevent the core from stopping
                pass
