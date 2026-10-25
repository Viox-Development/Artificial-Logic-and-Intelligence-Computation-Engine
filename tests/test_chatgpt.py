"""ChatGPT hesabıyla giriş: OAuth akışı, anahtar yenileme, Codex arka ucuna istek biçimi ve akış çözümü."""

from __future__ import annotations

import base64
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from alice.providers import chatgpt
from alice.providers.chatgpt import ChatGPTProvider, to_responses, to_tools
from alice.providers.openai_compat import ProviderError
from tests.conftest import make_config


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def tokens(exp_in=3600, email="a@b.c", acc="acc1", refresh="r1"):
    return {"access_token": jwt({"exp": time.time() + exp_in}), "refresh_token": refresh,
            "id_token": jwt({"email": email, "https://api.openai.com/auth": {"chatgpt_account_id": acc,
                                                                            "chatgpt_plan_type": "free"}})}


def pc():
    from alice.config import default_config_dict

    data = default_config_dict()["providers"]["chatgpt"]  # varsayılan ayarda tanımlı
    cfg = make_config({"brain": ["chatgpt:gpt-5.5"]}, {"chatgpt": data})
    return cfg.providers()["chatgpt"]


def sse(*events) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_provider_enabled_only_after_login():
    assert pc().auth == "chatgpt" and not pc().enabled
    chatgpt.save_auth(tokens())
    st = chatgpt.status()
    assert pc().enabled and (st["logged_in"], st["email"], st["plan"]) == (True, "a@b.c", "free")
    assert chatgpt.logout() and not pc().enabled


def test_messages_become_responses_items():
    msgs = [{"role": "system", "content": "Sen ALICE'sin."},
            {"role": "user", "content": [{"type": "text", "text": "bu ne?"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
            {"role": "assistant", "content": "bakıyorum", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "içerik"}]
    instr, items = to_responses(msgs)
    assert instr == "Sen ALICE'sin."
    assert items[0] == {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "bu ne?"}, {"type": "input_image", "image_url": "data:image/png;base64,AA"}]}
    assert items[1]["content"] == [{"type": "output_text", "text": "bakıyorum"}]
    assert items[2] == {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{\"path\": \"a\"}"}
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "içerik"}
    # Arka uç kendi istemini isterse: talimat Codex'inki, ALICE'inki geliştirici mesajı olarak başta
    instr, items = to_responses(msgs, codex_prompt="CODEX")
    assert instr == "CODEX" and items[0]["role"] == "developer"
    assert to_tools([{"type": "function", "function": {"name": "x", "description": "d", "parameters": {}}}]) == [
        {"type": "function", "name": "x", "description": "d", "parameters": {"type": "object", "properties": {}},
         "strict": False}]


def test_stream_text_tool_call_usage_and_headers():
    chatgpt.save_auth(tokens())
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
        return httpx.Response(200, content=sse(
            {"type": "response.output_text.delta", "delta": "Mer"}, {"type": "response.output_text.delta", "delta": "haba"},
            {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c9", "name": "weather",
                                                            "arguments": "{}"}},
            {"type": "response.completed", "response": {"model": "gpt-5.5", "usage": {"input_tokens": 50,
                                                                                    "output_tokens": 7}}}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    parts, first = [], []
    r = p.chat_stream("gpt-5.5", [{"role": "system", "content": "S"}, {"role": "user", "content": "selam"}],
                      parts.append, tools=[{"type": "function", "function": {"name": "weather", "parameters": {}}}],
                      extra={"reasoning_effort": "low"}, on_first=lambda: first.append(1))
    assert r.text == "Merhaba" and "".join(parts) == "Merhaba" and first == [1]
    assert r.message["tool_calls"][0]["function"]["name"] == "weather" and r.usage == {"prompt_tokens": 50,
                                                                                       "completion_tokens": 7}
    assert seen["url"].endswith("/backend-api/codex/responses")
    assert seen["headers"]["chatgpt-account-id"] == "acc1" and seen["headers"]["authorization"].startswith("Bearer ")
    b = seen["body"]
    assert b["instructions"] == "S" and b["stream"] is True and b["store"] is False
    assert b["reasoning"] == {"effort": "low", "summary": "auto"} and b["tools"][0]["name"] == "weather"


def test_rejected_instructions_retry_with_codex_prompt(isolated_home):
    chatgpt.save_auth(tokens())
    (isolated_home / "codex-prompt.md").write_text("CODEX PROMPT", encoding="utf-8")
    monkey_prompt = chatgpt._codex_prompt
    chatgpt._codex_prompt = None
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"detail": "Instructions are not valid"})
        return httpx.Response(200, content=sse({"type": "response.output_text.delta", "delta": "tamam"}))

    try:
        p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
        assert p.chat("gpt-5.5", [{"role": "system", "content": "ALICE"}, {"role": "user", "content": "x"}]).text == "tamam"
        assert bodies[1]["instructions"] == "CODEX PROMPT" and bodies[1]["input"][0]["role"] == "developer"
    finally:
        chatgpt._codex_prompt = monkey_prompt


