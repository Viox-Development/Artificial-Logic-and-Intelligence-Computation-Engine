"""Remote access: requests from the relay are forwarded to the local phone server; the reply comes back in chunks."""

from __future__ import annotations

import base64
import json
import queue
import time

import pytest

from alice.app import relay
from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config

RELAY_HOST = "alice-relay.ornek.workers.dev"


class FakeWS:
    """Worker side: puts requests into the queue and collects what the computer sends."""

    def __init__(self):
        self.inbox: queue.Queue = queue.Queue()
        self.sent: queue.Queue = queue.Queue()
        self.closed = False

    def recv(self, timeout=None):
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def send(self, text):
        self.sent.put(text)

    def close(self):
        self.closed = True


def collect(ws: FakeWS, rid: str, timeout: float = 10.0, until_end: bool = True):
    head, body, end = None, b"", False
    deadline = time.time() + timeout
    while time.time() < deadline and not end:
        try:
            raw = ws.sent.get(timeout=0.1)
        except queue.Empty:
            if head is not None and not until_end and body:
                break
            continue
        if raw == "ping":
            continue
        m = json.loads(raw)
        if m.get("rid") != rid:
            continue
        if m["t"] == "head":
            head = m
        elif m["t"] == "data":
            body += base64.b64decode(m["b"])
        elif m["t"] == "end":
            end = True
    return head, body, end


@pytest.fixture
def tunnel(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    state.allowed_hosts.add(RELAY_HOST)
    srv = AppServer(state).start()
    ws = FakeWS()
    seen = {}

    def connect(url, key):
        seen.update(url=url, key=key)
        return ws

    client = relay.RelayClient("https://" + RELAY_HOST, "k" * 32, srv.httpd.server_address[1], connect=connect).start()
    end = time.time() + 5
    while not client.connected and time.time() < end:
        time.sleep(0.02)
    yield state, ws, client, seen
    client.stop()
    srv.stop()


def send_req(ws, rid, method, path, headers, body=b""):
    ws.inbox.put(json.dumps({"t": "req", "rid": rid, "m": method, "p": path, "h": headers, "n": len(body)}))
    for i in range(0, len(body), 100):
        ws.inbox.put(json.dumps({"t": "body", "rid": rid, "b": base64.b64encode(body[i:i + 100]).decode()}))
    ws.inbox.put(json.dumps({"t": "bend", "rid": rid}))


def test_requests_from_relay_reach_local_server(tunnel):
    state, ws, client, seen = tunnel
    assert seen == {"url": f"wss://{RELAY_HOST}/_alice/pc", "key": "k" * 32}
    send_req(ws, "1", "GET", f"/api/state?t={state.token}", {"Host": RELAY_HOST, "Origin": f"https://{RELAY_HOST}"})
    head, body, end = collect(ws, "1")
    assert head["s"] == 200 and end and "running" in json.loads(body)
    # The key is still checked on the local server: the relay server grants no permission.
    send_req(ws, "2", "GET", "/api/state", {"Host": RELAY_HOST})
    head, _, _ = collect(ws, "2")
    assert head["s"] == 403
    # A request with a body arrives in chunks and is reassembled.
    send_req(ws, "3", "POST", "/api/stop", {"Host": RELAY_HOST, "X-Alice-Token": state.token,
                                             "Content-Type": "application/json"}, json.dumps({"sid": "x" * 150}).encode())
    head, body, _ = collect(ws, "3")
    assert head["s"] == 200 and json.loads(body)["ok"]


def test_event_stream_flows_and_cancel_stops_it(tunnel):
    state, ws, client, _ = tunnel
    send_req(ws, "9", "GET", f"/api/events?t={state.token}", {"Host": RELAY_HOST})
    deadline = time.time() + 5
    while time.time() < deadline and "9" not in client._reqs:
        time.sleep(0.02)
    time.sleep(0.3)
    state.bus.publish({"type": "info", "text": "uzaktan merhaba"})
    head, body, _ = collect(ws, "9", timeout=5, until_end=False)
    assert head["s"] == 200 and b"uzaktan merhaba" in body
    ws.inbox.put(json.dumps({"t": "cancel", "rid": "9"}))
    deadline = time.time() + 5
    while time.time() < deadline and "9" in client._reqs:
        time.sleep(0.05)
    assert "9" not in client._reqs


def test_pair_url_carries_relay_address(workdir):
    from alice.app.phone import pair_url

    assert pair_url("192.168.1.5", 8765, "tok") == "http://192.168.1.5:8765/m?t=tok"
    relay.save_settings("https://alice-relay.ornek.workers.dev/", "s" * 40)
    assert pair_url("192.168.1.5", 8765, "tok").endswith("&r=https%3A%2F%2Falice-relay.ornek.workers.dev")
    assert relay.host() == "alice-relay.ornek.workers.dev"
    relay.save_settings("http://duz.example", "s" * 40)  # an unencrypted relay is not accepted
    assert relay.settings() is None
