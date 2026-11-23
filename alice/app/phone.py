"""Phone bridge: access ALICE from the phone's browser and live watch data.

A separate server exposed to the local network (default port 8765). Address on the phone:
  http://<computer-IP>:8765/m?t=<token>         -> phone web app (chat, computer, screen)
  POST http://<IP>:8765/api/watch  (X-Alice-Token: <token>, JSON {"hr": 88})  -> live heart rate
The token is persisted in ~/.alice/phone-token; this way bookmarks and automation on the phone
work across restarts. Changed via `alice phone --new-token`.

The token is only carried in the URL on the first visit (QR / bookmark): the server converts it to an HttpOnly,
SameSite=Strict cookie and redirects to the tokenless URL; the page then communicates via cookie and
X-Alice-Token / "Authorization: Bearer" header (see phone_handler). The connection is plain HTTP on the LAN:
someone on the same network can sniff traffic. No built-in TLS (`cryptography` needed for self-signed certs is not
an ALICE dependency and Android WebView does not accept self-signed certificates without extra code); for access
outside the home or from untrusted networks, use the HTTPS relay server (relay.py, `alice relay`).

The Android app (android/) opens the same page; adds speech recognition, notifications, calling, and QR pairing.
The computer provides the APK (/api/phone/app.apk, see phone_apk.py); the app updates itself from there when a new
version is released. In the browser over plain HTTP, the microphone cannot open; voice input there uses the phone's
audio recorder.
"""

from __future__ import annotations

import secrets
import socket
import time
from pathlib import Path

from alice.config import Config, alice_home


