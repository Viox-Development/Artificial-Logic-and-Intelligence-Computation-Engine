"""OAuth2 authorization (Authorization Code + PKCE), with browser and local callback redirect URL.

Flow: ALICE starts a one-off server on a random port on 127.0.0.1, opens the service's authorization page
in the browser, the service redirects the user to `http://127.0.0.1:<port>/callback?code=...`, the code
is exchanged for a token, and the token is saved to ~/.alice/connect.json.

PKCE eliminates the need for a client secret (the right approach for a desktop app: a secret stored on the
user's disk is no secret anyway). For services requiring a secret, `client_secret_env` can be provided.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Callable

from alice.i18n import _
from alice.connect import store
from alice.connect.manifest import Auth

PAGE = ("<!doctype html><meta charset=utf-8><title>ALICE</title>"
        "<body style='font:16px system-ui;background:#16161a;color:#eee;display:grid;place-items:center;height:100vh'>"
        "<div style='text-align:center'><h2>{title}</h2><p>{text}</p></div>")


class OAuthError(RuntimeError):
    pass


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    want_state = ""
    done = threading.Event()
    timeout = 15  # sec: avoid waiting indefinitely for idle connections (browser pre-connects) that send no request

    def log_message(self, *a: Any) -> None:  # silent
        pass

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path != "/callback":  # favicon etc.: does not affect the result
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        q = urllib.parse.parse_qs(u.query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        err = (q.get("error") or [""])[0]
        ok = bool(code) and secrets.compare_digest(state, type(self).want_state)
        if "code" in type(self).result:  # same URL opened a second time (refresh): keep initial result
            title, text = _("Connected"), _("You can close this tab and go back to ALICE.")
        elif ok:
            type(self).result = {"code": code}
            title, text = _("Connected"), _("You can close this tab and go back to ALICE.")
        else:
            type(self).result = {"error": err or (_("the state did not match") if code else "kod gelmedi")}
            title, text = _("Failed"), type(self).result["error"]
        type(self).done.set()
        body = PAGE.format(title=title, text=text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def authorize(auth: Auth, name: str, client_id: str, client_secret: str = "", timeout: float = 240.0,
              open_browser: Callable[[str], Any] = webbrowser.open,
              emit: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Obtain authorization in browser, store and return token. Raises OAuthError on failure."""
    import httpx

    say = emit or (lambda _s: None)
    try:
        # Each connection in a separate thread: if the browser opens an idle pre-connection first, a single-thread
        # server hangs on it, leaving Google's redirect unanswered and freezing the consent page after "Devam Et".
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", auth.redirect_port), _Catcher)
        srv.daemon_threads = True
    except OSError as e:
        raise OAuthError(_("could not open the redirect address (port {port}): {error}. Close the program using that port and try again.", port=auth.redirect_port, error=e)) from e
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(16)
    _Catcher.want_state = state
    _Catcher.result = {}
    _Catcher.done = threading.Event()
    verifier, challenge = _pkce()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "state": state,
              **auth.extra_authorize}
    if auth.scopes:
        params["scope"] = " ".join(auth.scopes)
    if auth.pkce:
        params.update(code_challenge=challenge, code_challenge_method="S256")
    url = auth.authorize_url + ("&" if "?" in auth.authorize_url else "?") + urllib.parse.urlencode(params)

    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    t.start()
    say(_("redirect address: {url}  (must be registered in the service's app settings)", url=redirect))
    say(_("opening the permission page in the browser: {url}", url=auth.authorize_url))
    try:
        open_browser(url)
        _Catcher.done.wait(timeout)
    finally:
        srv.shutdown()
        srv.server_close()
    if not _Catcher.result:
        raise OAuthError(_("time ran out waiting for permission ({s} s). You can open the address by hand: {url}", s=int(timeout), url=url))
    if "error" in _Catcher.result:
        raise OAuthError(_("authorization failed: {error}", error=_Catcher.result["error"]))

    data = {"grant_type": "authorization_code", "code": _Catcher.result["code"], "redirect_uri": redirect,
            "client_id": client_id}
    if auth.pkce:
        data["code_verifier"] = verifier
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = httpx.post(auth.token_url, data=data, headers={"Accept": "application/json"}, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise OAuthError(_("could not get the token: {error}", error=e)) from e
    if r.status_code >= 400:
        raise OAuthError(_("could not get the token (HTTP {code}): {text}", code=r.status_code, text=r.text[:200]))
    tok = _save(name, r.json())
    say(_("connection done"))
    return tok


def refresh(auth: Auth, name: str, client_id: str, client_secret: str = "") -> dict[str, Any]:
    """Refresh access token using refresh token. Raises OAuthError if missing or unsuccessful."""
    import httpx

    row = store.get(name)
    rt = str(row.get("refresh_token") or "")
    if not rt:
        raise OAuthError(_("{name}: no refresh token; connect again: alice connect {name}", name=name))
    if row.get("refresh_dead") == _fingerprint(rt):
        # Provider already rejected this token: no point in retrying with the same dead token on every background
        # task and health check to produce the same error. When user reconnects, token changes and lock clears.
        raise OAuthError(dead_message(name, str(row.get("refresh_error") or "")))
    data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = httpx.post(auth.token_url, data=data, headers={"Accept": "application/json"}, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise OAuthError(f"token yenilenemedi: {e}") from e
    if r.status_code >= 400:
        # Only invalid_grant indicates token death. Errors like unauthorized_client are client ID/config issues:
        # no permanent lock is placed, the real error is shown each time (resolves once config is fixed).
        if r.status_code in (400, 401) and "invalid_grant" in r.text:
            why = _("expired or revoked") if "expired or revoked" in r.text else _("invalid")
            store.save(name, refresh_dead=_fingerprint(rt), refresh_error=why, refresh_dead_at=time.time())
            raise OAuthError(dead_message(name, why))
        raise OAuthError(_("could not refresh the token (HTTP {code}): {text}. Connect again: alice connect {name}", code=r.status_code, text=r.text[:200], name=name))
    return _save(name, r.json(), keep_refresh=rt)


def _fingerprint(token: str) -> str:
    """Short digest rather than the token itself: identifies which token the dead marker belongs to."""
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def needs_sign_in(name: str) -> bool:
    """Whether refresh token was rejected by provider (until re-authenticated)."""
    row = store.get(name)
    rt = str(row.get("refresh_token") or "")
    return bool(rt) and row.get("refresh_dead") == _fingerprint(rt)


GOOGLE_CONNECTIONS = ("google_", "gmail:")  # Connections using Google's OAuth client


def dead_message(name: str, why: str = "") -> str:
    cmd = (f"alice connect gmail --account {name[len('gmail:'):]}" if name.startswith("gmail:")
           else f"alice connect {name}")
    text = _("{name}: the sign-in is invalid; sign in again (the provider doesn't accept the refresh token{why}). ALICE "
             "can't renew it itself. Connect again: {cmd}.", name=name, why=f": {why}" if why else "", cmd=cmd)
    if name.startswith(GOOGLE_CONNECTIONS):
        text += _(" If this happens every week: while the OAuth app in Google Cloud is in 'Testing', Google "
                  "invalidates refresh tokens after 7 days; the lasting fix is to move the app to 'In production'.")
    return text


def _save(name: str, body: dict[str, Any], keep_refresh: str = "") -> dict[str, Any]:
    access = str(body.get("access_token") or "")
    if not access:
        raise OAuthError(f"cevapta access_token yok: {str(body)[:200]}")
    expires_in = float(body.get("expires_in") or 0)
    row = {"access_token": access, "token_type": str(body.get("token_type") or "Bearer"),
           "scope": str(body.get("scope") or ""), "obtained_at": time.time(),
           "expires_at": time.time() + expires_in if expires_in else 0.0,
           "refresh_token": str(body.get("refresh_token") or keep_refresh)}
    store.save(name, **row)
    return row
