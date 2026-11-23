"""ChatGPT account sign-in: using the sign-in that connects OpenAI's Codex to a ChatGPT plan (including free) in ALICE.

Same path as "Sign in with ChatGPT Plus/Pro" in OpenCode: the Codex CLI's (open source, Apache-2.0) OAuth flow
(PKCE, browser sign-in, return to http://localhost:1455/auth/callback) and Codex's backend
(chatgpt.com/backend-api/codex/responses, in Responses API format). No API key is needed; usage counts against the
ChatGPT plan's Codex limit. When the limit is reached, the router moves on to the next model in the queue.

Note: OpenAI does not explicitly forbid using this sign-in from other programs, but does not officially approve it
either (gray area). Multiple accounts can be saved and picked by hand; when a limit is reached the account is not
switched automatically (switching accounts to get around a limit goes against OpenAI's rules).

Login data is stored in ~/.alice/chatgpt-auth.json (readable by the user only); the access key is refreshed when it expires.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import queue
import secrets
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from alice.config import ProviderConfig, alice_home
from alice.providers.openai_compat import (_TOO_LARGE_HINTS, _TRANSPORT_ERRORS, ChatResult, OpenAICompatProvider,
                                           ProviderError, StreamCancelled, _transport_error, classify_error,
                                           normalize_message, redact_binary)

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Codex CLI's public OAuth client
PORT = 1455  # Codex's registered redirect address is tied to this port
REDIRECT = f"http://localhost:{PORT}/auth/callback"
SCOPE = "openid profile email offline_access"
# For some models the Codex backend wants its own prompt as instructions; in that case this (open source) text is used
# as the instructions, and ALICE's system prompt is put at the start of the conversation as a developer message.
CODEX_PROMPT_URLS = (  # the file can move within the repo: tried in order
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/protocol/src/prompts/base_instructions/default.md",
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/gpt_5_codex_prompt.md",
)
CLIENT_VERSION = "0.99.0"
REFRESH_EARLY = 300  # sec: refresh this long before it expires


# ------------------------------------------------------------------------------------------- login data
def auth_path() -> Path:
    return alice_home() / "chatgpt-auth.json"


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


# Read-modify-write steps on the account file run one at a time: several chats can write limits and refresh the key at once.
# Without a lock, one stream's save_limits overwrote the new (single-use) refresh_token saved by a refresh with an old
# copy; the next refresh was rejected and the account was logged out.
_store_lock = threading.RLock()
# An expired key for the same account is refreshed only once at a time: refresh_token is single-use, and a second
# refresh sent the same (now invalid) key and got the error "oturum yenilenemedi" (session could not be renewed).
_refresh_lock = threading.RLock()


def _locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    """A function that reads, modifies and writes the account file runs under _store_lock."""
    @functools.wraps(fn)
    def wrapper(*a: Any, **kw: Any) -> Any:
        with _store_lock:
            return fn(*a, **kw)
    return wrapper


def _store() -> dict[str, Any]:
    """{"active": key, "accounts": {key: login data}}. An old single-account file is also read."""
    try:
        with _store_lock:
            data = json.loads(auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": "", "accounts": {}}
    if not isinstance(data, dict):
        return {"active": "", "accounts": {}}
    if "accounts" not in data:  # old format: single account
        key = data.get("account_id") or data.get("email") or "hesap"
        return {"active": key, "accounts": {key: data}} if data.get("access_token") else {"active": "", "accounts": {}}
    return {"active": str(data.get("active") or ""), "accounts": dict(data.get("accounts") or {})}


def _write(store: dict[str, Any]) -> None:
    p = auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file, then replace it: so another process reading a half-written file doesn't see an empty
    # account list, save it, and wipe out all accounts.
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(store), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        os.replace(tmp, p)
    except OSError:  # Windows: the target can't be replaced if another process has it open right now
        p.write_text(json.dumps(store), encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def load_auth(key: str | None = None) -> dict[str, Any]:
    """Login data of the account in use (selected); if key is given, that account's."""
    st = _store()
    return dict(st["accounts"].get(key or st["active"]) or {})


