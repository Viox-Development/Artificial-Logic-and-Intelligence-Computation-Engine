"""Ayarlar → Otomasyon: add a rule/task from a sentence (preview first), try feeds (alice rules/tasks/feeds)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from alice.app.automations import Automations
from alice.core.rules import RuleSet
from alice.core.schedule import TaskStore


class Router:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat(self, role, msgs, **kw):
        self.calls += 1
        return SimpleNamespace(text=json.dumps(self.reply))


RULE = {"metric": "pc.gpu.temp", "op": ">", "threshold": 85, "duration": 120, "message": "GPU {value}°C",
        "severity": "warn"}
TASK = {"cron": "0 9 * * *", "prompt": "Discord'daki mesajları özetle", "catch_up": True}


def test_rule_preview_then_save_toggle_remove(tmp_path):
    a = Automations(Router(RULE), rules_path=tmp_path / "r.json", tasks_path=tmp_path / "t.json")
    before = len(RuleSet(tmp_path / "r.json").rules)  # built-in rules that come ready-made
    r = a.action("rule_parse", {"text": "GPU 85'i 2 dakika geçerse haber ver"})
    assert r["ok"] and r["view"]["condition"] == "pc.gpu.temp > 85, 2 dk boyunca"
    assert len(RuleSet(tmp_path / "r.json").rules) == before  # a preview does not save
    assert a.action("rule_add", {"rule": json.loads(json.dumps(r["rule"]))})["ok"]
    rules = a.view()["rules"]
    mine = next(x for x in rules if x["text"].startswith("GPU 85'i"))
    assert len(rules) == before + 1 and mine["enabled"]
    rid = mine["id"]
    find = lambda: next((x for x in a.view()["rules"] if x["id"] == rid), None)  # noqa: E731
    assert a.action("rule_enable", {"id": rid, "on": False})["ok"] and not find()["enabled"]
    assert a.action("rule_rm", {"id": rid})["ok"] and find() is None
    assert not a.action("rule_add", {"rule": {**RULE, "op": "~"}})["ok"]  # an invalid rule is not saved
    assert not a.action("rule_parse", {"text": " "})["ok"]
    assert not a.action("__init__", {})["ok"] and not a.action("rule-x", {})["ok"]


def test_task_preview_then_save_run_toggle_remove(tmp_path):
    a = Automations(Router(TASK), rules_path=tmp_path / "r.json", tasks_path=tmp_path / "t.json")
    r = a.action("task_parse", {"text": "her sabah 9'da Discord'u özetle"})
    assert r["ok"] and r["view"]["next"].endswith("09:00") and r["view"]["prompt"].startswith("Discord")
    assert a.action("task_add", {"task": json.loads(json.dumps(r["task"]))})["ok"]
    t = a.view()["tasks"][0]
    assert t["text"] == "her sabah 9'da Discord'u özetle" and t["last"] == "" and t["enabled"]
    assert a.action("task_run", {"id": t["id"]})["ok"] and TaskStore(tmp_path / "t.json").get(t["id"]).pending
    assert a.action("task_enable", {"id": t["id"], "on": False})["ok"] and not a.view()["tasks"][0]["enabled"]
    assert not a.action("task_run", {"id": "yok"})["ok"]
    assert a.action("task_rm", {"id": t["id"]})["ok"] and a.view()["tasks"] == []
    assert not a.action("task_add", {"task": {"cron": "0 0 30 2 *", "prompt": "x"}})["ok"]  # February 30


def test_feeds_open_creates_template_and_test_reports(monkeypatch):
    from alice.app import automations
    from alice.core import feeds as feeds_mod

    opened = []
    monkeypatch.setattr(automations, "open_file", opened.append)
    a = Automations(Router({}))
    assert a.view()["feeds"] == [] and not a.view()["feeds_file"]
    r = a.action("feed_open", {})
    assert r["ok"] and r["created"] and opened and a.view()["feeds_file"]
    assert a.action("feed_open", {})["created"] is False
    names = [f["name"] for f in a.view()["feeds"]]
    assert names == ["ornek"]

    def explain(self, feed):
        return [feeds_mod.Verdict(item={}, title="hayat: selam", body="", ok=True, reason="'hayat' geçiyor",
                                  severity="info"),
                feeds_mod.Verdict(item={}, title="bot", body="", ok=False, reason="'bot' geçiyor", severity="info")]

    monkeypatch.setattr(feeds_mod.FeedMonitor, "explain", explain)
    r = a.action("feed_test", {"name": "ornek"})
    assert r["ok"] and r["total"] == 2 and r["passed"] == 1 and r["items"][0]["reason"] == "'hayat' geçiyor"
    assert not a.action("feed_test", {"name": "yok"})["ok"]


def test_app_endpoints(workdir):
    import httpx

    from alice.app.server import AppServer, AppState
    from tests.conftest import FakeProvider, make_config

    c = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(c, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    state._automations = Automations(Router(TASK))
    srv = AppServer(state).start()
    try:
        client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        v = client.get("/api/automations").json()
        assert v["tasks"] == [] and v["core_running"] is False
        r = client.post("/api/automations/action", json={"action": "task_parse", "text": "her sabah 9"}).json()
        assert client.post("/api/automations/action", json={"action": "task_add", "task": r["task"]}).json()["ok"]
        assert len(client.get("/api/automations").json()["tasks"]) == 1
        assert client.post("/api/automations/action", json={"action": "nope"}).status_code == 400
    finally:
        srv.stop()
