"""`alice connect`: bağlantıları listele, bağlan (OAuth), dene, kaldır."""

from __future__ import annotations

import argparse
import os

from rich.console import Console
from rich.table import Table

from alice.config import load_config
from alice.connect import oauth, store
from alice.connect import manifest as mf
from alice.connect.runner import ConnectError, call, ready, render

console = Console(highlight=False)

TEMPLATE = '''# ALICE bağlantı tanımı. Ayrıntı: docs/CONNECT.md
name = "{name}"
title = "{title}"
description = "Ne işe yaradığı (modele bu metin gider)."
base_url = "https://api.example.com"
keywords = "{name}"

[auth]
kind = "api_key"        # none | api_key | bearer | oauth2
env = "{env}"
place = "header"        # query | header
name = "Authorization"
help = "Anahtarı nereden alacağın"

[actions.ornek]
summary = "Ne yaptığı"
method = "GET"
path = "/v1/seyler"
fields = ["id", "name"]

[actions.ornek.params.q]
type = "string"
description = "Arama ifadesi"
'''


def _gmail(a) -> int:
    """alice connect gmail [--account ad] [--logout]: birden fazla Gmail hesabı."""
    from alice.connect import store
    from alice.tools import gmail

    if not a.account:
        accs = gmail.accounts()
        if not accs:
            console.print("bağlı Gmail hesabı yok. Bağlamak için: [bold]alice connect gmail --account kisisel[/] "
                          "(ikinci hesap için başka bir ad, ör. --account okul)")
            return 1
        for acc in accs:
            console.print(f"  [green]●[/] {acc}  [dim]{store.get(gmail.PREFIX + acc).get('email', '')}[/]")
        return 0
    if a.logout:
        ok = store.forget(gmail.PREFIX + a.account)
        console.print("[green]hesap bağlantısı silindi[/]" if ok else "[dim]bu adla bağlı hesap yoktu[/]")
        return 0
    try:
        email = gmail.connect(a.account, emit=lambda s: console.print(f"[dim]{s}[/]"))
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]bağlanamadı:[/] {e}")
        return 1
    console.print(f"[green]bağlandı:[/] {a.account}" + (f" ({email})" if email else ""))
    return 0


def connect_cmd(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="alice connect", description="Servis bağlantıları (API'lar)")
    ap.add_argument("name", nargs="?", help="bağlantı adı (boş: hepsini listele)")
    ap.add_argument("--logout", action="store_true", help="saklanan token'ı sil")
    ap.add_argument("--test", metavar="EYLEM", help="bir eylemi dene, ör. --test library")
    ap.add_argument("--arg", action="append", default=[], metavar="AD=DEĞER", help="--test için parametre")
    ap.add_argument("--new", action="store_true", help="bu adla boş bir tanım dosyası oluştur")
    ap.add_argument("--account", help="gmail: hesap adı (birden fazla hesap için, ör. kisisel, okul)")
    a = ap.parse_args(argv)
    if a.name == "gmail":
        return _gmail(a)

    cfg = load_config()
    manifests, errors = mf.discover(cfg)
    for e in errors:
        console.print(f"[yellow]tanım atlandı:[/] {e}")

    if a.new:
        if not a.name:
            console.print("[red]ad gerekli:[/] alice connect <ad> --new")
            return 2
        mf.user_dir().mkdir(parents=True, exist_ok=True)
        p = mf.user_dir() / f"{a.name}.toml"
        if p.exists():
            console.print(f"[yellow]zaten var:[/] {p}")
            return 1
        p.write_text(TEMPLATE.format(name=a.name, title=a.name.title(), env=f"{a.name.upper()}_API_KEY"),
                     encoding="utf-8")
        console.print(f"[green]oluşturuldu:[/] {p}")
        return 0

    if not a.name:
        return _list(manifests)

    m = manifests.get(a.name)
    if m is None:
        console.print(f"[red]bağlantı yok:[/] {a.name}  [dim](alice connect ile listele, --new ile oluştur)[/]")
        return 1

    if a.logout:
        console.print("[green]token silindi[/]" if store.forget(m.name) else "[dim]saklanan token yoktu[/]")
        return 0

    if a.test:
        return _test(m, a.test, a.arg)

    if m.auth.kind == "oauth2":
        return _oauth(m)

    hazır, sebep = ready(m)
    console.print(f"[bold]{m.label}[/] · {sebep}")
    if not hazır:
        console.print(f"[dim]{m.auth.env} değerini ~/.alice/.env dosyasına yaz.[/]"
                      + (f"\n[dim]Anahtar: {m.auth.help}[/]" if m.auth.help else ""))
        return 1
    console.print("[dim]Eylemler: " + ", ".join(sorted(m.actions)) + "[/]")
    return 0


def _list(manifests: dict) -> int:
    if not manifests:
        console.print("[dim]tanımlı bağlantı yok. Yeni tanım: alice connect <ad> --new[/]")
        return 0
    t = Table(title="Bağlantılar")
    for c in ("ad", "servis", "kimlik", "durum", "eylemler"):
        t.add_column(c)
    for m in sorted(manifests.values(), key=lambda x: x.name):
        hazır, sebep = ready(m)
        t.add_row(m.name, m.label, m.auth.kind, f"[{'green' if hazır else 'yellow'}]{sebep}[/]",
                  str(len(m.actions)))
    console.print(t)
    console.print("[dim]bağlan: alice connect <ad> · dene: alice connect <ad> --test <eylem>[/]")
    return 0


def _oauth(m) -> int:
    cid = os.environ.get(m.auth.client_id_env, "").strip()
    if not cid:
        console.print(f"[yellow]{m.auth.client_id_env} yok.[/] ~/.alice/.env dosyasına ekle."
                      + (f"\n[dim]{m.auth.help}[/]" if m.auth.help else ""))
        return 1
    secret = os.environ.get(m.auth.client_secret_env, "").strip() if m.auth.client_secret_env else ""
    console.print(f"[bold]{m.label}[/] için tarayıcıda izin isteniyor…")
    try:
        oauth.authorize(m.auth, m.name, cid, secret, emit=lambda s: console.print(f"[dim]{s}[/]"))
    except oauth.OAuthError as e:
        console.print(f"[red]{e}[/]")
        return 1
    console.print(f"[green]{m.label} bağlandı.[/] Eylemler: " + ", ".join(sorted(m.actions)))
    return 0


def _test(m, action: str, raw_args: list[str]) -> int:
    act = m.actions.get(action)
    if act is None:
        console.print(f"[red]eylem yok:[/] {action}  [dim]({', '.join(sorted(m.actions))})[/]")
        return 1
    args = {}
    for item in raw_args:
        if "=" not in item:
            console.print(f"[red]--arg AD=DEĞER biçiminde olmalı:[/] {item}")
            return 2
        k, v = item.split("=", 1)
        args[k] = v
    try:
        data = call(m, act, args)
    except ConnectError as e:
        console.print(f"[red]{e}[/]")
        return 1
    console.print(render(data, act, 4000))
    return 0
