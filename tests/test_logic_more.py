"""Pure logic: session files, shortcut parsing/Windows message loops (fake ctypes), observers,
core and task tools, model ranking/measurement, site helpers. No network, screen or optional packages needed."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from alice import hotkeys, session, site
from alice.config import Config, alice_home, default_config_dict
from alice.providers import ranking
from alice.providers.openai_compat import ChatResult, ProviderError
from alice.providers.ranking import Probe
from alice.providers.router import Router
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config


# ===================================================================== session
def test_session_save_strips_system_and_images_and_lists(tmp_path):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "ilk soru\nikinci satır " + "x" * 100},
            {"role": "user", "content": [{"type": "text", "text": "bak"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    p = session.save("20260102-030405", msgs, tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["cwd"] == str(tmp_path) and len(data["messages"]) == 2
    assert "data:image" not in p.read_text(encoding="utf-8")
    assert not p.with_suffix(".json.tmp").exists()

    session.save("20260101-000000", [{"role": "system"}, {"role": "assistant", "content": "x"}], tmp_path)
    (session.sessions_dir() / "20251231-000000.json").write_text("{bozuk", encoding="utf-8")
    rows = session.list_sessions()
    assert [r[0] for r in rows] == ["20260102-030405", "20260101-000000"]  # corrupt file is skipped
    assert rows[0][2].startswith("ilk soru ikinci satır") and len(rows[0][2]) == 70
    assert rows[1][2] == ""  # no user message
    assert len(session.list_sessions(limit=1)) == 1


def test_session_save_concurrent_writers_keep_file_valid(tmp_path):
    """When the agent thread and UI requests save the same session at the same time, the shared temp file got
    mixed up or replace failed; now all succeed and the file is valid JSON."""
    errors: list[BaseException] = []
    msgs = [{"role": "system"}] + [{"role": "user", "content": "x" * 2000} for _ in range(200)]

    def work() -> None:
        try:
            for _ in range(15):
                session.save("20260103-000000", msgs, tmp_path)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    p = session.sessions_dir() / "20260103-000000.json"
    assert len(json.loads(p.read_text(encoding="utf-8"))["messages"]) == 200
    assert not [f for f in session.sessions_dir().iterdir() if f.name.endswith(".tmp")]


def test_session_load_latest_or_by_id():
    assert session.load() is None
    session.save("20260101-000000", [{"role": "system"}, {"role": "user", "content": "a"}], Path("."))
    session.save("20260102-000000", [{"role": "system"}, {"role": "user", "content": "b"}], Path("."))
    assert session.load() == ("20260102-000000", [{"role": "user", "content": "b"}])
    assert session.load("20260101-000000")[1][0]["content"] == "a"
    assert session.load("yok") is None


def test_new_session_id_format():
    sid = session.new_session_id()
    base = sid[:15]  # if another chat opens in the same second, -2/-3 is appended
    assert base[8] == "-" and base.replace("-", "").isdigit() and (sid == base or sid[15] == "-")


def test_session_load_skips_corrupt_latest_file():
    session.save("20260101-000000", [{"role": "system"}, {"role": "user", "content": "a"}], Path("."))
    (session.sessions_dir() / "20260102-000000.json").write_text("{yarım", encoding="utf-8")
    assert session.load() == ("20260101-000000", [{"role": "user", "content": "a"}])


def test_session_files_with_valid_but_wrong_shaped_json_do_not_crash():
    session.save("20260101-000000", [{"role": "system"}, {"role": "user", "content": "a"}], Path("."))
    d = session.sessions_dir()
    (d / "20260103-000000.json").write_text("[1, 2]", encoding="utf-8")
    (d / "20260102-000000.json").write_text('{"messages": 5}', encoding="utf-8")
    assert session.load() == ("20260101-000000", [{"role": "user", "content": "a"}])
    assert session.load("20260103-000000") is None
    assert [r[0] for r in session.list_sessions()] == ["20260101-000000"]
    assert session.load_extra("20260103-000000") == {} and session.saved_cwd("20260103-000000") is None
    session.checkpoints_path("x").write_text("[1]", encoding="utf-8")
    assert session.load_checkpoints("x") == {}
    session.checkpoints_path("y").write_text('{"1": [1], "2": {"a.txt": null}, "z": {}}', encoding="utf-8")
    assert session.load_checkpoints("y") == {2: {"a.txt": None}}


# ===================================================================== hotkeys
def test_key_vk_and_parse_combo_edge_cases():
    assert hotkeys.key_vk("<f24>") == 0x87
    assert hotkeys.key_vk("<Page_Up>") == 0x21
    assert hotkeys.key_vk(";") == 0xBA and hotkeys.key_vk("é") is None
    assert hotkeys.key_vk("<bilinmeyen>") is None
    assert hotkeys.parse_combo("<ctrl>+;") == (0x2, 0xBA)
    assert hotkeys.parse_combo("<super>+<shift>+<print_screen>") == (0x8 | 0x4, 0x2C)
    assert hotkeys.parse_combo("") is None and hotkeys.parse_combo("+") is None
    assert hotkeys.parse_combo("<win>+ä") is None


class _FakeUser32:
    def __init__(self, messages, refuse=()):
        self.messages = list(messages)
        self.refuse = set(refuse)
        self.registered: list[tuple[int, int, int]] = []
        self.unregistered: list[int] = []
        self.posted: list[tuple] = []

    def RegisterHotKey(self, hwnd, i, mods, vk):  # noqa: N802 - Win32 name
        if vk in self.refuse:
            return 0
        self.registered.append((i, mods, vk))
        return 1

    def UnregisterHotKey(self, hwnd, i):  # noqa: N802
        self.unregistered.append(i)

    def GetMessageW(self, ref, hwnd, a, b):  # noqa: N802
        if not self.messages:
            return 0
        msg = ref._obj
        msg.message, msg.wParam = self.messages.pop(0)
        return 1

    def PostThreadMessageW(self, *args):  # noqa: N802
        self.posted.append(args)


def _fake_windll(monkeypatch, user32):
    windll = SimpleNamespace(user32=user32, kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 4242))
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)


def test_win_hotkeys_registers_dispatches_and_cleans_up(monkeypatch):
    fired = []

    def boom():
        fired.append("boom")
        raise RuntimeError("kısayol hatası")

    user32 = _FakeUser32([(hotkeys.WM_HOTKEY, 1), (hotkeys.WM_HOTKEY, 99), (0x0100, 1), (hotkeys.WM_HOTKEY, 2),
                          (hotkeys.WM_HOTKEY, 1)], refuse={ord("C")})
    _fake_windll(monkeypatch, user32)
    hk = hotkeys.WinHotkeys({"<ctrl>+<alt>+a": lambda: fired.append("a"), "<ctrl>+<alt>+s": boom,
                             "<ctrl>+<alt>+c": lambda: fired.append("c"), "<ctrl>+<bozuk>": lambda: None,
                             "": lambda: None})
    assert hk.start() is True
    hk._thread.join(5)
    assert fired == ["a", "boom", "a"]  # an error in one shortcut does not stop listening
    assert hk.failed == ["<ctrl>+<alt>+c", "<ctrl>+<bozuk>"]
    assert user32.registered == [(1, 0x3 | hotkeys.MOD_NOREPEAT, ord("A")), (2, 0x3 | hotkeys.MOD_NOREPEAT, ord("S"))]
    assert sorted(user32.unregistered) == [1, 2]
    hk.stop()
    assert user32.posted == [(4242, hotkeys.WM_QUIT, 0, 0)]


def test_win_hotkeys_all_failed_and_stop_before_start(monkeypatch):
    user32 = _FakeUser32([], refuse={ord("A")})
    _fake_windll(monkeypatch, user32)
    hk = hotkeys.WinHotkeys({"<ctrl>+a": lambda: None})
    hk.stop()  # stopping before starting does nothing
    assert hk.start() is False
    hk._thread.join(5)
    assert user32.posted == []


def test_key_poller_reports_press_and_release(monkeypatch):
    with pytest.raises(ValueError):
        hotkeys.KeyPoller("<yok>", lambda: None, lambda: None)
    events = []
    states = [0, 0x8000, 0x8001, 0, 0x8000, 0]
    poller = None

    def get_state(vk):
        assert vk == 0x91
        if len(states) == 1:
            poller._stop.set()
        return states.pop(0) if states else 0

    _fake_windll(monkeypatch, SimpleNamespace(GetAsyncKeyState=get_state))
    poller = hotkeys.KeyPoller("<scroll_lock>", lambda: events.append("down"), lambda: events.append("up"), every=0)
    poller.start()
    poller._thread.join(5)
    assert events == ["down", "up", "down", "up"]
    poller.stop()
    assert poller._stop.is_set()


# ================================================================== observers
def test_element_describe():
    from alice.reflex.observers import Element

    assert Element(1, "button", "Gönder", False).describe() == "button 'Gönder'"
    assert Element(2, "input[email]", "E-posta", True, "a@b.c", in_view=False).describe() == \
        "input[email] 'E-posta' (şu an: 'a@b.c') [aşağıda]"


class _Page:
    def __init__(self):
        self.calls: list[tuple] = []
        self.url = "https://ornek.com/giris"
        self.mouse = SimpleNamespace(wheel=lambda x, y: self.calls.append(("wheel", y)))
        self.keyboard = SimpleNamespace(press=lambda k: self.calls.append(("key", k)))

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name, *a))
            if name == "wait_for_load_state":
                raise TimeoutError("yavaş")
            return "Giriş" if name == "title" else None

        return rec


class _Loc:
    def __init__(self, calls, eid):
        self.calls, self.eid = calls, eid

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, self.eid, *a))


class _Browser:
    def __init__(self, attached):
        self.attached = attached
        self.page = _Page()
        self.visible = _Page()
        self.tabs: list[str] = []

    def _visible_page(self):
        return self.visible

    def tab_for(self, url):
        self.tabs.append(url)
        return self.page

    def snapshot(self, limit):
        base = {"role": "", "tag": "", "type": "", "text": "", "placeholder": "", "href": "", "value": "",
                "inView": True}
        return [{**base, "id": 1, "tag": "input", "type": "email", "placeholder": "E-posta"},
                {**base, "id": 2, "tag": "input", "type": "submit", "text": "Giriş", "inView": False},
                {**base, "id": 3, "tag": "a", "href": "/kayit"},
                {**base, "id": 4, "tag": "div", "role": "textbox", "text": "Not", "value": "abc"},
                {**base, "id": 5, "tag": "textarea", "text": "Mesaj"}]

    def locator(self, eid):
        return _Loc(self.page.calls, eid)


def test_browser_observer(monkeypatch, tmp_path):
    from alice.reflex.observers import BrowserObserver
    from alice.tools import browser

    b = _Browser(attached=True)
    monkeypatch.setattr(browser, "get_browser", lambda ctx: b)
    obs = BrowserObserver(ToolContext(cwd=tmp_path, config=Config(default_config_dict())), "ornek.com")
    assert b.tabs == ["https://ornek.com"] and ("bring_to_front",) in b.page.calls
    assert ("goto", "https://ornek.com") in b.page.calls
    o = obs.observe()
    assert o.title == "Giriş" and o.location == "https://ornek.com/giris"
    kinds = [(e.id, e.kind, e.label, e.editable) for e in o.elements]
    assert kinds == [(1, "input[email]", "E-posta", True), (2, "input[submit]", "Giriş", False),
                     (3, "a", "/kayit", False), (4, "textbox", "Not", True), (5, "textarea", "Mesaj", True)]
    assert o.elements[1].in_view is False and o.elements[3].value == "abc"
    b.page.calls.clear()
    obs.click(3)
    obs.type(1, "jev@ornek.com", submit=True)
    obs.type(5, "selam", submit=False)
    obs.scroll(True)
    obs.scroll(False)
    obs.back()
    obs.press_enter()
    names = [c[0] for c in b.page.calls]
    assert names[:3] == ["scroll_into_view_if_needed", "click", "wait_for_timeout"]
    assert ("fill", 1, "jev@ornek.com") in b.page.calls and ("press", 1, "Enter") in b.page.calls
    assert ("fill", 5, "selam") in b.page.calls and ("press", 5, "Enter") not in b.page.calls
    assert ("wheel", 700) in b.page.calls and ("wheel", -700) in b.page.calls
    assert "go_back" in names and ("key", "Enter") in b.page.calls


def test_browser_observer_detached_uses_own_page(monkeypatch, tmp_path):
    from alice.reflex.observers import BrowserObserver
    from alice.tools import browser

    b = _Browser(attached=False)
    monkeypatch.setattr(browser, "get_browser", lambda ctx: b)
    BrowserObserver(ToolContext(cwd=tmp_path, config=Config(default_config_dict())), "http://yerel:8000")
    assert b.tabs == [] and ("goto", "http://yerel:8000") in b.page.calls


def test_desktop_observer(monkeypatch, tmp_path):
    from alice.reflex.observers import DesktopObserver
    from alice.tools import computer, desktop

    items = [{"id": 1, "role": "Edit", "name": "", "auto_id": "txtUser", "value": "", "enabled": True},
             {"id": 2, "role": "Button", "name": "Tamam", "auto_id": "ok", "value": "", "enabled": True},
             {"id": 3, "role": "Button", "name": "Kapalı", "auto_id": "", "value": "", "enabled": False}]
    seen = []
    monkeypatch.setattr(desktop, "collect_ui_elements", lambda ctx, title, limit: seen.append(title) or ("Not", items))
    monkeypatch.setattr(desktop, "ui_act", lambda ctx, eid, act, text=None: seen.append((eid, act, text)))
    monkeypatch.setattr(computer, "press_keys", lambda keys: seen.append(keys))
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(scroll=lambda n: seen.append(("scroll", n))))
    obs = DesktopObserver(ToolContext(cwd=tmp_path, config=Config(default_config_dict())), "Not Defteri")
    o = obs.observe()
    assert o.title == "Not" and o.location == "desktop"
    assert [(e.id, e.label, e.editable) for e in o.elements] == [(1, "txtUser", True), (2, "Tamam", False)]
    obs.click(2)
    obs.type(1, "jev", submit=True)
    obs.type(1, "x", submit=False)
    obs.scroll(True)
    obs.scroll(False)
    obs.back()
    obs.press_enter()
    assert seen == ["Not Defteri", (2, "click", None), (1, "set_text", "jev"), "enter", (1, "set_text", "x"),
                    ("scroll", -600), ("scroll", 600), "alt+left", "enter"]


# ================================================================ tools/core.py
@pytest.fixture
def core_ctx(tmp_path):
    from alice.core.store import Store

    ctx = ToolContext(cwd=tmp_path, config=Config(default_config_dict()))
    ctx.state["core_store"] = Store(":memory:")
    yield ctx
    ctx.state["core_store"].close()


def test_metrics_actions(core_ctx):
    from alice.core.service import state_path
    from alice.tools.core import Metrics

    m = Metrics()
    st = core_ctx.state["core_store"]
    now = time.time()
    for i in range(130):
        st.add("pc.cpu.percent", float(i), ts=now - 3000 + i * 10)
    st.add_event("alert", "CPU", "yüksek", ts=now - 50)
    st.add_event("alert", "eski", ts=now - 10 * 86400)
    assert "core is not running" in m.run(core_ctx, action="status").text
    alice_home().mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({"pid": os.getpid(), "ts": now, "mode": "flight",
                                        "policy": {"min_severity": "critical", "voice": True, "local_gpu": False}}),
                            encoding="utf-8")
    assert m.run(core_ctx, action="status").text == ("the core is running, mode: flight, lowest notification: critical, "
                                                     "voice: True, local GPU: False")
    assert m.run(core_ctx, action="keys", pattern="pc.*").text == "pc.cpu.percent"
    assert m.run(core_ctx, action="keys", pattern="watch.*").text == "no data"
    ev = m.run(core_ctx, action="events", since="1d").text
    assert "CPU: yüksek" in ev and "eski" not in ev
    assert m.run(core_ctx, action="events", since="1d", until="2d").text == "no events"
    assert m.run(core_ctx, action="modes", mode="dev").text == "no mode records in this range"
    series = m.run(core_ctx, action="series", key="pc.cpu.percent", since="2h").text
    lines = series.splitlines()
    assert lines[0] == "pc.cpu.percent, 130 measurements (1 in every 2 shown)" and len(lines) == 66
    assert m.run(core_ctx, action="series", key="watch.hr").text == "no data"
    assert "ort" in m.run(core_ctx, action="stats", key="pc.*").text
    with pytest.raises(ToolError, match="key is required"):
        m.run(core_ctx, action="stats")
    with pytest.raises(ToolError, match="zaman anlaşılamadı"):
        m.run(core_ctx, action="keys", since="geçen bayram")


def test_store_is_created_lazily(tmp_path):
    from alice.tools.core import _store

    ctx = ToolContext(cwd=tmp_path, config=Config(default_config_dict()))
    st = _store(ctx)
    assert ctx.state["core_store"] is st and _store(ctx) is st
    st.close()


def test_rules_tool_validation_and_toggles(core_ctx):
    from alice.core.rules import RuleSet
    from alice.tools.core import Rules

    r = Rules()
    assert r.risk_for({"action": "list"}) == "read" and r.risk_for({"action": "add"}) == "write"
    with pytest.raises(ToolError, match="add needs threshold"):
        r.run(core_ctx, action="add", metric="pc.gpu.temp", op=">", message="x")
    with pytest.raises(ToolError, match="geçersiz operatör"):
        r.run(core_ctx, action="add", metric="pc.gpu.temp", op="=>", threshold=1, message="x")
    with pytest.raises(ToolError, match="prompt gerekli"):
        r.run(core_ctx, action="add", metric="pc.gpu.temp", op=">", threshold=1, message="x", rule_action="agent")
    out = r.run(core_ctx, action="add", metric="pc.gpu.temp", op=">=", threshold=90, message="sıcak",
                modes=["flight"], severity=None).text
    assert "kural kuruldu" in out and "[flight]" in out and "(warn)" in out
    with pytest.raises(ToolError, match="id is required"):
        r.run(core_ctx, action="remove")
    assert r.run(core_ctx, action="disable", id="gpu-hot").text == "done"
    assert RuleSet().get("gpu-hot").enabled is False
    assert r.run(core_ctx, action="enable", id="gpu-hot").text == "done"
    res = r.run(core_ctx, action="remove", id="yok")
    assert res.is_error and res.text == "no rule: yok"


def test_schedule_tool_paths(core_ctx):
    from alice.tools.core import Schedule

    s = Schedule()
    assert s.risk_for({"action": "list"}) == "read" and s.risk_for({"action": "remove"}) == "write"
    assert s.summary({"action": "add", "cron": "bozuk cron"}).startswith("bozuk cron: ")
    assert s.summary({"action": "list"}) == '{"action": "list"}'
    assert s.run(core_ctx, action="list").text == "no scheduled tasks"
    with pytest.raises(ToolError, match="cron and prompt"):
        s.run(core_ctx, action="add", cron="0 8 * * *")
    with pytest.raises(ToolError):
        s.run(core_ctx, action="add", cron="0 9 31 2 *", prompt="imkansız")
    with pytest.raises(ToolError, match="id is required"):
        s.run(core_ctx, action="run_now")
    with pytest.raises(ToolError, match="no task"):
        s.run(core_ctx, action="run_now", id="yok")
    with pytest.raises(ToolError, match="no task"):
        s.run(core_ctx, action="remove", id="yok")
    s.run(core_ctx, action="add", cron="0 8 * * *", prompt="hava durumu", description="sabah")
    from alice.core.schedule import TaskStore

    tid = TaskStore().tasks[0].id
    assert "sabah" in s.run(core_ctx, action="list").text
    assert s.run(core_ctx, action="remove", id=tid).text == "deleted"


# =============================================================== tools/tasks.py
class _Runner:
    def __init__(self, ctx, goal, max_steps, step_tool_calls, answer_timeout, log):
        self.args = (goal, max_steps, step_tool_calls, answer_timeout)
        self.log = log
        self.answers: list[bool] = []
        self.waiting = True
        self.stopped = False
        _Runner.last = self

    def run(self):
        self.log("adım 1")
        return _Runner.result

    def status_text(self):
        return "adım 1/3"

    def stop(self):
        self.stopped = True

    def answer(self, ok):
        if not self.waiting:
            return False
        self.answers.append(ok)
        return True


def test_run_plan(monkeypatch, tmp_path):
    from alice import multistep
    from alice.tools.tasks import RunPlan

    logs = []
    monkeypatch.setattr(multistep, "TaskRunner", _Runner)
    monkeypatch.setattr(multistep, "task_log", lambda s: logs.append(("file", s)))
    cfg = Config(default_config_dict())
    cfg.agent.update(plan_max_steps=5, plan_step_tool_calls=3, plan_answer_timeout=9)
    ctx = ToolContext(cwd=tmp_path, config=cfg, state={"task_hooks": {"log": lambda s: logs.append(("ui", s))}})
    tool = RunPlan()
    assert tool.summary({"goal": "x" * 200}) == "x" * 150
    with pytest.raises(ToolError):
        tool.run(ctx, goal="  ")
    _Runner.result = "Bitti.\nayrıntı"
    res = tool.run(ctx, goal="  faturayı indir  ")
    assert res.text == "Bitti.\nayrıntı" and not res.is_error
    assert _Runner.last.args == ("faturayı indir", 5, 3, 9.0)
    assert "active_task" not in ctx.state
    assert ("ui", "görev: adım 1") in logs and ("file", "bitti: Bitti.") in logs
    _Runner.result = "error: adım başarısız"
    assert tool.run(ctx, goal="x").is_error
    ctx.state["active_task"] = object()
    res = tool.run(ctx, goal="ikinci")
    assert res.is_error and "zaten bir görev" in res.text


def test_task_control(tmp_path):
    from alice.tools.tasks import TaskControl

    ctx = ToolContext(cwd=tmp_path, config=Config(default_config_dict()))
    tc = TaskControl()
    assert tc.run(ctx, action="status").text == "şu an süren bir görev yok"
    runner = _Runner(ctx, "g", 1, 1, 1, lambda s: None)
    ctx.state["active_task"] = runner
    assert tc.run(ctx, action="status").text == "adım 1/3"
    assert tc.run(ctx, action="approve").text == "onaylandı, devam ediyor"
    assert tc.run(ctx, action="reject").text.startswith("reddedildi")
    assert runner.answers == [True, False]
    runner.waiting = False
    res = tc.run(ctx, action="approve")
    assert res.is_error and "onay beklemiyor" in res.text
    assert "durduruluyor" in tc.run(ctx, action="stop").text and runner.stopped
    with pytest.raises(ToolError):
        tc.run(ctx, action="patla")


class _Team:
    def __init__(self, ctx, goal, max_parallel, publish, review, council, pr=False):
        self.id = f"p{len(_Team.made) + 1}"
        self.kw = dict(goal=goal, max_parallel=max_parallel, review=review, council=council)
        self.specs: list[dict] = []
        self.members = {"backend": object()}
        self.summary = ""
        self.questions: list[dict] = []
        self.state = "running"
        self.calls: list[tuple] = []
        _Team.made.append(self)

    def add(self, spec):
        self.specs.append(spec)

    def start(self, plan):
        self.calls.append(("start", plan))

    def status(self):
        return self.state

    def status_text(self):
        return "backend: çalışıyor"

    def send(self, frm, to, text):
        self.calls.append(("send", frm, to, text))
        return "iletildi"

    def stop(self, target):
        self.calls.append(("stop", target))

    def resume(self):
        self.calls.append(("resume",))

    def wait_until(self, pred, limit, cancelled):
        self.calls.append(("wait", limit, pred()))


@pytest.fixture
def team_ctx(monkeypatch, tmp_path):
    from alice import team

    _Team.made = []
    monkeypatch.setattr(team, "Team", _Team)
    return ToolContext(cwd=tmp_path, config=Config(default_config_dict()))


def test_team_tool_start_and_validation(team_ctx):
    from alice.tools.tasks import TeamTool

    t = TeamTool()
    assert t.summary({"action": "start", "goal": "site", "agents": [{"name": "ui"}, "bozuk"]}) == "proje: ui · site"
    assert t.summary({"action": "start"}).startswith("proje: yönetici planlayacak")
    assert t.summary({"action": "send", "to": "ui", "text": "selam"}) == "ui: selam"
    assert t.summary({"action": "status"}) == "status"
    with pytest.raises(ToolError, match="goal gerekli"):
        t.run(team_ctx, action="start", goal=" ")
    with pytest.raises(ToolError, match="en çok 8"):
        t.run(team_ctx, action="start", goal="x", agents=[{"name": str(i)} for i in range(9)])
    res = t.run(team_ctx, action="status")
    assert res.is_error and "böyle bir proje yok" in res.text

    out = t.run(team_ctx, action="start", goal=" büyük iş ", review=False, council=False).text
    team = _Team.made[0]
    assert "Yönetici kodu inceleyip" in out and team.calls == [("start", True)]
    assert team.kw == {"goal": "büyük iş", "max_parallel": 5, "review": False, "council": False}
    out = t.run(team_ctx, action="start", goal="iki", agents=[{"name": "ui", "task": "t"}, "atla"]).text
    assert "Ajanlar:\nbackend: çalışıyor" in out and _Team.made[1].specs == [{"name": "ui", "task": "t"}]
    assert team_ctx.state["team"] is _Team.made[1] and set(team_ctx.state["teams"]) == {"p1", "p2"}


def test_team_tool_actions(team_ctx):
    from alice.tools.tasks import TeamTool

    t = TeamTool()
    t.run(team_ctx, action="start", goal="g1")
    t.run(team_ctx, action="start", goal="g2")
    first, second = _Team.made
    assert t.run(team_ctx, action="send", text="durum?").text == "iletildi"
    assert second.calls[-1] == ("send", "kullanıcı", "alice", "durum?")
    t.run(team_ctx, action="send", to=" Backend ", text="x", project="p1")
    assert first.calls[-1] == ("send", "kullanıcı", "backend", "x")
    with pytest.raises(ToolError, match="'yok' yok"):
        t.run(team_ctx, action="stop", name="yok")
    assert t.run(team_ctx, action="stop", name="Backend").text == "durduruldu: backend"
    assert t.run(team_ctx, action="stop").text == "durduruldu: proje"
    assert t.run(team_ctx, action="resume").text == "proje sürdürülüyor"
    second.summary = "özet metni"
    assert t.run(team_ctx, action="status").text == "proje p2: running\nbackend: çalışıyor\n\nÖzet:\nözet metni"

    out = t.run(team_ctx, action="wait", timeout=1).text
    assert second.calls[-1] == ("wait", 5, False) and "Süre doldu" in out
    out = t.run(team_ctx, action="wait", timeout=99999).text
    assert second.calls[-1][1] == 1800
    second.state = "done"
    assert t.run(team_ctx, action="wait").text.endswith("özet metni")
    second.state = "needs_you"
    second.questions = [{"text": "Hangi renk?"}, {"text": "cevaplı", "answer": "mavi"}]
    out = t.run(team_ctx, action="wait").text
    assert "- Hangi renk?" in out and "cevaplı" not in out
    second.state = "stopped"
    out = t.run(team_ctx, action="wait").text
    assert out.startswith("Proje durumu: stopped") and "Süre doldu" not in out
    with pytest.raises(ToolError, match="bilinmeyen action"):
        t.run(team_ctx, action="dans")


def test_council_tool_reads_paths_and_review_rules(monkeypatch, tmp_path):
    from alice import council
    from alice.tools.tasks import CouncilTool

    seen = {}

    class FakeCouncil:
        def __init__(self, ctx, kind, subject, context, critical, depth, rules):
            seen.update(kind=kind, subject=subject, context=context, critical=critical, depth=depth, rules=rules)

        def run(self):
            return {"karar": "kabul"}

    monkeypatch.setattr(council, "Council", FakeCouncil)
    monkeypatch.setattr(council, "report", lambda snap: f"rapor: {snap['karar']}")
    (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
    (tmp_path / ".alice").mkdir()
    (tmp_path / ".alice" / "REVIEW.md").write_text("Kural: test şart", encoding="utf-8")
    emitted = []
    ctx = ToolContext(cwd=tmp_path, config=Config(default_config_dict()), emit=emitted.append)
    tool = CouncilTool()
    assert tool.summary({"subject": "s" * 300}) == "s" * 150
    with pytest.raises(ToolError):
        tool.run(ctx, subject=" ")
    res = tool.run(ctx, subject="mimari", paths=["a.py", "yok.py"], critical=1, depth="2")
    assert res.text == "rapor: kabul"
    assert "## a.py\n```\nprint('a')" in seen["context"] and "## yok.py: okunamadı" in seen["context"]
    assert seen["rules"] == "Kural: test şart" and seen["critical"] is True and seen["depth"] == "2"
    assert seen["kind"] == "adhoc" and emitted == ["kurul toplanıyor…", "kurul kararını verdi"]
    (tmp_path / "REVIEW.md").write_text("kök kuralı", encoding="utf-8")
    tool.run(ctx, subject="x")
    assert seen["rules"] == "kök kuralı" and seen["depth"] == "auto" and seen["context"] == ""


# ================================================================= ranking
PROVIDERS = {
    "g": {"base_url": "http://g", "api_key_env": "", "max_input_tokens": 1000000, "vision_models": ["*"]},
    "v": {"base_url": "http://v", "api_key_env": "", "max_input_tokens": 128000, "vision_models": ["*"]},
    "w": {"base_url": "http://w", "api_key_env": "", "max_input_tokens": 128000, "vision_models": ["*"]},
    "n": {"base_url": "http://n", "api_key_env": "", "max_input_tokens": 128000},
}


def test_cache_helpers(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{bozuk", encoding="utf-8")
    assert ranking.load_cache(p) == {} and ranking.load_cache(tmp_path / "yok.json") == {}
    assert ranking.cache_age({"probed_ts": "dün"}) == float("inf")
    assert ranking.cache_age({}) > 1e9
    assert 0 <= ranking.cache_age({"probed_ts": time.time()}) < 5
    assert ranking.uses_auto({"brain": ["a:x", "auto"]}) and not ranking.uses_auto({"brain": ["a:x"], "fast": None})


def test_scores():
    # Weighted score: four metrics 0-100, using the role's weights.
    fast = Probe("a:x", True, 60, latency=0.1, ttft=0.1, tps=300)
    slow = Probe("a:y", True, 60, latency=10, ttft=4)
    assert ranking.metric_scores(fast) == {"zeka": 60, "hiz": 100, "tepki": 100, "akis": 100, "baglam": 50,
                                            "guven": 100}  # context unknown
    m = ranking.metric_scores(slow)
    assert m["hiz"] < 35 and m["tepki"] < 25 and m["akis"] == 50  # tps 0 = not measured, not a penalty
    assert ranking.fast_score(fast) > ranking.fast_score(slow) + 40
    assert ranking.brain_score_of(90, 2) > ranking.brain_score_of(90, 13) > ranking.brain_score_of(90, 100)
    assert ranking.voice_score(fast) - ranking.voice_score(slow) > ranking.brain_score(fast) - ranking.brain_score(slow)


def test_weights_change_order_and_are_saved():
    smart_slow = Probe("a:smart", True, 95, latency=4, ttft=1)
    quick = Probe("a:quick", True, 60, latency=1, ttft=0.3, tps=200)
    assert ranking.brain_score(smart_slow) > ranking.brain_score(quick)
    w = ranking.load_weights()
    w["brain"] = {"zeka": 20, "hiz": 60, "tepki": 10, "akis": 10}
    ranking.save_weights(w)
    assert ranking.brain_score(quick) > ranking.brain_score(smart_slow)  # read back from the saved file
    assert ranking.load_weights()["voice"] == ranking.DEFAULT_WEIGHTS["voice"]


def test_vision_backups_one_per_provider():
    pcs = make_config({"brain": []}, PROVIDERS).providers()
    results = {"v:gemini-3.8-flash": {"ok": True, "quality": 93, "latency": 2},
               "v:gemini-3.5-flash": {"ok": True, "quality": 87, "latency": 1},
               "w:gemma-4-31b": {"ok": True, "quality": 68, "latency": 1},
               "n:kimi-k3": {"ok": True, "quality": 92, "latency": 1},  # no vision
               "g:gemini-3-flash": {"ok": True, "quality": 84, "latency": 1},  # provider in the chain
               "w:gpt-oss-20b": {"ok": False, "quality": 60, "latency": 1}}
    assert ranking.vision_backups(results, pcs, ["g:gemini-3.8-flash"]) == ["v:gemini-3.8-flash", "w:gemma-4-31b"]
    assert ranking.vision_backups(results, pcs, ["g:x"], limit=1) == ["v:gemini-3.8-flash"]


def test_candidates_filters_catalogs():
    def pc(name, enabled=True):
        return SimpleNamespace(enabled=enabled, api_key_env=f"{name.upper()}_KEY")

    catalogs = {
        "gemini": [{"id": "models/gemini-3.8-flash"}, {"id": "models/gemini-3.8-flash-tts"},
                   {"id": "models/bilinmeyen"}, {"name": "models/gemini-2.5-pro"}],
        "openrouter": [{"id": f"qwen/qwen3.8-{i}b:free", "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": ["tools"]} for i in range(8)]
        + [{"id": "z-ai/glm-5:free", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": []},
           {"id": "kimi-k3", "pricing": {"prompt": "1", "completion": "1"}, "supported_parameters": ["tools"]}],
    }

    def client(name):
        if name == "nvidia":
            raise RuntimeError("401 yetkisiz")
        return SimpleNamespace(list_models=lambda: catalogs[name])

    router = SimpleNamespace(providers_cfg={"ollama": pc("ollama"), "groq": pc("groq", False), "nvidia": pc("nvidia"),
                                            "gemini": pc("gemini"), "openrouter": pc("openrouter")}, client=client)
    refs, errors, catalog = ranking.candidates(router)
    assert errors == {"groq": "GROQ_KEY tanımlı değil", "nvidia": "401 yetkisiz"}
    assert catalog["gemini"] == ["bilinmeyen", "gemini-2.5-pro", "gemini-3.8-flash", "gemini-3.8-flash-tts"]
    gem = [r for r in refs if r.startswith("gemini:")]
    # by quality; no tts; an unknown model last is still measured (SambaNova's models were never tried before)
    assert gem == ["gemini:gemini-3.8-flash", "gemini:gemini-2.5-pro", "gemini:bilinmeyen"]
    orr = [r for r in refs if r.startswith("openrouter:")]
    assert len(orr) == ranking.PROVIDER_CAP["openrouter"]  # shared daily quota: at most 6 candidates
    assert not any("glm-5" in r or "kimi" in r for r in orr)  # no models that lack tool support or are paid


class _FakeClient:
    responses: list = []

    def __init__(self, pc, timeout):
        self.timeout = timeout

    def chat_stream(self, model, msgs, on_delta, tools=None, extra=None, on_first=None):
        item = _FakeClient.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_first:
            on_first()
        return item

    def close(self):
        pass


def _call(name, args):
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": args}}


@pytest.fixture
def probe_router(monkeypatch):
    from alice.providers import openai_compat

    monkeypatch.setattr(openai_compat, "OpenAICompatProvider", _FakeClient)
    return Router(make_config({"brain": []}, PROVIDERS), auto_cache={})


def test_probe_outcomes(probe_router):
    ok = ChatResult({"content": "", "tool_calls": [_call("list_dir", '{"path": "C:/"}')]}, "g", "m",
                    {"completion_tokens": 42})
    bad = ChatResult({"content": "", "tool_calls": [_call("run_command", "{bozuk")]}, "g", "m", {})
    other = ChatResult({"content": "", "tool_calls": [_call("web_search", "{}")]}, "g", "m", {})
    text = ChatResult({"content": "Yapamam " * 10}, "g", "m", {})
    _FakeClient.responses = [ok, bad, other, text, ProviderError("quota", "günlük kota"), ConnectionError("koptu")]
    specs = [{"type": "function"}]
    r = ranking.probe(probe_router, "g:gemini-3.8-flash", specs, timeout=3)
    assert r.ok and r.tool == "list_dir" and r.tokens == 42 and r.quality == 93 and r.error_kind == ""
    r = ranking.probe(probe_router, "g:m", specs)
    assert not r.ok and r.error_kind == "bad_args" and r.tokens == int(len("{bozuk") / 3.5)
    r = ranking.probe(probe_router, "g:m", specs)
    assert r.error_kind == "no_tool"
    r = ranking.probe(probe_router, "g:m", specs)
    assert r.error_kind == "no_tool" and "Yapamam" in r.error
    r = ranking.probe(probe_router, "g:m", specs)
    assert r.error_kind == "quota" and "günlük kota" in r.error and not r.dead
    r = ranking.probe(probe_router, "g:m", specs)
    assert r.error_kind == "network" and r.error.startswith("ConnectionError: koptu")


def test_probe_all_emits_per_model(monkeypatch, probe_router):
    monkeypatch.setattr(ranking, "_probe_specs", lambda: [])
    monkeypatch.setattr(ranking, "probe", lambda router, ref, specs, timeout: Probe(ref, ref.endswith("1"), 80,
                                                                                    latency=1.5, error_kind="server"))
    lines = []
    res = ranking.probe_all(probe_router, ["g:m1", "g:m2", "n:m1"], emit=lines.append)
    assert sorted(p.ref for p in res) == ["g:m1", "g:m2", "n:m1"]
    assert sorted(lines) == ["g:m1: OK 1.5s", "g:m2: server", "n:m1: OK 1.5s"]


def test_refresh_in_background(monkeypatch, isolated_home):
    cfg = make_config({"brain": ["g:x"]}, PROVIDERS, auto_models=False)
    router = Router(cfg, auto_cache={})
    assert ranking.refresh_in_background(router) is False  # auto is not used

    cfg = make_config({"brain": ["auto"]}, PROVIDERS)
    router = Router(cfg, auto_cache={})
    monkeypatch.setattr(ranking, "load_cache", lambda path=None: {"probed_ts": time.time()})
    assert ranking.refresh_in_background(router) is False  # cache is fresh

    monkeypatch.setattr(ranking, "load_cache", lambda path=None: {})
    done = threading.Event()
    msgs = []
    fresh = {"chains": {"brain": ["g:a", "g:b", "g:c", "g:d"]}}
    monkeypatch.setattr(ranking, "refresh", lambda r: fresh)
    reloaded = []
    monkeypatch.setattr(router, "reload_auto", lambda c: reloaded.append(c))

    def emit(s):
        msgs.append(s)
        if "güncellendi" in s or "başarısız" in s:
            done.set()

    assert ranking._refreshing.acquire(blocking=False)
    try:
        assert ranking.refresh_in_background(router, emit) is False  # already being measured
    finally:
        ranking._refreshing.release()
    assert ranking.refresh_in_background(router, emit) is True
    assert done.wait(5)
    assert reloaded == [fresh] and msgs[-1] == "model sırası güncellendi: g:a, g:b, g:c"

    def boom(r):
        raise RuntimeError("ağ yok")

    monkeypatch.setattr(ranking, "refresh", boom)
    for _ in range(100):  # let the previous job release the lock
        if not ranking._refreshing.locked():
            break
        time.sleep(0.02)
    done.clear()
    assert ranking.refresh_in_background(router, emit) is True
    assert done.wait(5) and msgs[-1] == "model ölçümü başarısız: ağ yok"


def test_format_table():
    cache = {"results": {"g:m": {"ref": "g:m", "ok": False, "error_kind": "rate_limit_long", "quality": 80,
                                 "latency": 1.0, "ttft": 0.5, "tokens": 3, "tps": 2.0}}}
    rows = ranking.format_table(cache)
    assert rows[0].startswith("model") and "rate_limit" in rows[1] and "rate_limit_long" not in rows[1]
    assert ranking.format_table({}) == rows[:1]


# ===================================================================== site
def test_content_type():
    assert site.content_type(Path("a.html")) == "text/html; charset=utf-8"
    assert site.content_type(Path("a.js")) == "text/javascript; charset=utf-8"
    assert site.content_type(Path("a.mjs")) == "text/javascript; charset=utf-8"
    assert site.content_type(Path("a.png")) == "image/png"
    assert site.content_type(Path("a.bilinmeyenuzanti")) == "application/octet-stream"


def test_last_change_ignores_hidden_and_vendor(tmp_path):
    (tmp_path / "css").mkdir()
    for rel, mt in (("index.html", 1000), ("css/a.css", 3000), (".env", 9000)):
        f = tmp_path / rel
        f.write_text("x", encoding="utf-8")
        os.utime(f, (mt, mt))
    (tmp_path / "node_modules").mkdir()
    nm = tmp_path / "node_modules" / "x.js"
    nm.write_text("x", encoding="utf-8")
    os.utime(nm, (8000, 8000))
    assert site.last_change(tmp_path) == 3000
    assert site.last_change(tmp_path / "yok") == 0.0


def test_find_entry_two_levels_and_skips_hidden(tmp_path):
    assert site.find_entry(tmp_path) == ""
    (tmp_path / ".gizli").mkdir()
    (tmp_path / ".gizli" / "index.html").write_text("x", encoding="utf-8")
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "index.html").write_text("x", encoding="utf-8")
    assert site.find_entry(tmp_path) == "apps/web/index.html"
    assert site.find_entry(tmp_path / "yok") == ""


def test_safe_path_directories(tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    assert site.safe_path(tmp_path, "") == (tmp_path / "index.html").resolve()
    assert site.safe_path(tmp_path, "/./") == (tmp_path / "index.html").resolve()
    assert site.safe_path(tmp_path, "docs") is None  # no index.html in the folder
    assert site.safe_path(tmp_path, "docs\\..\\index.html") is None


def test_browser_exe_lookup(monkeypatch, tmp_path):
    for env in ("ALICE_CHROMIUM", "PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(site.shutil, "which", lambda name: None)
    monkeypatch.setattr(site, "MAC_BROWSERS", ())  # the macOS CI runner has Chrome in /Applications
    assert site.browser_exe() == ""
    monkeypatch.setattr(site.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "chromium" else None)
    assert site.browser_exe() == "/usr/bin/chromium"
    edge = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert site.browser_exe() == str(edge)
    exe = tmp_path / "my-chrome"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ALICE_CHROMIUM", str(exe))
    assert site.browser_exe() == str(exe)
    monkeypatch.setenv("ALICE_CHROMIUM", str(tmp_path / "olmayan"))
    assert site.browser_exe() == str(edge)


def test_cli_capture_runs_headless_browser(monkeypatch):
    png = b"\x89PNG" + b"0" * 2000
    cmds = []

    class Proc:  # fake browser that writes the image and exits
        def __init__(self, cmd, **kw):
            cmds.append(cmd)
            shot = next(a for a in cmd if a.startswith("--screenshot=")).split("=", 1)[1]
            Path(shot).write_bytes(png if len(cmds) == 1 else b"kucuk")  # second image is corrupt/empty

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(site, "browser_exe", lambda: "chrome")
    monkeypatch.setattr(site.subprocess, "Popen", Proc)
    for env in ("HTTPS_PROXY", "https_proxy"):  # on Windows both are the same variable
        monkeypatch.setenv(env, "http://proxy:3128")
    out = site._cli_capture("https://ornek.com", timeout=5, settle=100)
    assert out == {"masaüstü": base64.b64encode(png).decode()}
    assert cmds[0][0] == "chrome" and cmds[0][-1] == "https://ornek.com"
    assert "--window-size=1366,860" in cmds[0] and "--window-size=390,844" in cmds[1]
    assert "--virtual-time-budget=2000" in cmds[0] and "--proxy-server=http://proxy:3128" in cmds[0]
    cmds.clear()
    site._cli_capture("http://127.0.0.1:5000/", viewports={"tek": (10, 10)})
    assert not any(a.startswith("--proxy-server") for a in cmds[0])  # no proxy server is used for a local site


def test_cli_capture_timeout_and_no_browser(monkeypatch):
    monkeypatch.setattr(site, "browser_exe", lambda: "")
    assert site._cli_capture("https://x") == {}
    monkeypatch.setattr(site, "browser_exe", lambda: "chrome")
    procs = []

    class Hung:  # browser that hangs without writing an image
        def __init__(self, cmd, **kw):
            self.killed = False
            procs.append(self)

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(site.subprocess, "Popen", Hung)
    # Sizes start at the same time; when time runs out, all are closed and it returns empty.
    assert site._cli_capture("https://x", timeout=0.3) == {}
    assert len(procs) == 2 and all(p.killed for p in procs)

    def broken(cmd, **kw):
        raise OSError("tarayıcı açılamadı")

    monkeypatch.setattr(site.subprocess, "Popen", broken)
    assert site._cli_capture("https://x") == {}


def test_capture_falls_back_to_cli(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # playwright not installed
    assert site._pw_capture("https://x", 1, 0) == {}
    seen = []
    monkeypatch.setattr(site, "_cli_capture", lambda url, t, s, vp: seen.append((url, vp)) or {"x": "y"})
    assert site.capture("https://x", viewports=site.STUDY_VIEWPORTS) == {"x": "y"}
    assert seen == [("https://x", site.STUDY_VIEWPORTS)]


def test_launch_order(monkeypatch):
    calls = []

    def launch(**kw):
        calls.append(kw)
        if kw.get("channel") != "msedge" and "executable_path" not in kw:
            raise RuntimeError("tarayıcı yok")
        return "BROWSER"

    p = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.delenv("ALICE_CHROMIUM", raising=False)
    assert site._launch(p) == "BROWSER"
    assert calls == [{}, {"channel": "chrome"}, {"channel": "msedge"}]
    monkeypatch.setenv("ALICE_CHROMIUM", "/opt/chrome")
    calls.clear()
    assert site._launch(p) == "BROWSER" and calls == [{"executable_path": "/opt/chrome"}]
    monkeypatch.delenv("ALICE_CHROMIUM")
    p.chromium.launch = lambda **kw: (_ for _ in ()).throw(RuntimeError("hiç yok"))
    with pytest.raises(RuntimeError, match="hiç yok"):
        site._launch(p)


def test_reachable(monkeypatch):
    import httpx

    def resp(status, ctype, url="https://ornek.com/"):
        return SimpleNamespace(status_code=status, headers={"content-type": ctype} if ctype else {}, url=url)

    answers = [resp(200, "text/html; charset=utf-8", "https://www.ornek.com/"), resp(404, "text/html"),
               resp(200, "application/json"), resp(200, None), httpx.ConnectError("DNS hatası")]

    def get(url, **kw):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(httpx, "get", get)
    assert site.reachable("https://ornek.com") == (True, "https://www.ornek.com/")
    assert site.reachable("https://ornek.com") == (False, "HTTP 404")
    assert site.reachable("https://ornek.com") == (False, "web sayfası değil (application/json)")
    assert site.reachable("https://ornek.com")[0] is True  # no content type: counts as a page
    assert site.reachable("https://ornek.com") == (False, "ConnectError: DNS hatası")


def test_screenshots_serve_local_site(monkeypatch, tmp_path):
    import httpx

    assert site.screenshots(tmp_path) == {}
    assert site.cli_screenshots(tmp_path) == {}
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.html").write_text("<h1>Merhaba</h1>", encoding="utf-8")
    pages = []

    def grab(url, *a, **k):
        r = httpx.get(url, timeout=5, trust_env=False)  # is the local temporary server really serving?
        pages.append((url, r.status_code, r.text))
        return {"masaüstü": "png"}

    monkeypatch.setattr(site, "capture", grab)
    assert site.screenshots(tmp_path) == {"masaüstü": "png"}
    assert pages[0][0].startswith("http://127.0.0.1:") and pages[0][0].endswith("/public/index.html")
    assert pages[0][1:] == (200, "<h1>Merhaba</h1>")
    monkeypatch.setattr(site, "browser_exe", lambda: "")
    assert site.cli_screenshots(tmp_path) == {}
    monkeypatch.setattr(site, "browser_exe", lambda: "chrome")
    monkeypatch.setattr(site, "_cli_capture", lambda url, timeout: {"url": url})
    assert site.cli_screenshots(tmp_path, entry="public/index.html")["url"].endswith("/public/index.html")


def test_screenshots_close_preview_server_socket(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    servers = []
    real_serve = site.serve

    def serve(root):
        httpd, url = real_serve(root)
        servers.append(httpd)
        return httpd, url

    monkeypatch.setattr(site, "serve", serve)
    monkeypatch.setattr(site, "capture", lambda url, *a, **k: {})
    site.screenshots(tmp_path)
    sock = servers[0].socket
    try:
        assert sock.fileno() == -1
    finally:
        servers[0].server_close()