@_locked
def save_auth(tokens: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    """Save keys from a login or refresh. If key is missing (new login), the account is found from the keys
    and becomes the selected account; signing in to the same account again overwrites it."""
    claims = _jwt_claims(tokens.get("id_token") or "")
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    access = tokens.get("access_token") or ""
    st = _store()
    select = key is None  # new login becomes the selected account; refreshing an existing one keeps the selection
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
    # A refresh keeps the account's other fields (model list, limits, "dolu" = full mark). Previously every hourly
    # refresh wiped them, so a full account looked empty and the model menu fell back to "okunuyor" (loading) again.
    st["accounts"][key] = data if select else {**prev, **data}
    if select or not st["active"]:
        st["active"] = key
    _write(st)
    return data


def accounts() -> list[dict[str, Any]]:
    st = _store()
    return [{"key": k, "email": a.get("email", ""), "plan": a.get("plan", ""), "active": k == st["active"]}
            for k, a in st["accounts"].items()]


@_locked
def use(key: str) -> bool:
    """Switch manually to another saved account."""
    st = _store()
    if key not in st["accounts"]:
        return False
    st["active"] = key
    _write(st)
    return True


@_locked
def remove(key: str | None = None) -> bool:
    """Remove an account (default: the selected one); if the selected one is removed, the first remaining one is selected."""
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


@_locked
def _update(key: str | None, **fields: Any) -> None:
    """Update the fields of the account with this key (None: the selected account)."""
    st = _store()
    acc = st["accounts"].get(key or st["active"])
    if acc is None:
        return
    acc.update(fields)
    _write(st)


def _update_active(**fields: Any) -> None:
    _update(None, **fields)


# Account selection and switching are done one at a time: several chats can use one provider at the same time.
_switch_lock = threading.Lock()


_fetching = threading.Lock()
MODELS_RETRY = 300.0  # sec: if the list could not be fetched, retry no sooner than this
LOADING_MAX = 45.0  # sec: if the list has not arrived in this time, show an error instead of "okunuyor" (loading)


def ensure_models(pc: ProviderConfig) -> None:
    """If signed in but there is no model list (signed in on an older version, or the first try never ran),
    read it in the background. The error is saved: the model menu and the Setup card show the reason."""
    a = load_auth()
    if not a or a.get("models") or time.time() - float(a.get("models_tried") or 0) < MODELS_RETRY:
        return
    if not _fetching.acquire(blocking=False):
        return
    _update_active(models_tried=time.time())

    def run() -> None:
        try:
            ChatGPTProvider(pc, timeout=30.0).list_models()
        except Exception as e:  # noqa: BLE001 - show on screen
            _update_active(models_error=str(e)[:300])
        finally:
            _fetching.release()

    threading.Thread(target=run, daemon=True, name="alice-chatgpt-models").start()


def parse_models(data: Any) -> list[dict[str, Any]]:
    items = (data.get("models") or data.get("data") or []) if isinstance(data, dict) else data
    out = []
    for m in items if isinstance(items, list) else []:
        slug = (m.get("slug") or m.get("id")) if isinstance(m, dict) else m
        if slug and (not isinstance(m, dict) or m.get("visibility", "list") != "hide"):
            out.append({"id": str(slug), "context_length": m.get("context_window") if isinstance(m, dict) else None})
    return out


def add_model(name: str) -> list[str]:
    """Usable even when the list cannot be read: add the model name by hand (e.g. gpt-5.5)."""
    name = name.strip()
    models = cached_models()
    if name and name not in models:
        models.append(name)
        _update_active(models=models, models_error="")
    return models


def retry_models(pc: ProviderConfig) -> None:
    _update_active(models_tried=0, models_error="")
    ensure_models(pc)


def models_state() -> str:
    """'ok' | 'loading' | 'error' | '' (not signed in)."""
    a = load_auth()
    if not a:
        return ""
    if a.get("models"):
        return "ok"
    if a.get("models_error"):
        return "error"
    return "loading" if time.time() - float(a.get("models_tried") or 0) < LOADING_MAX else "error"


def models_error() -> str:
    a = load_auth()
    if a.get("models_error"):
        return str(a["models_error"])
    if a and not a.get("models") and a.get("models_tried") and time.time() - float(a["models_tried"]) >= LOADING_MAX:
        return "sunucu cevap vermedi"
    return ""


def cached_models() -> list[str]:
    """Last model list read for the selected account (shown in the model menu even before a probe)."""
    return [str(m) for m in load_auth().get("models") or []]


def _window(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    if used is None:
        return None
    secs = raw.get("limit_window_seconds") or (float(raw.get("window_minutes") or 0) * 60)
    reset = raw.get("reset_after_seconds")
    reset_at = raw.get("reset_at") or (time.time() + float(reset) if reset is not None else None)
    return {"used_percent": round(float(used), 1), "window_minutes": round(float(secs or 0) / 60),
            "reset_at": float(reset_at) if reset_at else None}


def limits_from_headers(headers: Any) -> dict[str, Any] | None:
    """Codex sends the limit status in the headers of every reply (x-codex-primary-used-percent...)."""
    out: dict[str, Any] = {}
    for name in ("primary", "secondary"):
        used = headers.get(f"x-codex-{name}-used-percent")
        if used is None:
            continue
        try:
            out[name] = _window({"used_percent": float(used),
                                 "window_minutes": float(headers.get(f"x-codex-{name}-window-minutes") or 0),
                                 "reset_after_seconds": float(headers.get(f"x-codex-{name}-reset-after-seconds") or 0)})
        except ValueError:
            continue
    return out or None


def limit_note() -> str:
    """Short limit summary for the model menu: '5 sa: %40 · haftalık: %12' (sa = hours, haftalık = weekly)."""
    lim = load_auth().get("limits") or {}
    parts = []
    for key in ("primary", "secondary"):
        w = lim.get(key)
        if not w:
            continue
        mins = int(w.get("window_minutes") or 0)
        name = "haftalık" if mins >= 6 * 24 * 60 else f"{round(mins / 60)} sa" if mins >= 60 else f"{mins} dk"
        parts.append(f"{name}: %{round(w.get('used_percent') or 0)}")
    return " · ".join(parts)


def save_limits(limits: dict[str, Any], key: str | None = None) -> None:
    _update(key, limits={**limits, "updated": time.time()})


def _used(acc: dict[str, Any], now: float) -> float:
    """The account's fullest limit window (%); a window whose reset time has passed counts as empty."""
    worst = 0.0
    for w in ((acc.get("limits") or {}).get(k) for k in ("primary", "secondary")):
        if isinstance(w, dict) and float(w.get("reset_at") or 0) > now:
            worst = max(worst, float(w.get("used_percent") or 0))
    return worst


@_locked
def _mark_exhausted(reset_in: float | None = None, key: str | None = None) -> None:
    """Mark an account whose usage limit is reached (key; None: selected) as full: it is skipped on the next pick.
    reset_in: the reset time (sec) the server reported; if unknown, the saved window's, and if that is missing too, 1 hour."""
    a = load_auth(key)
    lim = dict(a.get("limits") or {})
    w = dict(lim.get("primary") or {})
    w["used_percent"] = 100.0
    if reset_in:
        w["reset_at"] = time.time() + float(reset_in)
    elif float(w.get("reset_at") or 0) <= time.time():
        w["reset_at"] = time.time() + 3600
    lim["primary"] = w
    _update(key, limits={**lim, "updated": time.time()})


def _next_account(tried: set[str]) -> str | None:
    """The least-used account among those not tried yet, not at their limit, and logged in."""
    now = time.time()
    cands = [(_used(a, now), k) for k, a in _store()["accounts"].items()
             if k not in tried and (a.get("refresh_token") or a.get("access_token"))]
    cands = [c for c in cands if c[0] < 100.0]
    return min(cands)[1] if cands else None


def _earliest_reset() -> float | None:
    """Time (sec) until the earliest reset of accounts whose limit is full; None if unknown."""
    now = time.time()
    waits = [float(w["reset_at"]) - now for a in _store()["accounts"].values()
             for w in ((a.get("limits") or {}).get(k) for k in ("primary", "secondary"))
             if isinstance(w, dict) and float(w.get("used_percent") or 0) >= 100 and float(w.get("reset_at") or 0) > now]
    return min(waits) if waits else None


def _limit_error(status: int, body: str, headers: dict[str, str]) -> ProviderError:
    """Codex backend's 429: {"error": {"type": "usage_limit_reached", "resets_in_seconds": ...}}. The generic
    classification reads it as a 20-second rate limit: an account reopening in 15 days was retried every 30 s."""
    err = classify_error(status, body, headers)
    if status != 429:
        return err
    try:
        info = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return err
    if not isinstance(info, dict) or "usage_limit" not in str(info.get("type") or info.get("code") or ""):
        return err
    secs = info.get("resets_in_seconds")
    if secs is None and info.get("resets_at"):
        secs = float(info["resets_at"]) - time.time()
    try:
        secs = max(60.0, float(secs)) if secs is not None else None
    except (TypeError, ValueError):
        secs = None
    return ProviderError("quota", f"ChatGPT kullanım limiti doldu ({info.get('plan_type') or 'plan'})", 429, secs)


def logged_in() -> bool:
    a = load_auth()
    return bool(a.get("refresh_token") or a.get("access_token"))


def logout() -> bool:
    return remove()


def status() -> dict[str, Any]:
    a = load_auth()
    return {"logged_in": logged_in(), "email": a.get("email", ""), "plan": a.get("plan", ""), "accounts": accounts(),
            "models": cached_models(), "models_error": models_error(), "models_state": models_state(),
            "limits": a.get("limits") or {}}


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"  # Limit status read by Codex's /status screen


def fetch_usage(client: httpx.Client | None = None) -> dict[str, Any]:
    """Query the limit status now, without calling a model. Otherwise use what the last response said."""
    c = client or httpx.Client(timeout=20.0)
    try:
        a = access(c)
        h = {"Authorization": f"Bearer {a['access_token']}", "originator": "codex_cli_rs"}
        if a.get("account_id"):
            h["chatgpt-account-id"] = a["account_id"]
        r = c.get(USAGE_URL, headers=h)
    except _TRANSPORT_ERRORS as e:
        raise _transport_error(e) from e
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        raise classify_error(r.status_code, r.text, dict(r.headers))
    data = r.json() if r.content else {}
    rl = (data.get("rate_limit") or data) if isinstance(data, dict) else {}
    limits = {k: w for k, w in (("primary", _window(rl.get("primary_window") or rl.get("primary"))),
                                ("secondary", _window(rl.get("secondary_window") or rl.get("secondary")))) if w}
    if isinstance(data, dict) and data.get("plan_type"):
        _update_active(plan=data["plan_type"])
    if limits:
        save_limits(limits)
    return limits


def refresh(client: httpx.Client | None = None, key: str | None = None) -> dict[str, Any]:
    key = key or _store()["active"]
    a = load_auth(key)
    if not a.get("refresh_token"):
        raise ProviderError("auth", "ChatGPT'ye giriş yapılmamış (Sistem → Kurulum → ChatGPT ile giriş)")
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
    return save_auth(r.json(), key=key)


def access(client: httpx.Client | None = None, key: str | None = None) -> dict[str, Any]:
    a = load_auth(key)
    if not a.get("access_token") and not a.get("refresh_token"):
        raise ProviderError("auth", "ChatGPT'ye giriş yapılmamış (Sistem → Kurulum → ChatGPT ile giriş)")
    if float(a.get("expires_at") or 0) - REFRESH_EARLY < time.time():
        key = key or _store()["active"]
        with _refresh_lock:
            a = load_auth(key)  # another request may have refreshed while we waited: don't spend the same refresh_token again
            if float(a.get("expires_at") or 0) - REFRESH_EARLY < time.time():
                a = refresh(client, key)
    return a


# ------------------------------------------------------------------------------------------- browser sign-in
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
    """Open the ChatGPT sign-in in the browser, wait for the return on localhost:1455, and save the keys (tokens)."""
    import webbrowser

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name from http.server
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


# ------------------------------------------------------------------------------------------- request format
def _system_text(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p["text"] for p in c if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                         and isinstance(p.get("text"), str) and p["text"])
    return ""


def to_responses(messages:list[dict[str, Any]], codex_prompt: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Chat Completions messages -> (instructions, Responses API input)."""
    # Part-list system messages also go into instructions (before: only plain text was read, lists dropped).
    system = "\n\n".join(t for t in (_system_text(m.get("content")) for m in messages if m.get("role") == "system") if t)
    items: list[dict[str, Any]] = []
    if codex_prompt is not None and system:
        items.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": system}]})
    images: list[dict[str, Any]] = []  # images from tool results, sent as a user message after the tool outputs

    def flush_images() -> None:
        if images:
            items.append({"type": "message", "role": "user", "content": list(images)})
            images.clear()

    for m in messages:
        role = m.get("role")
        c = m.get("content")
        if role == "tool":
            output, imgs = _tool_output(c)
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id") or "", "output": output})
            images.extend(imgs)
            continue
        flush_images()
        if role == "system":
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
    flush_images()
    return (codex_prompt if codex_prompt is not None else system), items


def _tool_output(c: Any) -> tuple[str, list[dict[str, Any]]]:
    """Tool result -> (function_call_output text, input_image parts to send separately). The part list used to be
    dumped into JSON text: the screenshot's base64 was going to the model as plain text."""
    if isinstance(c, str):
        return c, []
    if not isinstance(c, list):
        return redact_binary(c), []
    texts: list[str] = []
    imgs: list[dict[str, Any]] = []
    for p in c:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            texts.append(p["text"])
        elif p.get("type") == "image_url":
            url = p["image_url"].get("url") if isinstance(p.get("image_url"), dict) else p.get("image_url")
            if url:
                imgs.append({"type": "input_image", "image_url": url})
        elif p.get("type") == "image" and isinstance(p.get("data"), str):  # MCP format
            mime = p.get("mimeType") or "image/png"
            imgs.append({"type": "input_image", "image_url": f"data:{mime};base64,{p['data']}"})
    if not texts and not imgs:
        return redact_binary(c), []
    if imgs:
        texts.append(f"[araç {len(imgs)} görüntü döndürdü; görüntüler sonraki mesajda]")
    return "\n".join(texts), imgs


def text_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    """Chat Completions response_format -> Responses API text.format."""
    if not isinstance(response_format, dict):
        return None
    kind = response_format.get("type")
    if kind == "json_schema":
        js = response_format.get("json_schema") or {}
        if not isinstance(js.get("schema"), dict):
            return None
        out: dict[str, Any] = {"type": "json_schema", "name": js.get("name") or "output", "schema": js["schema"]}
        if "strict" in js:
            out["strict"] = bool(js["strict"])
        return out
    if kind == "json_object":
        return {"type": "json_object"}
    return None


def to_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function") or {}
        out.append({"type": "function", "name": fn.get("name"), "description": fn.get("description") or "",
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}}, "strict": False})
    return out


_codex_prompt: str | None = None


def codex_prompt(client: httpx.Client) -> str:
    """Codex's own prompt (downloaded once and kept in ~/.alice)."""
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
    """ChatGPT models through the Codex backend. Same interface as OpenAICompatProvider, for the router."""

    # The first data normally arrives in 1-4 s (a reasoning summary counts as data). A single request that hung
    # for 90 s had held a task for ~280 s through the router's retries.
    FIRST_DATA = 30.0
    # Max silence between chunks after the first data: during long reasoning the model can go minutes without
    # sending a summary; applying the 30 s first-data limit to every gap cut healthy answers short.
    STREAM_READ = 300.0
    WATCH_TICK = 0.5  # watchdog: first-data time and cancellation are checked at this interval

    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: httpx.Client | None = None):
        super().__init__(cfg, timeout, client)
        self.session = str(uuid.uuid4())
        self.use_codex_prompt = False  # set to True if the backend rejects our own instructions

    def _auth_headers(self, key: str | None = None) -> dict[str, str]:
        a = access(self._client, key)
        h = {"Authorization": f"Bearer {a['access_token']}", "OpenAI-Beta": "responses=experimental",
             "originator": "codex_cli_rs", "session_id": self.session, "Accept": "text/event-stream",
             "Content-Type": "application/json"}
        if a.get("account_id"):
            h["chatgpt-account-id"] = a["account_id"]
        return h

    def list_models(self) -> list[dict[str, Any]]:
        if self.cfg.models:
            return [{"id": m} for m in self.cfg.models]
        seen = ""
        # The server may filter the list by client version: ours first, then a newer one, then no version.
        for params in ({"client_version": CLIENT_VERSION}, {"client_version": "9.99.0"}, {}):
            try:
                r = self._client.get(f"{self.cfg.base_url}/models", params=params, headers=self._auth_headers())
            except _TRANSPORT_ERRORS as e:
                raise _transport_error(e) from e
            if r.status_code != 200:
                raise classify_error(r.status_code, r.text, dict(r.headers))
            try:
                data = r.json()
            except ValueError as e:
                raise ProviderError("server", f"model listesi okunamadı: {r.text[:160]}", 200) from e
            out = parse_models(data)
            if out:
                _update_active(models=[m["id"] for m in out], models_error="")
                return out
            seen = r.text[:200]
        raise ProviderError("server", f"model listesi boş geldi (sunucu cevabı: {seen or 'boş'})", 200)

    def chat(self, model, messages, tools=None, temperature=None, max_tokens=None, response_format=None, extra=None):
        # max_tokens/temperature are deliberately not sent: the Codex backend rejects max_output_tokens and
        # temperature with "Unsupported parameter" (Codex CLI doesn't send them either). response_format becomes text.format.
        return self.chat_stream(model, messages, lambda _t: None, tools=tools, max_tokens=max_tokens,
                                response_format=response_format, extra=extra)

    def chat_stream(self, model, messages, on_delta, tools=None, temperature=None, max_tokens=None,
                    response_format=None, extra=None, cancel=None, on_reasoning=None, on_first=None, on_tool=None,
                    on_call=None):
        """If the selected account hits its limit or its session drops, switch to the other saved accounts in turn."""
        tried: set[str] = set()
        full = 0  # number of accounts dropped for an exhausted usage limit (temporary rate limits don't count)
        with _switch_lock:
            st = _store()
            key = st["active"]  # this request's account, kept even if another chat switches the selected one
            if _used(st["accounts"].get(key) or {}, time.time()) >= 100.0:
                # The selected account is already known to be full: switch to a free one without a wasted request.
                free = _next_account({key})
                if free is not None:
                    tried.add(key)
                    full += 1
                    use(free)
                    key = free
        while True:
            tried.add(key)
            started = [False]  # no account switch once data arrives: a partial answer is already on screen
            try:
                return self._chat_one(model, messages, on_delta, tools, extra, cancel, on_reasoning, on_first, on_tool,
                                      started, key, on_call=on_call, response_format=response_format)
            except ProviderError as e:
                if e.kind not in ("rate_limit", "quota", "auth") or started[0]:
                    raise
                # A usage limit (quota) blocks an account for a long time; a temporary rate limit (429) doesn't:
                # only this request tries another account, and the account isn't marked as full.
                used_up = e.kind == "quota" or getattr(e, "usage_limit", False)
                with _switch_lock:
                    if used_up:
                        full += 1
                        _mark_exhausted(e.retry_after if e.kind == "quota" else None, key)
                    nxt = _next_account(tried)
                    if nxt is not None and _store()["active"] == key:
                        use(nxt)  # switch if the selected account still failed; skip if another chat changed it
                if nxt is not None:
                    key = nxt
                    continue
                if full < 2 or full < len(tried):
                    raise  # single account, sign-in error or temporary rate limit: pass the error to the router as is
                # Every account is at its usage limit: let the router rest this model until the earliest reset
                # (at most half an hour; accounts may be added in the meantime).
                wait = min(_earliest_reset() or 1800.0, 1800.0)
                raise ProviderError("quota", f"ChatGPT: kayıtlı {len(tried)} hesabın hepsinin limiti dolu", 429,
                                    max(wait, 60.0)) from e

    def _chat_one(self, model, messages, on_delta, tools, extra, cancel, on_reasoning, on_first, on_tool,
                  started: list[bool] | None = None, key: str | None = None, on_call=None, response_format=None):
        started = started if started is not None else [False]

        def first() -> None:
            started[0] = True
            if on_first is not None:
                on_first()

        def delta(t: str) -> None:
            started[0] = True
            on_delta(t)

        try:
            try:
                return self._stream(model, messages, delta, tools, extra, cancel, on_reasoning, first, on_tool, key,
                                    on_call=on_call, response_format=response_format)
            except ProviderError as e:
                if self.use_codex_prompt or e.kind != "bad_request" or "instruction" not in str(e).lower():
                    raise
                self.use_codex_prompt = True  # the backend wants its own prompt: retry with it
                return self._stream(model, messages, delta, tools, extra, cancel, on_reasoning, first, on_tool, key,
                                    on_call=on_call, response_format=response_format)
        except ProviderError as e:
            raise self._scrub(e) from e.__cause__

    def _stream(self, model, messages, on_delta, tools, extra, cancel, on_reasoning, on_first, on_tool,
                key: str | None = None, on_call=None, response_format=None) -> ChatResult:
        instructions, items = to_responses(messages, codex_prompt(self._client) if self.use_codex_prompt else None)
        body: dict[str, Any] = {"model": model, "instructions": instructions, "input": items, "tools": to_tools(tools),
                                "tool_choice": "auto", "parallel_tool_calls": bool(tools), "store": False,
                                "stream": True, "prompt_cache_key": self.session}
        effort = (extra or {}).get("reasoning_effort") or ((extra or {}).get("reasoning") or {}).get("effort")
        if effort:
            body["reasoning"] = {"effort": effort, "summary": "auto"}
        fmt = text_format(response_format)
        if fmt:
            body["text"] = {"format": fmt}
            if fmt["type"] == "json_object" and "json" not in json.dumps(body["input"], ensure_ascii=False).lower() \
                    and "json" not in str(instructions).lower():
                # Responses API json_object requires "JSON" in the input, otherwise the request returns 400.
                body["input"] = [*items, {"type": "message", "role": "developer",
                                          "content": [{"type": "input_text", "text": "Cevabı JSON olarak ver."}]}]
        t0 = time.monotonic()
        text: list[str] = []
        calls: dict[str, dict[str, Any]] = {}
        closed: set[str] = set()  # calls whose arguments are complete (output_item.done received)
        usage: dict[str, Any] = {}
        served = model
        finish = ""
        got_data = False
        completed = False
        aborted = [""]  # why the watchdog stopped the stream, if it did: "first" (no first data) or "cancel"
        stop = threading.Event()

        def first_timeout() -> ProviderError:
            return ProviderError("network", f"zaman aşımı: model {int(self.FIRST_DATA)} sn'dir ilk cevabı vermedi")

        def lines(r: httpx.Response) -> Iterator[str]:
            """The stream's lines, read in another thread so the first-data limit and cancel apply even while the
            server is silent. Closing the socket wakes a blocked read on Linux and Windows but not on macOS, so this
            loop does not wait for the read: it gives up at once and leaves the reader to end on its own."""
            q: queue.Queue[Any] = queue.Queue()
            end = object()

            def pump() -> None:
                try:
                    for ln in r.iter_lines():
                        if stop.is_set():
                            return
                        q.put(ln)
                except BaseException as e:  # noqa: BLE001 - handed to the reading thread
                    q.put(e)
                else:
                    q.put(end)

            threading.Thread(target=pump, daemon=True, name="alice-chatgpt-read").start()
            while True:
                try:
                    item = q.get(timeout=self.WATCH_TICK)
                except queue.Empty:
                    if cancel is not None and cancel():
                        aborted[0] = "cancel"
                    elif not got_data and time.monotonic() - t0 > self.FIRST_DATA:
                        aborted[0] = "first"
                    else:
                        continue
                    _abort_stream(r)  # wakes the reader where the OS allows it
                    raise (StreamCancelled() if aborted[0] == "cancel" else first_timeout()) from None
                if item is end:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item

        try:
            # Read timeout for the headers is the first-data limit; in the body (between chunks) it is STREAM_READ.
            # The watchdog checks that the first data arrives within 30 s.
            with self._client.stream("POST", f"{self.cfg.base_url}/responses", headers=self._auth_headers(key),
                                     json=body, timeout=httpx.Timeout(self.FIRST_DATA, connect=15.0)) as r:
                lim = limits_from_headers(r.headers)
                if lim:
                    save_limits(lim, key)
                if r.status_code != 200:
                    r.read()
                    raise _limit_error(r.status_code, r.text, dict(r.headers))
                tm = r.request.extensions.get("timeout")
                if isinstance(tm, dict):
                    tm["read"] = self.STREAM_READ  # httpcore takes its body read timeout from here
                for line in lines(r):
                    if cancel and cancel():
                        raise StreamCancelled()
                    if not got_data and time.monotonic() - t0 > self.FIRST_DATA:
                        raise first_timeout()
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
                            if item.get("id") and item["id"] != cid:
                                calls.pop(item["id"], None)  # avoids a duplicate call if 'added' came without call_id
                            if kind == "response.output_item.done":
                                closed.add(cid)
                            if on_tool is not None:
                                on_tool(item.get("name") or "", len(item.get("arguments") or ""))
                            if on_call is not None and kind == "response.output_item.done":
                                try:  # call complete: the agent can start the tool safely before the reply finishes
                                    on_call(dict(calls[cid]))
                                except Exception:  # noqa: BLE001
                                    pass
                    elif kind in ("response.completed", "response.incomplete"):
                        completed = True
                        resp = ev.get("response") or {}
                        if (resp.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
                            finish = "length"
                        served = resp.get("model") or served
                        u = resp.get("usage") or {}
                        usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                                 "cached_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens", 0)}
                    elif kind in ("response.failed", "error"):
                        err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        code = str(err.get("code") or "") if isinstance(err, dict) else ""
                        if "usage_limit" in code or "rate_limit" in code:
                            limit_err = ProviderError("rate_limit", f"ChatGPT limiti doldu: {msg}", 429)
                            limit_err.usage_limit = "usage_limit" in code  # usage limit: the account counts as full
                            raise limit_err
                        if any(h in f"{code} {msg}".lower() for h in _TOO_LARGE_HINTS):
                            # Context overflow arrives inside the stream as response.failed (context_length_exceeded).
                            # As "server", the router would cool the model off as broken, not shrink the chat.
                            raise ProviderError("too_large", f"ChatGPT: {msg}")
                        raise ProviderError("server", f"ChatGPT: {msg}")
        except _TRANSPORT_ERRORS as e:
            if aborted[0] == "cancel":
                raise StreamCancelled() from e
            if aborted[0] == "first":
                raise first_timeout() from e
            raise _transport_error(e) from e
        finally:
            stop.set()
        if aborted[0] == "cancel":
            raise StreamCancelled()
        if aborted[0] == "first" and not completed:
            raise first_timeout()
        if not completed and any(cid not in closed for cid in calls):
            # The stream was cut before its end event and a tool call's arguments are partial (empty text from
            # output_item.added). Before, the call counted as a success with empty arguments and the tool ran that way.
            raise ProviderError("network", "ChatGPT akışı yarıda kesildi (araç çağrısı tamamlanmadı)")
        msg: dict[str, Any] = {"content": "".join(text)}
        if calls:
            msg["tool_calls"] = list(calls.values())
        if not msg["content"] and not calls:
            raise ProviderError("server", "ChatGPT boş cevap döndü")
        return ChatResult(normalize_message(msg), self.name, served, usage, time.monotonic() - t0, finish=finish)


