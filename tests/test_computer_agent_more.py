"""alice/computer_agent.py: loop edge cases, actions and screenshots (fake mss/pyautogui)."""

from __future__ import annotations

import base64
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest
from PIL import Image

from alice import computer_agent as ca
from alice.computer_agent import ComputerAgent, Screen, Shot, TaskResult, parse_action, router_asker
from alice.config import alice_home


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ca.time, "sleep", lambda s: None)


class FakeScreen:
    def __init__(self, fail_on=None, observe_error=None):
        self.acts = []
        self.fail_on = fail_on
        self.observe_error = observe_error
        self.elements = [{"id": 1, "role": "Button", "name": "Kaydet", "center": (100, 50)},
                         {"id": 2, "role": "Edit", "name": "Ara", "value": "abc", "center": (10, 10)}]

    def observe(self):
        if self.observe_error:
            raise self.observe_error
        return Shot("IMG", 0, 0, 1920, 1080, self.elements, "")

    def act(self, a, shot):
        if self.fail_on and a.get("action") == self.fail_on:
            raise ValueError("olmadı")
        self.acts.append(a)
        return f"ok {a.get('action')}"


def scripted(*answers):
    it = iter(answers)
    seen = []

    def ask(messages):
        seen.append(messages)
        a = next(it)
        if isinstance(a, Exception):
            raise a
        return a

    ask.seen = seen
    return ask


# -------------------------------------------------------------- parse_action
def test_parse_action_variants():
    assert parse_action('Düşünce... {"action": "key", "keys": "ctrl+s"} bitti') == {"action": "key", "keys": "ctrl+s"}
    assert parse_action('{"thought": "t", "action": {"action": "type", "text": "a"}}') == {
        "thought": "t", "action": "type", "text": "a"}
    assert parse_action('{"action": {"point": [1, 2]}}') == {"action": "click", "point": [1, 2]}
    assert parse_action('{"action": {"id": 4}}') == {"action": "click", "id": 4}
    assert parse_action('{"action": {"keys": "enter"}}') == {"action": "", "keys": "enter"}
    multi = parse_action('{"thought": "x", "actions": [{"action": "click", "id": 1}]}')
    assert multi["actions"] == [{"action": "click", "id": 1}]


@pytest.mark.parametrize("bad", ["", "hiç json yok", "{bozuk json}"])
def test_parse_action_errors(bad):
    with pytest.raises(ValueError):
        parse_action(bad)


def test_parse_action_none_is_value_error():
    with pytest.raises(ValueError):
        parse_action(None)


def test_task_result_report_tail():
    r = TaskResult("done", "tamam", [f"{i}. adım" for i in range(1, 21)])
    rep = r.report()
    assert rep.startswith("done: tamam\nadımlar:\n9. adım") and rep.endswith("20. adım") and "\n8. adım" not in rep
    assert TaskResult("fail", "olmadı", []).report() == "fail: olmadı"


# --------------------------------------------------------------------- loop
def test_prompt_lists_elements_values_and_placeholders():
    s = FakeScreen()
    ask = scripted('{"action": "done", "summary": "ok"}')
    ComputerAgent(s, ask, settle=0).run("hedef")
    text = ask.seen[0][1]["content"][0]["text"]
    assert "GOAL: hedef" in text and "FOREGROUND WINDOW: ?" in text and "(nothing yet)" in text
    assert "[2] Edit 'Ara' değer='abc'" in text and ask.seen[0][0]["content"] == ca.SYSTEM
    s.elements = []
    ask2 = scripted('{"action": "fail", "summary": "giriş gerekli"}')
    res = ComputerAgent(s, ask2, settle=0).run("x")
    assert res.status == "fail" and res.summary == "giriş gerekli"
    assert "(none: use points)" in ask2.seen[0][1]["content"][0]["text"]


