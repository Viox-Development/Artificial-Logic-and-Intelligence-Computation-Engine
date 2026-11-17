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
    # while a is cooling down, the second request must never go to a
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
    # sleep is fake, so we move the cooldown into the past by hand
    orig = r.available

    def avail(ref, now=None, pinned=False):
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

    now = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)  # Pacific 23:00 (PDT) or 22:00 (UTC-8)
    assert 3600 <= seconds_until_reset("gemini", now=now) <= 2 * 3600 + 60
    assert abs(seconds_until_reset("groq", now=now) - (18 * 3600 + 60)) < 1


def test_daily_quota_survives_restart(tmp_path, monkeypatch):
    from alice.providers import router as router_mod

    # Running a few minutes before midnight (UTC) counted the quota as already reset: pin the time.
    monkeypatch.setattr(router_mod, "seconds_until_reset", lambda *a, **k: 3600.0)
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
    assert r.chain("brain") == ["a:m1", "a:m2", "b:m3"]  # if everything is hidden, the role must not stop working
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


def test_catalog_view_explains_provider_with_no_working_model():
    """If a server added from the UI has no model answering, the card must not stay empty:
    the provider's error must show."""
    cfg = make_config({"brain": ["a:m1"]}, auto_models=False)
    err = ('[quota] {"error":{"code":"free_rate_limited","message":"Free models are not available to this '
           'account yet."}}')
    cache = {"results": {"b:free": {"ok": False, "error_kind": "quota", "error": err}}}
    r = router_with(cfg, {"a": FakeProvider("a", []), "b": FakeProvider("b", [])}, auto_cache=cache)
    view = {p["name"]: p for p in r.catalog_view()}
    assert view["b"]["models"] == [] and view["b"]["error"] == "Free models are not available to this account yet."
    assert "error" not in view["a"]
    from alice.providers.router import _short_error

    assert _short_error('{"error":{"message":"allowance is used up — top up (req') == "allowance is used up — top up (req"


GARBLED = ("Projeye tıklamayı denedim ama 项目视图没有找到请再试一次 Η προβολή έργου δεν βρέθηκε ProjectView "
           "tekrar deneyeceğim")


def test_garbled_answer_falls_back_to_next_model():
    """glm-4.7-flash produced invented text mixing Turkish, Chinese and Greek on a long context:
    do not show it to the user."""
    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [GARBLED])
    b = FakeProvider("b", ["İlk projeyi açtım."])
    events = []
    r = router_with(cfg, {"a": a, "b": b}, on_event=events.append)
    assert r.chat("brain", [{"role": "user", "content": "hi"}]).text == "İlk projeyi açtım."
    assert not r.available("a:m1")[0] and any("garbled" in e for e in events)


def test_looks_garbled_spares_real_multilingual_answers():
    from alice.providers.router import looks_garbled

    assert looks_garbled(GARBLED)
    assert not looks_garbled("Japonca 'merhaba dünya': こんにちは世界、お元気ですか")
    assert not looks_garbled("Örnek:\n```\nΑΒΓΔΕΖΗΘΙΚ = 漢字漢字漢字漢字漢字\n```\nbu kod çalışır")
    assert not looks_garbled("Pi sayısı π, alfa α, beta β ile gösterilir.")


def test_real_calls_are_recorded_for_reliability():
    from alice.providers import ranking

    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    r = router_with(cfg, {"a": FakeProvider("a", [ProviderError("network", "zaman aşımı (ReadTimeout)")]),
                          "b": FakeProvider("b", ["tamam"])})
    r._real_clients = True  # as if running with real providers (off by default in tests)
    assert r.chat("brain", [{"role": "user", "content": "hi"}]).text == "tamam"
    assert round(ranking.reliability("a:m1")[1], 3) == 1 and ranking.reliability("b:m2")[1] == 0
    assert ranking.load_health()["b:m2"]["ok"] == 1