def lan_ip() -> str:
    """Local network IP of the computer (without sending packets, via routing table)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _hide(p: Path) -> None:
    """Prevent sandboxed (low-integrity) commands from reading the token: otherwise they could control the computer
    with full privileges via the phone server on 127.0.0.1."""
    try:
        from alice.sandbox import hide_from_sandbox

        hide_from_sandbox(p)
    except Exception:  # noqa: BLE001 - if labeling fails, token still works
        pass


def token(new: bool = False) -> str:
    p = alice_home() / "phone-token"
    if not new and p.exists():
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 24:
            _hide(p)  # ensure files written by older versions are also labeled
            return t
    t = secrets.token_urlsafe(24)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass  # Windows: chmod is not supported; the file is in the user's folder
    _hide(p)
    return t


def pair_url(ip: str, port: int, tok: str) -> str:
    """Pairing URL for the phone. Includes the relay address if remote access is set up: phone uses LAN address
    at home and relay server when away."""
    from urllib.parse import quote

    from alice.app import relay

    url = f"http://{ip}:{port}/m?t={tok}"
    s = relay.settings()
    return url + (f"&r={quote(s['url'], safe='')}" if s else "")


def _relay_log(line: str) -> None:
    try:
        from datetime import datetime

        with (alice_home() / "relay.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass  # logging must not break the relay


COOKIE = "alice_phone"
COOKIE_MAX_AGE = 400 * 24 * 3600  # longest duration allowed by browsers: allows phone bookmark to work without QR
ENTRY_PAGES = ("/m", "/")  # QR/bookmark URLs: ?t= is accepted once here and converted to a cookie


def phone_handler(base: type, state) -> type:
    """Request handler for the phone server: prevents token leakage in URLs.

    - If an entry page (/m?t=..., /?t=...) arrives with the valid token, an HttpOnly + SameSite=Strict cookie is set
      and redirected (303) to the tokenless URL: token is not left in browser history, screenshots, or bookmarks.
    - Subsequent GETs are verified via cookie, API calls via X-Alice-Token or "Authorization: Bearer" header.
      POST requires header only: cookie alone is insufficient for state-changing requests (defense-in-depth against
      CSRF; SameSite already mitigates).
    - ?t= is still accepted on APIs: older app versions installed on phones (event stream, accessibility check) and
      watch automations use it; new frontend code does not include token in URL.
    """
    from http.cookies import CookieError, SimpleCookie
    from urllib.parse import parse_qs, urlencode, urlparse

    from alice.app.server import _same

    class PhoneHandler(base):  # type: ignore[misc, valid-type]
        def _cookie_token(self) -> str:
            raw = self.headers.get("Cookie") or ""
            if not raw:
                return ""
            try:
                jar = SimpleCookie()
                jar.load(raw)
            except CookieError:
                return ""
            m = jar.get(COOKIE)
            return m.value if m is not None else ""

        def _token_ok(self, query: dict[str, list[str]]) -> bool:
            auth = self.headers.get("Authorization") or ""
            if auth[:7].lower() == "bearer " and _same(auth[7:].strip(), state.token):
                return True
            if self.command in ("GET", "HEAD") and _same(self._cookie_token(), state.token):
                return True
            return super()._token_ok(query)

        def _get(self) -> None:
            u = urlparse(self.path)
            if u.path in ENTRY_PAGES and "t" in u.query and self._host_ok() and self._origin_ok():
                q = parse_qs(u.query, keep_blank_values=True)
                if _same((q.get("t") or [""])[0], state.token):
                    return self._pair_redirect(u.path, q)
            return super()._get()

        def _pair_redirect(self, path: str, q: dict[str, list[str]]) -> None:
            rest = urlencode([(k, v) for k, vs in q.items() if k != "t" for v in vs])
            host = (self.headers.get("Host") or "").split(":")[0]
            from alice.app import relay

            # Relay server is HTTPS: cookies originating from there must only go over HTTPS. LAN is plain HTTP (no Secure).
            secure = "; Secure" if host and host == relay.host() else ""
            self.send_response(303)
            self.send_header("Location", path + ("?" + rest if rest else ""))
            self.send_header("Set-Cookie", f"{COOKIE}={state.token}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly; "
                                           f"SameSite=Strict{secure}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return PhoneHandler


class PhoneServer:
    def __init__(self, cfg: Config, store=None, port: int | None = None, new_token: bool = False, state=None):
        from alice.app.server import AppServer, AppState
        from alice.core.store import Store

        self.ip = lan_ip()
        self.state = state or AppState(cfg, Path.home(), cfg.agent.get("permission_mode", "auto"))
        self.state.token = token(new_token)
        self.state.allowed_hosts = {"127.0.0.1", "localhost", self.ip}
        from alice.app import relay

        relay_host = relay.host()
        if relay_host:  # requests coming from outside home via relay server (see relay.py)
            self.state.allowed_hosts.add(relay_host)
        self._own_store = store is None  # stop() closes the store only if we opened it ourselves
        self.state.watch_store = store or Store()
        self.port = int(port if port is not None else cfg.data.get("phone", {}).get("port", 8765))
        self.server = AppServer(self.state, self.port, host="0.0.0.0", owns_state=state is None)
        httpd = self.server.httpd
        if getattr(httpd, "RequestHandlerClass", None) is not None:  # can be a mock server in tests
            httpd.RequestHandlerClass = phone_handler(httpd.RequestHandlerClass, self.state)
        self.server.start()
        from alice.app import phone_apk

        phone_apk.refresh_async()  # ensure the latest version of the phone app is ready on computer
        self.relay = relay.start_for(self.server.httpd.server_address[1], log=_relay_log)
        from alice.app import call

        calls = getattr(self.state, "calls", None)  # can be mock state in tests
        if calls is not None:
            call.register(calls)  # allow ALICE to place phone calls (phone_call tool)

    @property
    def url(self) -> str:
        return pair_url(self.ip, self.server.httpd.server_address[1], self.state.token)

    @property
    def app_url(self) -> str:
        return f"http://{self.ip}:{self.server.httpd.server_address[1]}/api/phone/app.apk?t={self.state.token}"

    @property
    def projects_url(self) -> str:
        return f"http://{self.ip}:{self.server.httpd.server_address[1]}/?t={self.state.token}#projects"

    @property
    def watch_url(self) -> str:
        return f"http://{self.ip}:{self.server.httpd.server_address[1]}/api/watch"

    def stop(self) -> None:
        from alice.app import call

        if call.current() is getattr(self.state, "_calls", None):
            call.register(None)
        if self.relay is not None:
            self.relay.stop()
        self.server.stop()
        if self._own_store and self.state.watch_store is not None:
            self.state.watch_store.close()
            self.state.watch_store = None


def print_qr(console, text: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make()
        for row in qr.get_matrix():
            console.print("".join("██" if c else "  " for c in row))
    except ImportError:
        pass  # no qrcode package: the link printed above is enough


def phone_cmd(argv: list[str]) -> int:
    from rich.console import Console

    from alice.config import load_config

    console = Console(highlight=False)
    cfg = load_config()
    ps = PhoneServer(cfg, new_token="--new-token" in argv)
    console.print(f"[bold magenta]ALICE telefon köprüsü[/] açık.\n"
                  f"QR'yi telefonun kamerasıyla okut (aynı Wi-Fi): [bold]{ps.url}[/]\n"
                  f"Uygulama kuruluysa \"ALICE ile aç\"ı seç; değilse sayfa tarayıcıda açılır, oradan kur.\n"
                  f"Android uygulaması (doğrudan): {ps.app_url}\n"
                  f"Projeler (izle, cevapla, onayla): {ps.projects_url}\n"
                  f"Canlı saat verisi: POST {ps.watch_url}  başlık X-Alice-Token: {ps.state.token}  "
                  'gövde {"hr": 88}')
    print_qr(console, ps.url)
    console.print("[dim]Windows güvenlik duvarı ilk açılışta izin isteyebilir (özel ağ). Ctrl+C çık[/]")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass  # Ctrl+C: shut down quietly
    finally:
        ps.stop()
    return 0


def watch_cmd(argv: list[str]) -> int:
    """alice watch status | import <Gadgetbridge.db>"""
    from rich.console import Console

    from alice.core.store import Store
    from alice.health.watch import cadence, import_gadgetbridge

    console = Console(highlight=False)
    sub = argv[0] if argv else "status"
    with Store() as st:
        if sub == "import" and len(argv) > 1:
            added = import_gadgetbridge(Path(argv[1]).expanduser(), st)
            console.print("[green]içe alındı:[/] " + (", ".join(f"{k} {v}" for k, v in added.items()) or "yeni veri yok"))
            sub = "status"
        if sub == "status":
            for key in ("watch.hr", "watch.steps", "watch.stress", "watch.spo2"):
                c = cadence(st, key)
                if not c["count"] and c["last_age_s"] is None:
                    continue
                age = c["last_age_s"]
                age_s = "hiç" if age is None else (f"{age:.0f} sn önce" if age < 120 else f"{age / 60:.0f} dk önce"
                                                   if age < 7200 else f"{age / 3600:.1f} sa önce")
                gap = c.get("median_gap_s")
                gap_s = f", tipik aralık {gap:.0f} sn" if gap is not None else ""
                console.print(f"[bold]{key}[/]: son 24 saatte {c['count']} örnek, son örnek {age_s}{gap_s}\n"
                              f"  → {c['verdict']}")
            if not st.keys("watch.*"):
                console.print("[yellow]henüz saat verisi yok.[/] Gadgetbridge'de otomatik dışa aktarmayı aç, dosyayı "
                              "bilgisayara senkronla ve config'te \\[watch] export_path'i ayarla; ya da "
                              "alice watch import <Gadgetbridge.db>")
            return 0
        console.print(watch_cmd.__doc__, markup=False)
        return 2
