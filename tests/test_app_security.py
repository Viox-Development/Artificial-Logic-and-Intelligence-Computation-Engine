"""Defensive checks of the app server: key, Origin, malformed body, unexpected error, timeout."""

from __future__ import annotations

import http.client
import json
import socket

import httpx
import pytest

from alice.app import server as srv_mod
from alice.app.server import AppServer, AppState
from alice.config import alice_home
from tests.conftest import FakeProvider, make_config


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", [])
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    base = srv.url.split("/?")[0]
    client = httpx.Client(base_url=base, headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, srv, client
    srv.stop()


def raw_post(srv, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders(body)
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        conn.close()


def test_non_ascii_token_is_denied_not_crash(app):
    state, srv, client = app
    anon = httpx.Client(base_url=str(client.base_url), timeout=10)
    # compare_digest raised TypeError on non-ASCII str: the connection was dropped without a response.
    assert anon.get("/api/state", params={"t": "ğğğ"}).status_code == 403
    assert anon.get("/", params={"t": "ü" * 32}).status_code == 403
    assert anon.get("/preview/%C4%9F%C4%9F/x/index.html").status_code == 403
    assert anon.get("/api/state", params={"t": state.token}).status_code == 200


def test_foreign_origin_is_denied_even_with_token(app):
    state, srv, client = app
    assert client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/stop", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/stop", json={}, headers={"Origin": "null"}).status_code == 403
    port = srv.httpd.server_address[1]
    assert client.post("/api/stop", json={}, headers={"Origin": f"http://127.0.0.1:{port}"}).json()["ok"]
    assert client.get("/api/state", headers={"Origin": f"http://localhost:{port}"}).status_code == 200
    assert client.post("/api/stop", json={}).json()["ok"]  # no-Origin client (scheduled automation) is unaffected
    assert client.get("/static/app.js", headers={"Origin": "null"}).status_code == 200


def test_denied_post_body_is_drained_so_the_403_is_not_reset(app):
    """The 403 for a forbidden Origin used to be sent without reading the request body. Closing a socket with unread
    bytes sends a TCP reset; on Windows the reset discards the 403 before the client reads it (WinError 10053, flaky
    test_foreign_origin_is_denied_even_with_token). The body is read first now, so the close is a clean FIN."""
    import errno
    import socket
    import sys
    import time

    state, srv, client = app
    body = b"x" * 300_000  # larger than the handler's read buffer, so it stays unread in the kernel
    s = socket.create_connection(("127.0.0.1", srv.httpd.server_address[1]), timeout=10)
    try:
        s.sendall(b"POST /api/stop HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: https://evil.example\r\n"
                  b"X-Alice-Token: " + state.token.encode() + b"\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\n\r\n" % len(body) + body)
        time.sleep(0.3)  # let the server answer and close
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        assert data.startswith(b"HTTP/1.0 403")
        if sys.platform.startswith("linux"):  # a reset leaves an error on the socket (EPIPE/ECONNRESET)
            assert errno.errorcode.get(s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)) is None
    finally:
        s.close()


def test_bad_bodies_get_clean_4xx(app):
    state, srv, client = app
    tok = {"X-Alice-Token": state.token, "Host": "127.0.0.1", "Content-Type": "application/json"}
    code, body = raw_post(srv, "/api/send", b"{}", {**tok, "Content-Length": "abc"})
    assert code == 400 and body["ok"] is False
    code, body = raw_post(srv, "/api/send", b"\xff\xfe{", {**tok, "Content-Length": "3"})
    assert code == 400 and "JSON" in body["error"]
    code, body = raw_post(srv, "/api/send", b"{not json", {**tok, "Content-Length": "9"})
    assert code == 400
    code, body = raw_post(srv, "/api/send", b"", {**tok, "Content-Length": str(srv_mod.MAX_BODY + 1)})
    assert code == 413 and body["ok"] is False
    # Wrong types should get a meaningful response, not a 500, and the server must stay up.
    assert client.post("/api/send", json={"text": "", "images": []}).status_code == 400
    assert client.post("/api/mode", json={"mode": ["auto"]}).status_code in (400, 500)
    assert client.get("/api/state").status_code == 200


