"""Key pool (multiple keys used in turn; 429/quota/auth) and the official Claude API client."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from alice.config import Config, ProviderConfig, default_config_dict
from alice.providers.keypool import KeyPool
from alice.providers.openai_compat import ChatResult, ProviderError


class FakeClient:
    """Fake provider client that behaves according to its key."""
    plan: dict[str, list] = {}
    calls: list[str] = []

    def __init__(self, pc: ProviderConfig):
        self.key = pc.api_key

    def _next(self, model):
        FakeClient.calls.append(f"{self.key}:{model}")
        todo = FakeClient.plan.get(self.key) or []
        step = todo.pop(0) if todo else "ok"
        if isinstance(step, ProviderError):
            raise step
        return ChatResult({"role": "assistant", "content": f"{self.key} cevapladı"}, "p", model)

    def chat(self, model, messages, **kw):
        return self._next(model)

    def chat_stream(self, model, messages, on_delta, **kw):
        res = self._next(model)
        on_delta(res.text)
        return res


@pytest.fixture
def pc(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "key-one-1111")
    monkeypatch.setenv("TEST_API_KEY_2", "key-two-2222")
    monkeypatch.setenv("TEST_API_KEY_3", "key-three-3333")
    FakeClient.plan, FakeClient.calls = {}, []
    return ProviderConfig(name="gemini", base_url="https://x", api_key_env="TEST_API_KEY")


def test_api_keys_are_read_in_order_and_deduplicated(pc, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY_10", "key-ten")
    monkeypatch.setenv("TEST_API_KEY_4", "key-one-1111")  # same key written twice
    assert pc.api_keys == ["key-one-1111", "key-two-2222", "key-three-3333", "key-ten"]


def test_round_robin_spreads_requests(pc):
    pool = KeyPool(pc, FakeClient)
    for _ in range(3):
        pool.chat("m", [])
    assert [c.split(":")[0] for c in FakeClient.calls] == ["key-one-1111", "key-two-2222", "key-three-3333"]


def test_rate_limited_key_waits_and_next_key_answers(pc):
    FakeClient.plan = {"key-one-1111": [ProviderError("rate_limit", "429", 429, retry_after=30)]}
    pool = KeyPool(pc, FakeClient, strategy="fill-first")
    assert pool.chat("m", []).text == "key-two-2222 cevapladı"
    # The first key is waiting on this model, not on another model.
    assert [k.key for k in pool.order("m")] == ["key-two-2222", "key-three-3333"]
    assert [k.key for k in pool.order("other")][0] == "key-one-1111"
    rows = pool.status()
    assert rows[0]["state"] == "beklemede" and rows[0]["preview"] == "••••1111" and "m" in rows[0]["waiting"]


def test_daily_quota_is_remembered_across_restarts(pc, monkeypatch):
    # If less than 10 minutes are left before the reset (right before Pacific midnight), the record is deliberately
    # not written; the test must not depend on the time of day (CI failed at 07:58 UTC).
    monkeypatch.setattr("alice.providers.router.seconds_until_reset", lambda *a, **k: 5 * 3600.0)
    FakeClient.plan = {"key-one-1111": [ProviderError("quota", "per day", 429, daily=True)]}
    KeyPool(pc, FakeClient, strategy="fill-first").chat("m", [])
    again = KeyPool(pc, FakeClient, strategy="fill-first")  # restarted
    assert [k.key for k in again.order("m")] == ["key-two-2222", "key-three-3333"]


def test_bad_key_is_dropped_and_others_keep_working(pc):
    FakeClient.plan = {"key-two-2222": [ProviderError("auth", "invalid key", 401)]}
    pool = KeyPool(pc, FakeClient)
    pool.chat("m", [])  # key-one
    assert pool.chat("m", []).text == "key-three-3333 cevapladı"  # key-two auth -> next one
    assert pool.status()[1]["state"] == "kapalı"


def test_all_keys_waiting_raises_rate_limit_with_wait(pc):
    FakeClient.plan = {k: [ProviderError("rate_limit", "429", 429, retry_after=40)]
                       for k in ("key-one-1111", "key-two-2222", "key-three-3333")}
    pool = KeyPool(pc, FakeClient)
    with pytest.raises(ProviderError) as e:
        pool.chat("m", [])
    assert e.value.kind == "rate_limit"
    with pytest.raises(ProviderError) as e:
        pool.chat("m", [])
    assert "hepsi beklemede" in str(e.value) and 30 < e.value.retry_after <= 40


def test_other_errors_are_not_retried_on_another_key(pc):
    FakeClient.plan = {"key-one-1111": [ProviderError("server", "500", 500)]}
    pool = KeyPool(pc, FakeClient, strategy="fill-first")
    with pytest.raises(ProviderError):
        pool.chat("m", [])
    assert len(FakeClient.calls) == 1


def test_router_uses_pool_and_shows_key_status(pc, tmp_path):
    from alice.providers.router import Router

    cfg = Config(default_config_dict())
    cfg.data["providers"] = {"gemini": {"base_url": "https://x", "api_key_env": "TEST_API_KEY"}}
    cfg.data["roles"] = {"brain": ["gemini:m"]}
    cfg.data.setdefault("agent", {})["auto_models"] = False
    FakeClient.plan = {"key-one-1111": [ProviderError("quota", "exhausted", 429)]}
    r = Router(cfg, provider_factory=FakeClient, usage_path=tmp_path / "u.json", auto_cache={})
    cfg.data["agent"]["key_strategy"] = "fill-first"
    assert r.chat("brain", [{"role": "user", "content": "selam"}]).text == "key-two-2222 cevapladı"
    st = r.key_status()["gemini"]
    assert [x["state"] for x in st] == ["beklemede", "hazır", "hazır"] and st[1]["uses"] == 1
    assert r.available("gemini:m")[0]  # model did not enter cooldown: the key changed


# ------------------------------------------------------------------------------------------------ Claude API
def test_messages_are_converted_to_claude_format():
    from alice.providers.anthropic_api import to_messages, to_tools

    system, msgs = to_messages([
        {"role": "system", "content": "Sen ALICE'sin."},
        {"role": "user", "content": [{"type": "text", "text": "bu ne?"},
                                     {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "screen", "arguments": '{"x": 1}'}},
            {"id": "c2", "type": "function", "function": {"name": "time", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ekran"},
        {"role": "tool", "tool_call_id": "c2", "content": "12:00"},
        {"role": "system", "content": "kısa cevap ver"},
    ])
    assert system == "Sen ALICE'sin."
    assert msgs[0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                 "data": "QUJD"}}
    assert msgs[1] == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "c1", "name": "screen", "input": {"x": 1}},
        {"type": "tool_use", "id": "c2", "name": "time", "input": {}}]}
    # Two tool results and the following system note are in one user turn.
    assert msgs[2]["role"] == "user" and [b["type"] for b in msgs[2]["content"]] == ["tool_result", "tool_result", "text"]
    assert len(msgs) == 3
    tools = to_tools([{"type": "function", "function": {"name": "screen", "description": "d",
                                                        "parameters": {"type": "object", "properties": {}}}}])
    assert tools == [{"name": "screen", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


def test_tool_ids_from_other_providers_are_made_claude_safe():
    """If an earlier step of the conversation was made by Kimi, the id is "functions.read_file:0": Claude rejected it with a 400."""
    import re

    from alice.providers.anthropic_api import to_messages

    _, msgs = to_messages([
        {"role": "user", "content": "oku"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "functions.read_file:0", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "functions.read_file:0", "content": "içerik"},
    ])
    use, result = msgs[1]["content"][0], msgs[2]["content"][0]
    assert re.fullmatch(r"[A-Za-z0-9_-]+", use["id"]) and result["tool_use_id"] == use["id"]


def test_tool_result_with_image_becomes_image_block_not_base64_text():
    from alice.providers.anthropic_api import to_messages

    b64 = "QUJD" * 400
    _, msgs = to_messages([
        {"role": "user", "content": "ekrana bak"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "screen", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "mcp", "arguments": "{}"}},
            {"id": "c3", "type": "function", "function": {"name": "raw", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": [
            {"type": "text", "text": "ekran görüntüsü"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": [{"type": "image", "data": b64, "mimeType": "image/jpeg"}]},
        {"role": "tool", "tool_call_id": "c3", "content": {"png": b64, "name": "x"}},
    ])
    r1, r2, r3 = msgs[2]["content"]
    assert r1["content"] == [{"type": "text", "text": "ekran görüntüsü"},
                             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}]
    assert r2["content"] == [{"type": "image", "source": {"type": "base64", "data": b64, "media_type": "image/jpeg"}}]
    assert isinstance(r3["content"], str) and b64 not in r3["content"] and '"name": "x"' in r3["content"]


def _final(stop="end_turn", text="Merhaba", tool=None):
    content = [NS(type="thinking", thinking="", signature="s"), NS(type="text", text=text)]
    if tool:
        content.append(NS(type="tool_use", id="t1", name=tool, input={"q": "ş"}))
    return NS(content=content, stop_reason=stop, stop_details=None, model="claude-opus-5-5",
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=90, cache_creation_input_tokens=0))


class FakeStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


def _provider(sent, final, events=()):
    from alice.providers.anthropic_api import AnthropicProvider

    def create(**kw):
        sent.update(kw)
        return final

    def stream(**kw):
        sent.update(kw)
        return FakeStream(list(events), final)

    fake = NS(beta=NS(messages=NS(create=create, stream=stream)), models=NS(list=lambda: [NS(id="claude-opus-5-5")]))
    return AnthropicProvider(ProviderConfig(name="anthropic", base_url="https://api.anthropic.com",
                                            api_key_env="ANTHROPIC_API_KEY", kind="anthropic"), client=fake)


def test_chat_sends_adaptive_thinking_effort_and_fallback():
    sent: dict = {}
    p = _provider(sent, _final(tool="search"))
    res = p.chat("claude-opus-5-5", [{"role": "user", "content": "selam"}],
                 tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
                 extra={"reasoning_effort": "high"})
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert sent["output_config"] == {"effort": "high"} and sent["fallbacks"] == "default"
    assert "temperature" not in sent and sent["max_tokens"] == 16000
    assert res.text == "Merhaba" and json.loads(res.tool_calls[0]["function"]["arguments"]) == {"q": "ş"}
    assert res.usage == {"prompt_tokens": 100, "completion_tokens": 5, "cached_tokens": 90,
                         "cache_creation_input_tokens": 0}


def test_non_streaming_call_gets_a_long_read_timeout():
    """A non-streaming reply only arrives when it is finished: the 300 s stream read timeout was cutting off long thinking replies."""
    sent: dict = {}
    p = _provider(sent, _final())
    p.chat("claude-opus-5-5", [{"role": "user", "content": "selam"}])
    t = sent["timeout"]
    secs = t if isinstance(t, (int, float)) else t.read
    assert secs >= 900 > p.STREAM_READ
    p.chat("claude-opus-5-5", [{"role": "user", "content": "selam"}], max_tokens=64000)
    t = sent["timeout"]
    assert (t if isinstance(t, (int, float)) else t.read) == 1800  # scales with max_tokens
    sent.clear()
    p.chat_stream("claude-opus-5-5", [{"role": "user", "content": "selam"}], lambda d: None)
    assert "timeout" not in sent  # the stream uses the client's read timeout


def test_error_event_mid_stream_is_classified_by_its_type(monkeypatch):
    """An overloaded_error arriving after the stream opened with 200 is a temporary server error, not "bad_request"."""
    import sys
    import types

    import httpx

    class APIStatusError(Exception):
        def __init__(self, msg, response, body):
            super().__init__(msg)
            self.response, self.body, self.status_code = response, body, response.status_code

    fake = types.ModuleType("anthropic")
    fake.APITimeoutError = type("APITimeoutError", (Exception,), {})
    fake.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake.APIStatusError = APIStatusError
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    def boom(kind):
        body = {"type": "error", "error": {"type": kind, "message": "x"}}
        resp = httpx.Response(200, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
        return APIStatusError(str(body), resp, body)

    class Stream(FakeStream):
        def __iter__(self):
            raise self.err

    for kind, want in (("overloaded_error", "server"), ("api_error", "server"), ("rate_limit_error", "rate_limit")):
        p = _provider({}, _final())
        stream = Stream([], _final())
        stream.err = boom(kind)
        p._client.beta.messages.stream = lambda **kw: stream  # noqa: B023
        with pytest.raises(ProviderError) as ei:
            p.chat_stream("claude-opus-5-5", [{"role": "user", "content": "x"}], lambda t: None)
        assert ei.value.kind == want, kind


def test_cache_write_tokens_reach_the_usage_counter():
    from alice.providers.router import cache_write_tokens, cached_tokens

    final = _final()
    final.usage = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=400)
    res = _provider({}, final).chat("claude-opus-5-5", [{"role": "user", "content": "selam"}])
    assert res.usage["prompt_tokens"] == 410
    assert cache_write_tokens(res.usage) == 400 and cached_tokens(res.usage) == 0


def test_haiku_gets_no_server_fallback_and_json_schema_maps_to_output_format():
    sent: dict = {}
    p = _provider(sent, _final(text='{"a": 1}'))
    p.chat("claude-haiku-5-5", [{"role": "user", "content": "x"}],
           response_format={"type": "json_schema", "json_schema": {"schema": {"type": "object"}}})
    assert "fallbacks" not in sent and sent["output_config"]["format"]["type"] == "json_schema"


def test_stream_emits_text_reasoning_and_tool_progress():
    sent: dict = {}
    ev = [NS(type="content_block_start", content_block=NS(type="thinking")),
          NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking="düşünüyorum")),
          NS(type="content_block_delta", delta=NS(type="text_delta", text="Mer")),
          NS(type="content_block_delta", delta=NS(type="text_delta", text="haba")),
          NS(type="content_block_start", content_block=NS(type="tool_use", name="search")),
          NS(type="content_block_delta", delta=NS(type="input_json_delta", partial_json='{"q":')),
          NS(type="text", text="Mer")]  # helper event: must not be written twice
    p = _provider(sent, _final(tool="search"), ev)
    out, why, tools, first = [], [], [], []
    res = p.chat_stream("claude-sonnet-5-5", [{"role": "user", "content": "x"}], out.append,
                        on_reasoning=why.append, on_tool=lambda n, ln: tools.append((n, ln)),
                        on_first=lambda: first.append(1))
    assert "".join(out) == "Merhaba" and why == ["düşünüyorum"] and tools == [("search", 5)] and first == [1]
    assert sent["max_tokens"] == 32000 and res.tool_calls


def test_refusal_and_empty_max_tokens_become_provider_errors():
    p = _provider({}, _final(stop="refusal"))
    with pytest.raises(ProviderError) as e:
        p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}])
    assert e.value.kind == "bad_request"
    p = _provider({}, _final(stop="max_tokens", text=""))
    with pytest.raises(ProviderError) as e:
        p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], max_tokens=50)
    assert e.value.kind == "too_large" and "max_tokens" in str(e.value)


def test_sdk_errors_are_classified():
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    from alice.providers.anthropic_api import AnthropicProvider

    p = AnthropicProvider(ProviderConfig(name="anthropic", base_url="", api_key_env="", kind="anthropic"))
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def err(status, body, headers=None):
        resp = httpx2.Response(status, request=req, json=body, headers=headers or {})
        return anthropic.APIStatusError("x", response=resp, body=body)

    rl = p._error(err(429, {"error": {"type": "rate_limit_error", "message": "slow down"}}, {"retry-after": "12"}))
    assert rl.kind == "rate_limit" and rl.retry_after == 12
    assert p._error(err(401, {"error": {"type": "authentication_error", "message": "bad"}})).kind == "auth"
    assert p._error(err(529, {"error": {"type": "overloaded_error", "message": "busy"}})).kind == "server"
    assert p._error(err(400, {"error": {"type": "invalid_request_error",
                                        "message": "Your credit balance is too low"}})).kind == "quota"
    assert p._error(anthropic.APITimeoutError(request=req)).kind == "network"


def test_default_config_has_claude_models_in_menu(monkeypatch, tmp_path):
    from alice.providers.router import Router

    cfg = Config(default_config_dict())
    pc = cfg.providers()["anthropic"]
    assert pc.kind == "anthropic" and not pc.auto and not pc.enabled
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    r = Router(cfg, provider_factory=FakeClient, usage_path=tmp_path / "u.json", auto_cache={})
    rows = next(x for x in r.catalog_view() if x["name"] == "anthropic")["models"]
    assert {x["ref"] for x in rows} >= {"anthropic:claude-opus-5-5", "anthropic:claude-sonnet-5-5"}
    assert "anthropic:claude-opus-5-5" not in r.chain("brain")  # paid: does not enter the automatic chain


# ------------------------------------------------------------------------------ Keys page (server)
@pytest.fixture
def state(tmp_path, monkeypatch):
    from alice.app.server import AppState
    from alice.config import init_home, load_config
    from alice.providers import ranking

    monkeypatch.setattr(ranking, "refresh_in_background", lambda *a, **k: None)
    init_home()
    return AppState(load_config(), tmp_path, "auto", provider_factory=FakeClient)


def test_add_extra_keys_and_see_pool(state):
    assert state.set_key("GROQ_API_KEY", "gsk-first-0001")["ok"]
    r = state.add_key("GROQ_API_KEY", "gsk-second-0002")
    assert r["ok"] and r["key"] == "GROQ_API_KEY_2"
    assert state.add_key("GROQ_API_KEY", "gsk-second-0002")["error"] == "bu anahtar zaten ekli"
    assert state.add_key("GROQ_API_KEY", "gsk-third-0003")["key"] == "GROQ_API_KEY_3"
    assert state.router.providers_cfg["groq"].api_keys == ["gsk-first-0001", "gsk-second-0002", "gsk-third-0003"]
    row = next(x for x in state.keys()["keys"] if x["key"] == "GROQ_API_KEY")
    assert [x["key"] for x in row["extra"]] == ["GROQ_API_KEY_2", "GROQ_API_KEY_3"]
    assert [x["preview"] for x in row["pool"]] == ["••••0001", "••••0002", "••••0003"]
    assert "gsk-second" not in json.dumps(state.keys())  # the key itself does not go to the browser
    assert state.set_key("GROQ_API_KEY_2", "")["ok"]  # delete
    assert state.router.providers_cfg["groq"].api_keys == ["gsk-first-0001", "gsk-third-0003"]
    assert not state.set_key("RANDOM_THING_2", "x")["ok"] and not state.set_key("GROQ_API_KEY_99", "x")["ok"]


def test_key_strategy_is_saved(state):
    from alice.config import alice_home

    assert state.set_key_strategy("fill-first")["ok"] and state.keys()["strategy"] == "fill-first"
    assert 'key_strategy = "fill-first"' in (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert not state.set_key_strategy("random")["ok"]


def test_add_and_remove_openai_compatible_server(state, monkeypatch):
    from alice.config import alice_home, load_config
    from alice.providers import openai_compat

    seen = {}

    def list_models(self):
        seen["url"], seen["key"] = self.cfg.base_url, self.cfg.api_key
        return [{"id": "qwen3-32b"}, {"id": "llama-4"}]

    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "list_models", list_models)
    assert not state.add_provider({"name": "gemini", "base_url": "http://x/v1"})["ok"]  # ready-made provider
    assert not state.add_provider({"name": "Bad Name", "base_url": "http://x/v1"})["ok"]
    r = state.add_provider({"name": "lmstudio", "base_url": "http://192.168.1.5:1234/v1/", "key": "lm-secret-key"})
    assert r["ok"] and r["models"] == ["qwen3-32b", "llama-4"]
    assert seen == {"url": "http://192.168.1.5:1234/v1", "key": "lm-secret-key"}
    pc = state.router.providers_cfg["lmstudio"]
    assert pc.custom and pc.enabled and pc.api_key_env == "LMSTUDIO_API_KEY"
    assert load_config().providers()["lmstudio"].base_url == "http://192.168.1.5:1234/v1"  # persisted
    row = next(x for x in state.keys()["keys"] if x["key"] == "LMSTUDIO_API_KEY")
    assert row["custom"] and row["provider"] == "lmstudio"
    assert state.add_key("LMSTUDIO_API_KEY", "lm-second")["ok"]  # pool for it too
    assert state.remove_provider("lmstudio")["ok"] and "lmstudio" not in state.router.providers_cfg
    assert "lmstudio" not in (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert not state.remove_provider("gemini")["ok"]


def test_unreachable_server_needs_force(state, monkeypatch):
    from alice.providers import openai_compat

    def fail(self):
        raise ProviderError("unreachable", "bağlanılamadı")

    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "list_models", fail)
    r = state.add_provider({"name": "vllm", "base_url": "http://10.0.0.2:8000/v1"})
    assert not r["ok"] and r["can_force"]
    assert state.add_provider({"name": "vllm", "base_url": "http://10.0.0.2:8000/v1", "force": True,
                               "models": "a, b"})["models"] == ["a", "b"]
    assert not state.router.providers_cfg["vllm"].needs_key
