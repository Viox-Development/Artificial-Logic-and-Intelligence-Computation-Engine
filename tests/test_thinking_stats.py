"""Thinking levels per model and the session statistics in the status bar."""

from __future__ import annotations

from alice.agent import cached_tokens
from alice.providers import thinking
from tests.conftest import tool_call
from tests.test_agent import make_agent


def test_levels_follow_the_model():
    gpt6, kind = thinking.levels_for("chatgpt", "gpt-6-luna")
    assert kind == "graded" and "max" in gpt6 and "xhigh" in gpt6
    assert "max" not in thinking.levels_for("cliproxy", "gpt-5.6-terra")[0]
    claude = thinking.levels_for("anthropic", "claude-opus-5-5")[0]
    assert "off" not in claude and "max" in claude  # Claude cannot turn thinking off
    assert thinking.levels_for("groq", "qwen/qwen3.8-27b")[1] == "binary"
    assert thinking.levels_for("gemini", "gemini-flash-lite-latest") == (["default"], "none")
    assert thinking.levels_for("evren", "glm-5.3") == (["default"], "none")


def test_unsupported_level_falls_to_nearest():
    assert thinking.fit("xhigh", ["off", "low", "medium", "high", "default"]) == "high"
    assert thinking.fit("off", ["low", "medium", "high", "max", "default"]) == "low"
    assert thinking.fit("low", ["off", "high", "default"]) == "off"
    assert thinking.fit("medium", ["default"]) == "default"


def test_cached_tokens_from_each_provider_shape():
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": 90}}) == 90
    assert cached_tokens({"cached_tokens": 5}) == 5
    assert cached_tokens({"prompt_cache_hit_tokens": 7}) == 7  # DeepSeek
    assert cached_tokens({}) == 0


def test_session_stats_count_turns_steps_and_context(workdir):
    agent, _, _ = make_agent([tool_call("list_dir", {"path": "."}), "tamam"], workdir)
    agent.run_turn("listele")
    s = agent.stats_view()
    assert s["turns"] == 1 and s["steps"] == 2
    assert s["ctx"]["system"] > 0 and s["ctx"]["tools"] > 0 and s["ctx"]["total"] >= s["ctx"]["system"]
    assert s["tool_s"] >= 0 and s["llm_s"] >= 0
