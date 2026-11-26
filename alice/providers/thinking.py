"""Thinking (reasoning) setting: translated to each provider's own parameter.

Thinking models (GLM, Qwen3, gpt-oss, Gemini 3, Nemotron...) can think for minutes even on simple tasks.
Level per role: off | low | medium | high | xhigh | max ("default" = leave to model). xhigh and max only exist in
ChatGPT input (Extra High, Max); other providers go to high. Ultracode is not a level: it is a switch
next to it (Router.ultracode, on until ALICE restarts) that makes substantive work run as workflows (alice/workflow;
ULTRACODE_NOTE, added by the agent) at whatever level is set. config.toml:

    [thinking]
    default = "low"
    brain = "low"
    fast = "off"

If the provider does not recognize the parameter, the router retries the request once without settings and never
sends it to that model again.
"""

from __future__ import annotations

import re
from typing import Any

LEVELS = ("off", "low", "medium", "high", "xhigh", "max", "default")
ULTRACODE_NOTE = (
    "\n\n[ultracode: the user wants the most thorough result for this request and accepts that it costs more. If the "
    "request is a question or a small job, just do it. Otherwise use the workflow tool: first scout inline (list the "
    "files, find the parts, read what decides the split), then run a workflow for each phase (understand, design, "
    "change, verify) with the work list you found, read its result and decide the next phase yourself. Two agents "
    "running at the same time never write to the same files (or use isolation='worktree'); every prompt is a complete "
    "brief. Verify adversarially: separate agents try to prove each finding or change wrong, what they confirm is "
    "fixed, and you check the whole yourself before you report. If you limited the coverage, say so. Not every agent "
    "needs this model or effort: give mechanical stages a cheaper model or effort='low'.{models}]")
DEFAULTS = {"default": "low", "fast": "off", "voice": "off", "live-think": "medium"}


_TARGET_RX = re.compile(r"(?<![\w.+])\+\s?(\d{1,6}(?:[.,]\d{1,3})?)\s?([km])\b", re.I)
# Messages ALICE's own background work sends to the chat: their text is a result, not something the user typed.
BACKGROUND_HEADS = ("[Workflow ", "[Subagent ", "[Session finished]", "[Command ")


def from_background(text: str) -> bool:
    return (text or "").startswith(BACKGROUND_HEADS)


def wants_ultracode(text: str) -> bool:
    """The user wrote the word in this message."""
    return not from_background(text) and bool(re.search(r"\bultracode\b", text or "", re.I))


def token_target(text: str) -> int | None:
    """The "+500k" / "+2m" token target in the user's message (the budget of workflows started in that turn)."""
    m = None if from_background(text) else _TARGET_RX.search(text or "")
    if m is None:
        return None
    return int(float(m.group(1).replace(",", ".")) * (1_000 if m.group(2).lower() == "k" else 1_000_000)) or None


def level_for(config_data: dict[str, Any], role: str) -> str:
    t = config_data.get("thinking", {}) or {}
    lvl = str(t.get(role) or t.get("default") or DEFAULTS.get(role) or DEFAULTS["default"]).lower()
    return lvl if lvl in LEVELS else "default"


BINARY = "binary"  # binary only: interface shows "off / on" (off and high)


