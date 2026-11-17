"""Subscription quota warning: once per threshold, again after window reset; mentions the ready backup account."""
from __future__ import annotations

from alice.quota_watch import QuotaWatch


def acc(name, pct, reset=2000000000, ready=True, label="5 saat"):
    return {"name": name, "email": f"{name}@x", "provider": "claude", "provider_name": "Claude", "ready": ready,
            "quota": {"percent": pct, "windows": [{"label": label, "percent": pct, "reset": reset}]}}


def test_warns_once_per_threshold_and_mentions_backup():
    w = QuotaWatch(lambda s: None)
    assert w.check([acc("a", 50), acc("b", 10)]) == []
    msgs = w.check([acc("a", 82), acc("b", 10)])
    assert len(msgs) == 1 and "%82" in msgs[0] and "Diğer Claude hesabı hazır" in msgs[0]
    assert w.check([acc("a", 85), acc("b", 10)]) == []  # the same threshold is not announced again
    assert len(w.check([acc("a", 96), acc("b", 10)])) == 1  # a higher threshold
    assert len(w.check([acc("a", 81, reset=2000018000), acc("b", 10)])) == 1  # window reset, new window


def test_no_backup_and_disabled_accounts():
    w = QuotaWatch(lambda s: None)
    msgs = w.check([acc("a", 90), acc("b", 90)])
    assert len(msgs) == 2 and all("Başka hazır Claude hesabı yok" in m for m in msgs)
    assert w.check([{**acc("c", 99), "disabled": True}]) == []


def test_maybe_check_is_throttled_and_survives_proxy_errors():
    import threading

    got, done = [], threading.Event()

    def fetch():
        done.set()
        raise OSError("proxy kapalı")

    w = QuotaWatch(got.append, fetch=fetch, interval=300)
    assert w.maybe_check(now=1000.0) and done.wait(2)
    assert not w.maybe_check(now=1100.0) and w.maybe_check(now=1400.0)
    assert got == []
