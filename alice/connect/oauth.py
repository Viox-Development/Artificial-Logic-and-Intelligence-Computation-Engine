"""OAuth2 yetkilendirme (Authorization Code + PKCE), tarayıcı ve yerel geri dönüş adresiyle.

Akış: ALICE 127.0.0.1'de rastgele bir porta tek seferlik sunucu açar, tarayıcıda servisin izin sayfasını
açar, servis kullanıcıyı `http://127.0.0.1:<port>/callback?code=...` adresine döndürür, kod token'la
takas edilir ve token ~/.alice/connect.json'a yazılır.

PKCE sayesinde client secret gerekmiyor (masaüstü uygulaması için doğru olan da bu: secret kullanıcının
diskinde duracaksa zaten sır değildir). Secret isteyen servisler için `client_secret_env` verilebilir.
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

    def log_message(self, *a: Any) -> None:  # sessiz
        pass

    def do_GET(self) -> None:  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        err = (q.get("error") or [""])[0]
        ok = bool(code) and secrets.compare_digest(state, type(self).want_state)
        if ok:
            type(self).result = {"code": code}
            title, text = "Bağlandı", "Bu sekmeyi kapatabilirsin, ALICE'e dön."
        else:
            type(self).result = {"error": err or ("state uyuşmadı" if code else "kod gelmedi")}
            title, text = "Olmadı", type(self).result["error"]
        body = PAGE.format(title=title, text=text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def authorize(auth: Auth, name: str, client_id: str, client_secret: str = "", timeout: float = 240.0,
              open_browser: Callable[[str], Any] = webbrowser.open,
              emit: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Tarayıcıda izin al, token'ı sakla ve döndür. Hata olursa OAuthError."""
    import httpx

    say = emit or (lambda _s: None)
    try:
        srv = http.server.HTTPServer(("127.0.0.1", auth.redirect_port), _Catcher)
    except OSError as e:
        raise OAuthError(f"geri dönüş adresi açılamadı (port {auth.redirect_port}): {e}. "
                         "O portu kullanan programı kapatıp tekrar dene.") from e
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(16)
    _Catcher.want_state = state
    _Catcher.result = {}
    verifier, challenge = _pkce()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "state": state,
              **auth.extra_authorize}
    if auth.scopes:
        params["scope"] = " ".join(auth.scopes)
    if auth.pkce:
        params.update(code_challenge=challenge, code_challenge_method="S256")
    url = auth.authorize_url + ("&" if "?" in auth.authorize_url else "?") + urllib.parse.urlencode(params)

    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    say(f"geri dönüş adresi: {redirect}  (servisin uygulama ayarında kayıtlı olmalı)")
    say(f"tarayıcıda izin sayfası açılıyor: {auth.authorize_url}")
    open_browser(url)
    end = time.time() + timeout
    while t.is_alive() and time.time() < end:
        t.join(0.2)
    srv.server_close()
    if not _Catcher.result:
        raise OAuthError(f"izin beklenirken süre doldu ({int(timeout)} sn). Adresi elle açabilirsin: {url}")
    if "error" in _Catcher.result:
        raise OAuthError(f"yetkilendirme başarısız: {_Catcher.result['error']}")

    data = {"grant_type": "authorization_code", "code": _Catcher.result["code"], "redirect_uri": redirect,
            "client_id": client_id}
    if auth.pkce:
        data["code_verifier"] = verifier
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = httpx.post(auth.token_url, data=data, headers={"Accept": "application/json"}, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise OAuthError(f"token alınamadı: {e}") from e
    if r.status_code >= 400:
        raise OAuthError(f"token alınamadı (HTTP {r.status_code}): {r.text[:200]}")
    tok = _save(name, r.json())
    say("bağlantı tamam")
    return tok


def refresh(auth: Auth, name: str, client_id: str, client_secret: str = "") -> dict[str, Any]:
    """Yenileme token'ıyla erişim token'ını tazele. Yenileme yoksa/çalışmazsa OAuthError."""
    import httpx

    row = store.get(name)
    rt = str(row.get("refresh_token") or "")
    if not rt:
        raise OAuthError(f"{name}: yenileme token'ı yok, tekrar bağlan: alice connect {name}")
    data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = httpx.post(auth.token_url, data=data, headers={"Accept": "application/json"}, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise OAuthError(f"token yenilenemedi: {e}") from e
    if r.status_code >= 400:
        raise OAuthError(f"token yenilenemedi (HTTP {r.status_code}): {r.text[:200]}. "
                         f"Tekrar bağlan: alice connect {name}")
    return _save(name, r.json(), keep_refresh=rt)


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