def levels_for(provider: str, model: str) -> tuple[list[str], str]:
    """Thinking levels and type ("graded" | "binary" | "none") actually supported by this model. The interface
    shows only these: previously all seven levels appeared on every model, and most were silently converted to
    something else (e.g., xhigh = high in Gemini). "default" (leave to model) is always present."""
    m = model.lower()
    if provider == "chatgpt" or (provider == "cliproxy" and m.startswith(("gpt-", "codex", "o3", "o4"))):
        lv = ["off", "low", "medium", "high", "xhigh"] + (["max"] if "gpt-6" in m else [])
        return lv + ["default"], "graded"
    if provider == "anthropic" or (provider == "cliproxy" and m.startswith("claude-")):
        lv = ["low", "medium", "high", "max"]  # Claude does not disable thinking entirely: at least "low"
        if "haiku" not in m:
            lv.insert(3, "xhigh")
        return (["off"] if provider == "cliproxy" else []) + lv + ["default"], "graded"
    if "gpt-oss" in m:
        return ["low", "medium", "high", "default"], "graded"
    if provider == "gemini" or (provider == "cliproxy" and m.startswith("gemini-")):
        if "flash-lite" in m:
            return ["default"], "none"  # rejects request with 400 (Nov 11 log)
        return ["off", "low", "medium", "high", "default"], "graded"
    if provider in ("openrouter", "ollama"):
        return ["off", "low", "medium", "high", "default"], "graded"
    if provider == "groq" and "qwen" in m:
        return ["off", "high", "default"], BINARY
    if provider in ("nvidia", "sambanova", "cerebras") and any(
            k in m for k in ("glm", "qwen3", "deepseek", "nemotron", "kimi", "magistral")):
        return ["off", "high", "default"], BINARY
    if provider == "cliproxy" and m.startswith("kimi-"):
        return ["off", "high", "default"], BINARY
    return ["default"], "none"


def fit(level: str, levels: list[str]) -> str:
    """Nearest supported level if the selected level is not in this model (so settings don't silently break on model change)."""
    if level in levels:
        return level
    order = ["off", "low", "medium", "high", "xhigh", "max"]
    if level not in order:
        return "default"
    i = order.index(level)
    for d in range(1, len(order)):
        for j in (i - d, i + d):  # first one below (faster), then one above
            if 0 <= j < len(order) and order[j] in levels:
                return order[j]
    return "default"


def _on(level: str) -> bool:
    # On models supporting only binary on/off, "low" is also off: goal is short response.
    return level in ("medium", "high")


def params(provider: str, model: str, level: str) -> dict[str, Any]:
    """Level -> fields to add to request body. Empty for unknown provider/model."""
    if level == "default" or not level:
        return {}
    if provider == "chatgpt":
        # ChatGPT input (Responses API, reasoning.effort). gpt-6-luna: none/low/medium/high/xhigh/max; rejects
        # "minimal" with 400 and error is counted as "not_found", marking the model dead for the session, so off -> none.
        return {"reasoning_effort": "none" if level == "off" else level}
    if provider == "anthropic":  # Claude API: output_config.effort (low..max); cannot be disabled, "off" is lowest thinking
        return {"reasoning_effort": "low" if level == "off" else level}
    if provider == "cliproxy":
        # Local CLIProxyAPI (OpenAI compatible endpoint): translates reasoning_effort itself to Claude's adaptive
        # thinking and Codex's reasoning.effort. "none" closes/thinks less in both; xhigh/max go as-is.
        return {"reasoning_effort": "none" if level == "off" else level}
    if level in ("xhigh", "max"):
        level = "high"  # highest level in other providers
    m = model.lower()
    effort = "low" if level == "off" else level
    if "gpt-oss" in m:  # OpenAI open-weights models: cannot be disabled, at least "low"
        return {"reasoning_effort": effort}
    if provider == "gemini":
        return {"reasoning_effort": "none" if level == "off" else level}
    if provider == "openrouter":
        return {"reasoning": {"enabled": False}} if level == "off" else {"reasoning": {"effort": level}}
    if provider == "groq":
        if "qwen" in m:
            return {"reasoning_effort": "default" if _on(level) else "none"}
        return {}
    if provider in ("nvidia", "sambanova", "cerebras"):
        if any(k in m for k in ("glm", "qwen3", "deepseek", "nemotron", "kimi", "magistral")):
            on = _on(level)
            return {"chat_template_kwargs": {"enable_thinking": on, "thinking": on}}
        return {}
    if provider == "ollama":
        return {"reasoning_effort": "none" if level == "off" else level}
    return {}
