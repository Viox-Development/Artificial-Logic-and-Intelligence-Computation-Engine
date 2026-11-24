"""Execute an action from the manifest: build request, add credentials, make response readable for the model."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from alice.i18n import _
from alice.connect import store
from alice.connect.manifest import Action, Auth, Manifest

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")  # Also Google's camelCase names like {calendarId}
TIMEOUT = 30.0
RETRY_STATUSES = {429, 502, 503, 504}
MAX_RETRY_WAIT = 5.0     # seconds: if Retry-After is longer than this, report the error without waiting
CACHE_MAX = 64
_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
_cache_lock = threading.Lock()
_sleep = time.sleep      # can be changed so tests pass without waiting


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_key(m: Manifest, act: Action, args: dict[str, Any]) -> str:
    return json.dumps([m.name, act.name, args], ensure_ascii=False, sort_keys=True, default=str)


class ConnectError(RuntimeError):
    """Connection error to show to user/model (missing key, HTTP error, malformed response)."""


# ------------------------------------------------------------------------------------- credentials
def api_key(auth: Auth) -> str:
    return os.environ.get(auth.env, "").strip() if auth.env else ""


def token_for(m: Manifest) -> str:
    """Valid access token for OAuth2 connection; refreshes if needed."""
    from alice.connect import oauth

    row = store.get(m.name)
    if not row.get("access_token"):
        raise ConnectError(_("{label} is not connected. To connect: alice connect {name}", label=m.label, name=m.name))
    if store.expired(row):
        if not row.get("refresh_token"):
            raise ConnectError(_("{label}: the session ended; connect again: alice connect {name}", label=m.label, name=m.name))
        cid = os.environ.get(m.auth.client_id_env, "").strip()
        if not cid:
            raise ConnectError(_("{env} is missing; add it to ~/.alice/.env and connect again.", env=m.auth.client_id_env))
        try:
            row = oauth.refresh(m.auth, m.name, cid, os.environ.get(m.auth.client_secret_env, "").strip())
        except oauth.OAuthError as e:
            raise ConnectError(str(e)) from e
    return str(row["access_token"])


def ready(m: Manifest) -> tuple[bool, str]:
    """(is_ready, reason). To display status in tool list and `alice connect`."""
    a = m.auth
    if a.kind == "none":
        return True, _("ready")
    if a.kind in ("api_key", "bearer"):
        if api_key(a):
            return True, _("ready")
        return False, _("{env} is missing", env=a.env) + (f" — {a.help}" if a.help else "")
    row = store.get(m.name)
    if not row.get("access_token"):
        return False, _("not connected (alice connect {name})", name=m.name)
    if store.expired(row) and not row.get("refresh_token"):
        return False, _("the session ended (alice connect {name})", name=m.name)
    from alice.connect import oauth

    if oauth.needs_sign_in(m.name):
        return False, _("the sign-in is invalid; sign in again (alice connect {name})", name=m.name)
    return True, _("connected")


def _apply_auth(m: Manifest, headers: dict[str, str], query: dict[str, Any]) -> None:
    a = m.auth
    if a.kind == "none":
        return
    if a.kind == "oauth2":
        headers["Authorization"] = f"Bearer {token_for(m)}"
        return
    key = api_key(a)
    if not key:
        raise ConnectError(_("{env} is missing for {label}. Add it to the ~/.alice/.env file.", env=a.env, label=m.label)
                           + (f" Anahtar: {a.help}" if a.help else ""))
    if a.kind == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif a.place == "query":
        query[a.name] = key
    else:
        headers[a.name] = f"{a.prefix}{key}" if a.prefix else key


# ------------------------------------------------------------------------------------- request
def build(m: Manifest, act: Action, args: dict[str, Any]) -> dict[str, Any]:
    """Prepare request parts (separated from the request itself so it can be tested)."""
    query: dict[str, Any] = dict(act.query)
    headers: dict[str, str] = {"Accept": "application/json", **act.headers}
    body: dict[str, Any] = dict(act.body)
    path_args: dict[str, str] = {}
    template_args: dict[str, Any] = {}
    missing = [p.name for p in act.params.values() if p.required and args.get(p.name) in (None, "")]
    if missing:
        raise ConnectError(f"{m.name}.{act.name}: eksik parametre: {', '.join(missing)}")
    for p in act.params.values():
        v = args.get(p.name, p.default)
        if v is None or v == "":
            continue
        where = p.into or ("path" if "{" + p.name + "}" in act.path else
                           ("body" if act.method in ("POST", "PUT", "PATCH") else "query"))
        if where == "template":  # "{name}" placeholder in body template (for nested bodies)
            template_args[p.name] = v
        elif where == "path":
            path_args[p.name] = str(v)
        elif where == "header":
            headers[p.name] = str(v)
        elif where == "body":
            body[p.name] = v
        else:
            query[p.name] = v
    if template_args:
        body = _fill(body, template_args)
    _apply_auth(m, headers, query)
    try:
        path = PLACEHOLDER.sub(lambda mt: _quote(path_args[mt.group(1)]), act.path)
    except KeyError as e:
        raise ConnectError(f"{m.name}.{act.name}: yol parametresi eksik: {e.args[0]}") from e
    # Absolute URL path: multiple hosts in a single connection (e.g. Google Drive, Docs, and Sheets with the same login).
    url = path if path.startswith(("https://", "http://")) else m.base_url + ("" if path.startswith("/") else "/") + path
    return {"method": act.method, "url": url, "params": query, "headers": headers, "json": body or None}


def _fill(node: Any, args: dict[str, Any]) -> Any:
    """Replace values exactly matching "{name}" in body template with parameter (preserves type); drop missing ones."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            f = _fill(v, args)
            if f is not _MISSING:
                out[k] = f
        return out
    if isinstance(node, list):
        return [x for x in (_fill(v, args) for v in node) if x is not _MISSING]
    if isinstance(node, str):
        mt = PLACEHOLDER.fullmatch(node)
        if mt:
            return args.get(mt.group(1), _MISSING)
    return node


