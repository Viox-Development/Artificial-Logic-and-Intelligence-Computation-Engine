"""now: answers "Nasılım?" and "ne durumdayım?" ("how am I?", "what's my status?") with one snapshot (alice/now.py)."""

from __future__ import annotations

from alice.tools.base import READ, Tool, ToolContext, ToolResult, obj


class NowState(Tool):
    name = "now"
    description = (
        "Kullanıcının şu anki durumu tek yerde: mod, öndeki program, oyunda mı, bugünün uçuşları (kaç, ortalama "
        "süre, son ölümlerden önce arkada düşman var mıydı), nabız, alışkanlığa göre beklenen oyun saati ve öneri.\n"
        "- Kullan: \"nasılım\", \"ne durumdayım\", \"mola vermeli miyim\", \"bugün nasıl gidiyor\".\n"
        "- Kullanma: geçmiş ölçüm grafikleri için metrics, canlı uçuş verisi için flight."
    )
    parameters = obj({}, [])
    risk = READ
    parallel_safe = False  # the sqlite connection is shared via ctx.state

    def run(self, ctx: ToolContext) -> ToolResult:
        from alice import now
        from alice.tools.core import _store

        return ToolResult(now.describe(now.snapshot(_store(ctx))))


TOOLS = [NowState()]
