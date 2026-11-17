"""timeline: search or delete texts seen on screen along the timeline (alice/timeline.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import DANGER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class TimelineSearch(Tool):
    name = "timeline"
    description = (
        "Zaman makinesi hafıza: kullanıcının ekranında daha önce görünmüş yazıları (yerel, şifreli kayıt) arar.\n"
        "- Kullan: \"geçen hafta Discord'da paylaşılan linki bul\", \"dün okuduğum o makale neydi\", \"şu hata mesajını "
        "ne zaman görmüştüm\".\n"
        "- Kullanma: dosya içeriği için grep/find_files, geçmiş sohbetler için memory search_chats.\n"
        "action=wipe bütün kaydı siler (her zaman onay ister). Kayıt kapalıysa boş döner."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["search", "wipe"], "description": "search: ara. wipe: hepsini sil."},
        "query": {"type": "string", "description": "search: aranan kelimeler (hepsi geçmeli), ör. 'discord link'."},
        "days": {"type": "number", "description": "search: kaç gün geriye. Varsayılan 30."},
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