def _abort_stream(r: httpx.Response) -> None:
    """Wakes a read blocked in another thread: on Linux only shutdown, on Windows only close ends the pending
    recv at once, so both are done. Without a socket (fake transport) the response is closed."""
    stream = (r.extensions or {}).get("network_stream")
    sock = None
    try:
        sock = stream.get_extra_info("socket") if stream is not None else None
    except Exception:  # noqa: BLE001
        sock = None
    if sock is not None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)  # SSLSocket.shutdown resets _sslobj, so use the raw socket
        except (OSError, TypeError, ValueError):
            pass
        try:
            sock.close()
            return
        except (OSError, TypeError, ValueError):
            pass
    try:
        r.close()
    except Exception:  # noqa: BLE001
        pass


def make_client(pc: ProviderConfig, timeout: float = 180.0) -> OpenAICompatProvider:
    """The provider's client: the Codex backend for ChatGPT sign-in, otherwise the OpenAI-compatible API."""
    from alice.providers import openai_compat  # via the module, so tests can inject a fake client

    if pc.auth == "chatgpt":
        return ChatGPTProvider(pc, timeout)
    if pc.kind == "anthropic":
        from alice.providers.anthropic_api import AnthropicProvider

        return AnthropicProvider(pc, timeout)  # type: ignore[return-value]
    return openai_compat.OpenAICompatProvider(pc, timeout)
