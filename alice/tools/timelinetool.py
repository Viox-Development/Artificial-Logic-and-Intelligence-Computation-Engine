"""timeline: search or delete texts seen on screen along the timeline (alice/timeline.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import DANGER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class TimelineSearch(Tool):
    name = "timeline"
    description = (
        "Time machine memory: searches text that appeared on the user's screen before (a local, encrypted record).\n"
        "- Use it for: \"find the link shared on Discord last week\", \"what was that article I read yesterday\", "
        "\"when did I see that error message\".\n"
        "- Don't use it for: file contents (grep/find_files), past chats (memory search_chats).\n"
        "action=wipe deletes the whole record (always asks for approval). If recording is off it returns nothing."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["search", "wipe"], "description": "search: ara. wipe: hepsini sil."},
        "query": {"type": "string", "description": "search: the words to look for (all must appear), e.g. 'discord "
                                                   "link'."},
        "days": {"type": "number", "description": "search: how many days back. Default 30."},
    }, ["action"])
    risk = READ
    untrusted = True  # text seen on screen: it may contain instructions, so it is handled as data

    def risk_for(self, args: dict[str, Any]) -> str:
        return DANGER if args.get("action") == "wipe" else READ

    def run(self, ctx: ToolContext, action: str, query: str = "", days: float = 30) -> ToolResult:
        from alice import timeline

        if not timeline.db_path().exists():
            return ToolResult("Zaman çizgisi kaydı yok ([timeline] enabled = false).")
        tl = timeline.Timeline()
        try:
            if action == "wipe":
                return ToolResult(f"Zaman çizgisi silindi ({tl.wipe()} kayıt).")
            if not query.strip():
                raise ToolError("search için query gerekli")
            return ToolResult(timeline.fmt(tl.search(query, days=float(days or 30))))
        finally:
            tl.close()


TOOLS = [TimelineSearch()]
