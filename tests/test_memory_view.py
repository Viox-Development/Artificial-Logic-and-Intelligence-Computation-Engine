"""Ayarlar > Hafıza: bilgileri tek tek silme ve eski rolden ("sevgili/mommy") kalan izleri temizleme."""

from __future__ import annotations

import httpx
import pytest

from alice.app.server import AppServer, AppState
from alice.memory import LongMemory
from tests.conftest import FakeProvider, make_config


def seeded() -> LongMemory:
    mem = LongMemory(":memory:", {})
    mem.remember("Kullanıcı DCS'de F-16 uçuruyor.", "fact")
    mem.remember("Kullanıcıya bebeğim diye hitap et.", "preference")
    mem.record("nasılsın", "Buradayım bebeğim, nasıl yardımcı olabilirim?")
    mem.record("saat kaç", "Saat beş.")
    mem.add_summary(0, 1, "Kullanıcı sevgilisiyle sohbet etti, ona aşkım dedi.")
    mem.add_summary(2, 3, "Kullanıcı Unity projesinde çarpışma hatasını çözdü.")
    return mem


def test_old_role_traces_found_and_purged_but_user_words_kept():
    mem = seeded()
    old = mem.old_role()
    assert [f.text for f in old["facts"]] == ["Kullanıcıya bebeğim diye hitap et."]
    assert len(old["summaries"]) == 1 and len(old["episodes"]) == 1
    assert mem.purge_old_role() == {"facts": 1, "summaries": 1, "episodes": 1}
    assert [f.text for f in mem.facts()] == ["Kullanıcı DCS'de F-16 uçuruyor."]
    eps = mem.recent(5)
    assert [e.user for e in eps] == ["nasılsın", "saat kaç"] and eps[0].reply == "" and eps[1].reply == "Saat beş."
    assert mem.old_role() == {"facts": [], "summaries": [], "episodes": []}


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    state.agent.memory = seeded()
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client
    srv.stop()


def test_memory_tab_lists_deletes_and_purges(app):
    state, client = app
    view = client.get("/api/memory").json()
    assert view["enabled"] and view["old_role"]["facts"] == 1 and view["old_role"]["episodes"] == 1
    old = next(f for f in view["facts"] if f["old"])
    assert old["kind"] == "tercih" and "bebeğim" in old["text"]
    keep = next(f for f in view["facts"] if not f["old"])
    assert client.post("/api/memory/action", json={"action": "forget", "id": keep["id"]}).json()["ok"]
    assert client.post("/api/memory/action", json={"action": "forget", "id": 9999}).status_code == 400
    r = client.post("/api/memory/action", json={"action": "purge_old_role"}).json()
    assert r["removed"] == {"facts": 1, "summaries": 1, "episodes": 1}
    view = client.get("/api/memory").json()
    assert view["facts"] == [] and sum(v for k, v in view["old_role"].items() if k != "examples") == 0
