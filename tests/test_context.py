from __future__ import annotations

from alice.agent import Agent, NullUI
from alice.config import load_config
from alice.permissions import Permissions
from alice.providers.context import context_limit, model_window
from alice.providers.openai_compat import ChatResult, normalize_message
from alice.providers.router import Router, estimate_tokens
from alice.tools import ToolContext, all_tools
from tests.conftest import make_config


def test_model_windows():
    assert model_window("z-ai/glm-5.3") == 1_048_576
    assert model_window("gemini-3.8-flash") == 1_048_576
    assert model_window("bilinmeyen-model") is None
    # For a known model, the model's own window applies, not the provider's general setting (NVIDIA 128K).
    assert context_limit(128_000, "z-ai/glm-5.3") == 1_048_576
    # The narrow free tier's per-minute limit (Groq 8K) is the upper bound for every model.
    assert context_limit(8_000, "qwen/qwen3.8-27b") == 8_000
    assert context_limit(32_000, "bilinmeyen") == 32_000
    # If the model rejected a request, the learned limit wins.
    assert context_limit(128_000, "z-ai/glm-5.3", learned=500_000) == 500_000


def _agent(cfg, usage_tokens=None):
    class P:
        name = "nvidia"

        def chat(self, model, messages, tools=None, **kw):
            usage = {"prompt_tokens": usage_tokens(messages, tools)} if usage_tokens else {}
            return ChatResult(normalize_message({"content": "tamam"}), "nvidia", model, usage)

    router = Router(cfg, provider_factory=lambda pc: P(), sleep=lambda s: None)
    ctx = ToolContext(cwd=__import__("pathlib").Path("."), config=cfg, router=router)
    return Agent(cfg, router, all_tools(), ctx, Permissions("auto"), NullUI(), system_prompt="sys")


def _cfg(**agent):
    return make_config({"brain": ["nvidia:z-ai/glm-5.3"], "fast": ["nvidia:z-ai/glm-5.3"], "vision": ["nvidia:z-ai/glm-5.3"]},
                       providers={"nvidia": {"base_url": "http://n", "api_key_env": "", "max_input_tokens": 128000}},
                       auto_models=False, **agent)


def test_compact_at_follows_model_window():
    a = _agent(_cfg(compact_at_tokens="auto", compact_max_tokens=0))
    assert a.compact_at == int(1_048_576 * 0.8)
    # A hand-written 48000 is respected (it used to be treated as "auto" and the chat grew to 800K).
    assert _agent(_cfg(compact_at_tokens=48000)).compact_at == 48000
    # Any other manually written number is kept as is.
    assert _agent(_cfg(compact_at_tokens=200000)).compact_at == 200000


def test_context_tokens_uses_provider_count():
    # The provider also counts tool definitions: an estimate based only on messages would stay far too low.
    a = _agent(_cfg(), usage_tokens=lambda messages, tools: estimate_tokens(messages, tools) + 5000)
    a.run_turn("merhaba")
    before = a.context_tokens()
    assert before >= estimate_tokens(a.messages[:-1], a.tool_specs()) + 5000
    a.messages.append({"role": "user", "content": "x" * 3500})
    assert abs(a.context_tokens() - (before + 1000)) <= 1


def test_legacy_brain_default_follows_current_default(tmp_path):
    # Untouched brain chains from an old `alice init` (including the retired GLM 5.3 one) get the current default.
    p = tmp_path / "config.toml"
    for old in ('["auto", "ollama:qwen3:8b"]', '["nvidia:z-ai/glm-5.3", "auto", "ollama:qwen3:8b"]'):
        p.write_text(f'[roles]\nbrain = {old}\nfast = ["auto"]\n', encoding="utf-8")
        assert load_config(p).roles["brain"] == ["auto", "ollama:qwen3:8b"]
    cfg = load_config(p)
    assert cfg.roles["fast"] == ["auto"]
    p.write_text('[roles]\nbrain = ["gemini:gemini-3.8-flash"]\n', encoding="utf-8")
    assert load_config(p).roles["brain"] == ["gemini:gemini-3.8-flash"]  # the user's choice stays
