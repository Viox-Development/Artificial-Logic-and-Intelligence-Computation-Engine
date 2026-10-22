from __future__ import annotations

import base64
import json
import time

import pytest

from alice.connect import store
from alice.tools import ToolContext
from alice.tools import gmail
from alice.tools.base import ToolError
from tests.conftest import make_config


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class Resp:
    def __init__(self, data, code=200):
        self._data, self.status_code = data, code
        self.content = json.dumps(data).encode()
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeGmail:
    """İki hesap: kişisel ve okul; her birinin kendi kutusu."""

    def __init__(self):
        self.calls = []
        self.boxes = {
            "tok-k": [{"id": "k1", "from": "Annem <anne@x.com>", "subject": "Akşam yemeği", "unread": True}],
            "tok-o": [{"id": "o1", "from": "\"Hoca\" <hoca@okul.edu>", "subject": "Proje teslimi", "unread": True,
                       "reply": "msg-1@okul"}],
        }

    def _box(self, headers):
        return self.boxes[headers["Authorization"].split()[-1]]

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        self.calls.append((method, url, json, headers["Authorization"]))
        box = self._box(headers)
        path = url.split("/users/me", 1)[1]
        if method == "GET" and path == "/messages":
            return Resp({"messages": [{"id": m["id"]} for m in box]})
        if method == "GET" and path.startswith("/messages/"):
            m = next(x for x in box if x["id"] == path.split("/")[2])
            hs = [{"name": "From", "value": m["from"]}, {"name": "Subject", "value": m["subject"]},
                  {"name": "Message-ID", "value": m.get("reply", "")}]
            return Resp({"id": m["id"], "threadId": "t-" + m["id"], "snippet": "kısa özet &amp; detay",
                         "labelIds": ["UNREAD", "INBOX"] if m["unread"] else ["INBOX"], "internalDate": "1761130000000",
                         "payload": {"headers": hs, "mimeType": "multipart/alternative", "parts": [
                             {"mimeType": "text/plain", "body": {"data": b64("Merhaba,\nproje cuma teslim.")}}]}})
        if method == "POST" and path == "/drafts":
            return Resp({"id": "d-1"})
        if method == "POST" and path == "/drafts/send":
            return Resp({"id": "sent-1"})
        if method == "POST" and path.endswith("/modify"):
            return Resp({})
        return Resp({"error": "?"}, 404)


@pytest.fixture
def two_accounts(monkeypatch):
    store.save("gmail:kisisel", access_token="tok-k", expires_at=time.time() + 3600, email="hayat@gmail.com")
    store.save("gmail:okul", access_token="tok-o", expires_at=time.time() + 3600, email="hayat@okul.edu")
    fake = FakeGmail()
    import httpx

    monkeypatch.setattr(httpx, "request", fake.request)
    return fake


def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_accounts_and_search_across_all(two_accounts, workdir):
    tool = gmail.Gmail()
    assert sorted(gmail.accounts()) == ["kisisel", "okul"]
    assert "kisisel, okul" in tool.description or "okul, kisisel" in tool.description
    assert "hayat@okul.edu" in tool.run(ctx(workdir), "accounts").text
    out = tool.run(ctx(workdir), "search", query="is:unread").text
    assert "[kisisel] id=k1" in out and "[okul] id=o1" in out and "● Hoca" in out and "kısa özet & detay" in out


def test_read_needs_account_when_ambiguous(two_accounts, workdir):
    tool = gmail.Gmail()
    with pytest.raises(ToolError, match="hangi hesap"):
        tool.run(ctx(workdir), "read", id="o1")
    out = tool.run(ctx(workdir), "read", account="okul", id="o1").text
    assert "Proje teslimi" in out and "proje cuma teslim" in out


def test_reply_draft_then_send_uses_right_account(two_accounts, workdir):
    tool = gmail.Gmail()
    out = tool.run(ctx(workdir), "draft", account="okul", id="o1", body="Hocam teslim ediyorum.").text
    assert "draft_id=d-1" in out and "Re: Proje teslimi" in out and "hoca@okul.edu" in out
    method, url, body, auth = next(c for c in two_accounts.calls if c[0] == "POST" and c[1].endswith("/drafts"))
    raw = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
    assert "In-Reply-To: msg-1@okul" in raw and body["message"]["threadId"] == "t-o1" and auth == "Bearer tok-o"
    assert tool.risk_for({"action": "send"}) == "send" and tool.risk_for({"action": "draft"}) == "write"
    assert "gönderildi [okul]" in tool.run(ctx(workdir), "send", account="okul", draft_id="d-1").text


def test_not_connected_account(two_accounts, workdir):
    with pytest.raises(ToolError, match="bağlı değil"):
        gmail.Gmail().run(ctx(workdir), "search", account="iş")


def test_important_unread_and_new_mail_notifications(two_accounts):
    lines = gmail.important_unread(limit=5)
    assert "okul: Hoca — Proje teslimi" in lines and any(x.startswith("kisisel:") for x in lines)
    assert gmail.new_important(make_config({"brain": ["a:m"]})) == []  # ilk kontrol: sadece hatırla
    two_accounts.boxes["tok-o"].append({"id": "o2", "from": "Müdür <m@okul.edu>", "subject": "Toplantı", "unread": True})
    assert gmail.new_important(make_config({"brain": ["a:m"]})) == ["okul: Müdür — Toplantı"]
    assert gmail.new_important(make_config({"brain": ["a:m"]})) == []


def test_body_text_html_fallback():
    payload = {"mimeType": "text/html", "body": {"data": b64("<p>Selam<br>dünya</p><script>x()</script>")}}
    assert gmail.body_text(payload).split() == ["Selam", "dünya"]
