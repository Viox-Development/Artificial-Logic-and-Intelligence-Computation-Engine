"""Samsung (Tizen) smart TV: local remote control API.

* Info and power state: http://<tv>:8001/api/v2/ (REST, no password).
* Keys, apps: wss://<tv>:8002 WebSocket. On the first connection the TV shows an "ALICE'e izin ver" prompt;
  once approved, the TV issues a token and later connections don't prompt. Stored in config as [samsung_tv] token.
* Turning on from standby: the network card sleeps with the TV, so Wake-on-LAN is used (the TV's "Mobil ile aç" /
  "Ağ üzerinden aç" must be enabled). The MAC address is read once via REST and saved.
"""

from __future__ import annotations

import base64
import json
import ssl
import time

import httpx

CLIENT_NAME = base64.b64encode(b"ALICE").decode()

# Tizen IDs of common apps; they are also found by name in the TV's installed-app list.
APPS = {
    "youtube": "111299001912", "netflix": "3201907018807", "prime": "3201910019365",
    "disney": "3201901017640", "spotify": "3201606009684", "apple tv": "3201807016597",
    "twitch": "3202203026841", "tv+": "3201905018355",
}
KEYS = {
    "power": "KEY_POWER", "volume_up": "KEY_VOLUP", "volume_down": "KEY_VOLDOWN", "mute": "KEY_MUTE",
    "channel_up": "KEY_CHUP", "channel_down": "KEY_CHDOWN", "home": "KEY_HOME", "back": "KEY_RETURN",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT", "ok": "KEY_ENTER",
    "play": "KEY_PLAY", "pause": "KEY_PAUSE", "play_pause": "KEY_PLAY_BACK", "stop": "KEY_STOP",
    "next": "KEY_FF", "previous": "KEY_REWIND", "source": "KEY_SOURCE", "hdmi": "KEY_HDMI", "menu": "KEY_MENU",
    "guide": "KEY_GUIDE", "exit": "KEY_EXIT", **{str(i): f"KEY_{i}" for i in range(10)},
}


class TVError(RuntimeError):
    pass