def test_thinking_model_out_of_budget_retries_with_bigger_max_tokens():
    # Cerebras qwen spent a small max_tokens on thinking and returned empty:
    # every request fell through to the next model
    cfg = make_config({"fast": ["a:think", "b:m"]})
    a = FakeProvider("a", [ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok"), "a ok", "a again"])
    b = FakeProvider("b", ["unused"])
    r = router_with(cfg, {"a": a, "b": b})
    assert r.chat("fast", [{"role": "user", "content": "x"}], max_tokens=200).text == "a ok"
    assert a.calls[1]["max_tokens"] == 1024 and b.calls == []
    assert r.size_limit == {}  # the input was not large, so no learned limit should be stored
    r.chat("fast", [{"role": "user", "content": "x"}], max_tokens=200)
    assert a.calls[2]["max_tokens"] == 1024  # remembered, so the first attempt gets the wide allowance


def test_thinking_model_at_max_budget_not_retried_and_backs_off():
    cfg = make_config({"fast": ["a:think", "b:m"]})
    err = lambda: ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")  # noqa: E731
    a = FakeProvider("a", [err(), err()])
    b = FakeProvider("b", ["b ok", "b ok"])
    r = router_with(cfg, {"a": a, "b": b})
    r.out_floor["a:think"] = 8192
    assert r.chat("fast", [{"role": "user", "content": "x"}], max_tokens=400).text == "b ok"
    assert len(a.calls) == 1  # the budget is already at the max: the same request is not repeated for nothing
    t = __import__("time").monotonic()
    r._penalize("a:think", "a", err(), False, False)
    assert round((r.cooldown_until["a:think"] - t) / 300) == 2  # second failure: 10 min


def test_repeated_timeouts_back_off_and_reset_on_success():
    r = Router(make_config({"brain": ["a:m"]}), provider_factory=lambda pc: None, sleep=lambda s: None)
    e = ProviderError("network", "zaman aşımı (ReadTimeout)")
    waits = []
    for _ in range(3):
        t = __import__("time").monotonic()
        r._penalize("a:m", "a", e, False, False)
        waits.append(round((r.cooldown_until["a:m"] - t) / 600))
    assert waits == [1, 2, 4]
    r.strikes.pop("a:m")  # success resets the counter
    r._penalize("a:m", "a", e, False, False)
    assert r.strikes["a:m"] == 1


def test_proxy_models_are_split_by_provider_without_chatgpt_duplicates():
    from alice.providers.router import Router

    row = lambda ref: {"ref": ref, "model": ref.partition(":")[2], "hidden": False}  # noqa: E731
    out = [{"name": "chatgpt", "enabled": True, "hidden": False, "key": "", "models": [row("chatgpt:gpt-6-luna")]},
           {"name": "cliproxy", "enabled": True, "hidden": False, "key": "K",
            "models": [row("cliproxy:claude-opus-5-5"), row("cliproxy:gpt-6-luna"), row("cliproxy:gpt-6.1-terra"),
                       row("cliproxy:kimi-k3")]}]
    view = {e["name"]: e for e in Router._split_proxy(out, {"cliproxy:kimi-*"})}
    assert "cliproxy" not in view
    assert [m["model"] for m in view["cliproxy:claude-*"]["models"]] == ["claude-opus-5-5"]
    # When the proxy has a ChatGPT account, the direct ChatGPT entry's group is not shown separately:
    # one ChatGPT heading.
    assert "chatgpt" not in view
    assert [m["model"] for m in view["cliproxy:gpt-*"]["models"]] == ["gpt-6-luna", "gpt-6.1-terra"]
    assert view["cliproxy:kimi-*"]["hidden"] and view["cliproxy:kimi-*"]["label"] == "Kimi"


def test_pattern_hides_whole_proxy_group():
    from alice.providers.router import _hidden_match

    assert _hidden_match("cliproxy:kimi-k3", {"cliproxy:kimi-*"})
    assert not _hidden_match("cliproxy:claude-opus-5-5", {"cliproxy:kimi-*"})


def test_repeated_rate_limit_backs_off_past_the_servers_hint():
    """Live log (9 Nov): Gemini with its quota used up said "retry after 23 s" on every 429; once that time passed,
    it returned 429 again, so the same model was hit several times a minute for nothing. On repeated errors, the wait
    grows past what the server said."""
    import time

    cfg = make_config({"brain": ["a:m", "b:m"]})
    a = FakeProvider("a", [ProviderError("rate_limit", "429", 429, retry_after=23)] * 4)
    b = FakeProvider("b", ["ok"] * 4)
    r = router_with(cfg, {"a": a, "b": b})
    waits = []
    for _ in range(4):
        r.cooldown_until.clear()  # as if the time had passed: a is retried
        r.chat("brain", [{"role": "user", "content": "x"}])
        waits.append(round(r.cooldown_until["a:m"] - time.monotonic()))
    assert waits[0] == 23 and waits[1] == 23 and waits[2] == 40 and waits[3] == 80


def test_quota_with_known_reset_waits_that_long():
    import time

    cfg = make_config({"brain": ["a:m", "b:m"]})
    a = FakeProvider("a", [ProviderError("quota", "limit doldu", 429, retry_after=1500)])
    r = router_with(cfg, {"a": a, "b": FakeProvider("b", ["ok"])})
    r.chat("brain", [{"role": "user", "content": "x"}])
    assert 1400 < r.cooldown_until["a:m"] - time.monotonic() < 1501 and r.cooldown_kind["a:m"] == "quota"


def test_long_quota_reset_is_not_cut_to_six_hours():
    import time

    cfg = make_config({"brain": ["a:m", "b:m"]})
    a = FakeProvider("a", [ProviderError("quota", "limit doldu", 429, retry_after=15 * 86400)])
    r = router_with(cfg, {"a": a, "b": FakeProvider("b", ["ok"])})
    r.chat("brain", [{"role": "user", "content": "x"}])
    assert r.cooldown_until["a:m"] - time.monotonic() > 14 * 86400


def test_pinned_model_does_not_fall_back_to_the_role_chain():
    # 11 Nov: while Opus was picked by hand it returned 503, and the work silently continued with gemini-flash-lite.
    # A manual choice must be final.
    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [ProviderError("server", "503", 503)])
    b = FakeProvider("b", ["from b"])
    r = router_with(cfg, {"a": a, "b": b}, max_wait=0)
    r.role_overrides["brain"] = ["a:m1"]
    assert r.chain("brain") == ["a:m1"]
    with pytest.raises(AllProvidersFailed) as e:
        r.chat("brain", [{"role": "user", "content": "hi"}])
    assert e.value.pinned and "Seçtiğin model" in str(e.value)
    assert not b.calls


def test_pinned_model_is_tried_even_while_cooling_down():
    # 13 Nov: once a hand-picked model timed out it went into cooldown, and "continue" was rejected for minutes as
    # "cooling down". Cooldown and session-long closing only affect the automatic order.
    import time

    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [ProviderError("network", "zaman aşımı", 0), "tekrar geldim"])
    r = router_with(cfg, {"a": a, "b": FakeProvider("b", ["b"])}, max_wait=0)
    r.role_overrides["brain"] = ["a:m1"]
    with pytest.raises(AllProvidersFailed):
        r.chat("brain", [{"role": "user", "content": "hi"}])
    assert r.cooldown_until["a:m1"] > time.monotonic()  # cooling down for the automatic order
    assert not r.available("a:m1")[0] and r.available("a:m1", pinned=True)[0]
    assert r.chat("brain", [{"role": "user", "content": "devam et"}]).text == "tekrar geldim"
    r.dead_providers.add("a")  # session-long closing does not stop a hand pick either
    a.script.append("yine")
    assert r.chat("brain", [{"role": "user", "content": "x"}]).text == "yine"


