"""BUDDY and KAIROS tools: your buddy's state, petting/feeding; the background watch's status, on/off, wake now."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class Buddy(Tool):
    name = "buddy"
    description = ("BUDDY, kullanıcının küçük arkadaşı (Tamagotchi). status: çizimi, adı, türü, nadirliği, seviyesi ve "
                   "tokluk/enerji/keyif. pet: okşa. feed: besle (yarım saatte bir). rename: value ile yeni ad.")
    parameters = obj({"action": {"type": "string", "enum": ["status", "pet", "feed", "rename"],
                                 "description": "Yapılacak iş."},
                      "value": {"type": "string", "description": "rename: yeni ad."}}, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str, value: str = "") -> ToolResult:
        from alice import buddy

        if action == "status":
            return ToolResult(buddy.card(ctx.config))
        return ToolResult(buddy.act(action, value))


class Kairos(Tool):
    name = "kairos"
    description = ("KAIROS, ALICE'in arka plan nöbeti: kendi kendine uyanıp durumu kollar, yalnız faydalı bir şey "
                   "varsa bildirir. status: açık mı, sıradaki uyanış, son uyanışlar. on/off: aç ya da kapat. wake: "
                   "şimdi uyandır.")
    parameters = obj({"action": {"type": "string", "enum": ["status", "on", "off", "wake"],
                                 "description": "Yapılacak iş."}}, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "status" else WRITE

    def run(self, ctx: ToolContext, action: str) -> ToolResult:
        from alice import kairos

        if action == "status":
            return ToolResult(describe(ctx.config))
        if action == "wake":
            kairos.request_wake()
            return ToolResult("KAIROS çekirdeğin bir sonraki turunda uyanacak (çekirdek açık olmalı).")
        if action in ("on", "off"):
            kairos.set_enabled(ctx.config, action == "on")
            return ToolResult("KAIROS " + ("açıldı: ilk uyanış birkaç dakika içinde." if action == "on" else "kapatıldı."))
        raise ToolError(f"bilinmeyen action: {action}")


def describe(cfg: Any) -> str:
    from alice import kairos

    v = kairos.view()
    on = kairos.is_enabled(cfg)
    rows = [f"KAIROS {'açık' if on else 'kapalı'}"]
    if on and v["next"]:
        rows.append("sıradaki uyanış: " + datetime.fromtimestamp(v["next"]).strftime("%d.%m %H:%M"))
    if v["skipped"]:
        rows.append(f"son ertelemenin nedeni: {v['skipped']}")
    rows.append(f"bugün {v['wakes']} uyanış")
    for h in v["history"][:8]:
        rows.append(f"- {datetime.fromtimestamp(h['ts']).strftime('%d.%m %H:%M')}: {h.get('summary', '')}")
    return "\n".join(rows)


TOOLS = [Buddy(), Kairos()]
