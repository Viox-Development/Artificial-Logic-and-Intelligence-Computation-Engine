"""ChatGPT hesabıyla giriş: OpenAI'nin Codex'i ChatGPT planına (ücretsiz dahil) bağlayan girişi ALICE'te kullanma.

OpenCode'daki "ChatGPT Plus/Pro ile giriş" ile aynı yol: Codex CLI'ın (açık kaynak, Apache-2.0) OAuth akışı
(PKCE, tarayıcıda giriş, http://localhost:1455/auth/callback'e dönüş) ve Codex'in arka ucu
(chatgpt.com/backend-api/codex/responses, Responses API biçiminde). API anahtarı gerekmez; kullanım ChatGPT planının
Codex limitinden düşer. Limit dolunca router sıradaki modele geçer.

Not: OpenAI bu girişin başka programlardan kullanılmasını açıkça yasaklamıyor ama resmen de onaylamıyor (gri bölge).
Birden çok hesap kaydedilebilir ve elle seçilir; limit dolunca kendiliğinden hesap değiştirilmez (limiti aşmak için
hesap değiştirmek OpenAI kurallarına aykırı).

Giriş bilgileri ~/.alice/chatgpt-auth.json'da (yalnız kullanıcı okuyabilir); erişim anahtarı süresi dolunca yenilenir.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from alice.config import ProviderConfig, alice_home
from alice.providers.openai_compat import (_TRANSPORT_ERRORS, ChatResult, OpenAICompatProvider, ProviderError,
                                           StreamCancelled, _transport_error, classify_error, normalize_message)

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Codex CLI'ın herkese açık OAuth istemcisi
PORT = 1455  # Codex'in kayıtlı dönüş adresi bu porta bağlı
REDIRECT = f"http://localhost:{PORT}/auth/callback"
SCOPE = "openid profile email offline_access"
# Codex arka ucu bazı modellerde talimat olarak kendi istemini istiyor; o zaman bu (açık kaynak) metin talimata,
# ALICE'in sistem istemi konuşmanın başına geliştirici mesajı olarak konur.
CODEX_PROMPT_URLS = (  # dosya depoda yer değiştirebiliyor: sırayla denenir
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/protocol/src/prompts/base_instructions/default.md",
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/gpt_5_codex_prompt.md",
)
CLIENT_VERSION = "0.99.0"
REFRESH_EARLY = 300  # sn: süresi bitmeden bu kadar önce yenile


# ------------------------------------------------------------------------------------------- giriş bilgisi
def auth_path() -> Path:
    return alice_home() / "chatgpt-auth.json"


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _store() -> dict[str, Any]:
    """{"active": anahtar, "accounts": {anahtar: giriş bilgisi}}. Eski tek hesaplı dosya da okunur."""
    try:
        data = json.loads(auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": "", "accounts": {}}
    if not isinstance(data, dict):
        return {"active": "", "accounts": {}}
    if "accounts" not in data:  # eski biçim: tek hesap
        key = data.get("account_id") or data.get("email") or "hesap"
        return {"active": key, "accounts": {key: data}} if data.get("access_token") else {"active": "", "accounts": {}}
    return {"active": str(data.get("active") or ""), "accounts": dict(data.get("accounts") or {})}


def _write(store: dict[str, Any]) -> None:
    p = auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_auth() -> dict[str, Any]:
    """Kullanılan (seçili) hesabın giriş bilgisi."""
    st = _store()
    return dict(st["accounts"].get(st["active"]) or {})


def save_auth(tokens: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    """Giriş ya da yenilemeden gelen anahtarları kaydet. key yoksa (yeni giriş) hesap anahtarlardan bulunur ve seçili
    hesap olur; aynı hesaba tekrar girilirse üzerine yazılır."""
    claims = _jwt_claims(tokens.get("id_token") or "")
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    access = tokens.get("access_token") or ""
    st = _store()
    if key is None:
        key = auth_claims.get("chatgpt_account_id") or claims.get("email") or f"hesap{len(st['accounts']) + 1}"
    prev = st["accounts"].get(key) or {}
    data = {
        "access_token": access,
        "refresh_token": tokens.get("refresh_token") or prev.get("refresh_token", ""),
        "id_token": tokens.get("id_token") or prev.get("id_token", ""),
        "account_id": auth_claims.get("chatgpt_account_id") or prev.get("account_id", ""),
        "plan": auth_claims.get("chatgpt_plan_type") or prev.get("plan", ""),
        "email": claims.get("email") or prev.get("email", ""),
        "expires_at": float(_jwt_claims(access).get("exp") or time.time() + 3600),
    }
    st["accounts"][key] = data
    st["active"] = key
    _write(st)
    return data


def accounts() -> list[dict[str, Any]]:
    st = _store()
    return [{"key": k, "email": a.get("email", ""), "plan": a.get("plan", ""), "active": k == st["active"]}
            for k, a in st["accounts"].items()]


def use(key: str) -> bool:
    """Kayıtlı başka bir hesaba geç (elle)."""
    st = _store()
    if key not in st["accounts"]:
        return False
    st["active"] = key
    _write(st)
    return True


def remove(key: str | None = None) -> bool:
    """Hesabı kaldır (varsayılan: seçili olan); seçili kaldırılırsa kalan ilk hesap seçilir."""
    st = _store()
    key = key or st["active"]
    if key not in st["accounts"]:
        return False
    del st["accounts"][key]
    if st["active"] == key:
        st["active"] = next(iter(st["accounts"]), "")
    if st["accounts"]:
        _write(st)
    else:
        try:
            auth_path().unlink()
        except OSError:
            pass
    return True


def logged_in() -> bool:
    a = load_auth()
    return bool(a.get("refresh_token") or a.get("access_token"))


def logout() -> bool:
    return remove()


def status() -> dict[str, Any]:
    a = load_auth()
    return {"logged_in": logged_in(), "email": a.get("email", ""), "plan": a.get("plan", ""), "accounts": accounts()}


def refresh(client: httpx.Client | None = None) -> dict[str, Any]:
    a = load_auth()
    if not a.get("refresh_token"):
        raise ProviderError("auth", "ChatGPT'ye giriş yapılmamış (Ayarlar → Kurulum → ChatGPT ile giriş)")
    c = client or httpx.Client(timeout=30.0)
    try:
        r = c.post(f"{ISSUER}/oauth/token", json={"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                                   "refresh_token": a["refresh_token"], "scope": "openid profile email"})
    except _TRANSPORT_ERRORS as e:
        raise _transport_error(e) from e
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        kind = "auth" if r.status_code in (400, 401, 403) else "server"
        raise ProviderError(kind, f"ChatGPT oturumu yenilenemedi ({r.status_code}); tekrar giriş yap", r.status_code)
    return save_auth(r.json(), key=_store()["active"])


def access(client: httpx.Client | None = None) -> dict[str, Any]:
    a = load_auth()
    if not a.get("access_token") and not a.get("refresh_token"):
        raise ProviderError("auth", "ChatGPT'ye giriş yapılmamış (Ayarlar → Kurulum → ChatGPT ile giriş)")
    if float(a.get("expires_at") or 0) - REFRESH_EARLY < time.time():
        a = refresh(client)
    return a


# ------------------------------------------------------------------------------------------- tarayıcıyla giriş
def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    return f"{ISSUER}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true", "state": state, "originator": "codex_cli_rs"})


def exchange(code: str, verifier: str, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client(timeout=30.0)
    try:
        r = c.post(f"{ISSUER}/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": REDIRECT, "client_id": CLIENT_ID,
                                                   "code_verifier": verifier})
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        raise ProviderError("auth", f"giriş tamamlanamadı ({r.status_code}): {r.text[:200]}", r.status_code)
    return save_auth(r.json())


DONE_PAGE = ("<!doctype html><meta charset=utf-8><title>ALICE</title><body style='font:16px system-ui;"
             "background:#2b2a27;color:#eee;display:grid;place-items:center;height:90vh'><div>{msg}</div>")


def login(open_url: Callable[[str], Any] | None = None, timeout: float = 300.0,
          emit: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    """Tarayıcıda ChatGPT girişini aç, dönüşü localhost:1455'te bekle, anahtarları kaydet."""
    import webbrowser

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server adı
            q = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != state:
                got["error"] = "güvenlik kontrolü (state) tutmadı"
            elif q.get("error"):
                got["error"] = q.get("error_description", q["error"])[0]
            else:
                got["code"] = q.get("code", [""])[0]
            msg = "Giriş tamam, bu sekmeyi kapatabilirsin." if got.get("code") else f"Giriş olmadı: {got.get('error')}"
            body = DONE_PAGE.format(msg=msg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *a: Any) -> None:
            pass

    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        raise ProviderError("network", f"localhost:{PORT} açılamadı (Codex CLI girişi açık olabilir): {e}") from e
    threading.Thread(target=server.serve_forever, daemon=True, name="alice-chatgpt-login").start()
    try:
        url = authorize_url(challenge, state)
        emit("tarayıcıda ChatGPT girişi açıldı")
        (open_url or webbrowser.open)(url)
        if not done.wait(timeout):
            raise ProviderError("auth", "giriş zaman aşımına uğradı (5 dk)")
    finally:
        server.shutdown()
        server.server_close()
    if not got.get("code"):
        raise ProviderError("auth", f"giriş olmadı: {got.get('error', 'kod gelmedi')}")
    return exchange(got["code"], verifier)


