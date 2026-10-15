from __future__ import annotations

import pytest

from alice.providers.openai_compat import ProviderError, classify_error
from alice.providers.router import AllProvidersFailed, Router, estimate_tokens, strip_images
from tests.conftest import FakeProvider, make_config


def router_with(cfg, providers: dict[str, FakeProvider], **kw) -> Router:
    return Router(cfg, provider_factory=lambda pc: providers[pc.name], sleep=lambda s: None, **kw)


def test_falls_back_on_rate_limit_and_cools_down():
    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [ProviderError("rate_limit", "429", 429, retry_after=30), "unused"])
    b = FakeProvider("b", ["from b", "again b"])
    r = router_with(cfg, {"a": a, "b": b})
    assert r.chat("brain", [{"role": "user", "content": "hi"}]).text == "from b"
    # a soğumada, ikinci istek a'ya hiç gitmemeli
    assert r.chat("brain", [{"role": "user", "content": "hi"}]).text == "again b"
    assert len(a.calls) == 1
    assert not r.available("a:m1")[0]


def test_not_found_kills_model_auth_kills_provider():
    cfg = make_config({"brain": ["a:gone", "a:other", "b:ok"]})
    a = FakeProvider("a", [ProviderError("not_found", "x", 404), ProviderError("auth", "bad key", 401)])
    b = FakeProvider("b", ["ok", "ok"])
    r = router_with(cfg, {"a": a, "b": b})
    assert r.chat("brain", [{"role": "user", "content": "x"}]).provider == "b"
    assert "a:gone" in r.dead_models and "a" in r.dead_providers
    r.chat("brain", [{"role": "user", "content": "x"}])
    assert len(a.calls) == 2


def test_too_large_skips_provider_by_budget():
    cfg = make_config({"brain": ["a:small", "b:big"]}, providers={
        "a": {"base_url": "http://a", "api_key_env": "", "max_input_tokens": 50},
        "b": {"base_url": "http://b", "api_key_env": "", "max_input_tokens": 100000},
    })
    a = FakeProvider("a", [])
    b = FakeProvider("b", ["big ok"])
    r = router_with(cfg, {"a": a, "b": b})
    assert r.chat("brain", [{"role": "user", "content": "x" * 2000}]).text == "big ok"
    assert a.calls == []


def test_missing_key_provider_skipped(monkeypatch):
    cfg = make_config({"brain": ["a:m", "b:m"]}, providers={
        "a": {"base_url": "http://a", "api_key_env": "NOPE_KEY"},
        "b": {"base_url": "http://b", "api_key_env": ""},
    })
    b = FakeProvider("b", ["ok"])
    r = router_with(cfg, {"a": FakeProvider("a", []), "b": b})
    assert r.chat("brain", [{"role": "user", "content": "x"}]).provider == "b"


def test_all_fail_waits_for_short_cooldown_then_retries():
    cfg = make_config({"brain": ["a:m"]})
    a = FakeProvider("a", [ProviderError("rate_limit", "slow down", 429, retry_after=2), "after wait"])
    slept = []
    r = Router(cfg, provider_factory=lambda pc: a, sleep=slept.append)
    # sleep sahte olduğu için cooldown'u elle geçmişe çekiyoruz
    orig = r.available

    def avail(ref, now=None):
        if slept:
            r.cooldown_until.clear()
        return orig(ref, now)

    r.available = avail
    assert r.chat("brain", [{"role": "user", "content": "x"}]).text == "after wait"
    assert slept and slept[0] <= 3


def test_all_fail_raises_with_reasons():
    cfg = make_config({"brain": ["a:m"]})
    a = FakeProvider("a", [ProviderError("quota", "daily limit", 429)])
    r = router_with(cfg, {"a": a})
    with pytest.raises(AllProvidersFailed) as ei:
        r.chat("brain", [{"role": "user", "content": "x"}])
    assert "quota" in str(ei.value)


def test_tools_bad_request_marks_no_tools():
    cfg = make_config({"brain": ["a:m", "b:m"]})
    a = FakeProvider("a", [ProviderError("bad_request", "tools not supported", 400)])
    b = FakeProvider("b", ["ok"])
    r = router_with(cfg, {"a": a, "b": b})
    r.chat("brain", [{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "t"}}])
    assert "a:m" in r.no_tools