def test_pinned_fallback_can_be_turned_back_on():
    cfg = make_config({"brain": ["a:m1", "b:m2"]}, pinned_fallback=True)
    r = router_with(cfg, {"a": FakeProvider("a", []), "b": FakeProvider("b", [])})
    r.role_overrides["brain"] = ["b:m2"]
    assert r.chain("brain") == ["b:m2", "a:m1"]


def test_auto_brain_chain_skips_weak_models(monkeypatch):
    # When the strong models' quota runs out, do not fall back to small models like flash-lite / gemma;
    # hand-written entries are left alone.
    from alice.providers import picker

    scores = {"a:gemini-3.5-flash": 66, "b:gemini-flash-lite-latest": 44, "b:gemma-4-31b-it": 48}
    monkeypatch.setattr(picker, "quality", lambda r: scores.get(r, 0))
    cache = {"chains": {"brain": ["a:gemini-3.5-flash", "b:gemini-flash-lite-latest", "b:gemma-4-31b-it"]}}
    cfg = make_config({"brain": ["b:mistral-small", "auto"]})
    r = router_with(cfg, {"a": FakeProvider("a", []), "b": FakeProvider("b", [])}, auto_cache=cache)
    assert r.chain("brain") == ["b:mistral-small", "a:gemini-3.5-flash"]
    # If only small models are reachable, the role is not left empty.
    weak = {"chains": {"brain": ["b:gemini-flash-lite-latest"]}}
    r2 = router_with(make_config({"brain": ["auto"]}), {"a": FakeProvider("a", []), "b": FakeProvider("b", [])},
                     auto_cache=weak)
    assert r2.chain("brain") == ["b:gemini-flash-lite-latest"]
    r3 = router_with(make_config({"brain": ["auto"]}, min_brain_quality=0),
                     {"a": FakeProvider("a", []), "b": FakeProvider("b", [])}, auto_cache=cache)
    assert len(r3.chain("brain")) == 3


