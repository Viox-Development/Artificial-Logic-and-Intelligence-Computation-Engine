"""now: answers "Nasılım?" and "ne durumdayım?" ("how am I?", "what's my status?") with one snapshot (alice/now.py)."""

from __future__ import annotations

from alice.tools.base import READ, Tool, ToolContext, ToolResult, obj


class NowState(Tool):
    name = "now"
    description = (
        "The user's current state in one place: mode, the program in front, whether they are in a game, today's "
        "flights (how many, average length, whether an enemy was behind them before recent deaths), heart rate, the "
        "expected play time from their habits, and a suggestion.\n"
        "- Use it for: \"how am I doing\", \"what's my status\", \"should I take a break\", \"how is today going\".\n"
        "- Don't use it for: past measurement charts (metrics), live flight data (flight)."
    )
    parameters = obj({}, [])
    risk = READ
    parallel_safe = False  # the sqlite connection is shared via ctx.state

    def run(self, ctx: ToolContext) -> ToolResult:
        from alice import now
        from alice.tools.core import _store

        return ToolResult(now.describe(now.snapshot(_store(ctx))))


TOOLS = [NowState()]
