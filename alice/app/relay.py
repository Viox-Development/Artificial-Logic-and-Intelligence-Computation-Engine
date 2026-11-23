"""Remote access: the phone reaches the PC via ALICE's own relay server when outside the home.

Home internet might be behind CGNAT, and port forwarding on the modem should not be required: the PC connects
outbound. The relay server runs on the user's own Cloudflare account on the free tier (relay/, `alice relay deploy`).
The PC opens and keeps open a WebSocket to it; every request made by the phone to the relay address arrives over this
connection, is queried here against the local phone server (127.0.0.1:8765), and the response is streamed back in
chunks (including event streams and call audio).

Authentication is identical: the local server verifies the phone's key. Only this PC can connect to the relay
server (the key in relay.json, PC_KEY secret in the Worker).

Config ~/.alice/relay.json: {"url": "https://alice-relay.<account>.workers.dev", "key": "..."}
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from alice.config import alice_home

CHUNK = 192 * 1024
PING_EVERY = 25.0
STREAM_PATHS = ("/api/events",)  # long-running responses: no timeout


def settings_path() -> Path:
    return alice_home() / "relay.json"


def settings() -> dict[str, str] | None:
    try:
        d = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url, key = str(d.get("url") or "").rstrip("/"), str(d.get("key") or "")
    if not url.startswith("https://") or len(key) < 24:
        return None
    return {"url": url, "key": key}


def save_settings(url: str, key: str) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"url": url.rstrip("/"), "key": key}, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass  # Windows: chmod is not supported; the file is in the user's folder
    _hide(p)


def _hide(p: Path) -> None:
    """Commands in the sandbox should not be able to read the relay key: if they did, they could connect to the relay
    server instead of the PC and obtain the phone's requests (and key)."""
    try:
        from alice.sandbox import hide_from_sandbox

        hide_from_sandbox(p)
    except Exception:  # noqa: BLE001
        pass


def host() -> str:
    s = settings()
    return (urlparse(s["url"]).hostname or "") if s else ""


class _Req:
    def __init__(self, rid: str, method: str, path: str, headers: dict[str, str]):
        self.rid, self.method, self.path, self.headers = rid, method, path, headers
        self.body = bytearray()
        self.cancelled = False
        self.conn: http.client.HTTPConnection | None = None


class RelayClient:
    """Stays connected to the relay server, forwards incoming requests to the local server. Reconnects on drop."""

    def __init__(self, url: str, key: str, port: int, log: Callable[[str], None] = lambda s: None,
                 connect: Callable[..., Any] | None = None):
        self.url, self.key, self.port, self.log = url.rstrip("/"), key, port, log
        self._connect = connect
        self._ws: Any = None
        self._send_lock = threading.Lock()
        self._reqs: dict[str, _Req] = {}
        self._stop = threading.Event()
        self.connected = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name="alice-relay")

    # ------------------------------------------------------------------ connection
    def start(self) -> "RelayClient":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _ws_url(self) -> str:
        return self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/_alice/pc"

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect(self._ws_url(), self.key)
        from websockets.sync.client import connect

        return connect(self._ws_url(), additional_headers={"X-Relay-Key": self.key}, open_timeout=15,
                       max_size=None, ping_interval=None, close_timeout=3)

    def _loop(self) -> None:
        wait = 2.0
        while not self._stop.is_set():
            try:
                ws = self._open()
            except Exception as e:  # noqa: BLE001 - no internet, no server: retry after a bit
                self.log(f"aktarma bağlanamadı: {e}")
                self._stop.wait(wait)
                wait = min(wait * 2, 120.0)
                continue
            self._ws, self.connected, wait = ws, True, 2.0
            self.log("aktarma sunucusuna bağlandı")
            last_ping = time.monotonic()
            try:
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=5)
                    except TimeoutError:
                        raw = None
                    if time.monotonic() - last_ping > PING_EVERY:
                        self._send_raw("ping")  # keep Cloudflare from closing idle connection
                        last_ping = time.monotonic()
                    if raw is None or raw == "pong":
                        continue
                    self._on_message(raw)
            except Exception as e:  # noqa: BLE001 - connection dropped
                if not self._stop.is_set():
                    self.log(f"aktarma bağlantısı koptu: {e}")
            finally:
                self.connected, self._ws = False, None
                for r in list(self._reqs.values()):
                    self._cancel(r)
                self._reqs.clear()
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(1.0)

    def _send_raw(self, text: str) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send(text)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _send(self, obj: dict[str, Any]) -> bool:
        return self._send_raw(json.dumps(obj, ensure_ascii=False))

    # ------------------------------------------------------------------ requests
    def _on_message(self, raw: str | bytes) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        kind, rid = m.get("t"), str(m.get("rid") or "")
        if kind == "req":
            self._reqs[rid] = _Req(rid, str(m.get("m") or "GET"), str(m.get("p") or "/"), dict(m.get("h") or {}))
        elif kind == "body":
            r = self._reqs.get(rid)
            if r is not None:
                r.body += base64.b64decode(str(m.get("b") or ""))
        elif kind == "bend":
            r = self._reqs.get(rid)
            if r is not None:
                threading.Thread(target=self._forward, args=(r,), daemon=True, name="alice-relay-req").start()
        elif kind == "cancel":
            r = self._reqs.pop(rid, None)
            if r is not None:
                self._cancel(r)

    @staticmethod
    def _cancel(r: _Req) -> None:
        r.cancelled = True
        conn = r.conn
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _forward(self, r: _Req) -> None:
        stream = r.path.split("?", 1)[0] in STREAM_PATHS
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=None if stream else 120)
        r.conn = conn
        try:
            headers = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length",)}
            conn.request(r.method, r.path, body=bytes(r.body) if r.body else None, headers=headers)
            resp = conn.getresponse()
            self._send({"t": "head", "rid": r.rid, "s": resp.status,
                        "h": {k: v for k, v in resp.getheaders() if k.lower() not in ("content-length", "connection")}})
            while not r.cancelled:
                data = resp.read1(CHUNK) if stream else resp.read(CHUNK)
                if not data:
                    break
                if not self._send({"t": "data", "rid": r.rid, "b": base64.b64encode(data).decode()}):
                    break
        except Exception as e:  # noqa: BLE001 - local server did not respond
            if not r.cancelled:
                self._send({"t": "head", "rid": r.rid, "s": 502, "h": {"Content-Type": "application/json"}})
                self._send({"t": "data", "rid": r.rid, "b": base64.b64encode(
                    json.dumps({"ok": False, "error": f"bilgisayar: {e}"}).encode()).decode()})
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._reqs.pop(r.rid, None)
            if not r.cancelled:
                self._send({"t": "end", "rid": r.rid})


