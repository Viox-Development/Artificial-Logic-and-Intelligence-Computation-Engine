from alice.providers import thinking


def test_params_per_provider():
    assert thinking.params("gemini", "gemini-3.6-flash", "off") == {"reasoning_effort": "none"}
    assert thinking.params("groq", "openai/gpt-oss-120b", "off") == {"reasoning_effort": "low"}
    assert thinking.params("groq", "qwen/qwen3.8-27b", "off") == {"reasoning_effort": "none"}
    assert thinking.params("openrouter", "x/y", "off") == {"reasoning": {"enabled": False}}
    assert thinking.params("openrouter", "x/y", "high") == {"reasoning": {"effort": "high"}}
    assert thinking.params("nvidia", "z-ai/glm-5.3", "low")["chat_template_kwargs"]["enable_thinking"] is False
    assert thinking.params("nvidia", "meta/llama-4", "low") == {}
    assert thinking.params("gemini", "gemini-3.6-flash", "default") == {}
    # ChatGPT login: gpt-6-luna rejects "minimal"; off = none
    assert thinking.params("chatgpt", "gpt-6-luna", "off") == {"reasoning_effort": "none"}
    assert thinking.params("chatgpt", "gpt-6-luna", "medium") == {"reasoning_effort": "medium"}
    assert thinking.params("chatgpt", "gpt-6-luna", "max") == {"reasoning_effort": "max"}
    # Local Claude Code proxy: the level is passed through as-is (the proxy maps it to Claude/Codex models)
    assert thinking.params("cliproxy", "claude-opus-5-5", "off") == {"reasoning_effort": "none"}
    assert thinking.params("cliproxy", "gpt-5.6-terra", "max") == {"reasoning_effort": "max"}
    assert thinking.params("cliproxy", "claude-opus-5-5", "xhigh") == {"reasoning_effort": "xhigh"}
    # xhigh/max only exist on ChatGPT: everywhere else they become high
    assert thinking.params("gemini", "gemini-3.6-flash", "xhigh") == {"reasoning_effort": "high"}
    assert thinking.params("openrouter", "x/y", "max") == {"reasoning": {"effort": "high"}}
    assert thinking.level_for({"thinking": {"brain": "xhigh"}}, "brain") == "xhigh"


def test_level_for_roles():
    assert thinking.level_for({}, "fast") == "off"
    assert thinking.level_for({}, "brain") == "low"
    assert thinking.level_for({}, "live-think") == "medium"  # low gets this wrong; medium is equally fast and right
    assert thinking.level_for({"thinking": {"default": "high", "brain": "medium"}}, "brain") == "medium"
    assert thinking.level_for({"thinking": {"default": "high"}}, "vision") == "high"


def test_router_retries_without_thinking_when_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    from alice.config import load_config
    from alice.providers.openai_compat import ChatResult, ProviderError
    from alice.providers.router import Router

    calls = []

    class Client:
        def chat(self, model, messages, extra=None, **kw):
            calls.append(extra)
            if extra and "reasoning_effort" in extra:
                raise ProviderError("bad_request", "unknown parameter reasoning_effort", 400)
            return ChatResult({"role": "assistant", "content": "ok"}, "gemini", model)

    cfg = load_config(tmp_path / "c.toml")
    r = Router(cfg, provider_factory=lambda pc: Client(), usage_path=tmp_path / "u.json", auto_cache={})
    ref = "gemini:gemini-x"
    assert r.chat(ref, [{"role": "user", "content": "hi"}]).text == "ok"
    assert calls[0] == {"reasoning_effort": "low"} and calls[1] is None and ref in r.no_thinking
    r.chat(ref, [{"role": "user", "content": "hi"}])
    assert calls[2] is None  # not sent again
