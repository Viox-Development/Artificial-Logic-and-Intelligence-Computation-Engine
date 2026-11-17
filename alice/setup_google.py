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

STEPS = [
    ("Proje", "https://console.cloud.google.com/projectcreate",
     "Proje adı olarak 'ALICE' yaz, Oluştur'a bas. Sağ üstte projenin seçili olduğundan emin ol."),
    ("Takvim API'si", "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
     "'Etkinleştir' (Enable) düğmesine bas."),
    ("Gmail API'si", "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
     "'Etkinleştir' (Enable) düğmesine bas."),
    ("İzin ekranı", "https://console.cloud.google.com/auth/branding",
     "Başla (Get started): uygulama adı 'ALICE', destek e-postası kendi adresin; Kitle (Audience): External; "
     "iletişim e-postası yine kendi adresin; kaydet."),
    ("Test kullanıcısı", "https://console.cloud.google.com/auth/audience",
     "'Test users' altında 'Add users' ile bağlayacağın HER Gmail adresini ekle (okul, kişisel...). Eklemezsen "
     "girişte 'erişim engellendi' hatası alırsın."),
    ("İstemci", "https://console.cloud.google.com/auth/clients/create",
     "Application type: Desktop app, ad: ALICE, Oluştur. Çıkan pencerede 'Download JSON' ile dosyayı indir "
     "(ya da Client ID ve Client secret'ı kopyala)."),
]


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
    os.environ[key] = value


def read_client_json(text: str) -> tuple[str, str]:
    """(client_id, client_secret) from the content of the client_secret_*.json that Google downloads."""
    data = json.loads(text)
    block = data.get("installed") or data.get("web") or data
    cid, secret = str(block.get("client_id") or ""), str(block.get("client_secret") or "")
    if not cid.endswith(".apps.googleusercontent.com"):
        raise ValueError("bu bir Google OAuth istemci dosyası değil (client_id bulunamadı)")
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
    say("Google Takvim + Gmail kurulumu. Her adımda sayfa tarayıcıda açılacak; işin bitince Enter'a bas.")
    if os.environ.get("GOOGLE_CLIENT_ID", "").strip():
        say("Google istemcisi zaten ayarlı, Cloud adımları atlanıyor.")
    else:
        for i, (title, url, how) in enumerate(STEPS, 1):
            say(f"\n{i}/{len(STEPS)} {title}: {how}")
            open_url(url)
            if ask("  bitince Enter (atlamak için 's', çıkmak için 'q') > ").strip().lower() == "q":
                say("Yarıda bırakıldı. Tekrar: alice setup google")
                return 1
        cid = secret = ""
        found = find_downloaded_json(downloads)
        if found:
            ans = ask(f"\nİndirilenlerde {found.name} bulundu. Bunu kullanayım mı? [E/h] > ").strip().lower()
            if ans in ("", "e", "evet", "y"):
                try:
                    cid, secret = read_client_json(found.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    say(f"okunamadı: {e}")
        while not cid:
            raw = ask("\nİndirdiğin JSON dosyasının yolunu ya da Client ID'yi yapıştır (boş = çık) > ").strip().strip('"')
            if not raw:
                say("İstemci girilmedi. Tekrar: alice setup google")
                return 1
            try:
                if raw.endswith(".json") or raw.startswith("{"):
                    cid, secret = read_client_json(Path(raw).read_text(encoding="utf-8") if raw.endswith(".json")
                                                   else raw)
                elif raw.endswith(".apps.googleusercontent.com"):
                    cid = raw
                    secret = ask("Client secret'ı yapıştır > ").strip()
                else:
                    say("Bu bir Client ID ya da JSON dosyası gibi görünmüyor (….apps.googleusercontent.com olmalı).")
            except (OSError, ValueError) as e:
                say(f"okunamadı: {e}")
        set_env(env_path, "GOOGLE_CLIENT_ID", cid)
        if secret:
            set_env(env_path, "GOOGLE_CLIENT_SECRET", secret)
        say(f"İstemci kaydedildi: {env_path}")

    if ask("\nGoogle Takvim'i bağlayayım mı? [E/h] > ").strip().lower() in ("", "e", "evet", "y"):
        ok = connect_calendar() if connect_calendar else False
        say("Takvim bağlandı." if ok else "Takvim bağlanamadı; sonra tekrar dene: alice connect google_calendar")
    connected = []
    while True:
        acc = ask("\nBağlanacak Gmail hesabına bir ad ver (ör. kisisel, okul; boş = bitir) > ").strip().lower()
        if not acc:
            break
        if not re.fullmatch(r"[a-z0-9_-]{1,30}", acc):
            say("Ad sadece harf, rakam, - ve _ olsun (Türkçe karakter yok), ör. okul.")
            continue
        say("Tarayıcıda o hesabı seç (test kullanıcısı olarak eklediğin adreslerden biri olmalı).")
        email = connect_gmail(acc) if connect_gmail else ""
        if email is None:
            continue
        connected.append(f"{acc}" + (f" ({email})" if email else ""))
        say(f"bağlandı: {connected[-1]}")
    say("\nBitti. " + (f"Gmail: {', '.join(connected)}. " if connected else "") +
        "Kontrol için: alice doctor")
    return 0


def setup_cmd(argv: list[str]) -> int:
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    if not argv or argv[0] != "google":
        console.print("kullanım: alice setup google   (Google Takvim + Gmail kurulumu)")
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
            console.print(f"[red]bağlanamadı: {escape(str(e))}[/]")
            return None

    return run(lambda q: console.input(q), lambda s: console.print(escape(s)), connect_calendar=connect_calendar,
               connect_gmail=connect_gmail)
