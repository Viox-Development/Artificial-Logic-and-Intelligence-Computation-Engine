import asyncio
import json
from types import SimpleNamespace

import pytest

from alice.tools import browser_bridge as bb


def test_bridge_keeps_one_browser_connection_across_clients():
    pytest.importorskip("websockets")  # "voice" extra; CI installs it
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    seen: list[dict] = []
    upstream_conns = []

    async def browser(ws):
        upstream_conns.append(ws)
        async for raw in ws:
            msg = json.loads(raw)
            seen.append(msg)
            result = {"sessionId": "S1"} if msg["method"] == "Target.attachToBrowserTarget" else {}
            await ws.send(json.dumps({"id": msg["id"], "result": result}))

    async def main():
        async with serve(browser, "127.0.0.1", 0) as up:
            port = up.sockets[0].getsockname()[1]
            bridge = bb.Bridge(find_upstream=lambda: f"ws://127.0.0.1:{port}")
            async with serve(bridge.handle, "127.0.0.1", 0, process_request=bb._health) as srv:
                url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}{bb.PATH}"
                answers = []
                for _ in range(2):  # the core started twice
                    async with connect(url) as c:
                        await c.send(json.dumps({"id": 1, "method": "Target.attachToBrowserTarget"}))
                        answers.append(json.loads(await c.recv()))
                    await asyncio.sleep(0.1)
        return answers

    answers = asyncio.run(main())
    assert len(upstream_conns) == 1  # one connection to the browser: permission is asked once
    assert [a["id"] for a in answers] == [1, 1]  # the client gets back its own id
    ids = [m["id"] for m in seen if m["method"] == "Target.attachToBrowserTarget"]
    assert len(set(ids)) == 2  # ids sent to the browser don't collide
    assert {"method": "Target.detachFromTarget", "params": {"sessionId": "S1"}}.items() <= next(
        m for m in seen if m["method"] == "Target.detachFromTarget").items()


def test_bridge_drops_stale_answers_and_sessions():
    b = bb.Bridge(find_upstream=lambda: None)
    sent = []

    class Client:
        async def send(self, raw):
            sent.append(json.loads(raw))

    async def main():
        b.client = Client()
        b.gen = 2
        b.pending = {10: (1, 5), 11: (2, 6)}
        b.sessions = {"NEW"}
        await b._from_browser(json.dumps({"id": 10, "result": {}}))  # answer from the old client
        await b._from_browser(json.dumps({"id": 11, "result": {}}))
        await b._from_browser(json.dumps({"method": "Page.loadEventFired", "sessionId": "OLD", "params": {}}))
        await b._from_browser(json.dumps({"method": "Page.loadEventFired", "sessionId": "NEW", "params": {}}))

    asyncio.run(main())
    assert sent == [{"id": 6, "result": {}}, {"method": "Page.loadEventFired", "sessionId": "NEW", "params": {}}]


