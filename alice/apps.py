"""Uygulama ve site tarifleri: "WhatsApp'tan Ahmet'e yaz" denince hangi yoldan gidileceği.

ALICE bir işi üç yoldan yapabiliyor: resmî API (hızlı, güvenilir), uygulamanın kendi derin bağlantısı
(`steam://rungameid/...` gibi) ve tarayıcı/arayüz otomasyonu (her yerde çalışır ama yavaş). Doğru yolu
her seferinde modelin tahmin etmesi hem yavaş hem hatalı; bu kayıt onu yazıya döküyor.

Kayıt iki yerden okunuyor: yerleşik `alice/apps.toml` ve kullanıcının `~/.alice/apps.toml` dosyası
(aynı ad varsa kullanıcınınki geçerli; alanları tek tek birleşir, yani sadece `url`'i değiştirebilirsin).

Bir giriş şöyle:

    [whatsapp]
    title = "WhatsApp"
    kind = "web"                      # web | app | protocol
    url = "https://web.whatsapp.com"
    keywords = "whatsapp|wp"
    note = "İlk açılışta telefondan QR okutman gerekir."
    [whatsapp.flows]
    send = "Sol listeden {kime} sohbetini aç, mesaj kutusuna {mesaj} yaz ve Enter'a bas"

`flows` altındaki her satır reflex için bir hedef şablonu: `{...}` yer tutucuları çağrıda doldurulur.
Seçici (CSS/XPath) yazmıyoruz; reflex sayfayı öğe listesine çevirip karar verdiği için arayüz
değiştiğinde tarif bozulmuyor.
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
    url: str = ""           # web: adres | protocol: şablon, ör. "steam://rungameid/{appid}"
    exe: str = ""           # app: açılacak program adı (windows open ile aranır)
    keywords: str = ""
    note: str = ""          # kullanıcıya/modele not: "ilk seferde QR okut" gibi
    api: str = ""           # bu iş için resmî bağlantı varsa onun adı (bkz. alice/connect)
    flows: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.title or self.name

    def fields(self, flow: str) -> list[str]:
        return sorted(set(FIELD_RX.findall(self.flows.get(flow, ""))))

    def goal(self, flow: str, args: dict[str, Any]) -> str:
        """Tarifteki yer tutucuları doldur. Eksik alan varsa ValueError."""
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
    return App(name=name, title=str(raw.get("title", "")), kind=kind, url=str(raw.get("url", "")),
               exe=str(raw.get("exe", "")), keywords=str(raw.get("keywords", "")), note=str(raw.get("note", "")),
               api=str(raw.get("api", "")), flows={str(k): str(v) for k, v in (raw.get("flows") or {}).items()})


def _load(p: Path) -> dict[str, dict[str, Any]]:
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def user_file() -> Path:
    return alice_home() / "apps.toml"


def registry(cfg: Any = None) -> dict[str, App]:
    """Yerleşik + kullanıcı kayıtları (kullanıcınınki alan alan üstüne yazar)."""
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
    """Ada, başlığa ya da anahtar kelimeye göre bul ("whatsapp", "WhatsApp", "wp" hepsi aynı yere gider)."""
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
    for app in apps.values():  # son çare: adın içinde geçiyor mu
        if q in app.name or q in app.title.lower():
            return app
    return None
