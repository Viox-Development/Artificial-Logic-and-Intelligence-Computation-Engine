from __future__ import annotations

from types import SimpleNamespace

from alice.config import ENV_TEMPLATE, default_config_dict
from alice.providers import discovery, ranking
from alice.providers.openai_compat import classify_error
from alice.providers.ranking import quality
from alice.voice.live import LiveError


class Client:
    def __init__(self, models):
        self.models = models
        self.calls = 0

    def list_models(self):
        self.calls += 1
        return self.models


def router(spec):
    """spec: {isim: (models, auto, katalog)}"""
    cfgs = {n: SimpleNamespace(enabled=True, api_key_env=f"{n.upper()}_KEY", models=m, auto=a)
            for n, (m, a, _) in spec.items()}
    clients = {n: Client(c) for n, (_, _, c) in spec.items()}
    return SimpleNamespace(providers_cfg=cfgs, client=lambda n: clients[n], clients=clients)


def test_default_config_has_new_providers():
    p = default_config_dict()["providers"]
    assert p["zai"]["base_url"] == "https://api.z.ai/api/paas/v4"
    assert "glm-4.7-flash" in p["zai"]["models"]
    assert p["cohere"]["base_url"].endswith("/compatibility/v1")
    assert p["vercel"]["auto"] is False
    for key in ("ZAI_API_KEY", "COHERE_API_KEY", "AI_GATEWAY_API_KEY"):
        assert key in ENV_TEMPLATE


def test_quality_for_new_models():
    assert quality("zai:glm-4.7-flash") > quality("zai:glm-4.5-flash") > 0
    assert quality("cohere:command-a-plus-05-2026") > quality("cohere:command-a-03-2025") > 0
    assert quality("cohere:command-a-vision-07-2025") > 0


def test_fixed_model_list_skips_catalog_request_and_manual_filter():
    r = router({"zai": (["glm-4.7-flash", "glm-4.5-flash"], True, [{"id": "glm-5"}])})
    refs, errors, catalog = ranking.candidates(r)
    assert refs == ["zai:glm-4.7-flash", "zai:glm-4.5-flash"]
    assert r.clients["zai"].calls == 0
    assert "zai" not in catalog and not errors  # elle yazılan zai:glm-5 "kalkmış" sayılmaz


def test_credit_providers_are_not_ranked_or_discovered():
    r = router({"vercel": ([], False, [{"id": "anthropic/claude-sonnet-5.5"}]),
                "zai": (["glm-4.7-flash"], True, [])})
    refs, _, _ = ranking.candidates(r)
    assert refs == ["zai:glm-4.7-flash"]
    found, errors = discovery.discover(r)
    assert [f.ref for f in found] == ["zai:glm-4.7-flash"]
    assert r.clients["vercel"].calls == 0 and not errors


def test_gemini_billing_rate_limit_is_quota():
    body = ("You have reached a rate limit. Set up billing to increase your limits and unblock your work. "
            "https://aistudio.google.com/rate-limit")
    assert classify_error(429, body).kind == "quota"
    assert classify_error(429, "Rate limit reached, try again in 2s").kind == "rate_limit"


def test_live_error_limited():
    assert LiveError("gemini-live: received 1011 (internal error) You have reached a rate limit").limited
    assert LiveError("m: RESOURCE_EXHAUSTED").limited
    assert not LiveError("bağlanılamadı: timed out").limited
