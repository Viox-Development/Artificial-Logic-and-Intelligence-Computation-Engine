from __future__ import annotations

from datetime import datetime

from alice import health_report as hr
from alice.core.store import Store
from alice.doctor import Check


def _now(hour=10, day=20):
    return datetime(2026, 10, day, hour).timestamp()


def test_report_says_new_problems_once_then_fixed(tmp_path):
    store = Store(":memory:")
    now = _now()
    for i in range(4):
        store.add_event("error", "takvim takibi hata verdi", "HTTP 404", ts=now - 100 * i)
    store.add("pc.disk.c.percent", 95.0, ts=now - 60)
    usage = {"2026-10-20": {"nvidia:glm": {"requests": 1, "errors": 9}, "gemini:ok": {"requests": 50, "errors": 2}}}
    checks = [Check("Gmail", "fail", "token yenilenemedi", "tekrar bağla"), Check("OCR", "off", "kapalı")]
    sent = []
    send = lambda t, b: sent.append((t, b)) or True  # noqa: E731
    path = tmp_path / "st.json"
    kw = dict(doctor_checks=lambda: checks, usage=usage, path=path)

    body = hr.report(None, store, send, now=now, **kw)
    assert sent[0][0] == "ALICE sağlık raporu: 4 sorun"
    assert "Gmail: token yenilenemedi (çözüm: tekrar bağla)" in body and "OCR" not in body
    assert "takvim takibi hata verdi (4 kez" in body and "C: diski %95 dolu" in body
    assert "nvidia:glm" in body and "gemini:ok" not in body

    assert hr.report(None, store, send, now=now + 3600, **kw) is None  # not checked before the day is over
    body = hr.report(None, store, send, now=now + 86400, **kw)  # the ongoing problem (Gmail) is not repeated
    assert body == "Düzelenler: takvim takibi hata verdi, C"  # errors older than 24 hours and disk space that was freed
    checks.clear()
    body = hr.report(None, store, send, now=now + 2 * 86400, **kw)
    assert body == "Düzelenler: Gmail, nvidia:glm" and len(sent) == 3


def test_report_waits_for_morning_and_retries_when_muted(tmp_path):
    store = Store(":memory:")
    kw = dict(doctor_checks=lambda: [Check("Takvim", "fail", "404")], usage={}, path=tmp_path / "st.json")
    sent = []
    assert hr.report(None, store, lambda t, b: True, now=_now(hour=7), **kw) is None
    assert hr.report(None, store, lambda t, b: False, now=_now(), **kw)  # flight mode: muted
    hr.report(None, store, lambda t, b: sent.append(b) or True, now=_now() + 600, **kw)
    assert sent and "Takvim: 404" in sent[0]  # the same report goes out without repeating the check
    assert hr.report(None, store, lambda t, b: sent.append(b) or True, now=_now() + 1200, **kw) is None


def test_summary_now_when_all_good():
    assert hr.summary_now(None, Store(":memory:"), doctor_checks=lambda: [], usage={}).startswith("Her şey çalışıyor")
