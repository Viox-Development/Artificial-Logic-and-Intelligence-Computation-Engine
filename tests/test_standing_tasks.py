"""Tasks ("Görevler"): task book, triggers, runner and approvals (alice/core/tasks.py, alice/app/tasks_panel.py).

No network and no real model: the agent is a fake function, or the real Agent with a FakeProvider.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from alice.core.store import Store
from alice.core.tasks import (LEVELS, Task, TaskBook, TaskRunner, Triggers, make_task_agent, template)
from tests.conftest import FakeProvider, make_config, tool_call


@pytest.fixture
def book(tmp_path):
    b = TaskBook(tmp_path / "t.db")
    yield b
    b.close()


class FakeAgent:
    """Records each call; `asks` / `notes` are what the agent "does" through task_ask / task_note."""

    def __init__(self, text="tamam", asks=None, notes=None):
        self.calls = []
        self.text, self.asks, self.notes = text, asks or [], notes or []

    def __call__(self, task, prompt, mode, approve, state):
        self.calls.append({"task": task.id, "prompt": prompt, "mode": mode, "approve": approve})
        if self.asks:
            state["task_asks"] = list(self.asks)
        if self.notes:
            state["task_notes"] = list(self.notes)
        return self.text


def runner(book, agent, **kw):
    sent = []
    r = TaskRunner(book, agent, notify=lambda *a: sent.append(a), **kw)
    r.sync = True
    return r, sent


# ------------------------------------------------------------------ task book
def test_task_validation():
    with pytest.raises(ValueError):
        Task("", "x")
    with pytest.raises(ValueError):
        Task("a", "b", permission="root")
    with pytest.raises(ValueError):
        Task("a", "b", trigger={"type": "schedule", "cron": "bad"})
    with pytest.raises(ValueError):
        Task("a", "b", trigger={"type": "github", "repo": "not a repo"})
    with pytest.raises(ValueError):
        Task("a", "b", trigger={"type": "nope"})
    assert Task("a", "b", trigger={"type": "metric", "metric": "pc.gpu.temp", "op": ">", "threshold": 80})


def test_book_crud_state_memory_log(book):
    t = book.save(Task("İzle", "bir şeyi izle"))
    assert [x.id for x in book.list()] == [t.id]
    book.set_state(t.id, a=1)
    book.set_state(t.id, b=2)
    assert book.state(t.id) == {"a": 1, "b": 2}
    for i in range(40):
        book.remember(t.id, f"not {i}")
    mem = book.state(t.id)["memory"]
    assert len(mem) == 30 and mem[-1]["text"] == "not 39"
    book.log(t.id, "run", "x")
    assert book.entries(t.id)[0]["kind"] == "run"
    a = book.ask(t.id, "Silinsin mi?")
    removed = book.remove(t.id)
    assert removed and book.get(t.id) is None
    assert book.approval(a.id).status == "rejected"  # a deleted task's question does not stay in the inbox


def test_book_is_shared_between_processes(tmp_path):
    """The app and the core open the same file: what one writes, the other reads."""
    app, core = TaskBook(tmp_path / "t.db"), TaskBook(tmp_path / "t.db")
    try:
        t = app.save(Task("A", "b"))
        a = core.ask(t.id, "Onay?")
        assert [x.id for x in app.approvals()] == [a.id]
        app.answer(a.id, "approve")
        assert [x.id for x in core.answered()] == [a.id]
    finally:
        app.close()
        core.close()


def test_answer_rules(book):
    t = book.save(Task("A", "b"))
    a = book.ask(t.id, "Onay?")
    with pytest.raises(ValueError):
        book.answer(a.id, "maybe")
    with pytest.raises(ValueError):
        book.answer(a.id, "reply", "  ")
    book.answer(a.id, "approve")
    with pytest.raises(ValueError):  # answered on the phone, then again on the desktop
        book.answer(a.id, "reject")
    with pytest.raises(KeyError):
        book.answer("nope", "approve")


# ------------------------------------------------------------------ triggers
def test_schedule_trigger(book):
    t = book.save(Task("Sabah", "özetle", trigger={"type": "schedule", "cron": "0 9 * * *"},
                       created=datetime(2026, 1, 1, 8, 0).timestamp()))
    tr = Triggers(book)
    assert tr.check(t, datetime(2026, 1, 1, 8, 30).timestamp()) is None
    fire = tr.check(t, datetime(2026, 1, 1, 9, 0, 30).timestamp())
    assert fire and "it's time" in fire.reason
    book.set_state(t.id, last_fire=datetime(2026, 1, 1, 9, 0, 30).timestamp())
    assert tr.check(t, datetime(2026, 1, 1, 12, 0).timestamp()) is None


def test_metric_trigger_with_glob_and_cooldown(book):
    st = Store(":memory:")
    now = time.time()
    st.add("pc.disk.C.percent", 95, ts=now - 5)
    st.add("pc.disk.D.percent", 40, ts=now - 5)
    t = book.save(Task("Disk", "temizle", trigger={"type": "metric", "metric": "pc.disk.*.percent", "op": ">",
                                                    "threshold": 90}))
    tr = Triggers(book, st)
    fire = tr.check(t, now)
    assert fire and "pc.disk.C.percent" in fire.reason
    book.set_state(t.id, last_fire=now)
    assert tr.check(t, now + 60) is None  # cooldown (default 6 h)


def test_event_trigger_only_new_matching_events(book):
    st = Store(":memory:")
    st.add_event("flight", "Uçuş değerlendirmesi: F-16", "eski uçuş", ts=time.time() - 100)
    t = book.save(Task("Koç", "not yaz", trigger={"type": "event", "kind": "flight", "match": "değerlendirmesi"}))
    tr = Triggers(book, st)
    assert tr.check(t, time.time()) is None  # first look: old events are not handled
    st.add_event("flight", "Uçuşa başladı", "")
    st.add_event("flight", "Uçuş değerlendirmesi: Su-27", "12 dk, en yüksek G 7.1")
    fire = tr.check(t, time.time())
    assert fire and len(fire.items) == 1 and "Su-27" in fire.items[0]
    assert tr.check(t, time.time()) is None  # not twice


def test_github_trigger_seeds_then_reports_new(book):
    items = [{"id": 5, "number": 5, "title": "eski", "url": "u5"}]
    t = book.save(Task("Issue", "cevapla", trigger={"type": "github", "repo": "a/b", "every_minutes": 1}))
    tr = Triggers(book, github_fetch=lambda repo, what: list(items))
    now = time.time()
    assert tr.check(t, now) is None
    items.insert(0, {"id": 7, "number": 7, "title": "çöküyor", "url": "u7", "body": "adımlar"})
    assert tr.check(t, now + 10) is None  # polled less than a minute ago
    fire = tr.check(t, now + 120)
    assert fire and len(fire.items) == 1 and "#7 çöküyor" in fire.items[0]
    assert tr.check(t, now + 240) is None


def test_folder_trigger(book, tmp_path):
    d = tmp_path / "dl"
    d.mkdir()
    (d / "a.zip").write_text("x")
    t = book.save(Task("İndirilen", "düzenle", trigger={"type": "folder", "path": str(d)}))
    tr = Triggers(book)
    now = time.time()
    assert tr.check(t, now) is None
    (d / "b.pdf").write_text("x")
    (d / "c.crdownload").write_text("x")  # still downloading: ignored
    fire = tr.check(t, now + 120)
    assert fire and [p.endswith("b.pdf") for p in fire.items] == [True]


# ------------------------------------------------------------------ runner + approvals
def test_run_creates_approval_and_notifies(book):
    t = book.save(Task("Issue", "taslak yaz", trigger={"type": "manual"}))
    agent = FakeAgent("taslak hazır", asks=[{"title": "Yorum yazılsın mı?", "body": "Merhaba...",
                                              "action": "issue #7'ye yorum yaz"}], notes=["#7 işlendi"])
    r, sent = runner(book, agent)
    book.set_state(t.id, run_now=True)
    assert r.tick() == [t.id]
    call = agent.calls[0]
    assert (call["mode"], call["approve"]) == LEVELS["read"] == ("ask", "n")
    assert "taslak yaz" in call["prompt"] and "run by hand" in call["prompt"]
    [a] = book.approvals()
    assert a.title == "Yorum yazılsın mı?" and a.action.startswith("issue #7")
    assert ("Waiting for approval · Issue", "Yorum yazılsın mı?", "warn", "approval") in sent
    assert book.state(t.id)["memory"][-1]["text"] == "#7 işlendi"
    assert [e["kind"] for e in book.entries(t.id)][:2] == ["result", "approval"]


def test_approve_runs_followup_with_permission(book):
    t = book.save(Task("Issue", "taslak yaz"))
    a = book.ask(t.id, "Yorum yazılsın mı?", "Merhaba", "issue #7'ye yorum yaz")
    agent = FakeAgent("yorum yazıldı")
    r, _ = runner(book, agent)
    r.tick()
    assert agent.calls == []  # not answered yet
    book.answer(a.id, "approve")
    r.tick()
    [call] = agent.calls
    assert (call["mode"], call["approve"]) == ("auto", "y")
    assert "APPROVED" in call["prompt"] and "issue #7'ye yorum yaz" in call["prompt"]
    assert book.approval(a.id).status == "done" and book.approval(a.id).result == "yorum yazıldı"
    r.tick()
    assert len(agent.calls) == 1  # acted on once


def test_reject_does_not_run_and_is_remembered(book):
    t = book.save(Task("Disk", "temizle"))
    a = book.ask(t.id, "setup.exe silinsin mi?")
    agent = FakeAgent()
    r, _ = runner(book, agent)
    book.answer(a.id, "reject", "o dosya lazım")
    r.tick()
    assert agent.calls == []
    assert "o dosya lazım" in book.state(t.id)["memory"][-1]["text"]
    assert book.approval(a.id).status == "done"


def test_reply_runs_with_task_permission(book):
    t = book.save(Task("Issue", "taslak yaz", permission="act"))
    a = book.ask(t.id, "Yorum?", action="yorum yaz")
    agent = FakeAgent()
    r, _ = runner(book, agent)
    book.answer(a.id, "reply", "daha kısa yaz")
    r.tick()
    [call] = agent.calls
    assert (call["mode"], call["approve"]) == LEVELS["act"]
    assert "daha kısa yaz" in call["prompt"]


def test_paused_task_and_min_gap(book):
    t = book.save(Task("A", "b", enabled=False))
    agent = FakeAgent()
    r, _ = runner(book, agent)
    book.set_state(t.id, run_now=True)
    assert r.tick() == []
    t.enabled = True
    book.save(t)
    assert r.tick() == [t.id]
    book.set_state(t.id, run_now=True)
    assert r.tick() == []  # ran less than a minute ago


def test_failed_run_and_trigger_error_are_logged(book):
    t = book.save(Task("A", "b"))
    g = book.save(Task("G", "b", trigger={"type": "github", "repo": "a/b"}))

    def boom(*a):
        raise RuntimeError("model yok")

    r, sent = runner(book, boom, github_fetch=lambda repo, what: (_ for _ in ()).throw(OSError("ağ yok")))
    book.set_state(t.id, run_now=True)
    r.tick()
    assert "model yok" in book.state(t.id)["last_result"] and book.state(t.id)["last_ok"] is False
    assert any(s[2] == "warn" and "failed" in s[0] for s in sent)
    assert book.entries(g.id)[0]["kind"] == "error"


def test_silent_result_is_not_notified(book):
    t = book.save(Task("A", "b"))
    r, sent = runner(book, FakeAgent("sessiz"))
    book.set_state(t.id, run_now=True)
    r.tick()
    assert sent == []


def test_no_agent_means_no_runs(book):
    t = book.save(Task("A", "b"))
    r, _ = runner(book, None)
    book.set_state(t.id, run_now=True)
    assert r.tick() == []


# ------------------------------------------------------------------ real agent, fake model
def test_task_agent_asks_through_tool(book, monkeypatch):
    from alice.providers.router import Router

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"]})
    script = [tool_call("task_ask", {"title": "Yorum yazılsın mı?", "body": "taslak", "action": "yorum yaz"}),
              tool_call("task_note", {"text": "issue 7 görüldü"}, "c2"),
              "Taslak hazırladım, onayını bekliyorum."]
    prov = FakeProvider("a", script)
    agent = make_task_agent(cfg, router_factory=lambda: Router(cfg, provider_factory=lambda pc: prov,
                                                                 sleep=lambda s: None))
    t = book.save(Task("Issue", "yeni issue'lara taslak yaz", tools=["github"]))
    r, sent = runner(book, agent)
    book.set_state(t.id, run_now=True)
    r.tick()
    assert [a.title for a in book.approvals()] == ["Yorum yazılsın mı?"]
    assert book.state(t.id)["memory"][-1]["text"] == "issue 7 görüldü"
    assert "Taslak hazırladım" in book.state(t.id)["last_result"]
    names = {f["function"]["name"] for f in prov.calls[0]["tools"]}
    assert {"task_ask", "task_note"} <= names and "ask_user" not in names
    assert "blender" not in names  # only the task's tool groups (+ core tools)


def test_read_only_task_cannot_write(book, tmp_path):
    """Permission level 'read': a write step is refused unattended; the file is not created."""
    from alice.providers.router import Router

    target = tmp_path / "x.txt"
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"]})
    prov = FakeProvider("a", [tool_call("write_file", {"path": str(target), "content": "hi"}), "yapamadım"])
    agent = make_task_agent(cfg, router_factory=lambda: Router(cfg, provider_factory=lambda pc: prov,
                                                                 sleep=lambda s: None))
    t = book.save(Task("Yaz", "dosya yaz"))
    r, _ = runner(book, agent)
    book.set_state(t.id, run_now=True)
    r.tick()
    assert not target.exists()


# ------------------------------------------------------------------ app panel
def test_panel_create_from_template_edit_answer(book):
    from alice.app.tasks_panel import TasksPanel

    p = TasksPanel(book=book)
    r = p.action("create", {"template": "github-issues", "trigger": {"type": "github", "repo": "me/app"}})
    assert r["ok"], r
    tid = r["task"]["id"]
    assert r["task"]["tools"] == ["github", "web"] and "me/app" in r["task"]["when"]
    book.set_state(tid, gh_last=12)
    r = p.action("update", {"id": tid, "trigger": {"type": "github", "repo": "me/other"}})
    assert r["ok"] and book.state(tid)["gh_last"] is None  # a new source starts clean
    assert p.action("enable", {"id": tid, "enabled": False})["task"]["enabled"] is False
    assert p.action("run", {"id": tid})["ok"] and book.state(tid)["run_now"]
    a = book.ask(tid, "Onay?")
    v = p.view()
    assert [x["id"] for x in v["approvals"]] == [a.id] and v["approvals"][0]["task"]
    assert p.action("answer", {"id": a.id, "decision": "approve"})["ok"]
    assert not p.action("answer", {"id": a.id, "decision": "approve"})["ok"]
    assert p.action("create", {"name": "x", "responsibility": "y", "trigger": {"type": "schedule", "cron": "x"}})[
        "ok"] is False
    assert p.log(tid)["ok"] and p.log(tid)["log"]
    assert p.action("remove", {"id": tid})["ok"]


def test_panel_parse_sentence(book):
    from alice.app.tasks_panel import TasksPanel

    class R:
        def chat(self, role, msgs, **kw):
            class Res:
                text = ('{"name": "Disk", "responsibility": "Disk dolunca temizlik öner", "trigger": {"type": '
                        '"metric", "metric": "pc.disk.*.percent", "op": ">", "threshold": 90}, '
                        '"tools": ["files", "uydurma"], "role": "fast"}')
            return Res()

    r = TasksPanel(router=R(), book=book).action("parse", {"text": "disk dolunca temizlik öner"})
    assert r["ok"] and r["draft"]["tools"] == ["files"] and r["draft"]["permission"] == "read"


def test_templates_are_valid():
    from alice.core.tasks import TEMPLATES

    assert len(TEMPLATES) >= 3
    for tp in TEMPLATES:
        Task.from_dict(template(tp["template"]))
    kinds = {tp["trigger"]["type"] for tp in TEMPLATES}
    assert {"github", "metric", "event"} <= kinds


# ------------------------------------------------------------------ core
def test_core_tick_runs_tasks(tmp_path):
    from alice.config import Config, default_config_dict
    from alice.core.rules import RuleSet
    from alice.core.schedule import TaskStore
    from alice.core.service import Core

    cfg = Config(default_config_dict())
    st = Store(":memory:")
    book = TaskBook(tmp_path / "t.db")
    agent = FakeAgent("bitti")
    sent = []
    r = TaskRunner(book, agent, notify=lambda *a: sent.append(a), store=st)
    r.sync = True
    core = Core(cfg, store=st, monitors=[], rules=RuleSet(tmp_path / "rules.json"),
                tasks=TaskStore(tmp_path / "tasks.json"), standing=r)
    t = book.save(Task("A", "b"))
    book.set_state(t.id, run_now=True)
    core.tick()
    assert agent.calls and book.state(t.id)["last_result"] == "bitti"
    book.close()


def test_core_notifies_approvals_as_warnings(tmp_path):
    from alice.config import Config, default_config_dict
    from alice.core.rules import RuleSet
    from alice.core.schedule import TaskStore
    from alice.core.service import Core

    cfg = Config(default_config_dict())
    cfg.data["core"]["desktop_notifications"] = False
    st = Store(":memory:")
    core = Core(cfg, store=st, monitors=[], rules=RuleSet(tmp_path / "rules.json"),
                tasks=TaskStore(tmp_path / "tasks.json"), run_agent=lambda p: "")
    core._task_notify("Onay bekliyor · Disk", "Silinsin mi?", "warn", "approval")
    [e] = [e for e in st.events() if e.kind == "approval"]
    assert e.severity == "warn" and e.delivered  # the phone app's poller pushes warn events
    core.standing.book.close()


def test_every_page_script_is_served():
    """A script added to index.html but missing from the static allow-list loads as 404 (the Tasks page did)."""
    import re
    from pathlib import Path

    from alice.app.server import STATIC_FILES

    html = (Path(__file__).parent.parent / "alice/app/static/index.html").read_text(encoding="utf-8")
    for name in re.findall(r'src="/static/([\w.-]+\.js)"', html):
        assert name in STATIC_FILES, name
