"""ChatGPT account sign-in: OAuth flow, key refresh, request format for the Codex backend, stream parsing."""

from __future__ import annotations

import base64
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from types import SimpleNamespace

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

    data = default_config_dict()["providers"]["chatgpt"]  # defined in the default settings
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
    # If the backend wants its own prompt: the instructions are Codex's, and ALICE's go first as a developer message
    instr, items = to_responses(msgs, codex_prompt="CODEX")
    assert instr == "CODEX" and items[0]["role"] == "developer"
    assert to_tools([{"type": "function", "function": {"name": "x", "description": "d", "parameters": {}}}]) == [
        {"type": "function", "name": "x", "description": "d", "parameters": {"type": "object", "properties": {}},
         "strict": False}]


def test_system_messages_with_parts_reach_instructions():
    msgs = [{"role": "system", "content": "A"},
            {"role": "system", "content": [{"type": "text", "text": "B1"}, {"type": "text", "text": "B2"}]},
            {"role": "system", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]},
            {"role": "user", "content": "selam"}]
    instr, items = to_responses(msgs)
    assert instr == "A\n\nB1\nB2" and len(items) == 1
    _, items = to_responses(msgs, codex_prompt="CODEX")
    assert items[0]["content"] == [{"type": "input_text", "text": "A\n\nB1\nB2"}]