def test_user_endpoint_prefers_bridge(monkeypatch):
    from alice.tools import browser

    monkeypatch.setattr(browser, "devtools_active_endpoint", lambda dirs=None: "ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setattr(browser, "ensure_bridge", lambda port, wait=8.0: True)
    assert browser.user_browser_endpoint(None, 9339) == bb.endpoint(9339)
    monkeypatch.setattr(browser, "ensure_bridge", lambda port, wait=8.0: False)
    assert browser.user_browser_endpoint(None, 9339) == "ws://127.0.0.1:9333/devtools/browser/x"
    assert browser.user_browser_endpoint(None, None) == "ws://127.0.0.1:9333/devtools/browser/x"


def test_bridge_keeps_reference_to_upstream_pump_task():
    # The event loop holds tasks only by weak reference: an unreferenced pump task can be garbage-collected.
    class FakeUp:
        def __init__(self):
            self.gate = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.gate.wait()
            raise StopAsyncIteration

    async def fake_connect(url, **kw):
        return FakeUp()

    async def main():
        bridge = bb.Bridge(find_upstream=lambda: "ws://x", connect=fake_connect)
        up = await bridge._upstream()
        task = bridge._pump
        assert isinstance(task, asyncio.Task) and not task.done()
        up.gate.set()
        await asyncio.wait_for(bridge.closed.wait(), 2)
        return task

    assert asyncio.run(main()).done()


def test_bridge_closes_when_browser_stops_answering():
    # The socket is open but the browser isn't answering (Opera woke from sleep, half-open connection): the bridge
    # must notice and close, otherwise the client waits forever.
    class SilentUp:
        def __init__(self):
            self.sent: list[dict] = []
            self.gate = asyncio.Event()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.gate.wait()
            raise StopAsyncIteration

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def close(self):
            self.closed = True
            self.gate.set()

    up = SilentUp()

    async def fake_connect(url, **kw):
        return up

    class Client:
        closed = False

        async def close(self):
            Client.closed = True

    async def main():
        bridge = bb.Bridge(find_upstream=lambda: "ws://x", connect=fake_connect, heartbeat=0.05,
                           heartbeat_timeout=0.1)
        bridge.client = Client()
        await bridge._upstream()
        await asyncio.wait_for(bridge.closed.wait(), 3)
        return bridge

    bridge = asyncio.run(main())
    assert up.closed and Client.closed and bridge.up is None
    assert any(m.get("id") == bb.HEARTBEAT_ID and m["method"] == "Browser.getVersion" for m in up.sent)


def test_bridge_stays_up_while_browser_answers_heartbeat():
    class EchoUp:
        def __init__(self, bridge_ref):
            self.bridge_ref = bridge_ref
            self.gate = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.gate.wait()
            raise StopAsyncIteration

        async def send(self, raw):
            msg = json.loads(raw)
            await self.bridge_ref[0]._from_browser(json.dumps({"id": msg["id"], "result": {}}))

        async def close(self):
            self.gate.set()

    ref: list = []

    async def fake_connect(url, **kw):
        return EchoUp(ref)

    async def main():
        bridge = bb.Bridge(find_upstream=lambda: "ws://x", connect=fake_connect, heartbeat=0.02,
                           heartbeat_timeout=0.2)
        ref.append(bridge)
        up = await bridge._upstream()
        await asyncio.sleep(0.3)  # a few heartbeats
        alive = not bridge.closed.is_set() and bridge.up is up
        await up.close()
        await asyncio.wait_for(bridge.closed.wait(), 2)
        return alive

    assert asyncio.run(main())


def test_closing_bridge_refuses_new_clients_and_reports_unhealthy():
    class WS:
        def __init__(self):
            self.closed_with = None

        async def close(self, code=1000, reason=""):
            self.closed_with = (code, reason)

    class Conn:
        def respond(self, status, body):
            return (status, body)

    async def main():
        bridge = bb.Bridge(find_upstream=lambda: pytest.fail("tarayıcıya yeniden bağlanmamalı"))
        bridge.closed.set()
        ws = WS()
        await bridge.handle(ws)
        return bridge, ws

    bridge, ws = asyncio.run(main())
    assert ws.closed_with and ws.closed_with[0] == 1011
    req = SimpleNamespace(path="/alice-bridge")
    assert bb._health(Conn(), req, bridge)[0] == 503
    assert bb._health(Conn(), req)[0] == 200


def test_neutralize_keeps_users_downloads_and_theme():
    """Live capture: in Opera, once ALICE connected, downloads landed in Playwright's temp folder under GUID names and
    some sites switched to the light theme. These commands must not reach the user's browser."""
    assert bb.neutralize({"id": 3, "method": "Browser.setDownloadBehavior",
                          "params": {"behavior": "allowAndName", "downloadPath": "C:/tmp/pw"}}) is None
    assert bb.neutralize({"id": 4, "sessionId": "S", "method": "Emulation.setEmulatedMedia", "params": {
        "media": "", "features": [{"name": "prefers-color-scheme", "value": "light"}]}}) is None
    kept = bb.neutralize({"id": 5, "method": "Emulation.setEmulatedMedia", "params": {
        "media": "print", "features": [{"name": "prefers-color-scheme", "value": "light"}]}})
    assert kept["params"] == {"media": "print", "features": []}
    click = {"id": 6, "method": "Input.dispatchMouseEvent", "params": {}}
    assert bb.neutralize(click) is click


def test_bridge_answers_blocked_commands_itself_and_resets_downloads():
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    seen: list[dict] = []

    async def browser(ws):
        async for raw in ws:
            msg = json.loads(raw)
            seen.append(msg)
            await ws.send(json.dumps({"id": msg["id"], "result": {}}))

    async def main():
        async with serve(browser, "127.0.0.1", 0) as up:
            port = up.sockets[0].getsockname()[1]
            bridge = bb.Bridge(find_upstream=lambda: f"ws://127.0.0.1:{port}", heartbeat=0)
            async with serve(bridge.handle, "127.0.0.1", 0, process_request=bb._health) as srv:
                url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}{bb.PATH}"
                async with connect(url) as c:
                    await c.send(json.dumps({"id": 7, "method": "Browser.setDownloadBehavior",
                                             "params": {"behavior": "allowAndName", "downloadPath": "x"}}))
                    return json.loads(await asyncio.wait_for(c.recv(), 3))

    reply = asyncio.run(main())
    assert reply == {"id": 7, "result": {}}  # so Playwright doesn't wait
    downloads = [m for m in seen if m["method"] == "Browser.setDownloadBehavior"]
    assert downloads and all(m["params"] == {"behavior": "default"} for m in downloads)  # only the reset was sent


