"""BUDDY and KAIROS tools: your buddy's state, petting/feeding; the background watch's status, on/off, wake now."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class Buddy(Tool):
    name = "buddy"
    description = ("BUDDY, the user's little companion (a Tamagotchi). status: its drawing, name, species, rarity, "
                   "level and fullness/energy/mood. pet: pet it. feed: feed it. play: play a game. rename: new name. "
                   "reroll: draw a new species/companion.")
    parameters = obj({"action": {"type": "string", "enum": ["status", "pet", "feed", "play", "rename", "reroll"],
                                 "description": "What to do."},
                      "value": {"type": "string", "description": "rename: yeni ad."}}, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str, value: str = "") -> ToolResult:
        from alice import buddy

        if action == "status":
            return ToolResult(buddy.card(ctx.config))
        return ToolResult(buddy.act(action, value))


class Kairos(Tool):
    name = "kairos"
    description = ("KAIROS, ALICE's background watch: wakes up by itself to keep an eye on things and notifies only "
                   "when there is something useful. status: whether it is on, the next wake-up, the last wake-ups. "
                   "on/off: turn it on or off. wake: wake it now.")
    parameters = obj({"action": {"type": "string", "enum": ["status", "on", "off", "wake"],
                                 "description": "What to do."}}, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "status" else WRITE

    def run(self, ctx: ToolContext, action: str) -> ToolResult:
        from alice import kairos

        if action == "status":
            return ToolResult(describe(ctx.config))
        if action == "wake":
            kairos.request_wake()
            return ToolResult("KAIROS will wake on the core's next tick (the core must be running).")
        if action in ("on", "off"):
            kairos.set_enabled(ctx.config, action == "on")
            return ToolResult("KAIROS " + ("turned on: the first wake-up within a few minutes." if action == "on" else "turned off."))
        raise ToolError(f"unknown action: {action}")


def describe(cfg: Any) -> str:
    from alice import kairos

    v = kairos.view()
    on = kairos.is_enabled(cfg)
    rows = [f"KAIROS {'on' if on else 'off'}"]
    if on and v["next"]:
        rows.append("next wake-up: " + datetime.fromtimestamp(v["next"]).strftime("%d.%m %H:%M"))
    if v["skipped"]:
        rows.append(f"son ertelemenin nedeni: {v['skipped']}")
    rows.append(f"{v['wakes']} wake-ups today")
    for h in v["history"][:8]:
        rows.append(f"- {datetime.fromtimestamp(h['ts']).strftime('%d.%m %H:%M')}: {h.get('summary', '')}")
    return "\n".join(rows)


TOOLS = [Buddy(), Kairos()]