def test_tool_result_images_go_as_input_image_not_base64_text():
    b64 = "QUJD" * 400
    msgs = [{"role": "user", "content": "bak"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "screen", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "raw", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": [
                {"type": "text", "text": "ekran"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
            {"role": "tool", "tool_call_id": "c2", "content": {"png": b64, "name": "x"}},
            {"role": "user", "content": "devam"}]
    _, items = to_responses(msgs)
    out1, out2, img, user = items[3:]
    assert out1["type"] == "function_call_output" and out1["output"].startswith("ekran") and b64 not in out1["output"]
    assert out2["type"] == "function_call_output" and b64 not in out2["output"] and '"name": "x"' in out2["output"]
    # The image is sent as a user message after the tool outputs end (not between them).
    assert img == {"type": "message", "role": "user", "content": [
        {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"}]}
    assert user["content"] == [{"type": "input_text", "text": "devam"}]
    _, items = to_responses(msgs[:3])  # the image is also sent when the conversation ends with a tool result
    assert items[-1]["role"] == "user" and items[-1]["content"][0]["type"] == "input_image"


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
                                                                                       "completion_tokens": 7,
                                                                                       "cached_tokens": 0}
    assert seen["url"].endswith("/backend-api/codex/responses")
    assert seen["headers"]["chatgpt-account-id"] == "acc1" and seen["headers"]["authorization"].startswith("Bearer ")
    b = seen["body"]
    assert b["instructions"] == "S" and b["stream"] is True and b["store"] is False
    assert b["reasoning"] == {"effort": "low", "summary": "auto"} and b["tools"][0]["name"] == "weather"


def test_chat_maps_response_format_and_does_not_send_max_tokens():
    chatgpt.save_auth(tokens())
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, content=sse({"type": "response.output_text.delta", "delta": "{}"}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    p.chat("gpt-5.5", [{"role": "user", "content": "x"}], max_tokens=50, temperature=0.2,
           response_format={"type": "json_schema", "json_schema": {"name": "cevap", "schema": schema, "strict": True}})
    b = bodies[-1]
    assert b["text"] == {"format": {"type": "json_schema", "name": "cevap", "schema": schema, "strict": True}}
    # The Codex backend rejects max_output_tokens/temperature, so they are not sent.
    assert "max_output_tokens" not in b and "temperature" not in b
    p.chat("gpt-5.5", [{"role": "user", "content": "x"}], response_format={"type": "json_object"})
    b = bodies[-1]
    assert b["text"] == {"format": {"type": "json_object"}}
    assert b["input"][-1]["role"] == "developer" and "JSON" in b["input"][-1]["content"][0]["text"]
    p.chat("gpt-5.5", [{"role": "user", "content": "JSON ver"}], response_format={"type": "json_object"})
    assert len(bodies[-1]["input"]) == 1  # no extra message if the input already mentions JSON
    p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    assert "text" not in bodies[-1]


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
    assert ei.value.kind == "auth" and "sign" in str(ei.value)


def test_browser_login_flow(monkeypatch):
    exchanged = {}

    def fake_exchange(code, verifier, client=None):
        exchanged.update(code=code, verifier=verifier)
        return chatgpt.save_auth(tokens())

    monkeypatch.setattr(chatgpt, "exchange", fake_exchange)
    monkeypatch.setattr(chatgpt, "PORT", 0)  # an empty port for the test

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
    chatgpt.save_auth(tokens(email="x@y.z", acc="acc2", refresh="r2"))  # "Add account": the new one becomes selected
    accs = chatgpt.accounts()
    assert [(a["email"], a["active"]) for a in accs] == [("a@b.c", False), ("x@y.z", True)]
    assert chatgpt.load_auth()["refresh_token"] == "r2"
    assert chatgpt.use("acc1") and chatgpt.load_auth()["email"] == "a@b.c"
    # refresh overwrites the selected account and leaves the others untouched
    chatgpt.save_auth({"access_token": tokens()["access_token"]}, key="acc1")
    assert chatgpt.load_auth()["refresh_token"] == "r1" and len(chatgpt.accounts()) == 2
    removed = chatgpt.remove("acc1")
    assert removed and chatgpt.load_auth()["email"] == "x@y.z"  # the remaining account is selected
    removed = chatgpt.remove()
    assert removed and not chatgpt.logged_in() and not chatgpt.auth_path().exists()
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
    assert chatgpt.limit_note() == "5 h: 40% · weekly: 12%"
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
    assert [m["ref"] for m in view["chatgpt"]["models"]] == ["chatgpt:gpt-5.5"]  # in the menu before being measured
    assert view["chatgpt"]["note"] == "5 h: 75%"


def test_stream_records_limit_headers():
    chatgpt.save_auth(tokens())

    def handler(req):
        return httpx.Response(200, headers={"x-codex-primary-used-percent": "5", "x-codex-primary-window-minutes": "300"},
                              content=sse({"type": "response.output_text.delta", "delta": "ok"}))

    ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler))).chat(
        "gpt-5.5", [{"role": "user", "content": "x"}])
    assert chatgpt.limit_note() == "5 h: 5%"


def test_models_fetched_later_if_login_was_before_list_existed(monkeypatch):
    """Signed in on an older version: there was no model list, so chatgpt never showed up in the menu."""
    from alice.providers.router import Router

    chatgpt.save_auth(tokens())
    calls = []

    def fail(self):
        calls.append(1)
        raise ProviderError("not_found", "404 yok")

    monkeypatch.setattr(ChatGPTProvider, "list_models", fail)
    from alice.config import default_config_dict

    cfg = make_config({"brain": ["a:m"]}, {"chatgpt": default_config_dict()["providers"]["chatgpt"]})
    router = Router(cfg, provider_factory=lambda p: None, auto_cache={})
    view = {p["name"]: p for p in router.catalog_view()}
    # The list is read in the background: on a fast machine the attempt may have finished and written its error
    # before this line (it happened sometimes on CI); both are correct states.
    assert view["chatgpt"]["models"] == []
    assert view["chatgpt"]["loading"] or "404" in (view["chatgpt"].get("error") or "")
    for _ in range(50):
        if chatgpt.models_error():
            break
        time.sleep(0.05)
    assert calls == [1] and "404" in chatgpt.models_error()
    view = {p["name"]: p for p in router.catalog_view()}
    assert "404" in view["chatgpt"]["error"] and calls == [1]  # not retried right away

    def ok(self):
        chatgpt._update_active(models=["gpt-5.5"])
        return [{"id": "gpt-5.5"}]

    monkeypatch.setattr(ChatGPTProvider, "list_models", ok)
    chatgpt._update_active(models_tried=0)
    router.catalog_view()
    for _ in range(50):
        if chatgpt.cached_models():
            break
        time.sleep(0.05)
    assert [m["ref"] for m in {p["name"]: p for p in router.catalog_view()}["chatgpt"]["models"]] == ["chatgpt:gpt-5.5"]


def test_empty_list_is_an_error_and_manual_add_works(monkeypatch):
    """User: 'okunuyor' (reading) showed for minutes. Empty list is now an error; a model name can be typed in."""
    chatgpt.save_auth(tokens())
    asked = []

    def handler(req):
        asked.append(dict(req.url.params))
        return httpx.Response(200, json={"models": []})

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="came back empty"):
        p.list_models()
    assert [a.get("client_version") for a in asked] == [chatgpt.CLIENT_VERSION, "9.99.0", None]

    assert chatgpt.models_state() == "error"  # never attempted, treated as expired
    chatgpt._update_active(models_tried=time.time())
    assert chatgpt.models_state() == "loading"
    chatgpt._update_active(models_tried=time.time() - chatgpt.LOADING_MAX - 1)
    assert chatgpt.models_state() == "error" and chatgpt.models_error() == "sunucu cevap vermedi"

    from alice.app.setup_panel import SetupPanel

    panel = SetupPanel(make_config({"brain": ["a:m"]}), SimpleNamespace(providers_cfg={}))
    assert not panel.action("chatgpt_model_add", {"name": "  "})["ok"]
    assert panel.action("chatgpt_model_add", {"name": "gpt-5.5"})["models"] == ["gpt-5.5"]
    assert chatgpt.models_state() == "ok" and chatgpt.cached_models() == ["gpt-5.5"]


def _limit_429(secs=1348566):
    return httpx.Response(429, json={"error": {"type": "usage_limit_reached", "message": "The usage limit has been "
                                               "reached", "plan_type": "free", "resets_in_seconds": secs}})


def test_limited_account_switches_to_a_free_one():
    """Live log (9 Nov): when the selected account hit its limit, it errored and fell back to Gemini
    while a saved, unused account sat idle."""
    chatgpt.save_auth(tokens())
    chatgpt.save_auth(tokens(email="x@y.z", acc="acc2", refresh="r2"))  # selected: acc2
    seen = []

    def handler(req):
        seen.append(req.headers.get("chatgpt-account-id"))
        if seen[-1] == "acc2":
            return _limit_429()
        return httpx.Response(200, content=sse({"type": "response.output_text.delta", "delta": "tamam"}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.chat("gpt-5.5", [{"role": "user", "content": "x"}]).text == "tamam"
    assert seen == ["acc2", "acc1"] and chatgpt.load_auth()["email"] == "a@b.c"
    # the used-up account's reset was saved with the time the server gave; the next request never goes to it
    accs = chatgpt._store()["accounts"]
    assert accs["acc2"]["limits"]["primary"]["reset_at"] > time.time() + 1_000_000
    assert chatgpt.use("acc2") and p.chat("gpt-5.5", [{"role": "user", "content": "x"}]).text == "tamam"
    assert seen == ["acc2", "acc1", "acc1"]


def test_all_accounts_limited_is_quota_with_reset_time():
    """When all were used up, the error was counted as a 20 s rate limit and the same accounts were
    retried every half minute."""
    chatgpt.save_auth(tokens())
    chatgpt.save_auth(tokens(email="x@y.z", acc="acc2", refresh="r2"))
    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(lambda req: _limit_429(600))))
    with pytest.raises(ProviderError) as ei:
        p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    assert ei.value.kind == "quota" and 500 < ei.value.retry_after <= 600 and "all 2 saved accounts" in str(ei.value)


def test_single_account_limit_keeps_server_reset_time():
    chatgpt.save_auth(tokens())
    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(lambda req: _limit_429(7200))))
    with pytest.raises(ProviderError) as ei:
        p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    assert ei.value.kind == "quota" and ei.value.retry_after == 7200


