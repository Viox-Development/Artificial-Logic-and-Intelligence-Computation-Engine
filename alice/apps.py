"""App and site recipes: which route to take for "write to Ahmet on WhatsApp".

ALICE can do a task in three ways: the official API (fast, reliable), the app's own deep link
(such as `steam://rungameid/...`), and browser/UI automation (works everywhere but is slow). Having the model guess
the right route every time is both slow and error-prone; this registry writes it down.

The registry is read from two places: the built-in `alice/apps.toml` and the user's `~/.alice/apps.toml` file
(if the same name exists, the user's wins; fields are merged one by one, so you can change just the `url`).

An entry looks like this:

    [whatsapp]
    title = "WhatsApp"
    kind = "web"                      # web | app | protocol
    url = "https://web.whatsapp.com"
    keywords = "whatsapp|wp"
    note = "İlk açılışta telefondan QR okutman gerekir."
    desktop = "whatsapp_desktop"      # used when the desktop app is installed
    [whatsapp.flows]
    send = "Sol listeden {kime} sohbetini aç, mesaj kutusuna {mesaj} yaz ve Enter'a bas"

Each line under `flows` is a goal template for reflex: `{...}` placeholders are filled in at call time.
We don't write selectors (CSS/XPath); reflex turns the page into a list of elements and decides from that,
so the recipe doesn't break when the UI changes.
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

BUILTIN = Path(__file__).parent / "apps.toml"
KINDS = ("web", "app", "protocol")
FIELD_RX = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class App:
    name: str
    title: str = ""
    kind: str = "web"
    url: str = ""           # web: address | protocol: template, e.g. "steam://rungameid/{appid}"
    exe: str = ""           # app: name of the program to open (searched with windows open)
    keywords: str = ""
    note: str = ""          # note for the user/model, e.g. "scan the QR on first use"
    api: str = ""           # name of the official connection for this task, if any (see alice/connect)
    flows: dict[str, str] = field(default_factory=dict)
    sends: list[str] = field(default_factory=list)  # flows that go to someone else: asked even in auto mode
    target: str = ""        # where flows run: browser | desktop (empty: web -> browser, app -> desktop)
    window: str = ""        # part of the window title for desktop (empty: title or exe)
    desktop: str = ""       # web entry: name of the entry to use instead when the desktop app is installed

    @property
    def flow_target(self) -> str:
        return self.target or ("browser" if self.kind == "web" else "desktop")

    @property
    def window_title(self) -> str:
        return self.window or self.exe or self.title or self.name

    @property
    def label(self) -> str:
        return self.title or self.name

    def fields(self, flow: str) -> list[str]:
        return sorted(set(FIELD_RX.findall(self.flows.get(flow, ""))))

    def goal(self, flow: str, args: dict[str, Any]) -> str:
        """Fill in the placeholders of the recipe. Raises ValueError if a field is missing."""
        tpl = self.flows.get(flow)
        if tpl is None:
            raise ValueError(f"{self.name}: '{flow}' diye bir akış yok. Olanlar: {', '.join(sorted(self.flows)) or '—'}")
        missing = [f for f in self.fields(flow) if not str(args.get(f, "")).strip()]
        if missing:
            raise ValueError(f"{self.name}.{flow}: eksik alan: {', '.join(missing)}")
        return FIELD_RX.sub(lambda m: str(args[m.group(1)]), tpl)


def _parse(name: str, raw: dict[str, Any]) -> App:
    kind = str(raw.get("kind", "web"))
    if kind not in KINDS:
        raise ValueError(f"{name}: kind {' | '.join(KINDS)} olmalı")
    if str(raw.get("target", "")) not in ("", "browser", "desktop"):
        raise ValueError(f"{name}: target browser ya da desktop olmalı")
    return App(name=name, title=str(raw.get("title", "")), kind=kind, url=str(raw.get("url", "")),
               exe=str(raw.get("exe", "")), keywords=str(raw.get("keywords", "")), note=str(raw.get("note", "")),
               api=str(raw.get("api", "")), flows={str(k): str(v) for k, v in (raw.get("flows") or {}).items()},
               sends=[str(x) for x in raw.get("sends", [])], target=str(raw.get("target", "")),
               window=str(raw.get("window", "")), desktop=str(raw.get("desktop", "")))


def _load(p: Path) -> dict[str, dict[str, Any]]:
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def user_file() -> Path:
    return alice_home() / "apps.toml"


def registry(cfg: Any = None) -> dict[str, App]:
    """Built-in + user entries (the user's entry overrides field by field)."""
    raw = _load(BUILTIN)
    for name, entry in _load(user_file()).items():
        base = dict(raw.get(name) or {})
        flows = {**(base.get("flows") or {}), **(entry.get("flows") or {})}
        base.update(entry)
        if flows:
            base["flows"] = flows
        raw[name] = base
    out: dict[str, App] = {}
    for name, entry in raw.items():
        try:
            out[name] = _parse(name, entry)
        except ValueError:
            continue
    return out


def find(name: str, cfg: Any = None, apps: dict[str, App] | None = None) -> App | None:
    """Find by name, title or keyword ("whatsapp", "WhatsApp", "wp" all go to the same place)."""
    apps = apps if apps is not None else registry(cfg)
    q = (name or "").strip().lower()
    if not q:
        return None
    if q in apps:
        return apps[q]
    for app in apps.values():
        if q == app.title.lower():
            return app
    for app in apps.values():
        if app.keywords and re.search(app.keywords, q, re.I):
            return app
    for app in apps.values():  # last resort: is it contained in the name
        if q in app.name or q in app.title.lower():
            return app
    return None