def test_images_stripped_for_non_vision_and_private_keys_removed():
    cfg = make_config({"brain": ["a:m"]})
    a = FakeProvider("a", ["ok"])
    r = router_with(cfg, {"a": a})
    msgs = [{"role": "user", "_image": True, "content": [
        {"type": "text", "text": "bak"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    r.chat("brain", msgs)
    sent = a.calls[0]["messages"][0]
    assert isinstance(sent["content"], str) and "atlandı" in sent["content"]
    assert "_image" not in sent


def test_vision_provider_keeps_images():
    cfg = make_config({"brain": ["b:m"]})
    b = FakeProvider("b", ["ok"])
    r = router_with(cfg, {"b": b})
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}]
    r.chat("brain", msgs)
    assert isinstance(b.calls[0]["messages"][0]["content"], list)


def test_classify_error():
    assert classify_error(429, "Rate limit reached for requests per day (RPD)").kind == "quota"
    assert classify_error(429, "Rate limit reached, try again in 2s").kind == "rate_limit"
    assert classify_error(413, "Request too large for model").kind == "too_large"
    assert classify_error(400, "The model `x` does not exist").kind == "not_found"
    assert classify_error(400, "This model's maximum context length is 8192").kind == "too_large"
    assert classify_error(401, "invalid api key").kind == "auth"
    assert classify_error(503, "overloaded").kind == "server"
    assert classify_error(429, "slow", {"retry-after": "7"}).retry_after == 7.0


def test_estimate_and_strip():
    msgs = [{"role": "user", "content": "x" * 350}]
    assert 90 <= estimate_tokens(msgs) <= 110
    assert strip_images([{"role": "user", "content": "plain"}])[0]["content"] == "plain"


def test_usage_recorded(isolated_home):
    cfg = make_config({"brain": ["a:m"]})
    r = router_with(cfg, {"a": FakeProvider("a", ["ok"])})
    r.chat("brain", [{"role": "user", "content": "x"}])
    assert r.usage.today()["a:m"]["requests"] == 1


def test_gemini_gets_thought_signatures_and_others_dont():
    from alice.providers.openai_compat import _merge_tool_delta, normalize_message
    from alice.providers.router import GEMINI_DUMMY_SIGNATURE, clean_messages

    calls: dict = {}
    _merge_tool_delta(calls, {"id": "c1", "function": {"name": "run_command", "arguments": "{}"},
                              "extra_content": {"google": {"thought_signature": "SIG"}}})
    own = normalize_message({"content": "", "tool_calls": [calls[0]]})
    other = normalize_message({"content": "", "tool_calls": [{"id": "c2", "function": {"name": "x", "arguments": "{}"}}]})
    msgs = [own, other]
    g = clean_messages(msgs, gemini=True)
    assert g[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG"
    assert g[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == GEMINI_DUMMY_SIGNATURE
    plain = clean_messages(msgs)
    assert all("extra_content" not in tc and "_extra_content" not in tc for m in plain for tc in m["tool_calls"])


def test_signature_error_does_not_disable_tools_and_too_large_learns_limit():
    from alice.providers.openai_compat import ProviderError
    from alice.providers.router import Router
    from tests.conftest import make_config

    r = Router(make_config({"brain": ["a:m"]}), provider_factory=lambda pc: None, sleep=lambda s: None)
    r._penalize("a:m", "a", ProviderError("bad_request", "Function call is missing a thought_signature"), True, False)
    assert "a:m" not in r.no_tools
    r._penalize("a:m", "a", ProviderError("too_large", "TPM limit 8000"), True, False, est=10000)
    assert r.size_limit["a:m"] == 9000


GEMINI_DAY = ('[{"error": {"code": 429, "message": "You exceeded your current quota. Please retry in 12.5s.", '
              '"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": '
              '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}, {"@type": '
              '"type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]}}]')


def test_gemini_minute_quota_is_rate_limit_and_daily_is_quota():
    minute = classify_error(429, GEMINI_DAY.replace("PerDay", "PerMinute"))
    assert minute.kind == "rate_limit" and minute.retry_after == 12.0
    day = classify_error(429, GEMINI_DAY)
    assert day.kind == "quota" and day.daily
    assert classify_error(429, "free-models-per-day limit reached").daily


def test_seconds_until_reset_uses_pacific_midnight_for_gemini():
    from datetime import datetime, timezone

    from alice.providers.router import seconds_until_reset

    now = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)  # Pasifik 23:00 (PDT) ya da 22:00 (UTC-8)
    assert 3600 <= seconds_until_reset("gemini", now=now) <= 2 * 3600 + 60
    assert abs(seconds_until_reset("groq", now=now) - (18 * 3600 + 60)) < 1


def test_daily_quota_survives_restart(tmp_path):
    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [ProviderError("quota", "per day", 429, daily=True)])
    b = FakeProvider("b", ["b1", "b2"])
    r = router_with(cfg, {"a": a, "b": b}, usage_path=tmp_path / "usage.json")
    assert r.chat("brain", [{"role": "user", "content": "x"}]).text == "b1"
    r2 = router_with(cfg, {"a": a, "b": b}, usage_path=tmp_path / "usage.json")
    assert not r2.available("a:m1")[0]
    assert r2.chat("brain", [{"role": "user", "content": "x"}]).text == "b2"
    assert len(a.calls) == 1


def test_openrouter_daily_quota_closes_all_free_models():
    cfg = make_config({"brain": ["openrouter:x:free", "openrouter:y:free", "b:m"]}, providers={
        "openrouter": {"base_url": "http://or", "api_key_env": ""},
        "b": {"base_url": "http://b", "api_key_env": ""},
    })
    o = FakeProvider("openrouter", [ProviderError("quota", "free-models-per-day", 429, daily=True)])
    b = FakeProvider("b", ["ok"])
    r = router_with(cfg, {"openrouter": o, "b": b})
    assert r.chat("brain", [{"role": "user", "content": "x"}]).text == "ok"
    assert len(o.calls) == 1 and not r.available("openrouter:y:free")[0]


def test_model_rejecting_images_is_skipped_for_vision():
    cfg = make_config({"vision": ["a:m", "b:m"]}, providers={
        "a": {"base_url": "http://a", "api_key_env": "", "vision_models": ["*"]},
        "b": {"base_url": "http://b", "api_key_env": "", "vision_models": ["*"]},
    })
    a = FakeProvider("a", [ProviderError("bad_request", "image input is not supported", 400)])
    b = FakeProvider("b", ["gördüm", "yine"])
    r = router_with(cfg, {"a": a, "b": b})
    img = [{"role": "user", "content": [{"type": "text", "text": "ne var"},
                                         {"type": "image_url", "image_url": {"url": "data:x"}}]}]
    assert r.chat("vision", img).text == "gördüm"
    assert not r.supports_vision("a:m")


def test_hidden_models_and_providers_leave_auto_chain_but_pin_still_works():
    cfg = make_config({"brain": ["a:m1", "a:m2", "b:m3"]}, auto_models=False, hidden_models=["a:m1"])
    r = router_with(cfg, {"a": FakeProvider("a", []), "b": FakeProvider("b", [])}, auto_cache={})
    assert r.chain("brain") == ["a:m2", "b:m3"]
    r.set_hidden("a", True)
    assert r.chain("brain") == ["b:m3"]
    r.set_hidden("b", True)
    assert r.chain("brain") == ["a:m1", "a:m2", "b:m3"]  # her şey gizlenirse rol çalışmaz hale gelmesin
    r.set_hidden("a", False)
    r.set_hidden("b", False)
    assert cfg.agent["hidden_models"] == ["a:m1"]
    r.role_overrides["brain"] = ["a:m1"]
    assert r.chain("brain")[0] == "a:m1"


def test_catalog_view_groups_tested_models_by_provider():
    cfg = make_config({"brain": ["a:m1"]}, auto_models=False, hidden_models=["b"])
    cache = {"results": {"b:fast": {"ok": True, "quality": 70, "latency": 1.2},
                         "b:broken": {"ok": False, "error_kind": "not_found"},
                         "a:extra": {"ok": False, "ever_ok": True, "quality": 50}}}
    r = router_with(cfg, {"a": FakeProvider("a", []), "b": FakeProvider("b", [])}, auto_cache=cache)
    view = {p["name"]: p for p in r.catalog_view()}
    assert [m["ref"] for m in view["a"]["models"]] == ["a:extra", "a:m1"]
    assert view["b"]["hidden"] and [m["ref"] for m in view["b"]["models"]] == ["b:fast"]
    assert view["b"]["models"][0]["tested"] and not view["a"]["models"][1]["tested"]
