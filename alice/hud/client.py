"""HUD client: show("arrow", clock=4, text="SAM 12 km", ttl=3).

Sends a single datagram via UDP to 127.0.0.1: no connection, no response expected. If HUD is closed, the packet drops, nothing
happens; flight announcement or voice never waits because of HUD.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from alice.hud import PORT

_port = PORT
_sock: socket.socket | None = None
_lock = threading.Lock()
active = False  # core started HUD: worth waiting to show (like click preview)


def configure(port: int, on: bool = True) -> None:
    global _port, active
    _port = int(port)
    active = on


def send(msg: dict[str, Any]) -> bool:
    """Send the command. Quietly False if unable to send (HUD closed, network stack error)."""
    global _sock
    try:
        raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with _lock:
            if _sock is None:
                _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _sock.setblocking(False)
            _sock.sendto(raw, ("127.0.0.1", _port))
        return True
    except (OSError, TypeError, ValueError):
        # ICMP of previous packet sent to closed port on Windows returns as ConnectionResetError on next call.
        return False


def show(kind: str, ttl: float = 3.0, **fields: Any) -> bool:
    """Show an item: arrow (clock), edge (clock), label (text, x, y), card (title, text), box (x, y, w, h),
    progress (value 0-1, text). severity: info | warn | critical. Same id replaces the new one."""
    return send({"kind": kind, "ttl": ttl, **fields})


def clear(id: str = "") -> bool:  # noqa: A002 - field name in protocol
    """Clear all or only the item with this id."""
    return send({"kind": "clear", "id": id} if id else {"kind": "clear"})


def toggle() -> bool:
    return send({"kind": "toggle"})