def test_bridge_survives_one_late_heartbeat():
    """A busy browser answered late once; the connection must not drop, since every drop
    means asking for permission again in Opera."""

    class FlakyUp:
        def __init__(self):
            self.n = 0
            self.queue: asyncio.Queue = asyncio.Queue()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raw = await self.queue.get()
            if raw is None:
                raise StopAsyncIteration
            return raw

        async def send(self, raw):
            msg = json.loads(raw)
            if msg.get("id") == bb.HEARTBEAT_ID:
                self.n += 1
                if self.n != 1:  # the first one is missed
                    await self.queue.put(json.dumps({"id": msg["id"], "result": {}}))

        async def close(self):
            self.closed = True
            await self.queue.put(None)

    up = FlakyUp()

    async def fake_connect(url, **kw):
        return up

    async def main():
        bridge = bb.Bridge(find_upstream=lambda: "ws://x", connect=fake_connect, heartbeat=0.05,
                           heartbeat_timeout=0.1)
        await bridge._upstream()
        await asyncio.sleep(0.6)
        alive = not bridge.closed.is_set()
        await up.close()
        return alive

    assert asyncio.run(main()) and up.n >= 3


def test_bridge_rejects_web_pages_and_rebinding():
    """Any web page connecting to ws://127.0.0.1:9339 must not be able to drive the user's browser (cookies, tabs):
    browsers send an Origin, Playwright doesn't."""
    class Conn:
        def respond(self, status, body):
            return (status, body)

    def req(**headers):
        return SimpleNamespace(path=bb.PATH, headers=headers)

    assert bb._health(Conn(), req(Host="127.0.0.1:9339", Origin="https://evil.example"))[0] == 403
    assert bb._health(Conn(), req(Host="127.0.0.1:9339", Origin="null"))[0] == 403
    assert bb._health(Conn(), req(Host="evil.example:9339"))[0] == 403
    assert bb._health(Conn(), req(Host="127.0.0.1:9339")) is None
    assert bb._health(Conn(), req(Host="localhost:9339")) is None


def test_bridge_handshake_with_origin_is_refused():
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import InvalidStatus

    async def main():
        bridge = bb.Bridge(find_upstream=lambda: pytest.fail("tarayıcıya bağlanmamalı"))
        async with serve(bridge.handle, "127.0.0.1", 0, process_request=bb._health) as srv:
            url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}{bb.PATH}"
            with pytest.raises(InvalidStatus):
                async with connect(url, origin="https://evil.example"):
                    pass

    asyncio.run(main())


def test_bridge_requires_token_from_local_programs(monkeypatch):
    """A program in the sandbox could connect to ws://127.0.0.1:9339 without sending an Origin
    and drive the user's browser. The bridge now generates a key on every start; a handshake
    without the key is refused."""
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    from alice import sandbox

    hidden = []
    monkeypatch.setattr(sandbox, "hide_from_sandbox", lambda p: hidden.append(p) or True)

    class Up:
        async def send(self, raw):
            pass

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)

    async def fake_connect(url, **kw):
        return Up()

    async def main():
        import socket

        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            port = so.getsockname()[1]
        old = bb.write_token()  # the previous bridge's key
        bridge = bb.Bridge(find_upstream=lambda: "ws://x", connect=fake_connect, heartbeat=0)
        task = asyncio.create_task(bb.serve(port, log=lambda s: None, bridge=bridge))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if bb.read_token() != old:
                break
        token = bb.read_token()
        assert token and token != old  # a new key on every run
        with pytest.raises(InvalidStatus):
            async with connect(bb.endpoint(port)):
                pass
        with pytest.raises(InvalidStatus):
            async with connect(bb.endpoint(port), additional_headers={bb.TOKEN_HEADER: "yanlis"}):
                pass
        async with connect(bb.endpoint(port), additional_headers={bb.TOKEN_HEADER: token}):
            pass
        bridge.closed.set()
        await task

    asyncio.run(main())
    assert hidden and all(p.parent == bb.token_path().parent for p in hidden)
    assert bb.token_path().is_file() and not list(bb.token_path().parent.glob(".browser-bridge-token*"))


def test_health_check_needs_no_token():
    class Conn:
        def respond(self, status, body):
            return (status, body)

    req = SimpleNamespace(path="/alice-bridge", headers={"Host": "127.0.0.1:9339"})
    assert bb._health(Conn(), req, None, "anahtar")[0] == 200
    ws = SimpleNamespace(path=bb.PATH, headers={"Host": "127.0.0.1:9339"})
    assert bb._health(Conn(), ws, None, "anahtar")[0] == 403
    ws.headers[bb.TOKEN_HEADER] = "anahtar"
    assert bb._health(Conn(), ws, None, "anahtar") is None