def test_batch_executes_in_order_and_caps_at_five():
    s = FakeScreen()
    batch = [{"action": "key", "keys": str(i)} for i in range(8)]
    ask = scripted(json.dumps({"thought": "toplu", "actions": batch + ["çöp"]}), '{"action": "done", "summary": "s"}')
    emitted = []
    res = ComputerAgent(s, ask, emit=emitted.append, settle=0).run("x")
    assert [a["keys"] for a in s.acts] == ["0", "1", "2", "3", "4"]
    assert res.status == "done" and len(res.steps) == 1 and emitted == res.steps
    assert "ok key; ok key" in res.steps[0] and "(toplu)" in res.steps[0]


def test_batch_stops_at_error_and_at_done():
    s = FakeScreen(fail_on="type")
    ask = scripted(json.dumps({"actions": [{"action": "click", "id": 1}, {"action": "type", "text": "a"},
                                           {"action": "key", "keys": "enter"}]}),
                   json.dumps({"actions": [{"action": "click", "id": 2}, {"action": "done", "summary": "x"},
                                           {"action": "key", "keys": "z"}]}),
                   '{"action": "done", "summary": "bitti"}')
    res = ComputerAgent(s, ask, settle=0).run("x")
    assert [a["action"] for a in s.acts] == ["click", "click"]  # not run after an error or after done
    assert "ERROR olmadı" in res.steps[0] and res.status == "done" and res.summary == "bitti"


def test_done_summary_falls_back_to_outer():
    res = ComputerAgent(FakeScreen(), scripted('{"summary": "dış", "actions": [{"action": "done"}]}'),
                        settle=0).run("x")
    assert res.status == "done" and res.summary == "dış"


def test_unparseable_and_empty_answers_are_steps():
    res = ComputerAgent(FakeScreen(), scripted("bilmiyorum", '{"actions": []}', '{"actions": ["x"]}'),
                        settle=0).run("x", max_steps=3)
    assert res.status == "max_steps" and res.summary == "adım sınırı doldu"
    assert res.steps[0].startswith("1. model cevabı okunamadı") and res.steps[1:] == ["2. boş aksiyon", "3. boş aksiyon"]


def test_two_model_errors_in_a_row_abort_with_quota_hint():
    emitted = []
    res = ComputerAgent(FakeScreen(), scripted(RuntimeError("429 Too Many"), RuntimeError("HTTP 429 quota")),
                        emit=emitted.append, settle=0).run("x")
    assert res.status == "error" and "günlük ücretsiz kota" in res.summary
    assert len(emitted) == 2 and emitted[0].startswith("görme modeli hatası")


def test_model_error_counter_resets_after_success():
    s = FakeScreen()
    ask = scripted(RuntimeError("timeout"), '{"action": "key", "keys": "a"}', RuntimeError("timeout"),
                   '{"action": "done", "summary": "ok"}')
    assert ComputerAgent(s, ask, settle=0).run("x").status == "done"
    res = ComputerAgent(s, scripted(RuntimeError("bağlantı"), RuntimeError("bağlantı koptu")), settle=0).run("x")
    assert res.summary == "görme modeli kullanılamıyor: bağlantı koptu"


def test_observe_failure_stop_and_deadline(monkeypatch):
    res = ComputerAgent(FakeScreen(observe_error=OSError("mss yok")), scripted(), settle=0).run("x")
    assert res.status == "error" and res.summary == "ekran okunamadı: mss yok"
    assert ComputerAgent(FakeScreen(), scripted(), stop=lambda: True).run("x").status == "stopped"
    t = [0.0]
    monkeypatch.setattr(ca.time, "monotonic", lambda: t[0])

    def slow(messages):
        t[0] += 100
        return '{"action": "key", "keys": "%d"}' % t[0]

    res = ComputerAgent(FakeScreen(), slow, settle=0).run("x", max_seconds=150)
    assert res.status == "fail" and res.summary == "150 sn içinde bitmedi" and len(res.steps) == 2


def test_repeat_detection_ignores_thought_changes():
    s = FakeScreen()
    answers = [json.dumps({"thought": f"deneme {i}", "action": "click", "id": 1}) for i in range(8)]
    res = ComputerAgent(s, scripted(*answers), settle=0).run("x")
    assert res.status == "fail" and "tekrarlanıyor" in res.summary and len(s.acts) == 4  # one recovery attempt


