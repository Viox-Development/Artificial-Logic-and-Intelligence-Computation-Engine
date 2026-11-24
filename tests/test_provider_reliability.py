"""Provider layer error resilience: Retry-After, HTML error pages, key masking, malformed/empty SSE
chunks, network exceptions, and closing the probe client. No network: httpx.MockTransport."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from alice.config import ProviderConfig
from alice.providers import ranking
from alice.providers.openai_compat import (OpenAICompatProvider, ProviderError, classify_error,
                                           parse_retry_after, redact, summarize_body)
from alice.providers.router import Router
from tests.conftest import FakeProvider, make_config

KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"


def provider(handler, key_env: str = "") -> OpenAICompatProvider:
    pc = ProviderConfig("x", "http://x", api_key_env=key_env)
    return OpenAICompatProvider(pc, client=httpx.Client(transport=httpx.MockTransport(handler)))


def sse_raw(*lines: str) -> bytes:
    return "".join(f"{ln}\n\n" for ln in lines).encode()


# ----------------------------------------------------------------- Retry-After
def test_retry_after_http_date_is_parsed():
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    when = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(when, now=now) == pytest.approx(90.0)
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("-5") == 0.0
    assert parse_retry_after("nan") is None and parse_retry_after("inf") is None
    assert parse_retry_after("yarın") is None and parse_retry_after("") is None
    later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=300), usegmt=True)
    e = classify_error(429, "slow down", {"retry-after": later})
    assert e.kind == "rate_limit" and 280 <= e.retry_after <= 301


def test_server_retry_after_extends_cooldown():
    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [ProviderError("server", "overloaded", 503, retry_after=120)])
    b = FakeProvider("b", ["ok"])
    r = Router(cfg, provider_factory=lambda pc: {"a": a, "b": b}[pc.name], sleep=lambda s: None, auto_cache={})
    r.chat("brain", [{"role": "user", "content": "x"}])
    left = r.cooldown_until["a:m1"] - time.monotonic()
    assert 100 < left <= 120.5  # previously a fixed 30 s (coarse Windows monotonic clock, hence a small margin)


def test_server_short_retry_after_keeps_base_cooldown():
    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [ProviderError("server", "overloaded", 503, retry_after=2)])
    b = FakeProvider("b", ["ok"])
    r = Router(cfg, provider_factory=lambda pc: {"a": a, "b": b}[pc.name], sleep=lambda s: None, auto_cache={})
    r.chat("brain", [{"role": "user", "content": "x"}])
    assert r.cooldown_until["a:m1"] - time.monotonic() > 25


# -------------------------------------------------------- new status codes
def test_402_is_quota_and_408_is_transient():
    assert classify_error(402, '{"error":{"message":"Insufficient credits"}}').kind == "quota"
    assert classify_error(408, "Request Timeout").kind == "network"


def test_inline_error_with_string_code_is_classified():
    body = {"error": {"code": "429", "message": "Rate limit exceeded: free-models-per-day"}}
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(200, json=body)).chat("m", [])
    assert ei.value.kind == "quota" and ei.value.daily


# --------------------------------------------------------- HTML error page
CF_PAGE = """<!DOCTYPE html><html><head><title>502 Bad Gateway</title>
<style>body{font-family:sans-serif;color:#333}</style><script>var x = 1;</script></head>
<body><h1>502 Bad Gateway</h1><p>cloudflare</p></body></html>"""


def test_html_error_page_is_summarized():
    e = classify_error(502, CF_PAGE)
    msg = str(e)
    assert e.kind == "server"
    assert "502 Bad Gateway" in msg and "cloudflare" in msg
    assert "<" not in msg and "font-family" not in msg and "var x" not in msg
    assert summarize_body("") == "(empty body)"
    assert summarize_body("  plain\n\n text ") == "plain text"


def test_200_html_body_gives_readable_error():
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(200, text=CF_PAGE)).chat("m", [])
    assert ei.value.kind == "server" and "<" not in str(ei.value)


# --------------------------------------------------------------- masking
def test_redact_patterns():
    assert KEY not in redact(f"bad key {KEY}")
    assert "AIza" not in redact("key=AIzaSyA0123456789abcdefghijklmnopqrstuv invalid")
    assert redact("Authorization: Bearer abcdefgh12345678") == "Authorization: Bearer ***"
    assert redact("http://h/v1?key=secret123&x=1") == "http://h/v1?key=***&x=1"
    assert redact("my custom-secret-value here", "custom-secret-value") == "my *** here"
    assert redact("nothing secret") == "nothing secret"


def test_echoed_key_is_redacted_in_provider_errors(monkeypatch):
    custom = "plainkey_without_known_prefix_123"
    monkeypatch.setenv("ALICE_TEST_FAKE_KEY", custom)  # fake key, not real
    body = f'{{"error":{{"message":"Incorrect API key provided: {custom}"}}}}'
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(401, text=body), key_env="ALICE_TEST_FAKE_KEY").chat("m", [])
    assert ei.value.kind == "auth" and custom not in str(ei.value)
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(401, text=body), key_env="ALICE_TEST_FAKE_KEY").chat_stream("m", [], lambda t: None)
    assert custom not in str(ei.value)
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(401, text=body), key_env="ALICE_TEST_FAKE_KEY").list_models()
    assert custom not in str(ei.value)


def test_router_log_redacts_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_TEST_FAKE_KEY", "a-very-secret-key-42")  # fake key
    cfg = make_config({"brain": ["a:m1", "b:m2"]}, {
        "a": {"base_url": "http://a", "api_key_env": "ALICE_TEST_FAKE_KEY", "max_input_tokens": 100000},
        "b": {"base_url": "http://b", "api_key_env": "", "max_input_tokens": 100000}})
    a = FakeProvider("a", [ProviderError("server", "upstream said Bearer zzzzzzzzzzzzzzzz")])
    b = FakeProvider("b", ["ok"])
    r = Router(cfg, provider_factory=lambda pc: {"a": a, "b": b}[pc.name], sleep=lambda s: None, auto_cache={},
               usage_path=tmp_path / "usage.json")
    r._log("hata: a-very-secret-key-42 echoed")
    r.chat("brain", [{"role": "user", "content": "x"}])
    log = r.log_path.read_text(encoding="utf-8")
    assert "a-very-secret-key-42" not in log and "zzzzzzzz" not in log and "***" in log


# ------------------------------------------------------------ malformed SSE/JSON
def test_stream_survives_malformed_chunks():
    body = sse_raw(
        "data: {not json",
        'data: "just a string"',
        'data: {"choices": "oops"}',
        'data: {"choices": ["x", {"delta": "bad"}, {"delta": {"content": ["parca"]}}]}',
        'data: {"choices": [{"delta": {"content": [{"type": "text", "text": "Mer"}]}}]}',
        'data: {"choices": [{"delta": {"content": "haba", "tool_calls": ["bad", {"index": 0, "id": "t1",'
        ' "function": {"name": "read_file", "arguments": 5}}]}}], "usage": "bozuk", "model": 3}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": "bad"}]}}]}',
        "data: [DONE]",
    )
    res = provider(lambda r: httpx.Response(200, content=body)).chat_stream("m", [], lambda t: None)
    assert res.text == "Merhaba" and res.usage == {} and res.model == "m"
    assert res.tool_calls[0]["function"]["name"] == "read_file"


def test_stream_without_any_chunk_is_an_error():
    for content in (b"", CF_PAGE.encode(), b": keep-alive\n\n"):
        with pytest.raises(ProviderError) as ei:
            provider(lambda r, c=content: httpx.Response(200, content=c)).chat_stream("m", [], lambda t: None)
        assert ei.value.kind == "server"
    # A stream containing only [DONE] is the provider's deliberate empty answer: not an error.
    res = provider(lambda r: httpx.Response(200, content=b"data: [DONE]\n\n")).chat_stream("m", [], lambda t: None)
    assert res.text == ""


def test_chat_non_dict_json_and_bad_choices():
    for data in ([1, 2], {"choices": ["x"]}, {"choices": [{"message": "str"}]}, {"choices": {}}):
        with pytest.raises(ProviderError) as ei:
            provider(lambda r, d=data: httpx.Response(200, json=d)).chat("m", [])
        assert ei.value.kind == "server"
    ok = provider(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}],
                                                       "usage": "bozuk"})).chat("m", [])
    assert ok.text == "hi" and ok.usage == {}


def test_list_models_accepts_bare_list_and_rejects_junk():
    assert provider(lambda r: httpx.Response(200, json=[{"id": "a"}, "x"])).list_models() == [{"id": "a"}]
    assert provider(lambda r: httpx.Response(200, json={"data": [{"id": "b"}]})).list_models() == [{"id": "b"}]
    assert provider(lambda r: httpx.Response(200, json={"data": None})).list_models() == []
    with pytest.raises(ProviderError):
        provider(lambda r: httpx.Response(200, text="<html>nope</html>")).list_models()

    def boom(r):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError) as ei:
        provider(boom).list_models()
    assert ei.value.kind == "unreachable"


# ------------------------------------------------------------ network exceptions
@pytest.mark.parametrize("exc,kind,needle", [
    (httpx.ReadTimeout(""), "network", "ReadTimeout"),
    (httpx.ConnectTimeout(""), "network", "ConnectTimeout"),
    (httpx.ConnectError("Name or service not known"), "unreachable", "Name or service"),
    (httpx.RemoteProtocolError("peer closed connection"), "network", "cut the connection off"),
])
def test_transport_errors_have_readable_messages(exc, kind, needle):
    def handler(r):
        raise exc

    for call in (lambda p: p.chat("m", []), lambda p: p.chat_stream("m", [], lambda t: None)):
        with pytest.raises(ProviderError) as ei:
            call(provider(handler))
        assert ei.value.kind == kind and needle in str(ei.value)


def test_mid_stream_disconnect_is_network_error():
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices": [{"delta": {"content": "yar"}}]}\n\n'
            raise httpx.RemoteProtocolError("incomplete chunked read")

    deltas: list[str] = []
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(200, stream=Broken())).chat_stream("m", [], deltas.append)
    assert deltas == ["yar"] and ei.value.kind == "network"


def test_invalid_url_does_not_escape_as_raw_exception():
    def handler(r):
        raise httpx.InvalidURL("bad url")

    with pytest.raises(ProviderError) as ei:
        provider(handler).chat("m", [])
    assert ei.value.kind == "unreachable"


# ------------------------------------------------------- probe client
def test_probe_closes_its_http_client(monkeypatch):
    closed: list[bool] = []
    real_close = OpenAICompatProvider.close

    def close(self):
        closed.append(True)
        real_close(self)

    def fail(self, *a, **k):
        raise ProviderError("server", "down")

    monkeypatch.setattr(OpenAICompatProvider, "close", close)
    monkeypatch.setattr(OpenAICompatProvider, "chat_stream", fail)
    cfg = make_config({"brain": ["a:m1"]})
    r = Router(cfg, provider_factory=lambda pc: FakeProvider(pc.name, []), auto_cache={})
    pr = ranking.probe(r, "a:m1", specs=[{"type": "function", "function": {"name": "list_dir"}}])
    assert pr.error_kind == "server" and closed == [True]

    def ok(self, model, msgs, on_delta, **k):
        from alice.providers.openai_compat import ChatResult
        return ChatResult({"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "list_dir", "arguments": json.dumps({})}}]}, "a", model)

    monkeypatch.setattr(OpenAICompatProvider, "chat_stream", ok)
    pr = ranking.probe(r, "a:m1", specs=[{"type": "function", "function": {"name": "list_dir"}}])
    assert pr.ok and closed == [True, True]
