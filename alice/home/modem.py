"""Modem/router: model detection, external IP, connection status, uptime, traffic.

Works even when the model is unknown, using the manufacturer-independent UPnP IGD (on by default on most home modems):
SSDP finds the modem, the description XML gives brand/model, and SOAP reads the WAN info. If UPnP is off, the
gateway's admin page title is used to guess the model.
"""

from __future__ import annotations

import re
import socket
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import httpx

SSDP_ADDR = ("239.255.255.250", 1900)
IGD = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
WAN_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")
WAN_COMMON = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def discover(timeout: float = 2.5) -> list[str]:
    """SSDP M-SEARCH → description URLs (LOCATION) of internet gateway devices."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           f"MX: 2\r\nST: {IGD}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.5)
    out: list[str] = []
    try:
        s.sendto(msg, SSDP_ADDR)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m and (loc := m.group(1).decode(errors="replace")) not in out:
                out.append(loc)
    except OSError:
        pass  # no network or multicast blocked: no modem found
    finally:
        s.close()
    return out


def parse_description(xml_text: str, base: str) -> dict:
    """Description XML → {name, manufacturer, model, model_number, wan: (serviceType, controlURL) | None,
    common: controlURL | None}. Embedded devices (WANDevice > WANConnectionDevice) are searched too."""
    root = ET.fromstring(xml_text)
    dev = next((e for e in root.iter() if _local(e.tag) == "device"), None)

    def field(name: str) -> str:
        if dev is None:
            return ""
        el = next((c for c in dev if _local(c.tag) == name), None)
        return (el.text or "").strip() if el is not None else ""

    services = {}
    for svc in (e for e in root.iter() if _local(e.tag) == "service"):
        vals = {_local(c.tag): (c.text or "").strip() for c in svc}
        if vals.get("serviceType") and vals.get("controlURL"):
            services[vals["serviceType"]] = urljoin(base, vals["controlURL"])
    url_base = next((e.text for e in root.iter() if _local(e.tag) == "URLBase" and e.text), None)
    if url_base:
        services = {k: urljoin(url_base, urlparse(v).path) for k, v in services.items()}
    wan = next(((t, services[t]) for t in WAN_SERVICES if t in services), None)
    return {"name": field("friendlyName"), "manufacturer": field("manufacturer"), "model": field("modelName"),
            "model_number": field("modelNumber"), "wan": wan, "common": services.get(WAN_COMMON)}


def soap(url: str, service: str, action: str, client: httpx.Client | None = None) -> dict[str, str]:
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}"/></s:Body></s:Envelope>')
    c = client or httpx.Client(timeout=4)
    r = c.post(url, content=body, headers={"Content-Type": 'text/xml; charset="utf-8"',
                                           "SOAPAction": f'"{service}#{action}"'})
    r.raise_for_status()
    return parse_soap(r.text)


def parse_soap(text: str) -> dict[str, str]:
    root = ET.fromstring(text)
    resp = next((e for e in root.iter() if _local(e.tag).endswith("Response")), None)
    return {_local(c.tag): (c.text or "").strip() for c in resp} if resp is not None else {}


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_uptime(sec: int) -> str:
    d, rem = divmod(int(sec), 86400)
    h, m = rem // 3600, rem % 3600 // 60
    return (f"{d} gün " if d else "") + f"{h} sa {m} dk"


def page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def gateway_guess() -> str:
    from alice.home.net import local_ip

    return local_ip().rsplit(".", 1)[0] + ".1"


def info() -> str:
    """Human-readable modem report."""
    lines: list[str] = []
    with httpx.Client(timeout=4, follow_redirects=True) as c:
        for loc in discover():
            try:
                d = parse_description(c.get(loc).text, loc)
            except (httpx.HTTPError, httpx.InvalidURL, ET.ParseError, ValueError):
                # LOCATION comes from any device on the network; a bad address or XML must not drop the report.
                continue
            host = urlparse(loc).hostname
            model = " ".join(x for x in (d["manufacturer"], d["model"], d["model_number"]) if x)
            lines.append(f"Modem: {model or d['name'] or 'bilinmiyor'} ({host})")
            if d["wan"]:
                svc, url = d["wan"]
                for action, fn in (("GetExternalIPAddress", lambda r: f"Dış IP: {r.get('NewExternalIPAddress')}"),
                                   ("GetStatusInfo", lambda r: f"Bağlantı: {r.get('NewConnectionStatus')}"
                                    + (f", açık kalma süresi {fmt_uptime(int(r['NewUptime']))}"
                                       if r.get("NewUptime", "").isdigit() else ""))):
                    try:
                        lines.append(fn(soap(url, svc, action, c)))
                    except (httpx.HTTPError, ET.ParseError, ValueError):
                        pass  # the modem doesn't support this query: skip that line
            if d["common"]:
                try:
                    link = soap(d["common"], WAN_COMMON, "GetCommonLinkProperties", c)
                    down, up = link.get("NewLayer1DownstreamMaxBitRate"), link.get("NewLayer1UpstreamMaxBitRate")
                    if down and down.isdigit() and int(down):
                        up_n = int(up) if up and up.isdigit() else 0
                        lines.append(f"Hat hızı: {int(down) / 1e6:.0f} / {up_n / 1e6:.0f} Mbps")
                    rx = soap(d["common"], WAN_COMMON, "GetTotalBytesReceived", c).get("NewTotalBytesReceived", "")
                    tx = soap(d["common"], WAN_COMMON, "GetTotalBytesSent", c).get("NewTotalBytesSent", "")
                    if rx.isdigit() and tx.isdigit():
                        lines.append(f"Trafik (modem açıldığından beri, sayaç taşabilir): indirilen "
                                     f"{fmt_bytes(int(rx))}, gönderilen {fmt_bytes(int(tx))}")
                except (httpx.HTTPError, ET.ParseError, ValueError):
                    pass  # the modem doesn't report line speed or traffic: skip those lines
            break
        if not lines:
            gw = gateway_guess()
            try:
                r = c.get(f"http://{gw}/")
                title = page_title(r.text)
                server = r.headers.get("server", "")
                lines.append(f"UPnP kapalı; ağ geçidi {gw}, yönetim sayfası başlığı: '{title or '-'}'"
                             + (f", sunucu: {server}" if server else ""))
            except httpx.HTTPError:
                lines.append(f"Modem bulunamadı (UPnP kapalı, {gw} cevap vermiyor)")
            lines.append("Modeli kesin öğrenmek için modemin altındaki etikete ya da tarayıcıda yönetim sayfasına bak.")
    return "\n".join(lines)
