"""Bağlantı tanımları (manifest): bir siteyi ALICE aracına çeviren TOML dosyası.

Yeni bir servis eklemek için kod yazmak gerekmiyor; `~/.alice/connect/<ad>.toml` açıp şunu yazmak yetiyor:

    name = "steam"
    title = "Steam"
    description = "Kütüphane, oynama süreleri, arkadaşlar."
    base_url = "https://api.steampowered.com"

    [auth]
    kind = "api_key"          # none | api_key | bearer | oauth2
    env = "STEAM_API_KEY"     # anahtar .env'den okunur
    place = "query"           # query | header
    name = "key"
    help = "https://steamcommunity.com/dev/apikey"

    [actions.library]
    summary = "Kütüphanendeki oyunlar"
    method = "GET"
    path = "/IPlayerService/GetOwnedGames/v1/"
    query = { include_appinfo = "1", format = "json" }
    pick = "response.games"
    fields = ["name", "playtime_forever", "appid"]
    [actions.library.params.steamid]
    type = "string"
    required = true
    description = "SteamID64"

Yerleşik tanımlar `alice/connect/builtin/*.toml` içinde; kullanıcınınki aynı adı taşıyorsa onunki geçerli.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from alice.config import alice_home

NAME_RX = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
AUTH_KINDS = ("none", "api_key", "bearer", "oauth2")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
BUILTIN_DIR = Path(__file__).parent / "builtin"


class ManifestError(ValueError):
    """Tanım dosyası bozuk. ALICE düşmez; bozuk tanım atlanır ve sebebi yazılır."""


@dataclass
class Param:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    into: str = ""      # query | path | body | header (boş: metoda göre seçilir)
    enum: list[str] = field(default_factory=list)
    default: Any = None


@dataclass
class Action:
    name: str
    summary: str = ""
    method: str = "GET"
    path: str = "/"
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Param] = field(default_factory=dict)
    pick: str = ""              # cevabın hangi alanı gösterilsin, ör. "response.games"
    fields: list[str] = field(default_factory=list)  # liste sonuçlarında gösterilecek alanlar
    limit: int = 25
    writes: bool = False        # true: izin sistemi "yazma" riski saysın
    sends: bool = False         # true: başkasına giden mesaj/gönderi (auto modda da onay sorulur)
    cache_seconds: float = 0.0  # okuyan eylemin cevabı bu kadar saniye hatırlanır (0: hiç)


@dataclass
class Auth:
    kind: str = "none"
    env: str = ""
    place: str = "header"
    name: str = "Authorization"
    prefix: str = ""
    help: str = ""
    # oauth2
    client_id_env: str = ""
    client_secret_env: str = ""
    authorize_url: str = ""
    token_url: str = ""
    scopes: list[str] = field(default_factory=list)
    pkce: bool = True
    # Servis geri dönüş adresini önceden kaydettiriyorsa (Spotify gibi) sabit port şart.
    redirect_port: int = 0
    extra_authorize: dict[str, str] = field(default_factory=dict)


@dataclass
class Manifest:
    name: str
    title: str = ""
    description: str = ""
    base_url: str = ""
    keywords: str = ""
    docs: str = ""
    auth: Auth = field(default_factory=Auth)
    actions: dict[str, Action] = field(default_factory=dict)
    source: str = ""

    @property
    def label(self) -> str:
        return self.title or self.name


def _param(name: str, raw: Any) -> Param:
    if not isinstance(raw, dict):
        raise ManifestError(f"parametre {name}: tablo olmalı")
    into = str(raw.get("into", ""))
    if into and into not in ("query", "path", "body", "header"):
        raise ManifestError(f"parametre {name}: into query|path|body|header olmalı")
    return Param(name=name, type=str(raw.get("type", "string")), description=str(raw.get("description", "")),
                 required=bool(raw.get("required")), into=into,
                 enum=[str(x) for x in raw.get("enum", [])], default=raw.get("default"))


def _action(name: str, raw: Any) -> Action:
    if not isinstance(raw, dict):
        raise ManifestError(f"eylem {name}: tablo olmalı")
    method = str(raw.get("method", "GET")).upper()
    if method not in METHODS:
        raise ManifestError(f"eylem {name}: method {' | '.join(METHODS)} olmalı")
    params = {k: _param(k, v) for k, v in (raw.get("params") or {}).items()}
    return Action(
        name=name, summary=str(raw.get("summary", "")), method=method, path=str(raw.get("path", "/")),
        query={str(k): str(v) for k, v in (raw.get("query") or {}).items()},
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        body=dict(raw.get("body") or {}), params=params, pick=str(raw.get("pick", "")),
        fields=[str(x) for x in raw.get("fields", [])], limit=int(raw.get("limit", 25) or 25),
        writes=bool(raw.get("writes", method != "GET")), sends=bool(raw.get("sends", False)),
        cache_seconds=float(raw.get("cache_seconds", 0) or 0))


def _auth(raw: Any) -> Auth:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ManifestError("auth: tablo olmalı")
    kind = str(raw.get("kind", "none"))
    if kind not in AUTH_KINDS:
        raise ManifestError(f"auth.kind {' | '.join(AUTH_KINDS)} olmalı")
    a = Auth(kind=kind, env=str(raw.get("env", "")), place=str(raw.get("place", "header")),
             name=str(raw.get("name", "Authorization")), prefix=str(raw.get("prefix", "")),
             help=str(raw.get("help", "")), client_id_env=str(raw.get("client_id_env", "")),
             client_secret_env=str(raw.get("client_secret_env", "")),
             authorize_url=str(raw.get("authorize_url", "")), token_url=str(raw.get("token_url", "")),
             scopes=[str(x) for x in raw.get("scopes", [])], pkce=bool(raw.get("pkce", True)),
             redirect_port=int(raw.get("redirect_port", 0) or 0),
             extra_authorize={str(k): str(v) for k, v in (raw.get("extra_authorize") or {}).items()})
    if a.kind in ("api_key", "bearer") and not a.env:
        raise ManifestError(f"auth.kind={a.kind} için auth.env (ortam değişkeni adı) gerekir")
    if a.kind == "oauth2" and not (a.authorize_url and a.token_url and a.client_id_env):
        raise ManifestError("auth.kind=oauth2 için authorize_url, token_url ve client_id_env gerekir")
    if a.place not in ("query", "header"):
        raise ManifestError("auth.place query ya da header olmalı")
    return a


def parse(data: dict[str, Any], source: str = "") -> Manifest:
    name = str(data.get("name", "")).strip()
    if not NAME_RX.match(name):
        raise ManifestError(f"name küçük harf/rakam/alt çizgi olmalı: {name!r}")
    base = str(data.get("base_url", "")).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ManifestError("base_url http(s) ile başlamalı")
    actions = {k: _action(k, v) for k, v in (data.get("actions") or {}).items()}
    if not actions:
        raise ManifestError("en az bir [actions.<ad>] gerekir")
    return Manifest(name=name, title=str(data.get("title", "")), description=str(data.get("description", "")),
                    base_url=base, keywords=str(data.get("keywords", "")), docs=str(data.get("docs", "")),
                    auth=_auth(data.get("auth")), actions=actions, source=source)


def load_file(p: Path) -> Manifest:
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ManifestError(f"{p.name}: okunamadı: {e}") from e
    return parse(data, source=str(p))


def user_dir() -> Path:
    return alice_home() / "connect"


def discover(cfg: Any = None, emit: Any = None) -> tuple[dict[str, Manifest], list[str]]:
    """Yerleşik + kullanıcı tanımlarını yükle. (ad -> manifest, hatalar). Aynı ad: kullanıcınınki kazanır."""
    data = getattr(cfg, "data", None) or {}
    conf = data.get("connect") or {}
    out: dict[str, Manifest] = {}
    errors: list[str] = []
    dirs = [BUILTIN_DIR, user_dir()] + [Path(p).expanduser() for p in (conf.get("paths") or [])]
    disabled = {str(x) for x in (conf.get("disabled") or [])}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.toml")):
            try:
                m = load_file(p)
            except ManifestError as e:
                errors.append(str(e))
                if emit:
                    emit(f"bağlantı tanımı atlandı: {e}")
                continue
            if m.name in disabled:
                continue
            out[m.name] = m
    return out, errors
