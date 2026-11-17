"""ZTE ZXHN modems (H3600, H1600 and newer firmware using the same web UI): login, connected devices, restart.

The endpoints the web UI itself uses:
* Login: `?_type=loginData&_tag=login_entry` (JSON, session key) + `login_token` (salt);
  the password is sent as sha256(password + salt).
* Pages are opened by entering a menu (`?_type=menuView&_tag=<menu>`); the menu page gives a new temporary session key
  inside JavaScript (`_sessionTmpToken = "\\x.."`).
* Some firmware versions encrypt the sha256 of the POST body with the modem's public RSA key and expect it in the
  `Check` header; added when `cryptography` is installed.
The modem password is in `.env` as MODEM_PASSWORD (the username is in config under [modem] user).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx

# Public key from the ZTE web UI's JavaScript (H1600/H3600 v7+ firmware).
CHECK_KEY = ("-----BEGIN PUBLIC KEY-----\n"
             "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAodPTerkUVCYmv28SOfRV\n"
             "7UKHVujx/HjCUTAWy9l0L5H0JV0LfDudTdMNPEKloZsNam3YrtEnq6jqMLJV4ASb\n"
             "1d6axmIgJ636wyTUS99gj4BKs6bQSTUSE8h/QkUYv4gEIt3saMS0pZpd90y6+B/9\n"
             "hZxZE/RKU8e+zgRqp1/762TB7vcjtjOwXRDEL0w71Jk9i8VUQ59MR1Uj5E8X3WIc\n"
             "fYSK5RWBkMhfaTRM6ozS9Bqhi40xlSOb3GBxCmliCifOJNLoO9kFoWgAIw5hkSIb\n"
             "GH+4Csop9Uy8VvmmB+B3ubFLN35qIa5OG5+SDXn4L7FeAA5lRiGxRi8tsWrtew8w\n"
             "nwIDAQAB\n"
             "-----END PUBLIC KEY-----")


class ModemError(RuntimeError):
    pass


def check_header(body: str) -> str | None:
    try:
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        return None
    key = load_pem_public_key(CHECK_KEY.encode())
    digest = hashlib.sha256(body.encode()).hexdigest().encode()
    return base64.b64encode(key.encrypt(digest, padding.PKCS1v15())).decode()  # type: ignore[union-attr]


def tmp_token(page: str) -> str | None:
    """Decode the key from the `_sessionTmpToken = "\\x41\\x42..."` line of the menu page."""
    m = re.search(r'_sessionTmpToken\s*=\s*"([^"]+)"', page)
    if not m:
        return None
    raw = m.group(1)
    if "\\x" in raw:
        # Only \xNN escapes are decoded; stray plain characters don't raise ValueError the way bytes.fromhex does.
        text = re.sub(r"\\x([0-9a-fA-F]{2})", lambda h: chr(int(h.group(1), 16)), raw)
        return text.encode("latin-1", "replace").decode("utf-8", "replace")
    return raw


def instances(xml_text: str) -> list[dict[str, str]]:
    """Each <Instance> in the ZTE XML → {ParaName: ParaValue}."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for inst in root.iter("Instance"):
        kids = list(inst)
        d = {}
        for i, k in enumerate(kids):
            if k.tag == "ParaName" and i + 1 < len(kids) and kids[i + 1].tag == "ParaValue":
                d[(k.text or "").strip()] = (kids[i + 1].text or "").strip()
        if d:
            out.append(d)
    return out


def _pick(d: dict[str, str], *names: str) -> str:
    low = {k.casefold(): v for k, v in d.items()}
    for n in names:
        if low.get(n.casefold()):
            return low[n.casefold()]
    return ""


def device_rows(lan_xml: str, wifi_xml: str) -> list[dict[str, str]]:
    """Merge the wired and wireless lists into one list (unique by MAC)."""
    rows: dict[str, dict[str, str]] = {}
    for xml_text, link in ((lan_xml, "kablo"), (wifi_xml, "Wi-Fi")):
        for d in instances(xml_text):
            mac = _pick(d, "MACAddress", "MacAddr", "MAC").lower()
            if not mac:
                continue
            row = rows.setdefault(mac, {"mac": mac, "ip": "", "name": "", "link": link})
            row["ip"] = row["ip"] or _pick(d, "IPAddress", "IPAddr", "IP")
            row["name"] = row["name"] or _pick(d, "HostName", "DevName", "AliasName", "Name")
            if link == "Wi-Fi":
                row["link"] = "Wi-Fi" + (f" ({ssid})" if (ssid := _pick(d, "ESSID", "SSIDName", "SSID")) else "")
    return list(rows.values())


