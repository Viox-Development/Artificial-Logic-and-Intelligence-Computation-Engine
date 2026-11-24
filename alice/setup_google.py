"""`alice setup google`: step-by-step setup wizard for Google Calendar and Gmail.

Creating a project in Google Cloud, enabling the APIs, the OAuth consent screen and a desktop client is confusing by
hand. The wizard opens the right page in the browser at each step and says what to click; then it writes the client
info (the downloaded JSON file, or the ID/secret pasted by hand) to ~/.alice/.env and starts sign-in for Calendar and
as many Gmail accounts as you like. If you stop halfway, you can resume from the step you left off: if the client is
already written, those steps are skipped.
"""

from __future__ import annotations

import json
import os
import re
import webbrowser
from pathlib import Path
from typing import Any, Callable

from alice.i18n import _

STEPS = [
    ("Project", "https://console.cloud.google.com/projectcreate",
     "Enter 'ALICE' as the project name and click Create. Make sure the project is selected at the top right."),
    ("Calendar API", "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
     "Click 'Enable'."),
    ("Gmail API", "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
     "Click 'Enable'."),
    ("Consent screen", "https://console.cloud.google.com/auth/branding",
     "Get started: app name 'ALICE', support e-mail your own address; Audience: External; contact e-mail your "
     "own address again; save."),
    ("Test users", "https://console.cloud.google.com/auth/audience",
     "Under 'Test users', use 'Add users' to add EVERY Gmail address you will connect (school, personal...). If "
     "you don't, sign-in fails with 'access blocked'."),
    ("Client", "https://console.cloud.google.com/auth/clients/create",
     "Application type: Desktop app, name: ALICE, Create. In the window that opens, download the file with "
     "'Download JSON' (or copy the Client ID and Client secret)."),
]


# Yes answers to [Y/n] prompts, in both languages ("" = the default, yes).
YES = ("", "y", "yes", "e", "evet")


def set_env(path: Path, key: str, value: str) -> None:
    """Change the KEY=... line in `.env`, or append it at the end; don't touch other lines."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    rx = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if rx.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    from alice.config import make_private

    make_private(path)  # holds the OAuth client secret
    os.environ[key] = value


def read_client_json(text: str) -> tuple[str, str]:
    """(client_id, client_secret) from the content of the client_secret_*.json that Google downloads."""
    data = json.loads(text)
    block = data.get("installed") or data.get("web") or data
    cid, secret = str(block.get("client_id") or ""), str(block.get("client_secret") or "")
    if not cid.endswith(".apps.googleusercontent.com"):
        raise ValueError(_("this is not a Google OAuth client file (no client_id found)"))
    return cid, secret


def find_downloaded_json(folder: Path | None = None) -> Path | None:
    """The newest client_secret_*.json in the Downloads folder."""
    folder = folder or Path.home() / "Downloads"
    try:
        files = sorted(folder.glob("client_secret_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return files[0] if files else None


def run(ask: Callable[[str], str], say: Callable[[str], None], open_url: Callable[[str], Any] = webbrowser.open,
        connect_calendar: Callable[[], bool] | None = None, connect_gmail: Callable[[str], str] | None = None,
        env_path: Path | None = None, downloads: Path | None = None) -> int:
    from alice.config import alice_home

    env_path = env_path or alice_home() / ".env"
    say(_("Google Calendar + Gmail setup. Each step opens a page in the browser; press Enter when you're done."))
    if os.environ.get("GOOGLE_CLIENT_ID", "").strip():
        say(_("The Google client is already set up; skipping the Cloud steps."))
    else:
        for i, (title, url, how) in enumerate(STEPS, 1):
            say(f"\n{i}/{len(STEPS)} {_(title)}: {_(how)}")
            open_url(url)
            if ask(_("  Enter when done ('s' to skip, 'q' to quit) > ")).strip().lower() == "q":
                say(_("Stopped halfway. To resume: alice setup google"))
                return 1
        cid = secret = ""
        found = find_downloaded_json(downloads)
        if found:
            ans = ask(_("\nFound {name} in Downloads. Use it? [Y/n] > ", name=found.name)).strip().lower()
            if ans in YES:
                try:
                    cid, secret = read_client_json(found.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    say(_("could not read it: {error}", error=e))
        while not cid:
            raw = ask(_("\nPaste the path of the downloaded JSON file or the Client ID (empty = quit) > ")).strip().strip('"')
            if not raw:
                say(_("No client entered. To resume: alice setup google"))
                return 1
            try:
                if raw.endswith(".json") or raw.startswith("{"):
                    cid, secret = read_client_json(Path(raw).read_text(encoding="utf-8") if raw.endswith(".json")
                                                   else raw)
                elif raw.endswith(".apps.googleusercontent.com"):
                    cid = raw
                    secret = ask(_("Paste the Client secret > ")).strip()
                else:
                    say(_("That doesn't look like a Client ID or a JSON file (it should end in .apps.googleusercontent.com)."))
            except (OSError, ValueError) as e:
                say(_("could not read it: {error}", error=e))
        set_env(env_path, "GOOGLE_CLIENT_ID", cid)
        if secret:
            set_env(env_path, "GOOGLE_CLIENT_SECRET", secret)
        say(_("Client saved: {path}", path=env_path))

    if ask(_("\nConnect Google Calendar? [Y/n] > ")).strip().lower() in YES:
        ok = connect_calendar() if connect_calendar else False
        say(_("Calendar connected.") if ok else _("Calendar could not connect; try again later: alice connect google_calendar"))
    connected = []
    while True:
        acc = ask(_("\nName the Gmail account to connect (e.g. personal, school; empty = finish) > ")).strip().lower()
        if not acc:
            break
        if not re.fullmatch(r"[a-z0-9_-]{1,30}", acc):
            say(_("Use only letters, digits, - and _ (no accented letters), e.g. school."))
            continue
        say(_("Pick that account in the browser (it must be one of the addresses you added as test users)."))
        email = connect_gmail(acc) if connect_gmail else ""
        if email is None:
            continue
        connected.append(f"{acc}" + (f" ({email})" if email else ""))
        say(_("connected: {account}", account=connected[-1]))
    say(_("\nDone. ") + (f"Gmail: {', '.join(connected)}. " if connected else "") + _("To check: alice doctor"))
    return 0


def setup_cmd(argv: list[str]) -> int:
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    if not argv or argv[0] != "google":
        console.print(_("usage: alice setup google   (Google Calendar + Gmail setup)"))
        return 2

    def connect_calendar() -> bool:
        from alice.config import load_config
        from alice.connect import oauth
        from alice.connect.manifest import discover

        m = discover(load_config())[0].get("google_calendar")
        if m is None:
            return False
        try:
            oauth.authorize(m.auth, m.name, os.environ.get("GOOGLE_CLIENT_ID", ""),
                            os.environ.get("GOOGLE_CLIENT_SECRET", ""), emit=lambda s: console.print(f"[dim]{s}[/]"))
            return True
        except oauth.OAuthError as e:
            console.print(f"[red]{escape(str(e))}[/]")
            return False

    def connect_gmail(acc: str):
        from alice.tools import gmail

        try:
            return gmail.connect(acc, emit=lambda s: console.print(f"[dim]{s}[/]"))
        except Exception as e:  # noqa: BLE001
            console.print(_("[red]could not connect: {error}[/]", error=escape(str(e))))
            return None

    return run(console.input, lambda s: console.print(escape(s)), connect_calendar=connect_calendar,
               connect_gmail=connect_gmail)