def test_retry_on_same_model_is_not_reported_as_a_switch():
    cfg = make_config({"brain": ["a:m1"]})
    a = FakeProvider("a", [ProviderError("rate_limit", "429", 429, retry_after=1), "ok"])
    events: list[str] = []
    r = router_with(cfg, {"a": a}, on_event=events.append)
    r.sleep = lambda s: r.cooldown_until.clear()  # wait is over: the cooldown has expired
    assert r.chat("brain", [{"role": "user", "content": "hi"}]).text == "ok"
    assert not any("modeline geçildi" in e for e in events)


def test_subscription_order_claude_then_chatgpt_then_antigravity(monkeypatch):
    """The main model's automatic order starts with subscriptions: Claude accounts first, then ChatGPT if those are
    all off, then Antigravity. A service whose accounts are all full or whose login has lapsed never enters the order."""
    from alice.app import cliproxy
    from alice.providers import picker

    providers = {"cliproxy": {"base_url": "http://p", "api_key_env": "", "max_input_tokens": 200000},
                 "a": {"base_url": "http://a", "api_key_env": "", "max_input_tokens": 100000}}
    scores = {"cliproxy:claude-opus-5-5": 86, "cliproxy:claude-haiku-5-5": 32, "cliproxy:gpt-6-luna": 26,
              "cliproxy:gpt-6.1-terra": 61, "cliproxy:gemini-3.8-flash-high": 76, "a:x": 80}
    monkeypatch.setattr(picker, "quality", lambda r: scores.get(r, 0))
    r = router_with(make_config({"brain": ["auto"]}, providers=providers), {}, auto_cache={"chains": {"brain": ["a:x"]}})
    r._subscription_models = ["gemini-3.8-flash-high", "gpt-6-luna", "gpt-6.1-terra", "claude-haiku-5-5",
                              "claude-opus-5-5"]
    chain = r.chain("brain")
    assert chain[:4] == ["cliproxy:claude-opus-5-5", "cliproxy:claude-haiku-5-5", "cliproxy:gpt-6.1-terra",
                         "cliproxy:gpt-6-luna"]
    assert chain[4:] == ["cliproxy:gemini-3.8-flash-high", "a:x"]
    r._subscription_models = None
    r._real_clients = True
    monkeypatch.setattr(cliproxy, "live_models", lambda: ["claude-opus-5-5", "gpt-6.1-terra"])
    monkeypatch.setattr(cliproxy, "groups_ready", lambda: {"claude": False, "codex": True})
    assert r.subscription_chain() == ["cliproxy:gpt-6.1-terra"]  # Claude accounts' logins have lapsed: skipped


def test_proxy_auth_error_does_not_close_the_whole_proxy():
    """A lapsed Claude login must not close the ChatGPT and Gemini models on the proxy for the whole session."""
    providers = {"cliproxy": {"base_url": "http://p", "api_key_env": "", "max_input_tokens": 200000}}
    r = router_with(make_config({"brain": ["cliproxy:claude-opus-5-5"]}, providers=providers), {})
    r._penalize("cliproxy:claude-opus-5-5", "cliproxy", ProviderError("auth", "401", 401), False, False)
    assert "cliproxy" not in r.dead_providers
    assert r.available("cliproxy:gpt-6.1-terra")[0] and not r.available("cliproxy:claude-opus-5-5")[0]