class ZTE:
    def __init__(self, host: str, user: str, password: str, client: httpx.Client | None = None):
        if not password:
            raise ModemError("modem şifresi yok: ~/.alice/.env dosyasına MODEM_PASSWORD=... satırını ekle "
                             "(şifre genelde modemin altındaki etikette yazar)")
        self.base = f"http://{host}/"
        self.user, self.password = user or "admin", password
        self.c = client or httpx.Client(timeout=8)
        self.token = ""
        self.post_token = ""
        self.menu_now = ""

    def _req(self, path: str, data: str | None = None) -> str:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            if chk := check_header(data):
                headers["Check"] = chk
            r = self.c.post(self.base + path, content=data, headers=headers)
        else:
            r = self.c.get(self.base + path, headers=headers)
        r.raise_for_status()
        return r.text

    @staticmethod
    def _login_error(info: dict) -> None:
        try:
            lock = int(info.get("lockingTime") or 0)
        except (TypeError, ValueError):
            lock = 0
        msg = info.get("loginErrMsg") or ""
        if lock > 0:  # -1 means no lock; it used to give the wrong error "modem girişi -1 sn kilitli"
            raise ModemError(f"modem girişi {lock} sn kilitli (çok fazla yanlış deneme); sonra tekrar dene")
        if msg:
            raise ModemError(f"modem girişi reddedildi ({msg}): kullanıcı adı ya da şifre yanlış olabilir")

    def login(self) -> None:
        self.menu_now = ""
        try:
            entry = json.loads(self._req("?_type=loginData&_tag=login_entry"))
        except ValueError as e:
            raise ModemError("bu modemin giriş sayfası beklenen ZTE biçiminde değil") from e
        if not isinstance(entry, dict):
            raise ModemError("bu modemin giriş sayfası beklenen ZTE biçiminde değil")
        self._login_error(entry)
        self.token = entry.get("sess_token", "")
        m = re.search(r"<ajax_response_xml_root>(.*?)</ajax_response_xml_root>",
                      self._req("?_type=loginData&_tag=login_token"))
        if not m:
            raise ModemError("modem giriş tuzunu vermedi")
        hashed = hashlib.sha256((self.password + m.group(1)).encode()).hexdigest()
        body = (f"Password={hashed}&Username={quote(self.user)}&_sessionTOKEN={self.token}"
                f"&_sessionTOKENByPost={self.token}&action=login")
        try:
            res = json.loads(self._req("?_type=loginData&_tag=login_entry", body))
        except ValueError as e:
            raise ModemError("modem giriş cevabı beklenen ZTE biçiminde değil") from e
        if not isinstance(res, dict):
            raise ModemError("modem giriş cevabı beklenen ZTE biçiminde değil")
        self._login_error(res)
        self.token = res.get("sess_token", self.token)

    def _page(self, path: str, query: bool = False, data: str | None = None, retried: bool = False) -> str:
        text = self._req(path, data)
        if "<IF_ERRORSTR>SessionTimeout</IF_ERRORSTR>" in text:
            if retried:
                raise ModemError("modem oturumu açılamadı")
            menu, self.menu_now = self.menu_now, ""
            self.login()
            if menu:
                self.menu(menu)
            return self._page(path, query, data, True)
        if "?_type=menuView" in path:
            if tok := tmp_token(text):
                self.token = tok
        if query:
            if "<IF_ERRORSTR>SUCC</IF_ERRORSTR>" not in text:
                raise ModemError("modem isteği reddetti")
            if m := re.search(r"<_sessionTmpTokenByPost>(.*?)</_sessionTmpTokenByPost>", text):
                self.post_token = m.group(1)
        return text

    def menu(self, name: str, force: bool = False) -> str:
        if self.menu_now == name and not force:
            return ""
        text = self._page(f"?_type=menuView&_tag={name}&Menu3Location=0")
        self.menu_now = name
        return text

    def devices(self) -> list[dict[str, str]]:
        self.menu("localNetStatus")
        lan = self._page("?_type=menuData&_tag=accessdev_landevs_lua.lua")
        wifi = self._page("?_type=menuData&_tag=accessdev_ssiddev_lua.lua")
        return device_rows(lan, wifi)

    def restart(self) -> None:
        self.menu("statusMgr", force=True)
        self._page("?_type=menuData&_tag=devmgr_statusmgr_lua.lua", query=True)
        self.menu("rebootAndReset", force=True)
        try:
            self._page("?_type=menuData&_tag=devmgr_restartmgr_lua.lua", query=True,
                       data=f"IF_ACTION=Restart&Btn_restart=&_sessionTOKEN={self.token}"
                            f"&_sessionTOKENByPost={self.post_token}")
        except (httpx.TransportError, ModemError):
            # The modem may go down right after getting the request and cut the reply; check if it really went down.
            try:
                self.c.get(self.base, timeout=3)
            except httpx.TransportError:
                return
            raise

    def logout(self) -> None:
        try:
            self._req("?_type=loginData&_tag=logout_entry", f"IF_LogOff=1&_sessionTOKEN={self.token}")
        except httpx.HTTPError:
            pass

    def close(self) -> None:
        self.c.close()
