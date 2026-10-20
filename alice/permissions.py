"""İzin modları.

plan : sadece okuma/araştırma; dosya değiştiren, komut çalıştıran, ekrana dokunan her şey reddedilir
ask  : okuma dışındaki her şey onay ister
auto : tehlikeli komutlar (rm -r, format, registry, shutdown...), sistem klasörlerine yazma ve
       başkasına giden mesajlar (send: Discord/WhatsApp/e-posta) onay ister; geri kalan her şey
       (dosya düzenleme, komut, fare/klavye) serbest
yolo : hiçbir şey sormaz
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, NETWORK, READ, SEND, WRITE, Tool, ToolContext

MODES = ("plan", "ask", "auto", "yolo")

SYSTEM_DIRS_WIN = [r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata\microsoft"]
SYSTEM_DIRS_POSIX = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/var/lib", "/System", "/Library"]


def is_system_path(p: Path) -> bool:
    s = str(p)
    if sys.platform == "win32":
        low = s.lower()
        return any(low == d or low.startswith(d + "\\") for d in SYSTEM_DIRS_WIN)
    return any(s == d or s.startswith(d + "/") for d in SYSTEM_DIRS_POSIX)


def _confirm_outward(ctx: ToolContext) -> bool:
    cfg = getattr(ctx, "config", None)
    agent = getattr(cfg, "agent", None) or {}
    return bool(agent.get("confirm_outward", True)) if isinstance(agent, dict) else True


class Permissions:
    def __init__(self, mode: str = "auto"):
        if mode not in MODES:
            raise ValueError(f"geçersiz izin modu: {mode}")
        self.mode = mode
        self.always_allow: set[str] = set()

    def decide(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> str:
        """'allow', 'ask' ya da 'deny' döndürür."""
        if self.mode == "yolo":
            return "allow"
        risk = tool.risk_for(args)
        if risk == READ:
            return "allow"
        if self.mode == "plan":
            return "allow" if risk == NETWORK else "deny"
        if tool.name in self.always_allow and risk != DANGER:
            return "allow"
        if self.mode == "ask":
            return "ask"
        # auto
        if risk == DANGER:
            return "ask"
        if risk == SEND and _confirm_outward(ctx):
            return "ask"
        if risk == WRITE and "path" in args:
            try:
                if is_system_path(ctx.resolve(str(args["path"]))):
                    return "ask"
            except (OSError, ValueError):
                return "ask"
        return "allow"
