"""Ayarlar → Notlar ve Kayıtlar: alice notes / brief / report / log / eval uygulamadan."""

from __future__ import annotations

from alice import actionlog, evals
from alice.app.journal import Journal
from alice.config import Config, default_config_dict
from alice.core.schedule import TaskStore
from alice.extras import notes


def j():
    return Journal(Config(default_config_dict()))


def test_notes_add_search_remove():
    p = j()
    assert not p.notes_action("add", {"text": "  "})["ok"]
    p.notes_action("add", {"text": "yarın Ayşe'yi ara"})
    p.notes_action("add", {"text": "F-16 bingo yakıtı 2000"})
    v = p.notes_view()
    assert v["total"] == 2 and v["notes"][0]["text"].startswith("F-16")  # en yeni üstte
    assert [n["text"] for n in p.notes_view("ayse")["notes"]] == ["yarın Ayşe'yi ara"]  # Türkçe harfsiz arama
    ts = v["notes"][1]["ts"]
    assert p.notes_action("remove", {"ts": ts})["ok"] and [n.text for n in notes.all_notes()] == ["F-16 bingo yakıtı 2000"]
    assert not p.notes_action("remove", {"ts": 1.0})["ok"]


def test_brief_and_report_install_show_remove():
    p = j()  # şehir ayarlı değil: hava için ağa çıkılmaz
    assert p.notes_view()["brief"] is None
    assert p.notes_action("brief_install", {"at": "7:45", "weekdays": True})["ok"]
    b = p.notes_view()["brief"]
    assert b["at"] == "07:45" and b["weekdays"] and TaskStore().get("brief").cron == "45 7 * * 1-5"
    assert not p.notes_action("brief_install", {"at": "25:00"})["ok"]
    assert "bugün:" in p.notes_action("brief_show", {})["text"]
    assert p.notes_action("report_install", {"at": "21:30"})["ok"] and p.notes_view()["report"]["at"] == "21:30"
    r = p.notes_action("report_show", {"days": 7})
    assert r["ok"] and r["text"].startswith("Son 7 gün") and r["path"].endswith(".html")
    assert p.notes_action("brief_remove", {})["ok"] and p.notes_view()["brief"] is None
    assert not p.notes_action("uç", {})["ok"]


def test_logs_show_sent_and_feedback():
    actionlog.log("discord_send", "Ayşe'ye mesaj: geliyorum", "sent")
    actionlog.log("gmail_send", "patrona e-posta", "rejected", detail="kullanıcı reddetti")
    evals.record_feedback("hava nasıl", "Buradayım efendim", "soruma cevap vermedi", source="uygulama")
    v = j().logs_view()
    assert [s["status"] for s in v["sent"]] == ["reddedildi", "gönderildi"]  # en yeni üstte
    assert v["feedback"][0]["say"] == "hava nasıl" and v["cases"] == 1 and v["last_run"] is None
    assert v["run"]["state"] == "idle"


def test_eval_runs_in_background_with_progress(monkeypatch):
    p = j()
    assert not p.logs_action("eval_run", {})["ok"]  # boş set
    assert p.logs_action("eval_add", {"text": "saat kaç"})["added"]
    assert p.logs_action("eval_add", {"text": "hava nasıl"})["ok"]
    seen = []

    def fake_run_all(cases, on_case=lambda i, o: None, stop=lambda: False, cfg=None):
        out = []
        for i, c in enumerate(cases, 1):
            o = evals.Outcome(c, passed=c.say == "saat kaç", reply="beşi geçiyor", tools=[], reasons=[] if
                              c.say == "saat kaç" else ["beklenen yok"], seconds=0.1, model="fake")
            out.append(o)
            on_case(i, o)
            seen.append(p.eval_view()["done"])
        return out

    monkeypatch.setattr(evals, "run_all", fake_run_all)
    assert p.logs_action("eval_run", {})["ok"]
    p._eval_thread.join(5)
    v = p.eval_view()
    assert v["state"] == "done" and seen == [1, 2] and v["summary"].startswith("1/2 geçti")
    assert [r["passed"] for r in v["results"]] == [True, False]
    assert p.logs_view()["last_run"]["passed"] == 1


def test_eval_from_log_and_endpoints(workdir):
    import httpx

    from alice.app.server import AppServer, AppState
    from alice.config import alice_home
    from tests.conftest import FakeProvider, make_config

    alice_home().mkdir(parents=True, exist_ok=True)
    (alice_home() / "voice.log").write_text("10:00 duyulan [groq]: YouTube'u aç ve şarkı çal\n", encoding="utf-8")
    c = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(c, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    srv = AppServer(state).start()
    try:
        client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        assert client.post("/api/logs/action", json={"action": "eval_from_log", "n": 5}).json()["added"] == 1
        assert client.get("/api/logs").json()["cases"] == 1
        assert client.get("/api/logs/eval").json()["state"] == "idle"
        assert client.post("/api/notes/action", json={"action": "add", "text": "süt al"}).json()["ok"]
        assert client.get("/api/notes", params={"q": "sut"}).json()["notes"][0]["text"] == "süt al"
        assert client.post("/api/notes/action", json={"action": "x"}).status_code == 400
    finally:
        srv.stop()
