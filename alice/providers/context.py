"""The model's context window: how many tokens of conversation it accepts.

The provider's `max_input_tokens` setting is a fallback for unknown models, and serves as an upper bound on free tiers
with tight per-minute token limits (e.g. Groq ~8K). For known models, the window comes from this table, so 1M-token models
like GLM 5.3 or Gemini are not summarized at 48K. If a model rejects a request as "too large", the router learns its limit
separately (Router.size_limit).
"""

from __future__ import annotations

# The first pattern matching the model ID wins; put specific patterns first.
WINDOWS: list[tuple[str, int]] = [
    ("gemini", 1_048_576), ("glm-5", 1_048_576), ("glm-4.6", 200_000), ("glm-4.5", 128_000),
    ("kimi-k2", 262_144), ("qwen3-coder", 262_144), ("qwen3", 131_072), ("deepseek", 131_072),
    ("gpt-oss", 131_072), ("llama-4", 131_072), ("llama-3.3", 131_072), ("nemotron", 131_072),
    ("mistral-large", 131_072), ("mistral-medium", 131_072), ("mistral-small", 131_072), ("devstral", 131_072),
    ("gemma-4", 131_072), ("gemma-3", 131_072),
]
# If the provider limit is smaller than this, it applies regardless of the model window (tight per-minute token limit).
TPM_LIMITED_BELOW = 64_000


def model_window(model: str) -> int | None:
    low = model.lower()
    for pat, n in WINDOWS:
        if pat in low:
            return n
    return None


def context_limit(provider_max: int, model: str, learned: int | None = None) -> int:
    """The maximum request size (tokens) that can be sent to this model."""
    win = model_window(model)
    if win is None:
        limit = provider_max
    elif provider_max < TPM_LIMITED_BELOW:
        limit = min(win, provider_max)
    else:
        limit = win
    if learned:
        limit = min(limit, learned)
    return limit
