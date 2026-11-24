"""Wi-Fi: status, network scan, saved networks, connect/disconnect (Windows `netsh wlan`, Linux NetworkManager `nmcli`).

netsh's key names change with the system language ("Signal"/"Sinyal"), so parsing relies on format, not names:
the SSID line is "SSID" in every language, and the signal is a value ending in "%" in every language.
nmcli is read in terse mode (`-t`) with fixed fields, and the active network is the one marked "*" in IN-USE, so its
output does not depend on the language either.
"""

from __future__ import annotations

import os
import re
import shutil
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


def _linux() -> bool:
    if sys.platform == "win32":
        return False
    if sys.platform.startswith("linux"):
        return True
    raise RuntimeError("Wi-Fi control only works on Windows and Linux (NetworkManager) for now")


def _netsh(*args: str) -> str:
    r = run(["netsh", "wlan", *args])
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(out.strip() or "netsh failed")
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


# ---------------------------------------------------------------------- Linux (nmcli)
def _nmcli(*args: str, timeout: float = 15) -> str:
    exe = shutil.which("nmcli")
    if not exe:
        raise RuntimeError("nmcli not found: Wi-Fi control on Linux needs NetworkManager")
    r = run([exe, *args], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip() or "nmcli failed")
    return r.stdout or ""


def split_terse(line: str) -> list[str]:
    """One line of `nmcli -t` output -> fields. nmcli escapes ':' and '\\' inside values with a backslash."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _rows(text: str, n: int) -> list[list[str]]:
    return [f for line in text.splitlines() if line.strip() and len(f := split_terse(line)) == n]


NM_LIST = "IN-USE,SSID,SIGNAL,SECURITY,CHAN,RATE"


def _signal(val: str) -> int:
    return int(val) if val.strip().isdigit() else 0


def parse_nm_status(text: str) -> dict:
    """`nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY,CHAN,RATE dev wifi list` → {connected, ssid, signal, fields}."""
    row = next((r for r in _rows(text, 6) if r[0].strip() == "*"), None)
    if row is None:
        return {"connected": False, "ssid": "", "signal": None, "fields": []}
    _, ssid, signal, sec, chan, rate = row
    fields = [(k, v) for k, v in (("SSID", ssid), ("Signal", f"{signal}%" if signal else ""),
                                  ("Security", sec or "open"), ("Channel", chan), ("Rate", rate)) if v]
    return {"connected": True, "ssid": ssid, "signal": _signal(signal) if signal else None, "fields": fields}


def parse_nm_networks(text: str) -> list[dict]:
    """`nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY,... dev wifi list` → [{ssid, signal, auth}], one entry per SSID (the
    strongest access point), sorted by signal. An open network's auth is "open", as connect() expects."""
    best: dict[str, dict] = {}
    hidden: list[dict] = []
    for r in _rows(text, 6):
        ssid = "" if r[1] == "--" else r[1]
        net = {"ssid": ssid, "signal": _signal(r[2]), "auth": r[3] if r[3] not in ("", "--") else "open"}
        if not ssid:
            hidden.append(net)
        elif ssid not in best or net["signal"] > best[ssid]["signal"]:
            best[ssid] = net
    return sorted([*best.values(), *hidden], key=lambda n: -n["signal"])


def parse_nm_profiles(text: str) -> list[str]:
    """`nmcli -t -f NAME,TYPE connection show` → saved Wi-Fi connection names."""
    return [name for name, typ in _rows(text, 2) if typ in ("802-11-wireless", "wifi")]


def parse_nm_devices(text: str) -> list[tuple[str, str]]:
    """`nmcli -t -f DEVICE,TYPE,CONNECTION device` → [(device, connection)] for Wi-Fi devices, connected first."""
    devs = [(d, "" if c == "--" else c) for d, t, c in _rows(text, 3) if t == "wifi"]
    return sorted(devs, key=lambda dc: not dc[1])


def _nm_list(rescan: bool) -> str:
    return _nmcli("-t", "-f", NM_LIST, "device", "wifi", "list", "--rescan", "yes" if rescan else "no",
                  timeout=30 if rescan else 15)


# ---------------------------------------------------------------------- public API
def status() -> str:
    s = parse_nm_status(_nm_list(False)) if _linux() else parse_status(_netsh("show", "interfaces"))
    if not s["connected"]:
        return "Not connected to Wi-Fi."
    head = f"Connected: {s['ssid']}" + (f", signal {s['signal']}%" if s["signal"] is not None else "")
    return head + "\n" + "\n".join(f"{k}: {v}" for k, v in s["fields"])


def scan() -> list[dict]:
    if _linux():
        return parse_nm_networks(_nm_list(True))
    return parse_networks(_netsh("show", "networks", "mode=bssid"))


def profiles() -> list[str]:
    if _linux():
        return parse_nm_profiles(_nmcli("-t", "-f", "NAME,TYPE", "connection", "show"))
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
            raise RuntimeError(f"'{ssid}' isn't saved and is encrypted; give the password too")
        if _linux():
            _nmcli("device", "wifi", "connect", ssid, *(("password", password) if password else ()), timeout=45)
            return f"connecting to '{ssid}'"
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(profile_xml(ssid, password))
            _netsh("add", "profile", f"filename={path}", "user=current")
        finally:
            os.unlink(path)
    if _linux():
        _nmcli("connection", "up", "id", ssid, timeout=45)
    else:
        _netsh("connect", f"name={ssid}")
    return f"connecting to '{ssid}'"


def disconnect() -> str:
    if _linux():
        devs = parse_nm_devices(_nmcli("-t", "-f", "DEVICE,TYPE,CONNECTION", "device"))
        if not devs:
            raise RuntimeError("Wi-Fi device not found")
        if not devs[0][1]:
            return "Already not connected to Wi-Fi."
        _nmcli("device", "disconnect", devs[0][0])
    else:
        _netsh("disconnect")
    return "Wi-Fi disconnected"


def radio(on: bool) -> str:
    """Turn the Wi-Fi radio on or off (Linux only; Windows has no netsh equivalent)."""
    if not _linux():
        raise RuntimeError("turning Wi-Fi on and off only works on Linux (nmcli) for now")
    _nmcli("radio", "wifi", "on" if on else "off")
    return "Wi-Fi turned on" if on else "Wi-Fi turned off"
