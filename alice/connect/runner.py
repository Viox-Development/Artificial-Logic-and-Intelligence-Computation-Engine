"""Manifestteki bir eylemi çalıştır: isteği kur, kimliği ekle, cevabı modele okunur hale getir."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from alice.connect import store
from alice.connect.manifest import Action, Auth, Manifest

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
TIMEOUT = 30.0


class ConnectError(RuntimeError):
    """Kullanıcıya/modele gösterilecek bağlantı hatası (eksik anahtar, HTTP hatası, bozuk cevap)."""


# ------------------------------------------------------------------------------------- kimlik
def api_key(auth: Auth) -> str:
    return os.environ.get(auth.env, "").strip() if auth.env else ""


def token_for(m: Manifest) -> str:
    """OAuth2 bağlantısı için geçerli erişim token'ı; gerekiyorsa yeniler."""
    from alice.connect import oauth

    row = store.get(m.name)
    if not row.get("access_token"):
        raise ConnectError(f"{m.label} bağlı değil. Bağlanmak için: alice connect {m.name}")
    if store.expired(row):
        if not row.get("refresh_token"):
            raise ConnectError(f"{m.label}: oturum bitti, tekrar bağlan: alice connect {m.name}")
        cid = os.environ.get(m.auth.client_id_env, "").strip()
        if not cid:
            raise ConnectError(f"{m.auth.client_id_env} yok; ~/.alice/.env'e ekleyip tekrar bağlan.")
        try:
            row = oauth.refresh(m.auth, m.name, cid, os.environ.get(m.auth.client_secret_env, "").strip())
        except oauth.OAuthError as e:
            raise ConnectError(str(e)) from e
    return str(row["access_token"])


def ready(m: Manifest) -> tuple[bool, str]:
    """(hazır mı, sebep). Araç listesinde ve `alice connect`te durum göstermek için."""
    a = m.auth
    if a.kind == "none":
        return True, "hazır"
    if a.kind in ("api_key", "bearer"):
        if api_key(a):
            return True, "hazır"
        return False, f"{a.env} yok" + (f" — {a.help}" if a.help else "")
    row = store.get(m.name)
    if not row.get("access_token"):
        return False, f"bağlı değil (alice connect {m.name})"
    if store.expired(row) and not row.get("refresh_token"):
        return False, f"oturum bitti (alice connect {m.name})"
    return True, "bağlı"


def _apply_auth(m: Manifest, headers: dict[str, str], query: dict[str, Any]) -> None:
    a = m.auth
    if a.kind == "none":
        return
    if a.kind == "oauth2":
        headers["Authorization"] = f"Bearer {token_for(m)}"
        return
    key = api_key(a)
    if not key:
        raise ConnectError(f"{m.label} için {a.env} yok. ~/.alice/.env dosyasına ekle."
                           + (f" Anahtar: {a.help}" if a.help else ""))
    if a.kind == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif a.place == "query":
        query[a.name] = key
    else:
        headers[a.name] = f"{a.prefix}{key}" if a.prefix else key


# ------------------------------------------------------------------------------------- istek
def build(m: Manifest, act: Action, args: dict[str, Any]) -> dict[str, Any]:
    """İsteğin parçalarını hazırla (test edilebilsin diye istekten ayrı)."""
    query: dict[str, Any] = dict(act.query)
    headers: dict[str, str] = {"Accept": "application/json", **act.headers}
    body: dict[str, Any] = dict(act.body)
    path_args: dict[str, str] = {}
    missing = [p.name for p in act.params.values() if p.required and args.get(p.name) in (None, "")]
    if missing:
        raise ConnectError(f"{m.name}.{act.name}: eksik parametre: {', '.join(missing)}")
    for p in act.params.values():
        v = args.get(p.name, p.default)
        if v is None or v == "":
            continue
        where = p.into or ("path" if "{" + p.name + "}" in act.path else
                           ("body" if act.method in ("POST", "PUT", "PATCH") else "query"))
        if where == "path":
            path_args[p.name] = str(v)
        elif where == "header":
            headers[p.name] = str(v)
        elif where == "body":
            body[p.name] = v
        else:
            query[p.name] = v
    _apply_auth(m, headers, query)
    try:
        path = PLACEHOLDER.sub(lambda mt: _quote(path_args[mt.group(1)]), act.path)
    except KeyError as e:
        raise ConnectError(f"{m.name}.{act.name}: yol parametresi eksik: {e.args[0]}") from e
    return {"method": act.method, "url": m.base_url + ("" if path.startswith("/") else "/") + path,
            "params": query, "headers": headers, "json": body or None}


def _quote(v: str) -> str:
    from urllib.parse import quote

    return quote(v, safe="")


def call(m: Manifest, act: Action, args: dict[str, Any], client: Any = None, timeout: float = TIMEOUT) -> Any:
    import httpx

    req = build(m, act, args)
    c = client or httpx
    try:
        r = c.request(req["method"], req["url"], params=req["params"], headers=req["headers"],
                      json=req["json"], timeout=timeout)
    except Exception as e:  # noqa: BLE001 - ağ hatası modele anlaşılır dönsün
        raise ConnectError(f"{m.label}: istek başarısız: {e}") from e
    if r.status_code == 401:
        raise ConnectError(f"{m.label}: yetki reddedildi (401). Tekrar bağlan: alice connect {m.name}")
    if r.status_code == 429:
        raise ConnectError(f"{m.label}: hız sınırına takıldı (429), biraz sonra tekrar dene.")
    if r.status_code >= 400:
        raise ConnectError(f"{m.label}: HTTP {r.status_code}: {r.text[:300]}")
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return r.text


# ------------------------------------------------------------------------------------- cevap
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
    """Modelin okuyacağı metin: liste ise satır satır seçilen alanlar, değilse derli toplu JSON."""
    if act.pick:
        data = pick(data, act.pick)
    if data is None:
        return "sonuç yok"
    if isinstance(data, list):
        rows = data[: act.limit]
        if not rows:
            return "sonuç yok"
        out = []
        for i, row in enumerate(rows, 1):
            if isinstance(row, dict) and act.fields:
                parts = [f"{f}={_short(pick(row, f))}" for f in act.fields if pick(row, f) is not None]
                out.append(f"{i}. " + "  ".join(parts))
            else:
                out.append(f"{i}. {_short(row, 300)}")
        more = len(data) - len(rows)
        return "\n".join(out) + (f"\n… {more} sonuç daha" if more > 0 else "")
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