# ------------------------------------------------------------------------------------------- istek biçimi
def to_responses(messages: list[dict[str, Any]], codex_prompt: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Chat Completions mesajları -> (instructions, Responses API input)."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str))
    items: list[dict[str, Any]] = []
    if codex_prompt is not None and system:
        items.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": system}]})
    for m in messages:
        role = m.get("role")
        c = m.get("content")
        if role == "system":
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id") or "",
                          "output": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)})
            continue
        parts: list[dict[str, Any]] = []
        kind = "output_text" if role == "assistant" else "input_text"
        if isinstance(c, str) and c:
            parts.append({"type": kind, "text": c})
        elif isinstance(c, list):
            for p in c:
                if p.get("type") == "text" and p.get("text"):
                    parts.append({"type": kind, "text": p["text"]})
                elif p.get("type") == "image_url" and role != "assistant":
                    url = p["image_url"]["url"] if isinstance(p.get("image_url"), dict) else p.get("image_url")
                    parts.append({"type": "input_image", "image_url": url})
        if parts:
            items.append({"type": "message", "role": "assistant" if role == "assistant" else "user", "content": parts})
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            items.append({"type": "function_call", "call_id": tc.get("id") or "", "name": fn.get("name") or "",
                          "arguments": fn.get("arguments") or "{}"})
    return (codex_prompt if codex_prompt is not None else system), items