def test_router_asker_passes_role_and_effort():
    calls = []

    class R:
        def chat(self, role, messages, **kw):
            calls.append((role, messages, kw))
            return SimpleNamespace(text="cevap")

    assert router_asker(R())(["m"]) == "cevap"
    assert calls[0][0] == "vision" and calls[0][2] == {"temperature": 0, "max_tokens": 2000,
                                                      "gemini_extra": {"reasoning_effort": "low"}}
    router_asker(R(), role="brain", effort="")(["m"])
    assert calls[1][0] == "brain" and calls[1][2]["gemini_extra"] is None


def test_log_line_appends_truncated():
    alice_home().mkdir(parents=True, exist_ok=True)
    ca.log_line("x" * 1000)
    ca.log_line("ikinci")
    lines = (alice_home() / "voice.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0].endswith("computer_task " + "x" * 400) and lines[1].endswith("ikinci")


def test_log_line_ignores_missing_dir():
    ca.log_line("dizin yok")  # alice_home not created: must not raise an error


# ---------------------------------------------------------------- Screen.act
class FakePG:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a, k))


@pytest.fixture
def io_fakes(monkeypatch):
    pg = FakePG()
    log = []
    monkeypatch.setattr("alice.tools.computer._pyautogui", lambda: pg)
    monkeypatch.setattr("alice.tools.computer.type_text", lambda t: log.append(("type", t)))
    monkeypatch.setattr("alice.tools.computer.press_keys", lambda k: log.append(("keys", k)))
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: log.append(("open", t)) or f"açıldı {t}")
    return pg, log


SHOT = Shot("", 100, 50, 2000, 1000, [{"id": 3, "center": (500, 400)}])


def test_act_keyboard_open_wait(io_fakes, monkeypatch):
    pg, log = io_fakes
    sleeps = []
    monkeypatch.setattr(ca.time, "sleep", sleeps.append)
    sc = Screen(None)
    assert sc.act({"action": "type", "text": "merhaba", "enter": True}, SHOT) == "yazıldı: merhaba"
    assert sc.act({"action": "key", "keys": "ctrl+s"}, SHOT) == "tuş: ctrl+s"
    assert sc.act({"action": "open", "target": "notepad"}, SHOT) == "açıldı notepad"
    assert log == [("type", "merhaba"), ("keys", "enter"), ("keys", "ctrl+s"), ("open", "notepad")]
    assert sc.act({"action": "wait", "seconds": 60}, SHOT) == "beklendi"
    sc.act({"action": "wait", "seconds": 0}, SHOT)
    assert sleeps == [1.5, 5.0, 0.5] and pg.calls == []


def test_act_mouse_actions(io_fakes):
    pg, _ = io_fakes
    sc = Screen(None)
    assert sc.act({"action": "double_click", "id": "3"}, SHOT) == "double_click (500,400)"
    assert pg.calls[-1] == ("click", (500, 400), {"clicks": 2, "button": "left", "interval": 0.08})
    sc.act({"action": "right_click", "point": [0, 0]}, SHOT)
    assert pg.calls[-1] == ("click", (100, 50), {"clicks": 1, "button": "right", "interval": 0.08})
    assert sc.act({"action": "scroll", "point": [500, 500], "direction": "up", "amount": 50}, SHOT) == "kaydırıldı up 10"
    assert pg.calls[-1] == ("scroll", (1200,), {})
    sc.act({"action": "scroll", "point": [500, 500], "amount": 0}, SHOT)
    assert pg.calls[-1] == ("scroll", (-120,), {})
    res = sc.act({"action": "drag", "point": [0, 0], "to": [1000, 1000]}, SHOT)
    assert res == "sürüklendi (100,50) -> (2100,1050)"
    assert pg.calls[-1] == ("dragTo", (2100, 1050), {"duration": 0.5, "button": "left"})
    with pytest.raises(ValueError, match="bilinmeyen aksiyon"):
        sc.act({"action": "dance", "point": [1, 1]}, SHOT)


