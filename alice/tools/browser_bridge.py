"""Browser bridge: a small background process that holds one persistent CDP connection to the user's browser.

With "Allow remote debugging for this browser instance" on, Opera/Chrome prompts the user on every NEW CDP connection.
Every time the ALICE core restarted, it reconnected and asked again. The bridge
outlives the core: it connects to the browser once (one prompt), and the core connects to the bridge over a local
socket. A core restart does not drop the browser link, so permission is not asked again.

Only one client (Playwright) is served at a time. The bridge renumbers message IDs (so an old client's late reply
can't leak into the new one's); when a client leaves, the sessions it opened are closed in the browser, so the next
client sees a clean connection. When the browser closes, the bridge closes too.

The browser link can die without closing (Opera woke from sleep/hibernation, a tab process crashed, the link half-
open): the socket looks open but no command gets a reply, so every ALICE browser call hung there.
So the bridge periodically sends a small command (Browser.getVersion) to the browser; if no reply comes,
it treats the link as dead and closes. The client (Playwright) sees the drop; ALICE reconnects on the next call.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

DEFAULT_PORT = 9339
PATH = "/devtools/browser/alice-bridge"
CLEANUP_BASE = 2_000_000_000  # IDs of the bridge's own commands
HEARTBEAT_ID = CLEANUP_BASE - 1  # ID of the heartbeat command (client IDs count up from 1, so they don't collide)
HEARTBEAT_EVERY = 15.0  # seconds
HEARTBEAT_TIMEOUT = 20.0  # no reply within this long counts as a miss
HEARTBEAT_MISSES = 2  # consecutive misses = dead. Dropping on one delay made Opera re-prompt for permission each time
                      # (a busy browser can reply late once; observed 2026-11-02)


# When Playwright connects, it configures the user's browser like its own test browser. In the user's own browser,
# these cause harm: downloads go to Playwright's temp folder with GUID names (then deleted); pages get forced light
# theme. The bridge does not forward these commands; it tells the client "done".
BLOCKED = {"Browser.setDownloadBehavior"}
MEDIA_FEATURES = {"prefers-color-scheme", "prefers-reduced-motion", "forced-colors", "prefers-contrast"}


def neutralize(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Should a command from the client go to the browser? If not, return None (the bridge fakes success); otherwise
    the command is rewritten into a harmless form."""
    method = msg.get("method", "")
    if method in BLOCKED:
        return None
    if method == "Emulation.setEmulatedMedia":
        params = dict(msg.get("params") or {})
        feats = [f for f in params.get("features") or [] if f.get("name") not in MEDIA_FEATURES]
        if not feats and not params.get("media"):
            return None
        params["features"] = feats
        return {**msg, "params": params}
    return msg


def endpoint(port: int = DEFAULT_PORT) -> str:
    return f"ws://127.0.0.1:{port}{PATH}"


# Bridge token: regenerated on every start, written to ~/.alice/browser-bridge-token (hidden from the sandbox).
# The Origin check stops web pages, but sandbox programs send no Origin either: without a key, they could also
# drive the user's browser (cookies, JS on every tab). ALICE's Playwright client sends the key in the header.
# The extension does not use this bridge (its own port 9340, browser_ext).
TOKEN_HEADER = "X-Alice-Bridge-Token"


def token_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "browser-bridge-token"