def test_large_body_with_images_is_not_dropped(app, monkeypatch):
    state, srv, client = app
    seen = {}

    def fake_send(text, images=None, sid=""):
        seen["text"], seen["images"] = text, images
        return True, None

    monkeypatch.setattr(state, "send", fake_send)
    img = "A" * 3_000_000  # the old 2 MB limit silently emptied the body and returned "boş mesaj" (empty message)
    r = client.post("/api/send", json={"text": "bak", "images": [img]})
    assert r.json()["ok"] and seen["text"] == "bak" and len(seen["images"][0]) == 3_000_000


def test_get_exception_returns_json_500_without_traceback(app, monkeypatch):
    state, srv, client = app

    def boom():
        raise RuntimeError("gizli-ayrinti C:\\Users\\x\\secret")

    monkeypatch.setattr(state, "status", boom)
    r = client.get("/api/state")
    assert r.status_code == 500 and r.json()["ok"] is False
    assert "gizli" not in r.text and "Traceback" not in r.text
    assert "gizli-ayrinti" in (alice_home() / "app.log").read_text(encoding="utf-8")
    assert client.get("/api/history").status_code == 200  # server is still up


def test_responses_carry_no_referrer_and_idle_connections_time_out(app):
    state, srv, client = app
    assert client.get("/api/state").headers["Referrer-Policy"] == "no-referrer"
    assert srv_mod.make_handler(state).timeout == srv_mod.REQUEST_TIMEOUT


def test_idle_connection_is_closed(app, monkeypatch):
    state, _, _ = app
    monkeypatch.setattr(srv_mod, "REQUEST_TIMEOUT", 0.5)
    srv = AppServer(state)  # the timeout is read when the handler class is built
    srv._thread.start()
    s = socket.create_connection(("127.0.0.1", srv.httpd.server_address[1]), timeout=5)
    try:
        s.sendall(b"GET /api/state HTTP/1.1\r\n")  # a client that never finishes its request
        assert s.recv(1024) == b""  # the server closes the connection on timeout and the thread is freed
    finally:
        s.close()
        srv.httpd.shutdown()
        srv.httpd.server_close()


def test_phone_app_routes_need_token_and_actions_are_checked(app, monkeypatch):
    from alice.app import remote

    state, srv, client = app
    anon = httpx.Client(base_url=str(client.base_url), timeout=10)
    assert anon.get("/m").status_code == 403
    assert anon.get("/api/remote/status").status_code == 403
    assert anon.post("/api/remote/action", json={"action": "lock"}).status_code == 403
    page = anon.get("/m", params={"t": state.token})
    assert page.status_code == 200 and state.token in page.text and "m.js" in page.text
    man = anon.get("/m.webmanifest", params={"t": state.token}).json()
    assert man["start_url"] == f"/m?t={state.token}" and man["display"] == "standalone"
    assert client.get("/static/m.js").status_code == 200 and client.get("/static/m.css").status_code == 200
    monkeypatch.setattr(remote, "status", lambda: {"core": True, "mode": "normal", "events": []})
    assert client.get("/api/remote/status").json()["mode"] == "normal"
    done = []
    monkeypatch.setattr(remote, "_lock", lambda: done.append("lock") or "bilgisayar kilitlendi")
    r = client.post("/api/remote/action", json={"action": "lock"}).json()
    assert r == {"ok": True, "text": "bilgisayar kilitlendi"} and done == ["lock"]
    bad = client.post("/api/remote/action", json={"action": "format_c"})
    assert bad.status_code == 400 and "unknown" in bad.json()["error"]
    assert client.post("/api/remote/action", json={"action": "type", "text": ""}).json()["ok"] is False
