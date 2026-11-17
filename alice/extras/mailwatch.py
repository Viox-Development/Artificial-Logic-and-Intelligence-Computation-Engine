"""Date extraction from e-mail: invoice due dates and school/internship dates become reminders.

Every two hours, the last 3 days of invoice, statement, homework, exam and internship mails in all connected Gmail
accounts are checked (promotions tab excluded). Each new mail is read once by the fast model, and only a date that
is EXPLICITLY written in the mail is taken:
  * invoice/subscription/statement -> reminder at 10:00, 2 days before due ("Turkcell faturası 2 gün sonra, 349 TL")
  * homework submission, exam, internship interview, application deadline -> reminder at 10:00, 1 day before
Reminders are the "remind" follow-ups of memory: when the day comes there is a notification, and they also show in
chat. Everything found is also announced once; adding it to the calendar would write to an external account, so it is
not done automatically, only offered if asked.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

EVERY = 2 * 3600
QUERY = ('newer_than:3d -category:promotions -category:social (fatura OR "son ödeme" OR ekstre OR invoice OR '
         'abonelik OR ödev OR teslim OR sınav OR vize OR final OR staj OR mülakat OR görüşme OR başvuru OR deadline '
         'OR "due date")')

PROMPT = """Bu e-postada kullanıcının kaçırmaması gereken bir TARİH var mı? Sadece mailde açıkça yazan tarihi al, \
tahmin etme. Bugün: {today}.
Türler:
- "bill": kullanıcının KENDİSİNİN ödemesi gereken fatura ya da kredi kartı ekstresi (son ödeme tarihi)
- "date": ödev/proje teslimi, sınav, staj ya da iş görüşmesi, başvuru son günü, randevu
- "none": reklam, bülten, tarihsiz bilgi, geçmiş tarih, ödeme makbuzu/"ödemeniz alındı", kartdan OTOMATİK çekilen abonelik yenilemesi (YouTube Premium, Claude, Netflix gibi: kullanıcı bir şey yapmayacak)
Tarih biçimi: İngilizce mailde "11/04/2026" ay/gün (4 Kasım) olabilir; ayı yazıyla geçen tarihi tercih et, emin değilsen "none".
Sadece JSON yaz: {{"kind": "bill|date|none", "title": "kısa ad, ör. 'Turkcell faturası' ya da 'Algoritma ödevi \
teslimi'", "due": "YYYY-MM-DD ya da YYYY-MM-DDTHH:MM", "amount": "349,90 TL ya da null"}}

Gönderen: {sender}
Konu: {subject}
Metin:
{body}"""


def state_path() -> Path:
    return alice_home() / "mailwatch.json"


def extract(router, sender: str, subject: str, body: str, today: datetime) -> dict[str, Any] | None:
    from alice.memory import parse_json

    res = router.chat("fast", [{"role": "user", "content": PROMPT.format(
        today=today.strftime("%Y-%m-%d %A"), sender=sender, subject=subject, body=body[:3000])}],
        temperature=0.0, max_tokens=200)
    data = parse_json(res.message.get("content") or "")
    if data.get("kind") not in ("bill", "date") or not data.get("title") or not data.get("due"):
        return None
    try:
        due = datetime.fromisoformat(str(data["due"]).strip())
    except ValueError:
        return None
    horizon = 60 if data["kind"] == "bill" else 200  # bills are monthly: a "son ödeme" over 2 months away is misread
    if due.date() < today.date() or due > today + timedelta(days=horizon):
        return None
    amount = data.get("amount")
    return {"kind": data["kind"], "title": str(data["title"])[:80], "due": due,
            "amount": str(amount)[:30] if amount and str(amount).lower() not in ("null", "none") else ""}


def remind_at(item: dict[str, Any], now: datetime) -> float:
    days = 2 if item["kind"] == "bill" else 1
    at = (item["due"] - timedelta(days=days)).replace(hour=10, minute=0, second=0, microsecond=0)
    return max(at, now + timedelta(minutes=5)).timestamp()  # if the time has already passed, remind right away


def reminder_text(item: dict[str, Any]) -> str:
    when = item["due"].strftime("%d.%m") + (item["due"].strftime(" %H:%M") if item["due"].hour else "")
    if item["kind"] == "bill":
        return f"{item['title']} son ödeme {when}" + (f", {item['amount']}" if item["amount"] else "") + "."
    return f"{item['title']}: {when}."


def messages(cfg, query: str = QUERY, client: Any = None) -> list[tuple[str, str, str, str, str]]:
    """(account, id, sender, subject, body) — from all connected Gmail accounts."""
    from alice.tools import gmail

    out = []
    for acc in gmail.accounts():
        try:
            for m in gmail.search(acc, query, 15, client):
                full = gmail._request(acc, "GET", f"/messages/{m['id']}", client, params={"format": "full"})
                h = gmail._headers(full)
                out.append((acc, m["id"], gmail._short_from(h.get("from", "")), h.get("subject", ""),
                            gmail.body_text(full.get("payload") or {}) or full.get("snippet", "")))
        except Exception:  # noqa: BLE001 - keep going with the other accounts if one fails
            continue
    return out


def scan(cfg, router, mem, notify: Callable[[str, str], bool], now: float | None = None, force: bool = False,
         path: Path | None = None, fetch: Callable[[], list] | None = None) -> list[str]:
    """If it is time, scan for new mails; return the texts of the reminders added."""
    now = time.time() if now is None else now
    path = path or state_path()
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if router is None or mem is None or (not force and now - float(st.get("last") or 0) < EVERY):
        return []
    seen = set(st.get("seen") or [])
    today = datetime.fromtimestamp(now)
    added = []
    query = QUERY if st.get("last") else QUERY.replace("newer_than:3d", "newer_than:30d")  # first scan: last month
    for _acc, mid, sender, subject, body in (fetch or (lambda: messages(cfg, query)))():
        if mid in seen:
            continue
        seen.add(mid)
        try:
            item = extract(router, sender, subject, body, today)
        except Exception:  # noqa: BLE001 - if the model fails, don't leave this mail for the next scan; skip it
            continue
        if item is None:
            continue
        text = reminder_text(item)
        mem.add_followup(text, remind_at(item, today), "remind")
        added.append(text)
    st["last"] = now
    st["seen"] = sorted(seen)[-500:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass
    if added:
        notify("E-postadan hatırlatma kuruldu", "\n".join(added) + "\nTakvime de eklememi istersen söyle.")
    return added