def test_point_errors():
    with pytest.raises(ValueError, match="öğe 9 yok"):
        Screen.point({"id": 9}, SHOT)
    with pytest.raises(ValueError, match="id ya da point"):
        Screen.point({"point": [1]}, SHOT)
    with pytest.raises(ValueError, match="id ya da point"):
        Screen.point({}, SHOT)
    assert Screen.point({"id": 0, "point": [0, 0]}, Shot("", 0, 0, 10, 10, [{"id": 0, "center": (7, 7)}])) == (7, 7)


# ------------------------------------------------------------ Screen.observe
class FakeSct:
    def __init__(self, monitors):
        self.monitors = monitors
        self.grabbed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def grab(self, mon):
        self.grabbed.append(mon)
        w, h = mon["width"], mon["height"]
        return SimpleNamespace(size=(w, h), bgra=b"\x10\x20\x30\x00" * (w * h))


@pytest.fixture
def fake_mss(monkeypatch):
    mons = [{"left": 0, "top": 0, "width": 4000, "height": 1000},
            {"left": 0, "top": 0, "width": 2000, "height": 1000},
            {"left": 2000, "top": 0, "width": 640, "height": 480}]
    holder = {}

    def mss():
        holder["sct"] = FakeSct(mons)
        return holder["sct"]

    mod = types.ModuleType("mss")
    mod.mss = mss
    monkeypatch.setitem(sys.modules, "mss", mod)
    monkeypatch.setattr("alice.tools.computer._dpi_aware", lambda: None)
    return holder


def test_observe_downscales_and_keeps_elements(fake_mss, monkeypatch):
    items = [{"id": 1, "rect": (10, 10, 200, 60), "center": (105, 35)},
             {"id": 2, "rect": None},
             {"id": 3, "rect": (-500, -500, -400, -400), "center": (0, 0)}]  # off-screen: not drawn, kept in list
    monkeypatch.setattr("alice.tools.desktop.collect_ui_elements", lambda ctx, limit: ("Not Defteri", items))
    import ctypes

    monkeypatch.setattr(ctypes, "windll", None, raising=False)  # _monitor: no foreground window found -> monitor 1
    shot = Screen("CTX", max_width=1000).observe()
    assert shot.title == "Not Defteri" and [e["id"] for e in shot.elements] == [1, 3]
    assert (shot.left, shot.top, shot.width, shot.height) == (0, 0, 2000, 1000)
    img = Image.open(io.BytesIO(base64.b64decode(shot.png_b64)))
    assert img.size == (1000, 500)


def test_observe_explicit_monitor_and_no_uia(fake_mss, monkeypatch):
    def broken(ctx, limit):
        raise RuntimeError("UIA yok")

    monkeypatch.setattr("alice.tools.desktop.collect_ui_elements", broken)
    shot = Screen(None, max_width=1280, monitor=2).observe()
    assert shot.elements == [] and shot.title == "" and (shot.left, shot.width) == (2000, 640)
    with Image.open(io.BytesIO(base64.b64decode(shot.png_b64))) as img:
        size = img.size
    assert size == (640, 480)  # not enlarged


def test_monitor_picks_foreground_window_monitor(monkeypatch):
    import ctypes

    sct = FakeSct([{}, {"left": 0, "top": 0, "width": 1920, "height": 1080},
                   {"left": 1920, "top": 0, "width": 1920, "height": 1080}])

    def get_rect(hwnd, ref):
        r = ref._obj
        r.left, r.top, r.right, r.bottom = 2000, 100, 3000, 900

    user32 = SimpleNamespace(GetForegroundWindow=lambda: 1, GetWindowRect=get_rect)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    assert Screen._monitor(sct)["left"] == 1920
    monkeypatch.setattr(ctypes, "windll", None, raising=False)
    assert Screen._monitor(sct)["left"] == 0