def start_for(port: int, log: Callable[[str], None] = lambda s: None) -> RelayClient | None:
    s = settings()
    if s is None:
        return None
    _hide(settings_path())  # tag files written by older versions as well
    return RelayClient(s["url"], s["key"], port, log).start()


# ---------------------------------------------------------------------- installation
def relay_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "relay"


def _npx() -> str:
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        raise SystemExit("Node.js (npx) bulunamadı: https://nodejs.org adresinden kurup tekrar deneyin.")
    return exe


def deploy(out: Callable[[str], None] = print) -> dict[str, str]:
    """Deploy (or update) the relay server to the user's Cloudflare account, set the key, save the address.
    Must have logged into Cloudflare once beforehand from the browser via `npx wrangler login`."""
    import re

    d = relay_dir()
    if not (d / "wrangler.toml").exists():
        raise SystemExit(f"relay klasörü bulunamadı: {d}")
    npx = _npx()
    env = {**os.environ, "WRANGLER_SEND_METRICS": "false"}
    who = subprocess.run([npx, "--yes", "wrangler@4", "whoami"], cwd=d, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env)
    if "not authenticated" in (who.stdout + who.stderr).lower() or who.returncode != 0:
        raise SystemExit("Cloudflare'e giriş yapılmamış. Önce şunu çalıştır ve açılan sayfada izin ver:\n"
                         f"  cd \"{d}\" && npx wrangler login")
    out("Aktarma sunucusu yükleniyor…")
    dep = subprocess.run([npx, "--yes", "wrangler@4", "deploy"], cwd=d, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env)
    text = dep.stdout + dep.stderr
    if dep.returncode != 0:
        raise SystemExit("Yükleme başarısız:\n" + text[-2000:])
    m = re.search(r"https://[\w.-]+\.workers\.dev", text)
    if not m:
        raise SystemExit("Yüklendi ama adres okunamadı:\n" + text[-1500:])
    url = m.group(0)
    old = settings()
    key = old["key"] if old and old["url"] == url else secrets.token_urlsafe(32)
    sec = subprocess.run([npx, "--yes", "wrangler@4", "secret", "put", "PC_KEY"], cwd=d, input=key + "\n",
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if sec.returncode != 0:
        raise SystemExit("Anahtar ayarlanamadı:\n" + (sec.stdout + sec.stderr)[-1500:])
    save_settings(url, key)
    out(f"Hazır: {url}")
    return {"url": url}


def relay_cmd(argv: list[str]) -> int:
    """alice relay [deploy|status|off]"""
    sub = argv[0] if argv else "status"
    if sub == "deploy":
        deploy()
        print("ALICE'i yeniden başlatınca bilgisayar aktarma sunucusuna bağlanır; telefonda QR'ı yeniden okut.")
        return 0
    if sub == "off":
        try:
            settings_path().unlink()
        except OSError:
            pass  # already off
        print("Uzaktan erişim kapatıldı (yeniden başlatınca geçerli).")
        return 0
    s = settings()
    if s is None:
        print("Uzaktan erişim kurulu değil. Kurmak için: alice relay deploy")
        return 1
    import urllib.request

    try:
        st = json.loads(urllib.request.urlopen(s["url"] + "/_alice/status", timeout=10).read())
    except Exception as e:  # noqa: BLE001
        print(f"{s['url']} — ulaşılamadı: {e}")
        return 1
    print(f"{s['url']} — bilgisayar {'bağlı' if st.get('pc') else 'bağlı değil'}")
    return 0
