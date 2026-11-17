from __future__ import annotations

import sqlite3
import time

import httpx

from alice.app.phone import PhoneServer
from alice.app.server import AppState
from alice.core.store import Store
from alice.health.watch import WatchMonitor, cadence, import_gadgetbridge, ingest
from tests.conftest import FakeProvider, make_config


def make_db(path, rows, ms=False):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS HUAWEI_ACTIVITY_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, "
                "STEPS INTEGER, HEART_RATE INTEGER, RAW_KIND INTEGER)")
    con.executemany("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?, 1, ?, ?, 0)",
                    [(t * 1000 if ms else t, s, hr) for t, s, hr in rows])
    con.commit()
    con.close()


def test_import_incremental(tmp_path):
    db = tmp_path / "Gadgetbridge.db"
    now = int(time.time())
    make_db(db, [(now - 600, 12, 72), (now - 540, 0, 255), (now - 480, 30, 75)])
    st = Store(":memory:")
    assert import_gadgetbridge(db, st) == {"watch.hr": 2, "watch.steps": 2}
    assert import_gadgetbridge(db, st) == {}                      # does not import again
    make_db(db, [(now - 60, 5, 80)])
    assert import_gadgetbridge(db, st) == {"watch.hr": 1, "watch.steps": 1}
    s = st.stats("watch.steps")
    assert s.total == 47 and "toplam 47" in s.line()


def test_cadence_verdicts():
    st = Store(":memory:")
    now = time.time()
    for i in range(20):
        st.add("watch.hr", 70 + i % 5, ts=now - 3600 + i * 180)
    assert "birkaç dakikada bir" in cadence(st)["verdict"]
    st2 = Store(":memory:")
    for i in range(60):
        st2.add("watch.hr", 90, ts=now - 60 + i)
    assert "canlı" in cadence(st2)["verdict"]
    assert cadence(Store(":memory:"))["verdict"].startswith("veri yok")


def test_ingest_live():
    st = Store(":memory:")
    assert ingest(st, {"hr": 88}) == 1
    assert ingest(st, {"samples": [{"hr": 300}, {"hr": "91", "ts": 1_700_000_000_000, "stress": 40}]}) == 2
    assert st.last("watch.hr")[1] == 88 and st.series("watch.hr", since=0)[0] == (1_700_000_000.0, 91.0)


def test_watch_monitor_imports_folder(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    folder.mkdir()
    make_db(folder / "Gadgetbridge.db", [(int(time.time()) - 100, 10, 70)])
    st = Store(":memory:")
    mon = WatchMonitor(st, str(folder))
    monkeypatch.setattr("alice.health.watch.time.time", lambda: time.monotonic() + 1e10)  # the file is "eski" (old)
    mon.collect()
    assert st.last("watch.hr")[1] == 70


def test_phone_server_watch_and_hosts(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    st = Store(":memory:")
    ps = PhoneServer(cfg, st, port=0, state=state)
    try:
        port = ps.server.httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        assert ps.url.endswith(f"/m?t={state.token}") and ps.ip in state.allowed_hosts
        assert httpx.post(base + "/api/watch", json={"hr": 90}).status_code == 403
        r = httpx.post(base + "/api/watch", json={"hr": 90}, headers={"X-Alice-Token": state.token})
        assert r.json() == {"ok": True, "added": 1} and st.last("watch.hr")[1] == 90
        assert httpx.get(base + "/api/state", headers={"X-Alice-Token": state.token, "Host": "evil.com"}).status_code == 403
        assert httpx.get(f"{base}/quick?t={state.token}").status_code == 200
        # the key is persistent
        from alice.app.phone import token
        assert token() == state.token
    finally:
        ps.stop()