def info(host: str, timeout: float = 3) -> dict:
    """REST device info. Raises TVError if the TV is fully off (network asleep)."""
    if not host:
        raise TVError("Samsung TV adresi yok: önce find ya da host ile connect")
    try:
        r = httpx.get(f"http://{host}:8001/api/v2/", timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        raise TVError(f"TV'ye ulaşılamadı ({host}): kapalı ya da ağda değil") from e
    if not isinstance(data, dict):
        raise TVError(f"{host}:8001 bir Samsung TV gibi cevap vermedi")
    return data


def power_state(data: dict) -> str:
    """'on' | 'standby'. Older models don't report PowerState: if it answered, it is on."""
    return str((data.get("device") or {}).get("PowerState") or "on").lower()


def is_samsung(data: dict) -> bool:
    d = data.get("device") or {}
    return "samsung" in json.dumps(d).lower() or d.get("type") == "Samsung SmartTV"


def key_message(key: str) -> dict:
    return {"method": "ms.remote.control", "params": {"Cmd": "Click", "DataOfCmd": key, "Option": "false",
                                                      "TypeOfRemote": "SendRemoteKey"}}


def text_message(text: str) -> dict:
    return {"method": "ms.remote.control", "params": {"Cmd": base64.b64encode(text.encode()).decode(),
                                                      "DataOfCmd": "base64", "TypeOfRemote": "SendInputString"}}


def launch_message(app_id: str, meta: str = "") -> dict:
    data = {"appId": app_id, "action_type": "DEEP_LINK" if meta else "NATIVE_LAUNCH"}
    if meta:
        data["metaTag"] = meta
    return {"method": "ms.channel.emit", "params": {"event": "ed.apps.launch", "to": "host", "data": data}}


APPS_REQUEST = {"method": "ms.channel.emit", "params": {"event": "ed.installedApp.get", "to": "host"}}


def find_app(name: str, installed: list[dict]) -> str | None:
    """Name -> appId: the TV's installed list first (e.g. 'exxen'), then the known table. Numeric IDs pass through."""
    n = name.strip().casefold()
    if n.isdigit() or (len(n) > 8 and n.replace(".", "").isalnum() and "." in n):
        return name.strip()
    for a in installed:
        if (a.get("name") or "").casefold() == n:
            return a.get("appId")
    for a in installed:
        if n.replace(" ", "") in (a.get("name") or "").casefold().replace(" ", ""):
            return a.get("appId")
    return APPS.get(n)


def _closed(e: BaseException) -> bool:
    """Is it in the websockets ConnectionClosed family? (The package is optional, so the class name is checked.)"""
    return any(c.__name__ == "ConnectionClosed" for c in type(e).__mro__)


CLOSED_MSG = "TV uzaktan kumanda bağlantısını kapattı: TV kapanmış ya da izin geri alınmış olabilir, tekrar dene"


class Remote:
    def __init__(self, host: str, token: str = "", timeout: float = 30):
        if not host:
            raise TVError("Samsung TV adresi yok: önce find ya da host ile connect")
        self.host, self.token, self.timeout = host, token, timeout
        self.ws = None

    def url(self) -> str:
        u = f"wss://{self.host}:8002/api/v2/channels/samsung.remote.control?name={CLIENT_NAME}"
        return u + (f"&token={self.token}" if self.token else "")

    def __enter__(self) -> "Remote":
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            raise TVError("websockets kurulu değil: pip install websockets") from e
        ctx = ssl.create_default_context()
        # The TV uses its own self-signed certificate; the connection is on the local network only.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            self.ws = connect(self.url(), ssl=ctx, open_timeout=5, close_timeout=2)
        except Exception as e:  # websockets connection errors come from different exception classes
            raise TVError(f"TV'nin uzaktan kumanda bağlantısı açılamadı ({self.host}): {e.__class__.__name__}") from e
        # If approval is pending, a prompt appears on the TV screen; give the user time to allow it.
        deadline = time.monotonic() + (self.timeout if not self.token else 8)
        while time.monotonic() < deadline:
            msg = self._recv(deadline - time.monotonic())
            if msg is None:
                break
            ev = msg.get("event")
            if ev == "ms.channel.connect":
                self.token = (msg.get("data") or {}).get("token") or self.token
                return self
            if ev in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                self.ws.close()
                raise TVError("TV bağlantıya izin vermedi: TV ekranında çıkan soruda 'İzin ver'i seç ve tekrar dene "
                              "(reddedildiyse TV'de Ayarlar > Genel > Harici Aygıt Yöneticisi > Aygıt Bağlantı "
                              "Yöneticisi > Aygıt Listesi'nden ALICE'e izin ver)")
        self.ws.close()
        raise TVError("TV cevap vermedi: ekranda izin sorusu çıktıysa onayla ve tekrar dene")

    def __exit__(self, *exc) -> None:
        if self.ws is not None:
            self.ws.close()

    def _recv(self, timeout: float) -> dict | None:
        try:
            return json.loads(self.ws.recv(timeout=max(0.1, timeout)))
        except TimeoutError:
            return None
        except (ValueError, TypeError):
            return {}
        except Exception as e:
            if _closed(e):
                raise TVError(CLOSED_MSG) from e
            raise

    def send(self, payload: dict) -> None:
        try:
            self.ws.send(json.dumps(payload))
        except Exception as e:
            if _closed(e):
                raise TVError(CLOSED_MSG) from e
            raise

    def keys(self, key: str, times: int = 1) -> None:
        for i in range(max(1, min(int(times or 1), 50))):
            if i:
                time.sleep(0.15)
            self.send(key_message(key))
        time.sleep(0.2)  # if the connection closes immediately, some TVs drop the last key

    def installed_apps(self) -> list[dict]:
        self.send(APPS_REQUEST)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            msg = self._recv(deadline - time.monotonic())
            if msg is None:
                break
            if msg.get("event") == "ed.installedApp.get":
                return list(((msg.get("data") or {}).get("data")) or [])
        return []

    def launch(self, app_id: str, meta: str = "") -> None:
        self.send(launch_message(app_id, meta))
        time.sleep(0.3)
