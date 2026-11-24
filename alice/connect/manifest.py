"""Connection definitions (manifest): TOML files that turn a website into an ALICE tool.

No code is needed to add a new service: just open `~/.alice/connect/<ad>.toml` and write this:

    name = "steam"
    title = "Steam"
    description = "Library, playtime, friends."
    base_url = "https://api.steampowered.com"

    [auth]
    kind = "api_key"          # none | api_key | bearer | oauth2
    env = "STEAM_API_KEY"     # the key is read from .env
    place = "query"           # query | header
    name = "key"
    help = "https://steamcommunity.com/dev/apikey"

    [actions.library]
    summary = "Games in your library"
    method = "GET"
    path = "/IPlayerService/GetOwnedGames/v1/"
    query = { include_appinfo = "1", format = "json" }
    pick = "response.games"
    fields = ["name", "playtime_forever", "appid"]
    [actions.library.params.steamid]
    type = "string"
    required = true
    description = "SteamID64"

Built-in definitions live in `alice/connect/builtin/*.toml`; if a user definition has the same name, the user's one wins.
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

from alice.i18n import _
from alice.config import alice_home

NAME_RX = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
AUTH_KINDS = ("none", "api_key", "bearer", "oauth2")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
BUILTIN_DIR = Path(__file__).parent / "builtin"


class ManifestError(ValueError):
    """Broken definition file. ALICE does not crash: the definition is skipped and the reason is written out."""


@dataclass
class Param:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    into: str = ""      # query | path | body | header (empty: chosen by method)
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
    pick: str = ""              # which field of the response to show, e.g. "response.games"
    fields: list[str] = field(default_factory=list)  # fields to show for list results
    limit: int = 25
    writes: bool = False        # true: the permission system counts this as a "write" risk
    sends: bool = False         # true: a message/post sent to someone else (approval is still asked in auto mode)
    cache_seconds: float = 0.0  # a read action's response is remembered for this many seconds (0: not at all)


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
    # If the service requires the redirect address to be registered in advance (like Spotify), a fixed port is required.
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
        raise ManifestError(_("parameter {name}: must be a table", name=name))
    into = str(raw.get("into", ""))
    if into and into not in ("query", "path", "body", "header", "template"):
        raise ManifestError(_("parameter {name}: into must be query|path|body|header|template", name=name))
    return Param(name=name, type=str(raw.get("type", "string")), description=str(raw.get("description", "")),
                 required=bool(raw.get("required")), into=into,
                 enum=[str(x) for x in raw.get("enum", [])], default=raw.get("default"))


def _action(name: str, raw: Any) -> Action:
    if not isinstance(raw, dict):
        raise ManifestError(_("action {name}: must be a table", name=name))
    method = str(raw.get("method", "GET")).upper()
    if method not in METHODS:
        raise ManifestError(_("action {name}: method must be {methods}", name=name, methods=" | ".join(METHODS)))
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
        raise ManifestError(_("auth: must be a table"))
    kind = str(raw.get("kind", "none"))
    if kind not in AUTH_KINDS:
        raise ManifestError(_("auth.kind must be {kinds}", kinds=" | ".join(AUTH_KINDS)))
    a = Auth(kind=kind, env=str(raw.get("env", "")), place=str(raw.get("place", "header")),
             name=str(raw.get("name", "Authorization")), prefix=str(raw.get("prefix", "")),
             help=str(raw.get("help", "")), client_id_env=str(raw.get("client_id_env", "")),
             client_secret_env=str(raw.get("client_secret_env", "")),
             authorize_url=str(raw.get("authorize_url", "")), token_url=str(raw.get("token_url", "")),
             scopes=[str(x) for x in raw.get("scopes", [])], pkce=bool(raw.get("pkce", True)),
             redirect_port=int(raw.get("redirect_port", 0) or 0),
             extra_authorize={str(k): str(v) for k, v in (raw.get("extra_authorize") or {}).items()})
    if a.kind in ("api_key", "bearer") and not a.env:
        raise ManifestError(_("auth.kind={kind} needs auth.env (the environment variable name)", kind=a.kind))
    if a.kind == "oauth2" and not (a.authorize_url and a.token_url and a.client_id_env):
        raise ManifestError(_("auth.kind=oauth2 needs authorize_url, token_url and client_id_env"))
    if a.place not in ("query", "header"):
        raise ManifestError(_("auth.place must be query or header"))
    return a


def parse(data: dict[str, Any], source: str = "") -> Manifest:
    name = str(data.get("name", "")).strip()
    if not NAME_RX.match(name):
        raise ManifestError(_("name must be lowercase letters/digits/underscores: {name}", name=repr(name)))
    base = str(data.get("base_url", "")).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ManifestError(_("base_url must start with http(s)"))
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
        raise ManifestError(_("{name}: could not read it: {error}", name=p.name, error=e)) from e
    return parse(data, source=str(p))


def user_dir() -> Path:
    return alice_home() / "connect"


def discover(cfg: Any = None, emit: Any = None) -> tuple[dict[str, Manifest], list[str]]:
    """Load built-in and user definitions. Returns (name -> manifest, errors). Same name: the user's one wins."""
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
                    emit(_("connection definition skipped: {error}", error=e))
                continue
            if m.name in disabled:
                continue
            out[m.name] = m
    return out, errors
