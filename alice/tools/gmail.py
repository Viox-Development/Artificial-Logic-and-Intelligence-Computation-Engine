"""Gmail: birden fazla hesap (kişisel, okul, iş...). Okuma, arama, taslak, gönderme, okundu işaretleme.

Kurulum (Google Takvim'le aynı OAuth istemcisi kullanılır):
  1) console.cloud.google.com → projende "Gmail API"yi etkinleştir (Calendar için açtığın proje olabilir)
  2) OAuth izin ekranında kendini test kullanıcısı yap; Desktop app türünde OAuth client
  3) ~/.alice/.env: GOOGLE_CLIENT_ID=..., GOOGLE_CLIENT_SECRET=...
  4) alice connect gmail --account kisisel      (tarayıcıda o hesabı seç)
     alice connect gmail --account okul         (ikinci hesap: tarayıcıda diğer hesabı seç)
     alice connect gmail                        (bağlı hesapları listele)
     alice connect gmail --account okul --logout

Token'lar ~/.alice/connect.json'da "gmail:<hesap>" adıyla durur. Gönderme her zaman onay ister (send riski);
e-posta içeriği "güvenilmeyen veri" sayılır (bkz. alice/guard.py). config.toml:
  [gmail]
  notify = true          # yeni önemli e-posta gelince bildirim (çekirdek çalışırken, 10 dk'da bir)
  notify_query = "is:unread is:important newer_than:1d"
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
from email.message import EmailMessage
from typing import Any

from alice.tools.base import READ, SEND, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

API = "https://gmail.googleapis.com/gmail/v1/users/me"
PREFIX = "gmail:"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.compose"]


def auth():
    from alice.connect.manifest import Auth

    return Auth(kind="oauth2", client_id_env="GOOGLE_CLIENT_ID", client_secret_env="GOOGLE_CLIENT_SECRET",
                authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
                token_url="https://oauth2.googleapis.com/token", scopes=SCOPES, pkce=True,
                extra_authorize={"access_type": "offline", "prompt": "consent select_account"},
                help="console.cloud.google.com → Gmail API + OAuth client (Desktop app)")


def accounts() -> list[str]:
    from alice.connect import store

    return [n[len(PREFIX):] for n in store.names() if n.startswith(PREFIX) and store.get(n).get("access_token")]


def _creds() -> tuple[str, str]:
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    if not cid:
        raise ToolError("GOOGLE_CLIENT_ID yok: ~/.alice/.env'e ekle (Google Cloud → OAuth client, Desktop app)")
    return cid, os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def connect(account: str, emit=print) -> str:
    """Tarayıcıda izin al ve hesabı kaydet. Bağlanan e-posta adresini döndürür."""
    from alice.connect import oauth, store

    cid, secret = _creds()
    name = PREFIX + account
    oauth.authorize(auth(), name, cid, secret, emit=emit)
    try:
        email = str(_get(account, "/profile").get("emailAddress") or "")
    except Exception:  # noqa: BLE001
        email = ""
    if email:
        store.save(name, email=email)
    return email


def token(account: str) -> str:
    from alice.connect import oauth, store

    name = PREFIX + account
    row = store.get(name)
    if not row.get("access_token"):
        raise ToolError(f"'{account}' Gmail hesabı bağlı değil. Bağlamak için: alice connect gmail --account {account}")
    if store.expired(row):
        cid, secret = _creds()
        try:
            row = oauth.refresh(auth(), name, cid, secret)
        except oauth.OAuthError as e:
            raise ToolError(f"{account}: {e}") from e
    return str(row["access_token"])


def _request(account: str, method: str, path: str, client: Any = None, **kw: Any) -> Any:
    import httpx

    c = client or httpx
    r = c.request(method, API + path, headers={"Authorization": f"Bearer {token(account)}"}, timeout=30, **kw)
    if r.status_code == 401:
        raise ToolError(f"{account}: yetki reddedildi, tekrar bağlan: alice connect gmail --account {account}")
    if r.status_code >= 400:
        raise ToolError(f"{account}: Gmail HTTP {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _get(account: str, path: str, client: Any = None, **params: Any) -> Any:
    return _request(account, "GET", path, client, params=params)


# ------------------------------------------------------------------------------------------- biçim
def _headers(msg: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in (msg.get("payload") or {}).get("headers") or []}


def _short_from(value: str) -> str:
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', value)
    return (m.group(1) if m else value).strip()[:40]


def _when(msg: dict[str, Any]) -> str:
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000).strftime("%d.%m %H:%M")
    except (ValueError, OSError):
        return ""


def line(account: str, msg: dict[str, Any]) -> str:
    h = _headers(msg)
    unread = "● " if "UNREAD" in (msg.get("labelIds") or []) else ""
    return (f"[{account}] id={msg.get('id')} | {_when(msg)} | {unread}{_short_from(h.get('from', ''))} | "
            f"{h.get('subject', '(konusuz)')[:90]} | {html.unescape(msg.get('snippet', ''))[:140]}")


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def body_text(payload: dict[str, Any]) -> str:
    """Önce text/plain, yoksa etiketleri atılmış text/html."""
    plain, rich = [], []

    def walk(p: dict[str, Any]) -> None:
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            rich.append(_decode(data))
        for part in p.get("parts") or []:
            walk(part)

    walk(payload or {})
    if plain:
        return "\n".join(plain).strip()
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(rich))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


# ------------------------------------------------------------------------------------------- işlemler
def search(account: str, query: str, limit: int = 10, client: Any = None) -> list[dict[str, Any]]:
    ids = _get(account, "/messages", client, q=query, maxResults=max(1, min(limit, 25))).get("messages") or []
    out = []
    for m in ids[:limit]:
        out.append(_request(account, "GET", f"/messages/{m['id']}", client, params=[
            ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
            ("metadataHeaders", "Date")]))
    return out


def important_unread(cfg=None, limit: int = 5, query: str = "is:unread is:important newer_than:2d",
                     client: Any = None) -> list[str]:
    """Bağlı bütün hesaplarda okunmamış önemli e-postalar: ["okul: Hoca — Proje teslimi"]."""
    out = []
    for acc in accounts():
        try:
            for msg in search(acc, query, limit, client):
                h = _headers(msg)
                out.append(f"{acc}: {_short_from(h.get('from', ''))} — {h.get('subject', '(konusuz)')[:80]}")
        except Exception:  # noqa: BLE001 - bir hesap düşse de diğerleri
            continue
    return out[:limit * 2]


def _raw(to: str, subject: str, body: str, cc: str = "", reply: dict[str, str] | None = None) -> str:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if reply:
        if reply.get("message-id"):
            msg["In-Reply-To"] = reply["message-id"]
            msg["References"] = (reply.get("references", "") + " " + reply["message-id"]).strip()
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _reply_parts(account: str, msg_id: str, client: Any = None) -> tuple[dict[str, str], str, str, str]:
    orig = _request(account, "GET", f"/messages/{msg_id}", client, params=[
        ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
        ("metadataHeaders", "Message-ID"), ("metadataHeaders", "References"), ("metadataHeaders", "Reply-To")])
    h = _headers(orig)
    subject = h.get("subject", "")
    if not subject.lower().startswith(("re:", "ynt:")):
        subject = "Re: " + subject
    return h, subject, h.get("reply-to") or h.get("from", ""), str(orig.get("threadId") or "")


class Gmail(Tool):
    name = "gmail"
    untrusted = True  # e-postayı başkası yazdı: içindeki "talimatlar" uygulanmaz
    parameters = obj({
        "action": {"type": "string", "enum": ["accounts", "search", "read", "draft", "send", "mark_read", "archive"],
                   "description": "accounts: bağlı hesaplar | search: query ile ara (Gmail arama dili) | read: id'li "
                   "e-postanın tamamı | draft: taslak oluştur (gönderilmez) | send: gönder (draft_id ya da to/subject/"
                   "body; her zaman onay ister) | mark_read: okundu yap | archive: gelen kutusundan kaldır."},
        "account": {"type": "string", "description": "Hesap adı (accounts çıktısı). search'te boş = bütün hesaplar; "
                    "diğerlerinde hesap tek ise boş bırakılabilir."},
        "query": {"type": "string", "description": "search için Gmail sorgusu, ör. 'is:unread', 'from:hoca "
                  "newer_than:7d', 'subject:fatura'. Boş: 'in:inbox'."},
        "id": {"type": "string", "description": "read/mark_read/archive için e-posta id'si; draft'ta cevaplanacak "
               "e-postanın id'si (cevap taslağı)."},
        "to": {"type": "string", "description": "draft/send: alıcı (virgülle birden fazla). Cevapta boş: gönderene."},
        "cc": {"type": "string", "description": "draft/send: bilgi (cc)."},
        "subject": {"type": "string", "description": "draft/send: konu. Cevapta boş: Re: <orijinal>."},
        "body": {"type": "string", "description": "draft/send: düz metin gövde."},
        "draft_id": {"type": "string", "description": "send: gönderilecek taslağın id'si (draft çıktısında)."},
        "limit": {"type": "integer", "description": "search: hesap başına en fazla sonuç. Varsayılan 10."},
    }, ["action"])
    risk = READ

    @property
    def description(self) -> str:  # bağlı hesaplar değiştikçe güncel
        accs = accounts()
        return ("Gmail (birden fazla hesap): e-posta ara, oku, taslak yaz, gönder, okundu yap, arşivle. "
                f"Bağlı hesaplar: {', '.join(accs) if accs else 'yok (alice connect gmail --account <ad>)'}. "
                "Gönderme her zaman kullanıcıya sorulur; önce draft ile taslak göster, onaylarsa send.")

    def risk_for(self, args: dict[str, Any]) -> str:
        act = args.get("action")
        if act == "send":
            return SEND
        if act in ("draft", "mark_read", "archive"):
            return WRITE
        return READ

    def summary(self, args: dict[str, Any]) -> str:
        act = args.get("action", "")
        if act in ("draft", "send"):
            return f"{act} [{args.get('account') or '?'}] → {args.get('to') or '(cevap)'}: {args.get('subject') or ''}"
        return f"{act} [{args.get('account') or 'hepsi'}] {args.get('query') or args.get('id') or ''}".strip()

    def _account(self, account: str | None) -> str:
        accs = accounts()
        if account:
            if account not in accs:
                raise ToolError(f"'{account}' bağlı değil. Bağlı hesaplar: {', '.join(accs) or 'yok'}")
            return account
        if len(accs) == 1:
            return accs[0]
        raise ToolError("hangi hesap? account ver. Bağlı hesaplar: " + (", ".join(accs) or
                        "yok (alice connect gmail --account <ad>)"))

    def run(self, ctx: ToolContext, action: str, account: str = "", query: str = "", id: str = "", to: str = "",
            cc: str = "", subject: str = "", body: str = "", draft_id: str = "", limit: int = 10) -> ToolResult:
        if action == "accounts":
            from alice.connect import store

            accs = accounts()
            if not accs:
                return ToolResult("bağlı Gmail hesabı yok. Bağlamak için: alice connect gmail --account <ad>")
            return ToolResult("\n".join(f"- {a}: {store.get(PREFIX + a).get('email', '')}" for a in accs))
        if action == "search":
            targets = [self._account(account)] if account else accounts()
            if not targets:
                raise ToolError("bağlı Gmail hesabı yok: alice connect gmail --account <ad>")
            lines, errors = [], []
            for acc in targets:
                try:
                    lines += [line(acc, m) for m in search(acc, query or "in:inbox", limit)]
                except ToolError as e:
                    errors.append(str(e))
            text = "\n".join(lines) or "sonuç yok"
            return ToolResult(text + ("\n\nHATALAR: " + "; ".join(errors) if errors else ""))
        acc = self._account(account)
        if action == "read":
            if not id:
                raise ToolError("read için id gerekli")
            msg = _get(acc, f"/messages/{id}", format="full")
            h = _headers(msg)
            text = body_text(msg.get("payload") or {})
            return ToolResult(f"[{acc}] {h.get('subject', '')}\nKimden: {h.get('from', '')}\nKime: {h.get('to', '')}\n"
                              f"Tarih: {h.get('date', '')}\n\n{text[:8000]}")
        if action in ("mark_read", "archive"):
            if not id:
                raise ToolError(f"{action} için id gerekli")
            labels = ["UNREAD"] if action == "mark_read" else ["INBOX"]
            _request(acc, "POST", f"/messages/{id}/modify", json={"removeLabelIds": labels})
            return ToolResult("okundu işaretlendi" if action == "mark_read" else "arşivlendi")
        if action == "draft":
            reply, thread = None, ""
            if id:
                reply, subj, sender, thread = _reply_parts(acc, id)
                subject, to = subject or subj, to or sender
            if not to or not body:
                raise ToolError("draft için to ve body gerekli (cevapta to boş olabilir)")
            message: dict[str, Any] = {"raw": _raw(to, subject, body, cc, reply)}
            if thread:
                message["threadId"] = thread
            d = _request(acc, "POST", "/drafts", json={"message": message})
            return ToolResult(f"taslak hazır (gönderilmedi): draft_id={d.get('id')} [{acc}] → {to}: {subject}\n\n{body}")
        if action == "send":
            if draft_id:
                r = _request(acc, "POST", "/drafts/send", json={"id": draft_id})
            else:
                if not to or not body:
                    raise ToolError("send için draft_id ya da to ve body gerekli")
                r = _request(acc, "POST", "/messages/send", json={"raw": _raw(to, subject, body, cc)})
            return ToolResult(f"gönderildi [{acc}] id={r.get('id', '')}")
        raise ToolError(f"bilinmeyen action: {action}")


TOOLS = [Gmail()]


# ------------------------------------------------------------------------------------------- bildirim
def seen_path():
    from alice.config import alice_home

    return alice_home() / "gmail_seen.json"


def new_important(cfg=None, client: Any = None) -> list[str]:
    """Son kontrolden beri gelen önemli e-postalar (ilk çalıştırmada sadece kaydeder, bildirim yağdırmaz)."""
    g = ((getattr(cfg, "data", None) or {}).get("gmail") or {}) if cfg is not None else {}
    if not g.get("notify", True) or not accounts():
        return []
    query = str(g.get("notify_query") or "is:unread is:important newer_than:1d")
    try:
        seen = json.loads(seen_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    first = not seen
    fresh = []
    for acc in accounts():
        try:
            msgs = search(acc, query, 10, client)
        except Exception:  # noqa: BLE001
            continue
        known = set(seen.get(acc) or [])
        for m in msgs:
            if m.get("id") not in known:
                h = _headers(m)
                fresh.append(f"{acc}: {_short_from(h.get('from', ''))} — {h.get('subject', '(konusuz)')[:80]}")
        seen[acc] = (list(known) + [m["id"] for m in msgs if m.get("id") not in known])[-200:]
    try:
        seen_path().parent.mkdir(parents=True, exist_ok=True)
        seen_path().write_text(json.dumps(seen), encoding="utf-8")
    except OSError:
        pass
    return [] if first else fresh