_MISSING = object()


def _quote(v: str) -> str:
    from urllib.parse import quote

    return quote(v, safe="")


def call(m: Manifest, act: Action, args: dict[str, Any], client: Any = None, timeout: float = TIMEOUT,
         cache: bool = True) -> Any:
    """Call the action. Read actions are cached for `cache_seconds` (feeds always want fresh with cache=False).

    Retry is once and short: on 429 (server rejected request without processing) for any action; on network error
    and 502/503/504 only for read actions. Retrying a sending request on network error might send the message twice;
    not done to prevent user from receiving the same message twice."""
    safe = not (act.writes or act.sends)
    key = _cache_key(m, act, args) if (cache and safe and act.cache_seconds > 0) else ""
    if key:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < act.cache_seconds:
                _cache.move_to_end(key)
                return hit[1]
    data = _request(m, act, args, client, timeout, safe)
    if key:
        with _cache_lock:
            _cache[key] = (time.time(), data)
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
    return data


def _retry_wait(r: Any) -> float | None:
    """Wait duration according to Retry-After; None if too long (report error instead of waiting)."""
    raw = str((getattr(r, "headers", None) or {}).get("Retry-After", "") or "").strip()
    try:
        wait = float(raw) if raw else 1.0
    except ValueError:
        wait = 1.0
    return wait if wait <= MAX_RETRY_WAIT else None


def _request(m: Manifest, act: Action, args: dict[str, Any], client: Any, timeout: float, safe: bool) -> Any:
    import httpx

    req = build(m, act, args)
    c = client or httpx
    for attempt in (0, 1):
        try:
            r = c.request(req["method"], req["url"], params=req["params"], headers=req["headers"],
                          json=req["json"], timeout=timeout)
        except Exception as e:  # noqa: BLE001 - return network error comprehensibly to the model
            if attempt == 0 and safe:
                _sleep(1.0)
                continue
            raise ConnectError(_("{label}: the request failed: {error}", label=m.label, error=e)) from e
        if attempt == 0 and r.status_code in RETRY_STATUSES and (safe or r.status_code == 429):
            wait = _retry_wait(r)
            if wait is not None:
                _sleep(wait)
                continue
        break
    if r.status_code == 401:
        raise ConnectError(_("{label}: permission denied (401). Connect again: alice connect {name}", label=m.label, name=m.name))
    if r.status_code == 429:
        raise ConnectError(_("{label}: rate limit hit (429); try again in a bit.", label=m.label))
    if r.status_code >= 400:
        raise ConnectError(f"{m.label}: HTTP {r.status_code}: {r.text[:300]}")
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return r.text


# ------------------------------------------------------------------------------------- response
def pick(data: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit():
            data = data[int(part)] if int(part) < len(data) else None
        else:
            return None
    return data


def render(data: Any, act: Action, limit_chars: int = 12000) -> str:
    """Text to be read by the model: selected fields line-by-line if list, tidy JSON otherwise."""
    if act.pick:
        data = pick(data, act.pick)
    if data is None:
        return "no results"
    if isinstance(data, list):
        rows = data[: act.limit]
        if not rows:
            return "no results"
        out = []
        for i, row in enumerate(rows, 1):
            if isinstance(row, dict) and act.fields:
                parts = [f"{f}={_short(pick(row, f))}" for f in act.fields if pick(row, f) is not None]
                out.append(f"{i}. " + "  ".join(parts))
            else:
                out.append(f"{i}. {_short(row, 300)}")
        more = len(data) - len(rows)
        return "\n".join(out) + (f"\n… {more} more results" if more > 0 else "")
    if isinstance(data, dict) and act.fields:
        parts = [f"{f}: {_short(pick(data, f))}" for f in act.fields if pick(data, f) is not None]
        if parts:
            return "\n".join(parts)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
    return text if len(text) <= limit_chars else text[:limit_chars] + f"\n… ({len(text) - limit_chars} karakter daha)"


def _short(v: Any, n: int = 120) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"
