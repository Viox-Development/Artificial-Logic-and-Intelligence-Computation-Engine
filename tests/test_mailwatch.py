from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from alice.extras import mailwatch
from alice.memory import LongMemory


class Router:
    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def chat(self, role, msgs, **k):
        self.calls += 1
        content = msgs[0]["content"]
        for key, ans in self.answers.items():
            if key in content:
                return SimpleNamespace(message={"content": json.dumps(ans)})
        return SimpleNamespace(message={"content": '{"kind": "none"}'})


def test_bills_and_dates_become_reminders_once(tmp_path):
    now = datetime(2026, 11, 2, 14)
    router = Router({
        "Subject: Faturanız": {"kind": "bill", "title": "Turkcell faturası", "due": "2026-11-10", "amount": "349,90 TL"},
        "Subject: Ödev": {"kind": "date", "title": "Algoritma ödevi teslimi", "due": "2026-11-03T23:59", "amount": None},
        "Subject: Eski": {"kind": "bill", "title": "Eski fatura", "due": "2026-10-01", "amount": None},  # past: not picked up
    })
    mails = [("okul", "1", "Turkcell", "Faturanız", "Turkcell faturanız hazır"),
             ("okul", "2", "Hoca", "Ödev", "Ödev teslimi yarın"),
             ("okul", "3", "X", "Eski", "Eski"),
             ("okul", "4", "Trendyol", "Kampanya", "indirim")]
    mem = LongMemory(":memory:", {})
    sent = []
    kw = dict(path=tmp_path / "m.json", fetch=lambda: mails)
    added = mailwatch.scan(None, router, mem, lambda t, b: sent.append(b) or True, now=now.timestamp(), **kw)
    assert added == ["Turkcell faturası son ödeme 10.11, 349,90 TL.", "Algoritma ödevi teslimi: 03.11 23:59."]
    fups = {f.text: f.due for f in mem.followups()}
    assert datetime.fromtimestamp(fups[added[0]]) == datetime(2026, 11, 8, 10)  # 2 days before, in the morning
    assert datetime.fromtimestamp(fups[added[1]]) == datetime(2026, 11, 2, 14, 5)  # time already passed: immediately
    assert "Takvime de eklememi istersen söyle." in sent[0]
    calls = router.calls
    assert mailwatch.scan(None, router, mem, lambda t, b: True, now=now.timestamp() + 600, **kw) == []  # 2 hours
    assert mailwatch.scan(None, router, mem, lambda t, b: True, now=now.timestamp() + 8000, **kw) == []
    assert router.calls == calls  # a mail already read never goes to the model again
