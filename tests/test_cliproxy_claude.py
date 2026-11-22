"""cliproxy's native Claude endpoint as a second provider (kind = "anthropic"): same local key, Claude API thinking
rules, cache hints reach the Claude client."""
from __future__ import annotations

from alice.config import ProviderConfig
from alice.providers.router import Router
from tests.conftest import FakeProvider, make_config


def _router(level="high"):
    cfg = make_config({"brain": ["cliproxy-claude:claude-opus-5-5"]},
                      providers={"cliproxy-claude": {"base_url": "http://127.0.0.1:8317", "api_key_env": "",
                                                     "kind": "anthropic", "max_input_tokens": 1000000}})
    cfg.data["thinking"] = {"brain": level}
    prov = FakeProvider("cliproxy-claude", lambda *a, **k: "ok")
    return Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None), prov


def test_thinking_follows_api_format_not_provider_name():
    router, prov = _router("off")
    router.chat("brain", [{"role": "user", "content": "a"}], cache={"ttl": "1h", "mode": "write"})
    extra = prov.calls[-1]["extra"]
    # Claude API rule: thinking cannot be switched off, "off" becomes the lowest effort (cliproxy's OpenAI endpoint
    # would get "none" instead, which the Claude format does not know).
    assert extra["reasoning_effort"] == "low"
    assert extra["_cache_ttl"] == "1h" and extra["_cache_mode"] == "write"


def test_uses_the_proxys_local_key(monkeypatch):
    from alice import config

    monkeypatch.setattr(config, "cliproxy_local_key", lambda: "yerel-anahtar")
    pc = ProviderConfig(name="cliproxy-claude", base_url="http://127.0.0.1:8317", kind="anthropic")
    assert pc.api_keys == ["yerel-anahtar"]