def to_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function") or {}
        out.append({"type": "function", "name": fn.get("name"), "description": fn.get("description") or "",
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}}, "strict": False})
    return out


_codex_prompt: str | None = None


def codex_prompt(client: httpx.Client) -> str:
    """Codex'in kendi istemi (bir kez indirilir, ~/.alice'te saklanır)."""
    global _codex_prompt
    if _codex_prompt:
        return _codex_prompt
    p = alice_home() / "codex-prompt.md"
    try:
        _codex_prompt = p.read_text(encoding="utf-8")
        return _codex_prompt
    except OSError:
        pass
    for url in CODEX_PROMPT_URLS:
        try:
            r = client.get(url)
        except _TRANSPORT_ERRORS:
            continue
        if r.status_code == 200 and r.text.strip():
            _codex_prompt = r.text
            break
    else:
        raise ProviderError("bad_request", "Codex istemi indirilemedi; bu model ALICE'in talimatını kabul etmedi")
    try:
        p.write_text(r.text, encoding="utf-8")
    except OSError:
        pass
    return _codex_prompt


class ChatGPTProvider(OpenAICompatProvider):
    """Codex arka ucu üzerinden ChatGPT modelleri. Router için OpenAICompatProvider ile aynı arayüz."""

    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: httpx.Client | None = None):
        super().__init__(cfg, timeout, client)
        self.session = str(uuid.uuid4())
        self.use_codex_prompt = False  # arka uç kendi talimatımızı reddederse True olur

    def _auth_headers(self) -> dict[str, str]:
        a = access(self._client)
        h = {"Authorization": f"Bearer {a['access_token']}", "OpenAI-Beta": "responses=experimental",
             "originator": "codex_cli_rs", "session_id": self.session, "Accept": "text/event-stream",
             "Content-Type": "application/json"}
        if a.get("account_id"):
            h["chatgpt-account-id"] = a["account_id"]
        return h

    def list_models(self) -> list[dict[str, Any]]:
        if self.cfg.models:
            return [{"id": m} for m in self.cfg.models]
        try:
            r = self._client.get(f"{self.cfg.base_url}/models", params={"client_version": CLIENT_VERSION},
                                 headers=self._auth_headers())
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(e) from e
        if r.status_code != 200:
            raise classify_error(r.status_code, r.text, dict(r.headers))
        data = r.json()
        items = (data.get("models") or data.get("data") or []) if isinstance(data, dict) else data
        out = []
        for m in items if isinstance(items, list) else []:
            slug = m.get("slug") or m.get("id") if isinstance(m, dict) else m
            if slug and (not isinstance(m, dict) or m.get("visibility", "list") != "hide"):
                out.append({"id": str(slug), "context_length": (m.get("context_window") if isinstance(m, dict) else None)})
        return out

    def chat(self, model, messages, tools=None, temperature=None, max_tokens=None, response_format=None, extra=None):
        return self.chat_stream(model, messages, lambda _t: None, tools=tools, extra=extra)

    def chat_stream(self, model, messages, on_delta, tools=None, temperature=None, max_tokens=None,
                    response_format=None, extra=None, cancel=None, on_reasoning=None, on_first=None, on_tool=None):
        try:
            try:
                return self._stream(model, messages, on_delta, tools, extra, cancel, on_reasoning, on_first, on_tool)
            except ProviderError as e:
                if self.use_codex_prompt or e.kind != "bad_request" or "instruction" not in str(e).lower():
                    raise
                self.use_codex_prompt = True  # arka uç kendi istemini istiyor: onunla tekrar dene
                return self._stream(model, messages, on_delta, tools, extra, cancel, on_reasoning, on_first, on_tool)
        except ProviderError as e:
            raise self._scrub(e) from e.__cause__

    def _stream(self, model, messages, on_delta, tools, extra, cancel, on_reasoning, on_first, on_tool) -> ChatResult:
        instructions, items = to_responses(messages, codex_prompt(self._client) if self.use_codex_prompt else None)
        body: dict[str, Any] = {"model": model, "instructions": instructions, "input": items, "tools": to_tools(tools),
                                "tool_choice": "auto", "parallel_tool_calls": bool(tools), "store": False,
                                "stream": True, "prompt_cache_key": self.session}
        effort = (extra or {}).get("reasoning_effort") or ((extra or {}).get("reasoning") or {}).get("effort")
        if effort:
            body["reasoning"] = {"effort": effort, "summary": "auto"}
        t0 = time.monotonic()
        text: list[str] = []
        calls: dict[str, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        served = model
        got_data = False
        try:
            with self._client.stream("POST", f"{self.cfg.base_url}/responses", headers=self._auth_headers(),
                                     json=body, timeout=httpx.Timeout(self.FIRST_DATA, connect=15.0)) as r:
                if r.status_code != 200:
                    r.read()
                    raise classify_error(r.status_code, r.text, dict(r.headers))
                for line in r.iter_lines():
                    if cancel and cancel():
                        raise StreamCancelled()
                    if not got_data and time.monotonic() - t0 > self.FIRST_DATA:
                        raise ProviderError("network", f"zaman aşımı: model {int(self.FIRST_DATA)} sn'dir ilk "
                                                       "cevabı vermedi")
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    kind = ev.get("type", "")
                    if kind in ("response.output_text.delta", "response.reasoning_summary_text.delta",
                                "response.output_item.added", "response.function_call_arguments.delta"):
                        if not got_data and on_first is not None:
                            on_first()
                        got_data = True
                    if kind == "response.output_text.delta" and ev.get("delta"):
                        text.append(ev["delta"])
                        on_delta(ev["delta"])
                    elif kind == "response.reasoning_summary_text.delta" and on_reasoning and ev.get("delta"):
                        on_reasoning(ev["delta"])
                    elif kind in ("response.output_item.added", "response.output_item.done"):
                        item = ev.get("item") or {}
                        if item.get("type") == "function_call":
                            cid = item.get("call_id") or item.get("id") or f"call_{len(calls)}"
                            calls[cid] = {"id": cid, "type": "function", "function": {
                                "name": item.get("name") or "", "arguments": item.get("arguments") or ""}}
                            if on_tool is not None:
                                on_tool(item.get("name") or "", len(item.get("arguments") or ""))
                    elif kind == "response.completed":
                        resp = ev.get("response") or {}
                        served = resp.get("model") or served
                        u = resp.get("usage") or {}
                        usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)}
                    elif kind in ("response.failed", "error"):
                        err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        code = str(err.get("code") or "") if isinstance(err, dict) else ""
                        if "usage_limit" in code or "rate_limit" in code:
                            raise ProviderError("rate_limit", f"ChatGPT limiti doldu: {msg}", 429)
                        raise ProviderError("server", f"ChatGPT: {msg}")
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(e) from e
        msg: dict[str, Any] = {"content": "".join(text)}
        if calls:
            msg["tool_calls"] = list(calls.values())
        if not msg["content"] and not calls:
            raise ProviderError("server", "ChatGPT boş cevap döndü")
        return ChatResult(normalize_message(msg), self.name, served, usage, time.monotonic() - t0)


def make_client(pc: ProviderConfig, timeout: float = 180.0) -> OpenAICompatProvider:
    """Sağlayıcının istemcisi: ChatGPT girişiyse Codex arka ucu, değilse OpenAI uyumlu API."""
    from alice.providers import openai_compat  # modül üzerinden: testler sahte istemci koyabilsin

    if pc.auth == "chatgpt":
        return ChatGPTProvider(pc, timeout)
    return openai_compat.OpenAICompatProvider(pc, timeout)