def test_temporary_rate_limit_does_not_mark_the_account_as_used_up():
    """A temporary 429 does not mark the account "used up" for an hour; the request still tries another one.
    If all accounts are under a temporary limit, the error goes to the router as a rate limit, not a quota."""
    chatgpt.save_auth(tokens())
    chatgpt.save_auth(tokens(email="x@y.z", acc="acc2", refresh="r2"))  # selected: acc2
    seen = []

    def handler(req):
        seen.append(req.headers.get("chatgpt-account-id"))
        if seen[-1] == "acc2":
            return httpx.Response(429, json={"error": {"message": "Too many requests"}})
        return httpx.Response(200, content=sse({"type": "response.output_text.delta", "delta": "tamam"}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.chat("gpt-5.5", [{"role": "user", "content": "x"}]).text == "tamam" and seen == ["acc2", "acc1"]
    assert not (chatgpt._store()["accounts"]["acc2"].get("limits") or {}).get("primary")  # not marked as used up

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(429, json={"error": {"message": "Too many requests"}}))))
    with pytest.raises(ProviderError) as ei:
        p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    assert ei.value.kind == "rate_limit"


def test_request_keeps_its_own_account_when_another_chat_switches():
    """The request goes out with the account it started with, and the limit is written to that account,
    even if the selected account changes in the meantime."""
    chatgpt.save_auth(tokens())
    chatgpt.save_auth(tokens(email="x@y.z", acc="acc2", refresh="r2"))  # selected: acc2
    seen = []

    def handler(req):
        seen.append(req.headers.get("chatgpt-account-id"))
        chatgpt.use("acc1")  # another chat switched the account while the request was running
        return httpx.Response(200, headers={"x-codex-primary-used-percent": "40", "x-codex-primary-window-minutes": "300"},
                              content=sse({"type": "response.output_text.delta", "delta": "ok"}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    accs = chatgpt._store()["accounts"]
    assert seen == ["acc2"] and accs["acc2"]["limits"]["primary"]["used_percent"] == 40
    assert not accs["acc1"].get("limits")  # the limit was not written to the wrong account
    # the selected account does not change when an expired key is refreshed
    chatgpt.save_auth({"access_token": tokens()["access_token"]}, key="acc2")
    assert chatgpt._store()["active"] == "acc1"


def test_stream_cut_mid_tool_call_is_an_error_not_an_empty_call():
    """A tool call whose arguments never arrived must not run with empty args if the stream is cut before its end."""
    chatgpt.save_auth(tokens())

    def handler(req):
        return httpx.Response(200, content=sse(
            {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc1", "call_id": "c1",
                                                             "name": "delete_file", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": "{\"pa"}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as ei:
        p.chat("gpt-5.5", [{"role": "user", "content": "x"}], tools=[
            {"type": "function", "function": {"name": "delete_file", "parameters": {}}}])
    assert ei.value.kind == "network"


def test_concurrent_limit_write_does_not_lose_refreshed_token(monkeypatch):
    """If a stream's save_limits and a key refresh save at the same time, neither should overwrite the other."""
    chatgpt.save_auth(tokens())
    real_write = chatgpt._write

    def slow_write(store):
        time.sleep(0.15)  # widen the read-modify-write window
        real_write(store)

    monkeypatch.setattr(chatgpt, "_write", slow_write)
    t = threading.Thread(target=chatgpt.save_limits, args=({"primary": {"used_percent": 40.0}},))
    t.start()
    time.sleep(0.05)
    chatgpt.save_auth(tokens(refresh="r2"), key="acc1")
    t.join()
    acc = chatgpt._store()["accounts"]["acc1"]
    assert acc["refresh_token"] == "r2" and acc["limits"]["primary"]["used_percent"] == 40.0


def test_expired_token_is_refreshed_once_by_concurrent_requests():
    """refresh_token is single-use: two requests whose token expires at the same time must do one refresh."""
    chatgpt.save_auth(tokens(exp_in=-10))
    posts = []

    def handler(req):
        posts.append(json.loads(req.content)["refresh_token"])
        time.sleep(0.2)
        if len(posts) > 1:
            return httpx.Response(400, json={"error": "refresh_token_reused"})
        return httpx.Response(200, json=tokens(refresh="r2"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out, errs = [], []

    def run():
        try:
            out.append(chatgpt.access(client))
        except ProviderError as e:
            errs.append(e)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert posts == ["r1"] and not errs and len(out) == 3
    assert chatgpt.load_auth()["refresh_token"] == "r2"


def test_token_refresh_keeps_models_and_used_up_mark():
    chatgpt.save_auth(tokens())
    chatgpt._update(None, models=["gpt-5.5"])
    chatgpt._mark_exhausted(3600)
    chatgpt.save_auth({"access_token": tokens()["access_token"], "refresh_token": "r2"}, key="acc1")
    acc = chatgpt.load_auth("acc1")
    assert acc["refresh_token"] == "r2" and acc["models"] == ["gpt-5.5"]
    assert acc["limits"]["primary"]["used_percent"] == 100.0
    assert chatgpt._next_account(set()) is None  # the used-up account is still used up after the refresh


def test_context_overflow_in_stream_is_too_large_not_server():
    chatgpt.save_auth(tokens())

    def handler(req):
        return httpx.Response(200, content=sse({"type": "response.failed", "response": {"error": {
            "code": "context_length_exceeded", "message": "Your input exceeds the context window of this model."}}}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as ei:
        p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
    assert ei.value.kind == "too_large"


def test_added_without_call_id_and_done_with_it_gives_one_call():
    chatgpt.save_auth(tokens())

    def handler(req):
        return httpx.Response(200, content=sse(
            {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc1", "name": "f",
                                                             "arguments": ""}},
            {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc1", "call_id": "c1",
                                                            "name": "f", "arguments": "{\"a\": 1}"}}))

    p = ChatGPTProvider(pc(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = p.chat("gpt-5.5", [{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "f"}}])
    assert [(c["id"], c["function"]["arguments"]) for c in r.message["tool_calls"]] == [("c1", "{\"a\": 1}")]


# ------------------------------------------------------------------- real-socket stream timing (first data / silence)
class _SlowSSE:
    """Local HTTP server: sends SSE in chunks; steps are (delay, event) pairs."""

    def __init__(self, steps):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.release = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for delay, ev in steps:
                        if delay and outer.release.wait(delay):
                            return
                        data = sse(ev)
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def _fast_provider(url, monkeypatch):
    import dataclasses

    chatgpt.save_auth(tokens())
    monkeypatch.setattr(ChatGPTProvider, "FIRST_DATA", 0.6)
    monkeypatch.setattr(ChatGPTProvider, "STREAM_READ", 5.0)
    monkeypatch.setattr(ChatGPTProvider, "WATCH_TICK", 0.05)
    return ChatGPTProvider(dataclasses.replace(pc(), base_url=url), client=httpx.Client())


def _delta(t):
    return {"type": "response.output_text.delta", "delta": t}


_DONE = {"type": "response.completed", "response": {"model": "gpt-5.5", "usage": {}}}


def test_long_silence_after_first_data_is_not_cut(monkeypatch):
    """After the first data arrived, a long thinking pause was cut off by the first-data limit: the read timeout
    applied to every gap. Now it is STREAM_READ between chunks."""
    srv = _SlowSSE([(0, {"type": "response.created"}), (0.05, _delta("Mer")), (1.5, _delta("haba")), (0, _DONE)])
    try:
        p = _fast_provider(srv.url, monkeypatch)
        assert p.chat("gpt-5.5", [{"role": "user", "content": "x"}]).text == "Merhaba"
    finally:
        srv.close()


def test_no_first_data_still_times_out_at_first_data_limit(monkeypatch):
    srv = _SlowSSE([(0, {"type": "response.created"}), (10, _delta("geç"))])
    try:
        p = _fast_provider(srv.url, monkeypatch)
        t = time.monotonic()
        with pytest.raises(ProviderError) as ei:
            p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
        assert ei.value.kind == "network" and "first answer" in str(ei.value)
        assert time.monotonic() - t < 3  # did not wait for STREAM_READ (5 s)
    finally:
        srv.close()


def test_cancel_during_silence_stops_promptly(monkeypatch):
    from alice.providers.openai_compat import StreamCancelled

    srv = _SlowSSE([(0, _delta("a")), (10, _delta("b")), (0, _DONE)])
    try:
        p = _fast_provider(srv.url, monkeypatch)
        t = time.monotonic()
        with pytest.raises(StreamCancelled):
            p.chat_stream("gpt-5.5", [{"role": "user", "content": "x"}], lambda d: None,
                          cancel=lambda: time.monotonic() - t > 0.3)
        assert time.monotonic() - t < 3
    finally:
        srv.close()


@pytest.mark.parametrize("case", ["cancel", "first"])
def test_cancel_and_first_data_limit_do_not_wait_for_the_read(monkeypatch, case):
    """On macOS closing the socket does not wake a read blocked in another thread, so cancel and the first-data
    limit waited for STREAM_READ. Simulated here by an abort that wakes nothing."""
    from alice.providers import chatgpt as mod
    from alice.providers.openai_compat import StreamCancelled

    monkeypatch.setattr(mod, "_abort_stream", lambda r: None)
    events = [(0, _delta("a")), (10, _delta("b")), (0, _DONE)] if case == "cancel" else \
        [(0, {"type": "response.created"}), (10, _delta("late"))]
    srv = _SlowSSE(events)
    try:
        p = _fast_provider(srv.url, monkeypatch)
        t = time.monotonic()
        if case == "cancel":
            with pytest.raises(StreamCancelled):
                p.chat_stream("gpt-5.5", [{"role": "user", "content": "x"}], lambda d: None,
                              cancel=lambda: time.monotonic() - t > 0.3)
        else:
            with pytest.raises(ProviderError) as ei:
                p.chat("gpt-5.5", [{"role": "user", "content": "x"}])
            assert ei.value.kind == "network"
        assert time.monotonic() - t < 3
    finally:
        srv.close()