def test_usage_limit_is_rate_limit_and_expired_token_refreshes():
    chatgpt.save_auth(tokens(exp_in=-10))
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/oauth/token":
            assert json.loads(req.content)["refresh_token"] == "r1"
            return httpx.Response(200, json=tokens())
        return httpx.Response(200, content=sse({"type": "response.failed", "response": {"error": {
            "code": "usage_limit_reached", "message": "limit"}}}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as ei:
        p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    assert ei.value.kind == "rate_limit" and calls[0] == "/oauth/token"
    assert chatgpt.load_auth()["expires_at"] > time.time()


def test_not_logged_in_is_auth_error():
    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(ProviderError) as ei:
        p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    assert ei.value.kind == "auth" and "giriş" in str(ei.value)


def test_browser_login_flow(monkeypatch):
    exchanged = {}

    def fake_exchange(code, verifier, client=None):
        exchanged.update(code=code, verifier=verifier)
        return chatgpt.save_auth(tokens())

    monkeypatch.setattr(chatgpt, "exchange", fake_exchange)
    monkeypatch.setattr(chatgpt, "PORT", 0)  # testte boş bir port

    real_server = chatgpt.HTTPServer
    ports = {}

    class Server(real_server):
        def __init__(self, addr, handler):
            super().__init__(("127.0.0.1", 0), handler)
            ports["p"] = self.server_address[1]

    monkeypatch.setattr(chatgpt, "HTTPServer", Server)

    def open_url(url):
        q = parse_qs(urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["client_id"] == [chatgpt.CLIENT_ID]

        def visit():
            httpx.get(f"http://127.0.0.1:{ports['p']}/auth/callback?code=abc&state={q['state'][0]}")
        threading.Thread(target=visit).start()

    info = chatgpt.login(open_url=open_url, timeout=10)
    assert exchanged["code"] == "abc" and len(exchanged["verifier"]) > 40 and info["email"] == "a@b.c"


def test_several_accounts_switched_by_hand(isolated_home):
    chatgpt.save_auth(tokens())
    chatgpt.save_auth(tokens(email="x@y.z", acc="acc2", refresh="r2"))  # "Hesap ekle": yeni hesap seçili olur
    accs = chatgpt.accounts()
    assert [(a["email"], a["active"]) for a in accs] == [("a@b.c", False), ("x@y.z", True)]
    assert chatgpt.load_auth()["refresh_token"] == "r2"
    assert chatgpt.use("acc1") and chatgpt.load_auth()["email"] == "a@b.c"
    # yenileme seçili hesabın üstüne yazar, diğerine dokunmaz
    chatgpt.save_auth({"access_token": tokens()["access_token"]}, key="acc1")
    assert chatgpt.load_auth()["refresh_token"] == "r1" and len(chatgpt.accounts()) == 2
    assert chatgpt.remove("acc1") and chatgpt.load_auth()["email"] == "x@y.z"  # kalan hesap seçilir
    assert chatgpt.remove() and not chatgpt.logged_in() and not chatgpt.auth_path().exists()
    assert not chatgpt.use("yok")


def test_old_single_account_file_still_works(isolated_home):
    isolated_home.mkdir(parents=True, exist_ok=True)
    chatgpt.auth_path().write_text(json.dumps({"access_token": "a", "refresh_token": "r", "email": "e@f.g",
                                               "account_id": "old"}), encoding="utf-8")
    assert chatgpt.logged_in() and chatgpt.accounts()[0]["email"] == "e@f.g"


def test_switching_clears_previous_account_limit():
    from types import SimpleNamespace

    from alice.app.setup_panel import SetupPanel

    chatgpt.save_auth(tokens())
    chatgpt.save_auth(tokens(email="x@y.z", acc="acc2"))
    router = SimpleNamespace(cooldown_until={"chatgpt:gpt-5.5": 9e9, "g:m": 9e9}, cooldown_kind={"chatgpt:gpt-5.5": "rate_limit"},
                             dead_providers={"chatgpt"})
    p = SetupPanel(make_config({"brain": ["a:m"]}), router)
    assert p.action("chatgpt_use", {"key": "acc1"})["ok"] and chatgpt.load_auth()["email"] == "a@b.c"
    assert router.cooldown_until == {"g:m": 9e9} and not router.dead_providers
    assert not p.action("chatgpt_use", {"key": "yok"})["ok"]


def test_limits_from_headers_usage_endpoint_and_model_menu():
    chatgpt.save_auth(tokens())
    lim = chatgpt.limits_from_headers(httpx.Headers({"x-codex-primary-used-percent": "40",
                                                     "x-codex-primary-window-minutes": "300",
                                                     "x-codex-primary-reset-after-seconds": "3600",
                                                     "x-codex-secondary-used-percent": "12",
                                                     "x-codex-secondary-window-minutes": "10080"}))
    chatgpt.save_limits(lim)
    assert chatgpt.limit_note() == "5 sa: %40 · haftalık: %12"
    assert chatgpt.status()["limits"]["primary"]["reset_at"] > time.time()

    def handler(req):
        if req.url.path.endswith("/wham/usage"):
            return httpx.Response(200, json={"plan_type": "free", "rate_limit": {
                "primary_window": {"used_percent": 75, "limit_window_seconds": 18000, "reset_after_seconds": 60}}})
        return httpx.Response(200, json={"models": [{"slug": "gpt-5.5"}, {"slug": "gizli", "visibility": "hide"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert chatgpt.fetch_usage(client)["primary"]["used_percent"] == 75
    assert ChatGPTProvider(pc(), client=client).list_models() == [{"id": "gpt-5.5", "context_length": None}]
    assert chatgpt.cached_models() == ["gpt-5.5"]

    from alice.providers.router import Router

    cfg = make_config({"brain": ["a:m"]}, {"chatgpt": dict(__import__("alice.config", fromlist=["x"])
                                                          .default_config_dict()["providers"]["chatgpt"])})
    view = {p["name"]: p for p in Router(cfg, provider_factory=lambda p: None, auto_cache={}).catalog_view()}
    assert [m["ref"] for m in view["chatgpt"]["models"]] == ["chatgpt:gpt-5.5"]  # ölçülmeden menüde
    assert view["chatgpt"]["note"] == "5 sa: %75"


def test_stream_records_limit_headers():
    chatgpt.save_auth(tokens())

    def handler(req):
        return httpx.Response(200, headers={"x-codex-primary-used-percent": "5", "x-codex-primary-window-minutes": "300"},
                              content=sse({"type": "response.output_text.delta", "delta": "ok"}))

    ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler))).chat(
        "gpt-5.5", [{"role": "user", "content": "x"}])
    assert chatgpt.limit_note() == "5 sa: %5"
