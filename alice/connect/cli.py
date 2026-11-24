"""`alice connect`: list connections, connect (OAuth), test, remove."""

from __future__ import annotations

import argparse
import os

from rich.console import Console
from rich.table import Table

from alice.config import load_config
from alice.connect import oauth, store
from alice.connect import manifest as mf
from alice.connect.runner import ConnectError, call, ready, render
from alice.i18n import _

console = Console(highlight=False)

TEMPLATE = '''# ALICE connection definition. Details: docs/CONNECT.md
name = "{name}"
title = "{title}"
description = "What it is for (this text goes to the model)."
base_url = "https://api.example.com"
keywords = "{name}"

[auth]
kind = "api_key"        # none | api_key | bearer | oauth2
env = "{env}"
place = "header"        # query | header
name = "Authorization"
help = "Where to get the key"

[actions.example]
summary = "What it does"
method = "GET"
path = "/v1/things"
fields = ["id", "name"]

[actions.example.params.q]
type = "string"
description = "Search phrase"
'''


def _gmail(a) -> int:
    """alice connect gmail [--account name] [--logout]: multiple Gmail accounts."""
    from alice.connect import store
    from alice.tools import gmail

    if not a.account:
        accs = gmail.accounts()
        if not accs:
            console.print(_("no Gmail account connected. To connect one: [bold]alice connect gmail --account personal[/] "
                            "(another name for a second account, e.g. --account school)"))
            return 1
        for acc in accs:
            console.print(f"  [green]●[/] {acc}  [dim]{store.get(gmail.PREFIX + acc).get('email', '')}[/]")
        return 0
    if a.logout:
        ok = store.forget(gmail.PREFIX + a.account)
        console.print(_("[green]account connection removed[/]") if ok else _("[dim]no account was connected with this name[/]"))
        return 0
    try:
        email = gmail.connect(a.account, emit=lambda s: console.print(f"[dim]{s}[/]"))
    except Exception as e:  # noqa: BLE001
        console.print(_("[red]could not connect:[/] {error}", error=e))
        return 1
    console.print(_("[green]connected:[/] {account}", account=a.account) + (f" ({email})" if email else ""))
    return 0


def connect_cmd(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="alice connect", description=_("Service connections (APIs)"))
    ap.add_argument("name", nargs="?", help=_("connection name (empty: list them all)"))
    ap.add_argument("--logout", action="store_true", help=_("delete the stored token"))
    ap.add_argument("--test", metavar=_("ACTION"), help=_("try an action, e.g. --test library"))
    ap.add_argument("--arg", action="append", default=[], metavar=_("NAME=VALUE"), help=_("parameter for --test"))
    ap.add_argument("--new", action="store_true", help=_("create an empty definition file with this name"))
    ap.add_argument("--account", help=_("gmail: account name (for several accounts, e.g. personal, school)"))
    a = ap.parse_args(argv)
    if a.name == "gmail":
        return _gmail(a)

    cfg = load_config()
    manifests, errors = mf.discover(cfg)
    for e in errors:
        console.print(_("[yellow]definition skipped:[/] {error}", error=e))

    if a.new:
        if not a.name:
            console.print(_("[red]a name is needed:[/] alice connect <name> --new"))
            return 2
        mf.user_dir().mkdir(parents=True, exist_ok=True)
        p = mf.user_dir() / f"{a.name}.toml"
        if p.exists():
            console.print(_("[yellow]already exists:[/] {path}", path=p))
            return 1
        p.write_text(TEMPLATE.format(name=a.name, title=a.name.title(), env=f"{a.name.upper()}_API_KEY"),
                     encoding="utf-8")
        console.print(_("[green]created:[/] {path}", path=p))
        return 0

    if not a.name:
        return _list(manifests)

    m = manifests.get(a.name)
    if m is None:
        console.print(_("[red]no such connection:[/] {name}  [dim](list them with alice connect, create one with --new)[/]",
                        name=a.name))
        return 1

    if a.logout:
        console.print(_("[green]token deleted[/]") if store.forget(m.name) else _("[dim]there was no stored token[/]"))
        return 0

    if a.test:
        return _test(m, a.test, a.arg)

    if m.auth.kind == "oauth2":
        return _oauth(m)

    ok, why = ready(m)
    console.print(f"[bold]{m.label}[/] · {why}")
    if not ok:
        console.print(_("[dim]Write {env} in ~/.alice/.env.[/]", env=m.auth.env)
                      + (_("\n[dim]Key: {help}[/]", help=_(m.auth.help)) if m.auth.help else ""))
        return 1
    console.print(_("[dim]Actions: {actions}[/]", actions=", ".join(sorted(m.actions))))
    return 0


def _list(manifests: dict) -> int:
    if not manifests:
        console.print(_("[dim]no connections defined. New definition: alice connect <name> --new[/]"))
        return 0
    t = Table(title=_("Connections"))
    for c in (_("name"), _("service"), _("auth"), _("status"), _("actions")):
        t.add_column(c)
    for m in sorted(manifests.values(), key=lambda x: x.name):
        ok, why = ready(m)
        t.add_row(m.name, m.label, m.auth.kind, f"[{'green' if ok else 'yellow'}]{why}[/]",
                  str(len(m.actions)))
    console.print(t)
    console.print(_("[dim]connect: alice connect <name> · try: alice connect <name> --test <action>[/]"))
    return 0


def _oauth(m) -> int:
    cid = os.environ.get(m.auth.client_id_env, "").strip()
    if not cid:
        console.print(_("[yellow]no {env}.[/] Add it to ~/.alice/.env.", env=m.auth.client_id_env)
                      + ("\n[dim]" + _(m.auth.help) + "[/]" if m.auth.help else ""))
        return 1
    secret = os.environ.get(m.auth.client_secret_env, "").strip() if m.auth.client_secret_env else ""
    console.print(_("Asking for permission for [bold]{label}[/] in the browser…", label=m.label))
    try:
        oauth.authorize(m.auth, m.name, cid, secret, emit=lambda s: console.print(f"[dim]{s}[/]"))
    except oauth.OAuthError as e:
        console.print(f"[red]{e}[/]")
        return 1
    console.print(_("[green]{label} connected.[/] Actions: {actions}", label=m.label, actions=", ".join(sorted(m.actions))))
    return 0


def _test(m, action: str, raw_args: list[str]) -> int:
    act = m.actions.get(action)
    if act is None:
        console.print(_("[red]no such action:[/] {action}  [dim]({actions})[/]", action=action,
                        actions=", ".join(sorted(m.actions))))
        return 1
    args = {}
    for item in raw_args:
        if "=" not in item:
            console.print(_("[red]--arg must look like NAME=VALUE:[/] {value}", value=item))
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
