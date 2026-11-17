"""Wi-Fi: status, network scan, saved networks, connect/disconnect (Windows `netsh wlan`).

netsh's key names change with the system language ("Signal"/"Sinyal"), so parsing relies on format, not names:
the SSID line is "SSID" in every language, and the signal is a value ending in "%" in every language.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from xml.sax.saxutils import escape

from alice.home.net import run

KV = re.compile(r"^\s*(.+?)\s*:\s(.*)$")
# Turkish Windows can put the percent sign first ("%87"); English puts it last ("87%").
PERCENT = re.compile(r"(?:(\d{1,3})\s?%|%\s?(\d{1,3}))")


def _percent(val: str) -> int | None:
    m = PERCENT.fullmatch(val.strip())
    return int(m.group(1) or m.group(2)) if m else None


def _windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Wi-Fi kontrolü şimdilik yalnız Windows'ta çalışıyor")


def _netsh(*args: str) -> str:
    _windows()
    r = run(["netsh", "wlan", *args])
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(out.strip() or "netsh hata verdi")
    return out


def parse_status(text: str) -> dict:
    """`netsh wlan show interfaces` → {connected, ssid, signal, fields}. fields: raw key/value pairs."""
    fields = [(m.group(1).strip(), m.group(2).strip()) for line in text.splitlines() if (m := KV.match(line))]
    ssid = next((v for k, v in fields if k == "SSID"), "")
    signal = next((p for _, v in fields if (p := _percent(v)) is not None), None)
    return {"connected": bool(ssid), "ssid": ssid, "signal": signal, "fields": fields}


def parse_networks(text: str) -> list[dict]:
    """`netsh wlan show networks mode=bssid` → [{ssid, signal, auth}] sorted by signal."""
    nets: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^SSID \d+\s*:\s?(.*)$", line.strip())
        if m:
            cur = {"ssid": m.group(1).strip(), "signal": 0, "auth": ""}
            nets.append(cur)
            continue
        if cur is None or not (kv := KV.match(line)):
            continue
        key, val = kv.group(1).strip().casefold(), kv.group(2).strip()
        if (pct := _percent(val)) is not None:
            cur["signal"] = max(cur["signal"], pct)
        elif not cur["auth"] and ("auth" in key or "kimlik" in key):
            cur["auth"] = val
    return sorted(nets, key=lambda n: -n["signal"])


def parse_profiles(text: str) -> list[str]:
    """`netsh wlan show profiles` → saved network names."""
    out = []
    for line in text.splitlines():
        if line.startswith("    ") and (m := KV.match(line)) and m.group(2).strip():
            out.append(m.group(2).strip())
    return out


def status() -> str:
    s = parse_status(_netsh("show", "interfaces"))
    if not s["connected"]:
        return "Wi-Fi'a bağlı değil."
    head = f"Bağlı: {s['ssid']}" + (f", sinyal %{s['signal']}" if s["signal"] is not None else "")
    return head + "\n" + "\n".join(f"{k}: {v}" for k, v in s["fields"])


def scan() -> list[dict]:
    return parse_networks(_netsh("show", "networks", "mode=bssid"))


def profiles() -> list[str]:
    return parse_profiles(_netsh("show", "profiles"))


def profile_xml(ssid: str, password: str) -> str:
    """WPA2-Personal (AES) profile; for an open network the password is empty."""
    name = escape(ssid)
    if password:
        sec = ("<authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption>"
               "<useOneX>false</useOneX></authEncryption><sharedKey><keyType>passPhrase</keyType>"
               f"<protected>false</protected><keyMaterial>{escape(password)}</keyMaterial></sharedKey>")
    else:
        sec = ("<authEncryption><authentication>open</authentication><encryption>none</encryption>"
               "<useOneX>false</useOneX></authEncryption>")
    return ('<?xml version="1.0"?><WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
            f"<name>{name}</name><SSIDConfig><SSID><name>{name}</name></SSID></SSIDConfig>"
            "<connectionType>ESS</connectionType><connectionMode>auto</connectionMode>"
            f"<MSM><security>{sec}</security></MSM></WLANProfile>")


def connect(ssid: str, password: str = "") -> str:
    known = profiles()
    if ssid not in known:
        if not password and any(n["ssid"] == ssid and n["auth"].casefold() not in ("open", "açık") for n in scan()):
            raise RuntimeError(f"'{ssid}' kayıtlı değil ve şifreli; şifreyi de ver")
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(profile_xml(ssid, password))
            _netsh("add", "profile", f"filename={path}", "user=current")
        finally:
            os.unlink(path)
    _netsh("connect", f"name={ssid}")
    return f"'{ssid}' ağına bağlanılıyor"


def disconnect() -> str:
    _netsh("disconnect")
    return "Wi-Fi bağlantısı kesildi"
