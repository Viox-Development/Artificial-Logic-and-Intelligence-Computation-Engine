"""Local network helpers: your own IP, subnet scanning, ARP table, Wake-on-LAN."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def run(cmd: list[str], timeout: float = 15, encoding: str | None = None) -> subprocess.CompletedProcess:
    """Run the command; on Windows decode with the console code page (so Turkish netsh/powercfg output is not garbled).
    encoding: for tools that always print in one fixed encoding instead of the console code page (e.g. adb: UTF-8)."""
    enc = encoding or ("oem" if sys.platform == "win32" else "utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding=enc, errors="replace", timeout=timeout,
                              creationflags=flags)
    except subprocess.TimeoutExpired as e:
        # The tools catch OSError, but a raw TimeoutExpired used to reach the user as a long command line.
        raise TimeoutError(f"{Path(cmd[0]).stem} {timeout:g} sn içinde cevap vermedi") from e


def local_ip() -> str:
    """IP of the default network interface (no packet is sent; only the routing table is checked)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def subnet_hosts(ip: str | None = None) -> list[str]:
    """Home networks are almost always /24: 254 addresses, excluding your own IP."""
    ip = ip or local_ip()
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return [str(h) for h in net.hosts() if str(h) != ip]


def network_mismatch(host: str, me: str | None = None) -> str | None:
    """Text explaining why host is not visible on the computer's home network, or None if it is."""
    ip = host.split(":")[0].strip()
    me = me or local_ip()
    try:
        a, b = ipaddress.ip_address(ip), ipaddress.ip_address(me)
    except ValueError:
        return None
    if me.startswith("127.") or not a.is_private:
        return None
    if ipaddress.ip_network(f"{me}/16", strict=False) == ipaddress.ip_network(f"{ip}/16", strict=False) or a == b:
        return None
    vpn = " 10.2.0.x adresi tipik olarak telefonda açık bir VPN'in (ör. Proton VPN) adresidir." \
        if ip.startswith("10.2.0.") else ""
    return (f"{ip} bilgisayarın ağında değil (bilgisayar {me}).{vpn} Telefonda VPN'i kapat, telefonun da aynı Wi-Fi'a "
            f"bağlı olduğundan emin ol; Kablosuz hata ayıklama ekranında {me.rsplit('.', 1)[0]}.x ile başlayan "
            "adres görünmeli.")


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_port(port: int, hosts: list[str] | None = None, timeout: float = 0.4) -> list[str]:
    """Addresses on the subnet with the port open (e.g. 5555: an Android TV with network ADB enabled)."""
    hosts = hosts if hosts is not None else subnet_hosts()
    with ThreadPoolExecutor(64) as ex:
        hits = ex.map(lambda h: h if port_open(h, port, timeout) else None, hosts)
    return [h for h in hits if h]


def open_ports(host: str, ports, timeout: float = 0.25, workers: int = 256) -> list[int]:
    """Open ports on host (to find the phone's wireless debugging port, which changes every time)."""
    with ThreadPoolExecutor(workers) as ex:
        hits = ex.map(lambda p: p if port_open(host, p, timeout) else None, ports)
    return [p for p in hits if p]


# macOS omits leading zeros (0:1a:2b:...), so each byte may be 1-2 digits.
ARP_LINE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\)?\s+(?:at\s+)?([0-9a-fA-F]{1,2}(?:[-:][0-9a-fA-F]{1,2}){5})(?![\w:-])")


def parse_arp(text: str) -> list[tuple[str, str]]:
    """`arp -a` (Windows and Linux/macOS formats) → [(ip, mac)]. Broadcast and multicast addresses are skipped."""
    out = []
    for line in text.splitlines():
        m = ARP_LINE.search(line)
        if not m:
            continue
        ip = m.group(1)
        mac = ":".join(b.zfill(2) for b in re.split(r"[-:]", m.group(2).lower()))
        last = int(ip.rsplit(".", 1)[1])
        if mac in ("ff:ff:ff:ff:ff:ff",) or mac.startswith("01:00:5e") or last in (0, 255) or ip.startswith("224."):
            continue
        if (ip, mac) not in out:
            out.append((ip, mac))
    return out


def _poke(host: str) -> None:
    # Even an empty UDP packet triggers ARP resolution; much lighter than ping.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(b"", (host, 9))
    except OSError:
        pass
    finally:
        s.close()


def _hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def devices(sweep: bool = True) -> list[dict[str, str]]:
    """Devices on the network: ip, mac, and name if any. sweep: first touch every subnet address so ARP fills up."""
    import time

    me = local_ip()
    if sweep:
        with ThreadPoolExecutor(64) as ex:
            list(ex.map(_poke, subnet_hosts(me)))
        time.sleep(1.0)
    prefix = me.rsplit(".", 1)[0] + "."
    found = [(ip, mac) for ip, mac in parse_arp(run(["arp", "-a"]).stdout) if ip.startswith(prefix)]
    with ThreadPoolExecutor(32) as ex:
        names = list(ex.map(lambda p: _hostname(p[0]), found))
    return [{"ip": ip, "mac": mac, "name": n} for (ip, mac), n in zip(found, names)]


def magic_packet(mac: str) -> bytes:
    hexs = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(hexs) != 12:
        raise ValueError(f"geçersiz MAC adresi: {mac}")
    return b"\xff" * 6 + bytes.fromhex(hexs) * 16


def wake_on_lan(mac: str, broadcast: str = "255.255.255.255") -> None:
    pkt = magic_packet(mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(pkt, (broadcast, port))
    finally:
        s.close()