def read_token() -> str:
    try:
        return token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_token() -> str:
    """Generate and write a new key. The file is first written under a temp name, hidden from the sandbox, then moved
    into place, so the key is never readable."""
    token = secrets.token_urlsafe(32)
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(token, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows: chmod is not supported; the file is in the user's folder
    try:
        from alice.sandbox import hide_from_sandbox

        hide_from_sandbox(tmp)
    except Exception:  # noqa: BLE001 - if the label can't be set, the key still works
        pass
    os.replace(tmp, path)
    return token


class Bridge:
    def __init__(self, find_upstream=None, log=lambda s: None, connect=None, heartbeat: float = HEARTBEAT_EVERY,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT):
        if find_upstream is None:
            from alice.tools.browser import devtools_active_endpoint

            find_upstream = devtools_active_endpoint
        self.find_upstream = find_upstream
        self.log = log
        self._connect = connect
        self.up = None  # browser connection
        self._pump: asyncio.Task | None = None  # the event loop holds tasks only weakly: keep a reference here
        self._beat: asyncio.Task | None = None
        self.heartbeat, self.heartbeat_timeout = heartbeat, heartbeat_timeout
        self._pong = asyncio.Event()
        self.client = None
        self.gen = 0
        self.next_id = 1
        self.pending: dict[int, tuple[int, Any]] = {}  # bridge id -> (client generation, client id)
        self.sessions: set[str] = set()  # CDP sessions opened by the current client
        self.lock = asyncio.Lock()
        self.closed = asyncio.Event()

    async def _upstream(self):
        if self.up is not None:
            return self.up
        url = self.find_upstream()
        if not url:
            raise ConnectionError("tarayıcıda uzaktan hata ayıklama açık değil")
        connect = self._connect
        if connect is None:
            from websockets.asyncio.client import connect
        # The browser asks the user for permission here, once: give them time to answer.
        self.up = await connect(url, open_timeout=120, max_size=None, ping_interval=None)
        self.log(f"tarayıcıya bağlanıldı: {url}")
        loop = asyncio.get_running_loop()
        self._pump = loop.create_task(self._pump_up(self.up))
        if self.heartbeat > 0:
            self._beat = loop.create_task(self._heartbeat(self.up))
        return self.up

    async def _heartbeat(self, up) -> None:
        """Is the browser still replying? If it misses replies several times in a row, close the link (otherwise
        the client waits forever)."""
        misses = 0
        while self.up is up:
            await asyncio.sleep(self.heartbeat)
            if self.up is not up:
                return
            self._pong.clear()
            try:
                await asyncio.wait_for(up.send(json.dumps({"id": HEARTBEAT_ID, "method": "Browser.getVersion"})),
                                       self.heartbeat_timeout)
                await asyncio.wait_for(self._pong.wait(), self.heartbeat_timeout)
                misses = 0
            except Exception:  # noqa: BLE001 - timeout or broken socket
                misses += 1
                if misses < HEARTBEAT_MISSES:
                    self.log(f"tarayıcı {self.heartbeat_timeout:.0f} sn cevap vermedi; bir kez daha bekleniyor")
                    continue
                self.log(f"tarayıcı üst üste {misses} kez cevap vermedi; bağlantı ölü sayılıyor")
                await self._shutdown(up)
                return

    async def _shutdown(self, up) -> None:
        """Drop the dead browser link, disconnect the client, and close the bridge. The closing handshake can also
        hang, so each step is time-limited."""
        if self.up is up:
            self.up = None
        for ws in (up, self.client):
            if ws is None:
                continue
            try:
                await asyncio.wait_for(ws.close(), 3)
            except Exception:  # noqa: BLE001
                transport = getattr(ws, "transport", None)
                if transport is not None:
                    transport.abort()
        self.closed.set()

    async def _pump_up(self, up) -> None:
        try:
            async for raw in up:
                await self._from_browser(raw)
        except Exception as e:  # noqa: BLE001
            self.log(f"tarayıcı bağlantısı koptu: {e}")
        finally:
            self.log("tarayıcı kapandı; köprü kapanıyor")
            await self._shutdown(up)

    async def _from_browser(self, raw) -> None:
        msg = json.loads(raw)
        if "id" in msg:
            if msg["id"] == HEARTBEAT_ID:
                self._pong.set()
                return
            ref = self.pending.pop(msg["id"], None)
            if ref is None or ref[0] != self.gen:
                return  # the bridge's own command, or a reply from an old client
            msg["id"] = ref[1]
            sid = (msg.get("result") or {}).get("sessionId")
            if sid:
                self.sessions.add(sid)
        else:
            method, params = msg.get("method", ""), msg.get("params") or {}
            if method == "Target.attachedToTarget" and params.get("sessionId"):
                if msg.get("sessionId") and msg["sessionId"] not in self.sessions:
                    return
                self.sessions.add(params["sessionId"])
            elif method == "Target.detachedFromTarget":
                self.sessions.discard(params.get("sessionId"))
            if msg.get("sessionId") and msg["sessionId"] not in self.sessions:
                return  # event left over from an old client's session
            if self.client is None:
                return
        client = self.client
        if client is not None:
            try:
                await client.send(json.dumps(msg))
            except Exception:  # noqa: BLE001
                pass

    async def _cleanup(self) -> None:
        """Clean up after the departing client: close its sessions and stop auto-attach."""
        up = self.up
        if up is None:
            return
        cmds: list[dict[str, Any]] = [{"method": "Target.detachFromTarget", "params": {"sessionId": s}}
                                      for s in self.sessions]
        cmds += [{"method": "Target.setAutoAttach", "params": {"autoAttach": False, "waitForDebuggerOnStart": False,
                                                                "flatten": True}},
                 {"method": "Target.setDiscoverTargets", "params": {"discover": False}},
                 # if an older bridge/client version routed downloads to a temp folder, restore the browser's
                 # own setting
                 {"method": "Browser.setDownloadBehavior", "params": {"behavior": "default"}}]
        self.sessions = set()
        for i, c in enumerate(cmds):
            c["id"] = CLEANUP_BASE + i
            try:
                await up.send(json.dumps(c))
            except Exception:  # noqa: BLE001
                return

    async def handle(self, ws) -> None:
        async with self.lock:
            old = self.client
            if old is not None:
                await old.close()
            if self.closed.is_set():
                # The browser link died and the bridge is closing: don't reconnect and prompt again; the client
                # starts a new bridge.
                await ws.close(1011, "köprü kapanıyor")
                return
            try:
                up = await self._upstream()
            except Exception as e:  # noqa: BLE001
                self.log(f"tarayıcıya bağlanılamadı: {e}")
                await ws.close(1011, str(e)[:120])
                return
            await self._cleanup()
            self.gen += 1
            gen = self.gen
            self.pending.clear()
            self.client = ws
        self.log("istemci bağlandı")
        try:
            async for raw in ws:
                msg = neutralize(json.loads(raw))
                if msg is None:  # not forwarded to the browser: don't make the client wait
                    orig = json.loads(raw)
                    if "id" in orig:
                        reply = {"id": orig["id"], "result": {}}
                        if orig.get("sessionId"):
                            reply["sessionId"] = orig["sessionId"]
                        await ws.send(json.dumps(reply))
                    continue
                if "id" in msg:
                    bid = self.next_id
                    self.next_id += 1
                    self.pending[bid] = (gen, msg["id"])
                    msg["id"] = bid
                await up.send(json.dumps(msg))
        except Exception:  # noqa: BLE001 - client disconnected
            pass
        finally:
            async with self.lock:
                if self.client is ws:
                    self.client = None
                    self.gen += 1
                    await self._cleanup()
            self.log("istemci ayrıldı")


LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _health(connection, request, bridge: Bridge | None = None, token: str | None = None):
    # The bridge gives full CDP access to the user's browser (cookies, JS on every tab). Browsers send Origin in
    # every WebSocket handshake; Playwright/websockets clients don't. An Origin-bearing request comes from a web
    # page: rejected.
    # The Host check blocks DNS rebinding. If a token is set, the connection requires the key.
    # (the /alice-bridge health check needs no key: it gives no browser access).
    headers = getattr(request, "headers", None) or {}
    if headers.get("Origin") is not None:
        return connection.respond(403, "forbidden\n")
    host = headers.get("Host")
    if host is not None and host.rsplit(":", 1)[0].lower() not in LOCAL_HOSTS:
        return connection.respond(403, "forbidden\n")
    if request.path == "/alice-bridge":
        if bridge is not None and bridge.closed.is_set():
            return connection.respond(503, "closing\n")  # don't connect to a closing bridge; let a new one be started
        return connection.respond(200, "alice-bridge\n")
    if token and not hmac.compare_digest(str(headers.get(TOKEN_HEADER) or "").encode(), token.encode()):
        return connection.respond(403, "forbidden\n")
    return None


async def serve(port: int, log=print, bridge: Bridge | None = None, auth: bool = True) -> None:
    from websockets.asyncio.server import serve as ws_serve

    bridge = bridge or Bridge(log=log)
    key: dict[str, str | None] = {"token": None}
    async with ws_serve(bridge.handle, "127.0.0.1", port, max_size=None, ping_interval=None,
                        process_request=lambda c, r: _health(c, r, bridge, key["token"] if auth else None)):
        # The key is written AFTER the port is acquired (same step, no request in between): if the port is taken
        # (another bridge is running), its key must not be overwritten.
        if auth:
            key["token"] = write_token()
        log(f"köprü dinliyor: {endpoint(port)}")
        await bridge.closed.wait()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="ALICE tarayıcı köprüsü")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = p.parse_args(argv)
    from alice.config import alice_home

    path = alice_home() / "browser-bridge.log"

    def log(s: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {s}\n")
        except OSError:
            pass  # logging must not break the bridge

    try:
        asyncio.run(serve(a.port, log))
    except OSError as e:  # port in use: another bridge is already running
        log(f"köprü başlatılamadı: {e}")


if __name__ == "__main__":
    main()
