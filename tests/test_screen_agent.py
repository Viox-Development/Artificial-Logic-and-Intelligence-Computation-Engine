"""Screen agent (Jev + code): item list, actions (click/type/key/drag), verification, visual fallback, trace log."""

from __future__ import annotations

import json

from PIL import Image

from alice.decider import Answer, Decision, Noul, _validate
from alice.reflex import perceive
from alice.reflex.agent import ScreenAgent, goal_keys
from alice.reflex.perceive import Item, Screen, build_items, is_goal_echo


# ------------------------------------------------------------------------------------------- perception
def test_build_items_merges_sources_drops_goal_echo_and_adds_context():
    ax = [Item("", "ax", "button", "Kaydet", (0.8, 0.02, 0.9, 0.06), ref=7),
          Item("", "ax", "Edit", "", (0.1, 0.9, 0.9, 0.95), editable=True, ref=8),
          Item("", "ax", "button", "", None)]  # unnamed and not editable: dropped
    ocr = [("Kaydet", (0.81, 0.03, 0.88, 0.05)),  # the button's own label: not a separate option
           ("projeler kısmındaki ilk projeye tıkla", (0.4, 0.3, 0.7, 0.33)),  # the user's message
           ("Uydurma bir savunma", (0.02, 0.14, 0.15, 0.16)), ("Sil", (0.2, 0.14, 0.23, 0.16)),
           ("Proje genel olarak", (0.02, 0.2, 0.15, 0.22)), ("Sil", (0.2, 0.2, 0.23, 0.22))]
    items = build_items(ax, ocr, "Projeler kısmındaki ilk projeye tıkla")
    labels = [i.label for i in items]
    assert "projeler kısmındaki ilk projeye tıkla" not in labels  # echo of the goal
    assert labels.count("Kaydet") == 1 and next(i for i in items if i.label == "Kaydet").source == "ax+ocr"
    sil = [i for i in items if i.label == "Sil"]
    assert {i.context for i in sil} == {"in the row of 'Uydurma bir savunma'", "in the row of 'Proje genel olarak'"}
    assert [i.key for i in items] == [f"i{n}" for n in range(1, len(items) + 1)]
    kaydet = next(i for i in items if i.label == "Kaydet")
    assert kaydet.describe() == "button 'Kaydet' (top-right)"
    assert next(i for i in items if i.label == "Uydurma bir savunma").describe().startswith("text 'Uydurma")
    assert items[0].editable or "proje" in items[0].label.lower()  # relevant/editable ones first


def test_goal_echo_needs_most_words_from_goal():
    assert is_goal_echo("ilk projeye tıkla lütfen", "Projeler kısmındaki ilk projeye tıkla")
    assert not is_goal_echo("Projeler", "Projeler kısmındaki ilk projeye tıkla")  # short: may be the goal itself
    assert not is_goal_echo("Uydurma bir savunma ve hav", "ilk projeye tıkla")


def test_ocr_lines_become_fractions():
    img = Image.new("RGB", (1000, 500))
    out = perceive.ocr_image(img, recognize=lambda im: [("Merhaba", (100, 50, 200, 25))])
    assert out == [("Merhaba", (0.1, 0.1, 0.3, 0.15))]


def test_goal_keys():
    assert goal_keys("ctrl+shift+n ile gizli pencere aç, sonra F11") == ["ctrl+shift+n", "f11"]


# ------------------------------------------------------------------------------------------- agent
class FakeScreen:
    """Two-screen app: list -> project page. Cards can be dragged, the field is editable."""

    def __init__(self):
        self.page = "list"
        self.acts = []
        self.image = None

    def screen(self, goal=""):
        if self.page == "list":
            items = [Item("i1", "ocr", "text", "Uydurma bir savunma", (0.02, 0.14, 0.15, 0.16)),
                     Item("i2", "ax", "Edit", "Ara", (0.1, 0.02, 0.4, 0.05), editable=True, ref=2),
                     Item("i3", "ocr", "text", "Yapılacaklar", (0.5, 0.1, 0.6, 0.12)),
                     Item("i4", "ocr", "text", "Bitti", (0.8, 0.1, 0.9, 0.12))]
        else:
            items = [Item("i1", "ocr", "text", "Proje: Uydurma bir savunma", (0.3, 0.05, 0.6, 0.08))]
        return Screen(self.page, "desktop", items, image=self.image)

    def act(self, action, item=None, text="", keys="", drop=None):
        self.acts.append((action, item.label if item else None, text, keys, drop.label if drop else None))
        if action == "click" and item and item.label == "Uydurma bir savunma":
            self.page = "project"

    def click_at(self, fx, fy):
        self.acts.append(("click_at", round(fx, 2), round(fy, 2)))
        self.page = "project"


class Brain:
    """Scenario: a fake Jev that answers, step by step, with what to reply at each step."""

    supports_images = False
    names = ["fake"]

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def decide(self, state, questions, images=None):
        _validate(questions)  # like the real engine: a single-option Choice is rejected
        self.calls.append((state, questions, images))
        s = self.steps.pop(0)
        items = {v: k for k, v in questions["item"].criteria.items()} if "item" in questions else {}

        def key_of(label):
            return next((k for k, v in questions["item"].criteria.items() if f"'{label}'" in v), "i1")

        ans = {"action": Answer("choice", s.get("action", "click"), s.get("conf", 0.9)),
               "done": Answer("noul", s.get("done", 0.05), 0.9), "missing": Answer("noul", s.get("missing", 0.05), 0.9),
               "key": Answer("choice", s.get("key", "enter"), 0.9)}
        if items:
            ans["item"] = Answer("choice", key_of(s.get("item", "")), 0.9)
            ans["drop"] = Answer("choice", key_of(s.get("drop", "")), 0.9)
        if "field" in questions:
            ans["field"] = Answer("choice", key_of(s.get("field", s.get("item", ""))), 0.9)
        if "target" in questions:
            ans["target_visible"] = Answer("noul", s.get("visible", 0.95), 0.9)
            ans["target"] = Answer("box", s.get("box", {"left": 0.0, "top": 0.1, "right": 0.2, "bottom": 0.2}), 0.8)
        return Decision(ans, "fake", 0.01)


def test_click_flow_english_goal_notes_and_trace(tmp_path):
    obs = FakeScreen()
    brain = Brain([{"action": "click", "item": "Uydurma bir savunma"}, {"done": 0.95}])
    trace = tmp_path / "trace.jsonl"
    res = ScreenAgent(obs, brain, translate=lambda g: "Open the first project", settle=0,
                      trace_path=trace).run("İlk projeyi aç ve içine bak", notes="Projects are listed on the left.")
    assert res.status == "done" and obs.acts[0][:2] == ("click", "Uydurma bir savunma")
    state, questions, _ = brain.calls[0]
    assert state["goal"] == "Open the first project" and state["goal_original_language"] == "İlk projeyi aç ve içine bak"
    assert state["notes"] == "Projects are listed on the left." and state["recent_actions"] == ["none yet"]
    assert "text 'Uydurma bir savunma' (top-left)" in questions["item"].criteria.values()
    assert isinstance(questions["done"], Noul) and questions["done"].criteria["false"]
    second = brain.calls[1][0]["recent_actions"]
    assert second[0].startswith("clicked text 'Uydurma bir savunma'") and second[0].endswith("screen changed")
    rows = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["action"] == "click" and rows[0]["items"] == 4 and rows[0]["goal_en"] == "Open the first project"


def test_single_click_goal_stops_when_screen_changes():
    obs = FakeScreen()
    res = ScreenAgent(obs, Brain([{"action": "click", "item": "Uydurma bir savunma"}]), settle=0).run(
        "Projelerdeki ilk projeye tıkla")
    assert res.status == "clicked" and "Uydurma" in res.reason


def test_type_key_and_drag_actions():
    obs = FakeScreen()
    brain = Brain([{"action": "type_text", "item": "Ara"}, {"action": "press_key", "key": "enter"},
                   {"action": "drag", "item": "Yapılacaklar", "drop": "Bitti"}, {"done": 0.9}])
    res = ScreenAgent(obs, brain, settle=0).run("ara kutusuna lofi yaz, enter'a bas, kartı Bitti'ye taşı",
                                                inputs={"ara": "lofi"})
    assert res.status == "done"
    assert obs.acts == [("type_text", "Ara", "lofi", "", None), ("press_key", None, "", "enter", None),
                        ("drag", "Yapılacaklar", "", "", "Bitti")]
    assert "typed 'lofi' into" in brain.calls[1][0]["recent_actions"][0]
    assert "onto text 'Bitti'" in brain.calls[3][0]["recent_actions"][-1]


def test_useless_action_is_not_repeated_forever():
    obs = FakeScreen()
    res = ScreenAgent(obs, Brain([{"action": "press_key", "key": "tab"}] * 5), settle=0).run("bir şey yap")
    assert res.status == "needs_help" and "değiştirmedi" in res.reason
    assert len(obs.acts) == 2


def test_missing_target_uses_screen_or_asks_for_help():
    obs = FakeScreen()
    res = ScreenAgent(obs, Brain([{"action": "click", "missing": 0.9}]), settle=0).run("dişli ikonuna bas")
    assert res.status == "needs_help" and "bulunamadı" in res.reason  # can't look at the screen

    obs = FakeScreen()
    obs.image = Image.new("RGB", (1200, 800))
    brain = Brain([{"action": "click", "missing": 0.9, "visible": 0.95}, {"visible": 0.95}, {"done": 0.9}])
    brain.supports_images = True
    res = ScreenAgent(obs, brain, settle=0).run("dişli ikonuna bas ve ayarları aç")
    assert obs.acts[0][0] == "click_at" and res.status == "done"  # looked closer, then clicked
    assert "screen" in brain.calls[0][0] and brain.calls[0][2]  # the image was sent too


def test_unsure_action_kind_escalates():
    res = ScreenAgent(FakeScreen(), Brain([{"action": "click", "conf": 0.2}]), settle=0).run("x")
    assert res.status == "needs_help" and "emin değil" in res.reason


# ------------------------------------------------------------------------------------------- actions
def test_desktop_actions_map_to_mouse_keyboard_and_uia(monkeypatch):
    from alice.reflex import observers
    from alice.tools import computer, desktop

    calls = []

    class PG:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append((name, a, k))

    monkeypatch.setattr(computer, "_pyautogui", lambda: PG())
    monkeypatch.setattr(computer, "press_keys", lambda k: calls.append(("keys", k)))
    monkeypatch.setattr(computer, "type_text", lambda t: calls.append(("type", t)))
    monkeypatch.setattr(desktop, "ui_act", lambda ctx, ref, action, text=None: calls.append(("ui", ref, action, text)))
    monkeypatch.setattr(observers.time, "sleep", lambda s: None)
    obs = observers.DesktopObserver(ctx=None)
    obs._rect = (100, 50, 1000, 500)
    btn = Item("i1", "ax", "button", "Kaydet", (0.5, 0.5, 0.6, 0.6), ref=3)
    txt = Item("i2", "ocr", "text", "Kart", (0.1, 0.1, 0.2, 0.2))
    dst = Item("i3", "ocr", "text", "Bitti", (0.8, 0.1, 0.9, 0.2))
    field = Item("i4", "ax", "Edit", "Ad", (0.1, 0.8, 0.5, 0.85), editable=True, ref=9)
    obs.act("click", btn)
    obs.act("click", txt)
    obs.act("double_click", txt)
    obs.act("right_click", txt)
    obs.act("type_text", field, text="ALICE")
    obs.act("press_key", keys="ctrl+s")
    obs.act("drag", txt, drop=dst)
    assert calls[0] == ("ui", 3, "click", None)  # via accessibility
    assert calls[1] == ("click", (250, 125), {})  # window (100,50) + 0.15*1000, 0.15*500
    assert calls[2][0] == "doubleClick" and calls[3][0] == "rightClick"
    assert calls[4] == ("ui", 9, "set_text", "ALICE") and calls[5] == ("keys", "ctrl+s")
    assert calls[6] == ("moveTo", (250, 125), {}) and calls[7][0] == "dragTo" and calls[7][1] == (950, 125)
    assert obs._frac((600, 300, 700, 350)) == (0.5, 0.5, 0.6, 0.6) and obs._frac((0, 0, 50, 40)) is None


def test_browser_key_names():
    from alice.reflex.observers import _browser_key

    assert _browser_key("ctrl+s") == "Control+s" and _browser_key("enter") == "Enter" and _browser_key("f5") == "F5"


def test_noul_criteria_go_to_jev():
    import httpx

    from alice.decider import JevEngine

    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"answers": {"done": {"type": "noul", "noul": 0.8}}})

    eng = JevEngine("https://api.typesafe.ai/v1/systemone", "jev-latest", "k",
                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    eng.decide({"goal": "g"}, {"done": Noul("Is it done?", {"true": "yes case", "false": "no case"})})
    assert sent["questions"]["done"]["criteria"] == {"true": "yes case", "false": "no case"}


def test_browser_snapshot_lists_drag_sources_and_drop_targets():
    from alice.tools.browser import SNAPSHOT_JS

    assert "[draggable=true]" in SNAPSHOT_JS and "[ondrop]" in SNAPSHOT_JS


# ------------------------------------------------------------------------------------- found on real Windows
def test_words_match_turkish_capital_i():
    from alice.reflex.perceive import words

    assert words("ALİCE uygulaması") == ["alice", "uygulamasi"] and words("ALICE") == ["alice"]


def test_button_named_in_goal_is_not_an_echo():
    # Unciv main menu: with goal "start a new game", the "Start new game" button was dropped as an echo.
    assert not is_goal_echo("Start new game", "start a new game")
    items = build_items([], [("Start new game", (0.3, 0.5, 0.5, 0.55)), ("Load game", (0.3, 0.6, 0.5, 0.65))],
                        "start a new game")
    assert items[0].label == "Start new game"
    longer = [("Click on the Start new game button", (0.1, 0.1, 0.5, 0.12))]
    assert build_items([], longer, "click on the start new game button") == []
    assert len(build_items([], longer, "click on the start new game button", drop_echo=False)) == 1  # game window


def test_user_message_echo_is_dropped_even_when_goal_was_rephrased():
    msg = "Şuanda reflex katmanı ile ekrana bakıp projeler kısmındaki ilk projeye tıkla"
    goal = "ALICE uygulamasında Projeler bölümündeki ilk projeye tıkla"
    assert not is_goal_echo(msg, goal)  # when the brain model rewrites the goal, overlap stays below 60%
    assert is_goal_echo(msg, goal, (msg,))
    items = build_items([], [(msg, (0.5, 0.3, 0.9, 0.33)), ("Uydurma bir savunma", (0.02, 0.2, 0.2, 0.22))], goal,
                        echoes=(msg,))
    assert [i.label for i in items] == ["Uydurma bir savunma"]


def test_duplicate_controls_are_merged_and_big_items_are_not_row_context():
    ax = [Item("", "ax", "Edit", "Address Bar", (0.18, 0.05, 0.6, 0.08), editable=True, ref=1),
          Item("", "ax", "Edit", "Address Bar", (0.181, 0.05, 0.6, 0.08), editable=True, ref=2),
          Item("", "ax", "Document", "Sayfa", (0.0, 0.0, 1.0, 1.0), ref=3),
          Item("", "ax", "Button", "Sil", (0.8, 0.2, 0.85, 0.22), ref=4),
          Item("", "ax", "Button", "Sil", (0.8, 0.3, 0.85, 0.32), ref=5)]
    items = build_items(ax, [], "adres çubuğuna yaz")
    assert [i.label for i in items].count("Address Bar") == 1
    assert all("Sayfa" not in i.context for i in items)


def test_phantom_webview_tab_strip_and_window_union():
    from alice.reflex.observers import is_phantom_tab, window_union

    doc = [(10, 30, 1300, 870)]
    assert is_phantom_tab("TabItem", "ALICE - Memory usage - 31.8 MB", (14, 30, 40, 70), doc)
    assert is_phantom_tab("Button", "New Tab", (6, 30, 36, 70), doc)  # sticks out a few pixels from the page
    assert not is_phantom_tab("Button", "New Tab", (6, 0, 36, 25), doc)  # real browser: the strip is above the page
    assert not is_phantom_tab("Button", "Kaydet", (14, 30, 40, 70), doc)
    # Notepad + a Save window overflowing downward; the part off-screen is clipped.
    assert window_union([(590, 640, 2130, 1194), (596, 640, 1796, 1360)], (-1920, 0, 3440, 1440)) == \
        (590, 640, 1540, 720)
    assert window_union([(3000, 1400, 3600, 1600)], (0, 0, 3440, 1440)) is None  # only 40 pixels left on screen
    assert window_union([], (0, 0, 10, 10)) is None


def test_desktop_screen_drops_webview_body_and_phantom_tabs(monkeypatch):
    from alice.reflex import observers
    from alice.tools import desktop

    def el(i, role, name, rect, auto_id=""):
        return {"id": i, "role": role, "name": name, "auto_id": auto_id, "value": "", "enabled": True, "rect": rect}

    raw = [el(1, "Document", "ALICE", (156, 228, 1312, 1070)),
           el(2, "Document", "", (156, 228, 1312, 1070), "view_1055"),
           el(3, "Button", "Close tab", (172, 230, 188, 245)),
           el(4, "Button", "Gönder", (1100, 600, 1150, 640))]
    monkeypatch.setattr(desktop, "collect_ui_elements", lambda ctx, title, limit=200, **k: ("ALICE", raw))
    monkeypatch.setattr(observers, "ocr_available", lambda: (False, "yok"))
    obs = observers.DesktopObserver(ctx=None)
    monkeypatch.setattr(obs, "snapshot", lambda hwnd=0: setattr(obs, "_rect", (156, 198, 1320, 880)))
    assert [i.label for i in obs.screen("gönder").items] == ["Gönder"]


def test_drag_that_worked_is_likely_done_not_failed():
    steps = [{"action": "drag", "item": "Yapılacaklar", "drop": "Bitti"}, {"action": "wait", "conf": 0.39, "done": 0.79}]
    res = ScreenAgent(FakeScreen(), Brain(steps), settle=0).run("Yapılacaklar kartını Bitti'ye sürükle")
    assert res.status == "needs_help"  # FakeScreen does not change the screen on drag: unclear whether it worked

    class Moves(FakeScreen):
        def act(self, action, item=None, text="", keys="", drop=None):
            super().act(action, item, text, keys, drop)
            self.page = "project"

    res = ScreenAgent(Moves(), Brain(steps), settle=0).run("Yapılacaklar kartını Bitti'ye sürükle")
    assert res.status == "likely_done" and "0.79" in res.reason


def test_type_text_goes_to_the_field_and_is_not_repeated():
    class Explorer(FakeScreen):
        addr = "C:\\Windows"

        def screen(self, goal=""):
            items = [Item("i1", "ax", "TreeItem", "Desktop", (0.0, 0.2, 0.1, 0.22), ref=1),
                     Item("i2", "ax", "Edit", "Address Bar", (0.2, 0.05, 0.6, 0.08), self.addr, editable=True, ref=2)]
            return Screen(self.addr, "desktop", items)

        def act(self, action, item=None, text="", keys="", drop=None):
            super().act(action, item, text, keys, drop)
            if action == "type_text":
                self.addr = text

    obs = Explorer()
    brain = Brain([{"action": "type_text", "item": "Desktop", "field": "Address Bar"}] * 4)
    res = ScreenAgent(obs, brain, settle=0).run("adres çubuğuna C:\\Users yaz", inputs={"adres": "C:\\Users"})
    assert obs.acts == [("type_text", "Address Bar", "C:\\Users", "", None)]  # not to 'Desktop' in the tree
    assert res.status == "needs_help" and "already contains" in res.reason


def test_single_field_or_single_item_is_used_without_asking():
    class Notepad(FakeScreen):
        def screen(self, goal=""):
            items = [Item("i1", "ax", "Document", "Metin düzenleyici", (0.0, 0.1, 1.0, 0.9), editable=True, ref=1),
                     Item("i2", "ax", "MenuItem", "Dosya", (0.0, 0.0, 0.05, 0.05), ref=2)]
            return Screen("Adsız, Not Defteri", "desktop", items[: 1 if self.page == "project" else 2])

    obs = Notepad()
    brain = Brain([{"action": "type_text", "item": "Dosya"}, {"done": 0.95}])
    res = ScreenAgent(obs, brain, settle=0).run("merhaba yaz", inputs={"x": "merhaba"})
    assert "field" not in brain.calls[0][1] and obs.acts[0][:3] == ("type_text", "Metin düzenleyici", "merhaba")
    assert res.status == "done"

    obs.page = "project"  # only one item on screen: the 'item' question is not asked either
    brain = Brain([{"action": "click"}, {"done": 0.95}])
    ScreenAgent(obs, brain, settle=0).run("belgeye tıkla ve bitir")
    assert "item" not in brain.calls[0][1] and obs.acts[-1][:2] == ("click", "Metin düzenleyici")


def test_screen_read_is_retried_once(monkeypatch):
    from alice.reflex import agent

    monkeypatch.setattr(agent, "SCREEN_RETRY", 0)

    class Flaky(FakeScreen):
        fails = 1

        def screen(self, goal=""):
            if self.fails:
                self.fails -= 1
                raise OSError(-2146233083, None)
            return super().screen(goal)

    res = ScreenAgent(Flaky(), Brain([{"done": 0.95}]), settle=0).run("bak")
    assert res.status == "done"
    obs = Flaky()
    obs.fails = 2
    assert ScreenAgent(obs, Brain([]), settle=0).run("bak").status == "error"


def test_trace_has_item_list_confidences_and_result(tmp_path):
    trace = tmp_path / "t.jsonl"
    ScreenAgent(FakeScreen(), Brain([{"action": "click", "item": "Uydurma bir savunma"}]), settle=0,
                trace_path=trace).run("Projelerdeki ilk projeye tıkla")
    rows = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["item_conf"] == 0.9 and rows[0]["list"][0].startswith("i1 text 'Uydurma bir savunma'")
    assert "@(0.02, 0.14, 0.15, 0.16)" in rows[0]["list"][0]
    assert rows[-1]["result"] == "clicked" and rows[-1]["steps"] == 1


def test_ocr_waits_on_winrt_operation_inside_and_outside_event_loop():
    import asyncio

    class Op:  # like IAsyncOperation: not a coroutine, only awaitable
        def __await__(self):
            yield from asyncio.sleep(0).__await__()
            return "sonuç"

    assert perceive._wait(Op()) == "sonuç"

    async def inside():
        return perceive._wait(Op())

    assert asyncio.run(inside()) == "sonuç"


def test_set_text_falls_back_to_keyboard_when_field_reverts(monkeypatch):
    from alice.tools import computer, desktop

    calls = []

    class Addr:  # Gezgin (Explorer) address bar: accepts what is typed, then reverts to the old address
        def set_edit_text(self, t):
            calls.append(("set", t))

        def get_value(self):
            return "C:\\Windows"

        def click_input(self):
            calls.append(("click",))

    class Doc(Addr):  # value can't be read: assume the typed text stuck, so it is not typed twice
        def get_value(self):
            raise RuntimeError("desen yok")

    monkeypatch.setattr(desktop, "SET_TEXT_SETTLE", 0)
    monkeypatch.setattr(computer, "press_keys", lambda k: calls.append(("keys", k)))
    monkeypatch.setattr(computer, "type_text", lambda t: calls.append(("type", t)))
    ctx = type("C", (), {"state": {"ui_elems": {5: Addr(), 6: Doc()}}})()
    desktop.ui_act(ctx, 5, "set_text", "C:\\Users")
    assert calls == [("set", "C:\\Users"), ("click",), ("keys", "ctrl+a"), ("type", "C:\\Users")]
    calls.clear()
    desktop.ui_act(ctx, 6, "set_text", "merhaba")
    assert calls == [("set", "merhaba")]


def test_reflex_refuses_to_type_into_alice_itself(monkeypatch, tmp_path):
    import pytest

    from alice.reflex import observers
    from alice.tools.base import ToolContext, ToolError
    from alice.tools.meta import Reflex, _last_user_text

    monkeypatch.setattr(observers, "foreground_title", lambda: "ALICE")
    ctx = ToolContext(cwd=tmp_path, config=None, router=object(), decider=object())
    with pytest.raises(ToolError, match="ALICE'in kendisi"):
        Reflex().run(ctx, goal="Not Defteri'ni aç, Ctrl+S'ye bas", target="desktop")
    observers.DesktopObserver(ctx, None, "ALICE'te ilk projeye tıkla")  # fine when the target is ALICE itself
    observers.DesktopObserver(ctx, "Not Defteri", "merhaba yaz")  # fine when a window is given

    ctx.state["agent"] = type("A", (), {"messages": [
        {"role": "system", "content": "s"}, {"role": "user", "content": "ilk projeye tıkla"},
        {"role": "user", "content": "devam et", "_nudge": True}]})()
    assert _last_user_text(ctx) == ("ilk projeye tıkla",)
    ctx.state["agent"].messages.append({"role": "user", "_display": "ikinci projeye tıkla",
                                        "content": "ikinci projeye tıkla\n\n[hatırlanan] Uydurma bir savunma sanayi"})
    assert _last_user_text(ctx) == ("ikinci projeye tıkla",)  # the memory attachment should not count as an echo


# ------------------------------------------------------------------------------------------- keyboard
def test_keyboard_typing_combos_and_holding_on_desktop(monkeypatch):
    from alice.reflex import observers
    from alice.tools import computer

    calls = []

    class PG:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append((name, a))

    monkeypatch.setattr(computer, "_pyautogui", lambda: PG())
    monkeypatch.setattr(computer.time, "sleep", lambda s: calls.append(("sleep", s)))
    monkeypatch.setattr(computer, "type_text", lambda t: calls.append(("type", t)))
    obs = observers.DesktopObserver(ctx=None)
    obs.act("type_keys", text="1234*5678=")
    obs.hold_seconds = 2
    obs.act("hold_key", keys="shift+w")
    obs.act("press_key", keys="ctrl+shift+s")
    assert calls[0] == ("type", "1234*5678=")  # to the foreground window, without selecting a field
    assert calls[1:6] == [("keyDown", ("shift",)), ("keyDown", ("w",)), ("sleep", 2), ("keyUp", ("w",)),
                          ("keyUp", ("shift",))]  # held down together, released in reverse order
    assert calls[6] == ("hotkey", ("ctrl", "shift", "s"))  # at the same time


def test_symbols_are_sent_as_unicode_not_layout_keys(monkeypatch):
    """On a Turkish Q keyboard, pyautogui typing '1234*5678=' lost the '*' (it presses keys per the US layout)."""
    from alice.tools import computer

    calls = []

    class PG:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append((name, a))

    monkeypatch.setattr(computer, "_pyautogui", lambda: PG())
    monkeypatch.setattr(computer, "send_unicode", lambda t: calls.append(("unicode", t)) or True)
    computer.type_text("1234*5678=")
    computer.type_text("merhaba 42")
    assert calls == [("unicode", "1234*5678="), ("write", ("merhaba 42",))]  # letters/digits are the same on every layout
    monkeypatch.setattr(computer, "send_unicode", lambda t: False)  # non-Windows: old path
    calls.clear()
    computer.type_text("a*b")
    assert calls == [("write", ("a*b",))]


def test_reflex_refuses_wrong_foreground_window(monkeypatch):
    """No title; goal names Not Defteri (Notepad) but Hesap Makinesi (Calculator) is in front: 1-10 went into it."""
    import pytest

    from alice.reflex import observers
    from alice.reflex.observers import app_name, named_window
    from alice.tools.base import ToolError

    assert app_name("*sayilar.txt, Not Defteri") == "Not Defteri"
    assert app_name("refleks-test - File Explorer") == "File Explorer"
    wins = ["Hesap Makinesi", "*1, Not Defteri", "cizim.png - Paint"]
    goal = "Not Defteri'ne 1'den 10'a kadar sayıları yaz"
    assert named_window(goal, "Hesap Makinesi", wins) == "Not Defteri"
    assert named_window(goal, "*1, Not Defteri", wins) == ""  # already in front
    assert named_window("1234 çarpı 5678 hesapla", "Hesap Makinesi", wins) == ""  # no other window name
    monkeypatch.setattr(observers, "foreground_title", lambda: "Hesap Makinesi")
    monkeypatch.setattr(observers, "_open_windows", lambda: wins)
    with pytest.raises(ToolError, match="title='Not Defteri'"):
        observers.DesktopObserver(None, None, goal)
    observers.DesktopObserver(None, "Not Defteri", goal)  # no problem if a title is given


def test_long_or_multiline_text_is_pasted(monkeypatch):
    """Line-by-line typing of 1-10 into Notepad left only '9, 10' in the file: multi-line text is pasted instead."""
    import sys
    import types

    from alice.tools import computer

    pyperclip = types.ModuleType("pyperclip")  # optional package is not installed on CI: a fake is enough
    monkeypatch.setitem(sys.modules, "pyperclip", pyperclip)

    calls, board = [], {"v": "eski pano"}

    class PG:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append((name, a))

    monkeypatch.setattr(computer, "_pyautogui", lambda: PG())
    monkeypatch.setattr(computer.time, "sleep", lambda s: None)
    pyperclip.paste = lambda: board["v"]
    pyperclip.copy = lambda t: board.update(v=t) or calls.append(("copy", (t,)))
    monkeypatch.setattr(computer, "send_unicode", lambda t: False)  # don't send real input during the test
    computer.type_text("1234*5678=")
    assert calls == [("write", ("1234*5678=",))]  # short single line: not sent via the clipboard (calculator)
    calls.clear()
    computer.type_text("1\n2\n3")
    paste = "command" if sys.platform == "darwin" else "ctrl"
    assert calls[:2] == [("copy", ("1\n2\n3",)), ("hotkey", (paste, "v"))] and board["v"] == "eski pano"


def test_agent_types_on_keyboard_in_one_step_and_reports_the_result():
    """Clicked calculator keys one by one and hit the step limit; reading the result needed another turn."""

    class Calc(FakeScreen):
        display = "0"

        def screen(self, goal=""):
            return Screen("Hesap Makinesi", "desktop", [
                Item("i1", "ax", "Text", f"Gösterim {self.display}", (0.1, 0.1, 0.9, 0.2)),
                Item("i2", "ax", "Button", "Eşittir", (0.8, 0.9, 0.9, 0.95), ref=2)])

        def act(self, action, item=None, text="", keys="", drop=None):
            super().act(action, item, text, keys, drop)
            if action == "type_keys" and text == "1234*5678=":
                self.display = "7.006.652"

    obs = Calc()
    brain = Brain([{"done": 0.95}])
    res = ScreenAgent(obs, brain, settle=0).run("1234 çarpı 5678", text="1234*5678=", keys=["ctrl+shift+s"])
    # Text given by the main model is typed at step one, without asking Jev (who picked clicking "İki").
    assert res.status == "done" and obs.acts == [("type_keys", None, "1234*5678=", "", None)]
    assert len(brain.calls) == 1 and "type_keys" in brain.calls[0][1]["action"].criteria
    assert "Press ctrl+shift+s" in brain.calls[0][1]["key"].criteria.values()  # the combination the tool gave
    report = res.report()
    assert "ekranda başa göre çıkan: Gösterim 7.006.652" in report and "kaybolan: Gösterim 0" in report
    assert brain.calls[0][0]["recent_actions"][0].startswith("typed '1234*5678=' on the keyboard -> screen changed")

    # Previous input is cleared: it had been typed after the leftover "2" from the previous action (212345678).
    obs = Calc()
    brain = Brain([{"done": 0.95}])
    ScreenAgent(obs, brain, settle=0).run("1234 çarpı 5678", text="1234*5678=", press_first=["escape"])
    assert obs.acts[:2] == [("press_key", None, "", "escape", None), ("type_keys", None, "1234*5678=", "", None)]
    assert brain.calls[0][0]["recent_actions"][0].startswith("pressed escape, then typed '1234*5678='")

    # If Jev itself chooses to type on the keyboard, the text is generated from the goal.
    obs = Calc()
    brain = Brain([{"action": "type_keys"}, {"done": 0.95}])
    res = ScreenAgent(obs, brain, text_writer=lambda goal, where: "1234*5678=", settle=0).run("1234 çarpı 5678")
    assert res.status == "done" and obs.acts[0][:3] == ("type_keys", None, "1234*5678=")


# ------------------------------------------------------------------------------- harness principles (like jev-mcp)
class Panel(FakeScreen):
    """Three-button screen: 'Kaydet' (Save) does nothing, 'Gönder' (Send) sends, 'Ayarlar' (Settings) opens it."""

    def __init__(self):
        super().__init__()
        self.page = "main"

    def screen(self, goal=""):
        if self.page == "settings":
            return Screen("Ayarlar", "desktop", [Item("i1", "ax", "Text", "Ayarlar açık", (0.1, 0.1, 0.5, 0.2))])
        return Screen("Uygulama", "desktop", [Item("i1", "ax", "Button", "Kaydet", (0.1, 0.9, 0.2, 0.95), ref=1),
                                              Item("i2", "ax", "Button", "Ayarlar", (0.3, 0.9, 0.4, 0.95), ref=2),
                                              Item("i3", "ax", "Button", "Gönder", (0.8, 0.9, 0.9, 0.95), ref=3)])

    def act(self, action, item=None, text="", keys="", drop=None):
        self.acts.append((action, item.label if item else None))
        if item is not None and item.label == "Ayarlar":
            self.page = "settings"


class ProbBrain:
    """Fake Jev picking items by the given probabilities at each step; also answers the second-look question."""

    supports_images = False

    def __init__(self, steps, second=None):
        self.steps, self.second, self.calls = list(steps), second, []

    def decide(self, state, questions, images=None):
        self.calls.append((state, questions))
        if set(questions) == {"item"}:  # second look
            pool = questions["item"].criteria
            pick = self.second if self.second in pool else next(iter(pool))
            return Decision({"item": Answer("choice", pick, 0.9, {pick: 0.9})}, "fake", 0.01)
        s = self.steps.pop(0)
        probs = s.get("probs", {"i1": 1.0})
        top = max(probs, key=probs.get)
        return Decision({"action": Answer("choice", s.get("action", "click"), 0.9),
                         "done": Answer("noul", s.get("done", 0.05), 0.9), "missing": Answer("noul", 0.05, 0.9),
                         "key": Answer("choice", "enter", 0.9), "item": Answer("choice", top, probs[top], probs),
                         "drop": Answer("choice", top, 0.9)}, "fake", 0.01)


def test_irreversible_button_needs_confirmation():
    obs = Panel()
    res = ScreenAgent(obs, ProbBrain([{"probs": {"i3": 0.95, "i1": 0.05}}]), settle=0).run("mesajı gönder")
    assert res.status == "needs_confirmation" and "Gönder" in res.reason and obs.acts == []
    obs = Panel()
    agent = ScreenAgent(obs, ProbBrain([{"probs": {"i3": 0.95, "i1": 0.05}}, {"done": 0.95}]), settle=0)
    agent.allow_irreversible = True  # the user explicitly asked for it
    assert agent.run("mesajı gönder").status == "done" and obs.acts == [("click", "Gönder")]


def test_candidate_that_did_nothing_is_not_picked_again():
    obs = Panel()
    brain = ProbBrain([{"probs": {"i1": 0.7, "i2": 0.3}}, {"probs": {"i1": 0.7, "i2": 0.3}}, {"done": 0.95}])
    res = ScreenAgent(obs, brain, settle=0).run("ayarları aç")
    assert obs.acts == [("click", "Kaydet"), ("click", "Ayarlar")] and res.status in ("done", "clicked")
    assert brain.calls[1][0]["tried_here_without_effect"] == ["Button 'Kaydet' (bottom-left)"]  # the code's ground truth


def test_second_look_when_unsure():
    obs = Panel()
    brain = ProbBrain([{"probs": {"i1": 0.45, "i2": 0.4, "i3": 0.15}}, {"done": 0.95}], second="i2")
    ScreenAgent(obs, brain, settle=0).run("ayarları aç")
    second = [q for _, q in brain.calls if set(q) == {"item"}]
    assert len(second) == 1 and set(second[0]["item"].criteria) == {"i1", "i2", "i3"}  # the top candidates
    assert obs.acts == [("click", "Ayarlar")]  # average of the two looks


def test_stuck_agent_consults_and_continues_without_the_main_model():
    obs = Panel()
    brain = ProbBrain([{"action": "click", "probs": {"i1": 1.0}}, {"action": "click", "probs": {"i1": 1.0}},
                       {"probs": {"i2": 1.0}}, {"done": 0.95}, {"done": 0.95}])
    asked = []
    agent = ScreenAgent(obs, brain, settle=0)
    agent.consult = lambda situation: asked.append(situation) or {"next": "Click the Ayarlar button"}
    res = agent.run("ayarlarda bildirimleri bul")
    assert res.status == "done" and asked and asked[0]["stuck_because"].startswith("aynı eylem")
    assert "Button 'Ayarlar' (bottom-middle)" in asked[0]["on_screen"]
    assert "(danışma 1) Click the Ayarlar button" in res.log and ("click", "Ayarlar") in obs.acts
    agent = ScreenAgent(Panel(), ProbBrain([{"probs": {"i1": 1.0}}] * 2), settle=0)
    agent.consult = lambda situation: {"give_up": True}
    assert agent.run("x").status == "needs_help"  # if the consultant gives up, control returns to the main model
    # Read target: if the answer is on screen, the consultant answers; the reflex won't mistake it for a target.
    agent = ScreenAgent(Panel(), ProbBrain([{"probs": {"i1": 1.0}}] * 2), settle=0)
    agent.consult = lambda situation: {"next": "", "answer": "Off: Night light is off"}
    res = agent.run("gece ışığı açık mı bak")
    assert res.status == "done" and "Off: Night light is off" in res.reason


def test_draw_across_sweeps_the_canvas_not_a_tiny_drag():
    """In Paint, Jev's drag boxes pointed at the same spot (2 pixels); the drawing area is scanned edge to edge."""

    class Canvas(Panel):
        drags: list = []

        def screen(self, goal=""):
            return Screen("Paint", "desktop", [Item("i1", "ax", "Button", "Dikdörtgen", (0.1, 0.05, 0.15, 0.1), ref=1),
                                               Item("i2", "ax", "Pane", "Tuval", (0.1, 0.2, 0.9, 0.9), ref=2)])

        def drag_at(self, fx, fy, tx, ty):
            self.drags.append(tuple(round(v, 2) for v in (fx, fy, tx, ty)))

    obs = Canvas()
    brain = ProbBrain([{"action": "draw_across", "probs": {"i2": 0.9, "i1": 0.1}}, {"done": 0.95}])
    res = ScreenAgent(obs, brain, settle=0).run("tuvale dikdörtgen çiz")
    assert res.status == "done" and obs.drags == [(0.3, 0.38, 0.7, 0.72)]  # quarter to quarter
    assert "draw_across" in brain.calls[0][1]["action"].criteria
    obs = Canvas()
    obs.drags = []
    res = ScreenAgent(obs, ProbBrain([{"action": "draw_across", "probs": {"i1": 1.0}}]), settle=0).run("çiz")
    assert res.status == "needs_help" and obs.drags == []  # a button is not an area; the screen can't be read


def test_multiline_text_is_not_typed_into_a_one_line_field():
    """The 1-10 meant for Notepad went into the file name field of a Save dialog that had been left open."""

    class Dialog(FakeScreen):
        def screen(self, goal=""):
            return Screen("Not Defteri", "desktop", [Item("i1", "ax", "Edit", "Dosya adı:", (0.2, 0.8, 0.6, 0.85),
                                                          editable=True, ref=1),
                                                     Item("i2", "ax", "Button", "İptal", (0.8, 0.9, 0.9, 0.95), ref=2)],
                          focused="Edit 'Dosya adı:' in window 'Farklı kaydet'")

    obs = Dialog()
    brain = Brain([{"action": "press_key", "key": "escape"}, {"done": 0.95}])
    ScreenAgent(obs, brain, settle=0).run("1-10 yaz", text="1\n2\n3")
    assert all(a[0] != "type_keys" for a in obs.acts)  # not forced into the field
    assert brain.calls[0][0]["keyboard_focus"] == "Edit 'Dosya adı:' in window 'Farklı kaydet'"  # Jev sees it too
    from alice.reflex.agent import wrong_field

    assert wrong_field("Edit 'Dosya adı:' in window 'Farklı kaydet'", "1\n2") and not wrong_field("Document 'x'", "1\n2")
    assert not wrong_field("Edit 'Ara'", "lofi")


def test_text_writer_keeps_all_lines():
    """Only the first line was taken: with the goal '1'den 10'a alt alta yaz' (write 1 to 10 line by line),
    '1, Enter' was typed for 20 steps."""
    from alice.reflex.loop import clean_typed

    assert clean_typed("1\n2\n3") == "1\n2\n3"
    assert clean_typed('```\n1\n2\n```') == "1\n2"
    assert clean_typed('"merhaba"') == "merhaba"


def test_parked_mouse_is_moved_off_the_corner(monkeypatch):
    """The mouse was parked in the corner at task start; the emergency stop cancelled every key press in the
    first two tasks."""
    from alice.tools import computer

    real = computer._unpark_mouse  # the real one, not conftest's fake
    calls = []

    class PG:
        FAILSAFE, FAILSAFE_POINTS = True, [(-3440, 0), (3439, 1439)]
        pos = (-3440, 0)

        def position(self):
            return self.pos

        def moveTo(self, x, y):
            calls.append(("move", x, y, self.FAILSAFE))

    pg = PG()
    monkeypatch.setattr(computer, "_pyautogui", lambda: pg)
    assert real(500, 400) and calls == [("move", 500, 400, False)] and pg.FAILSAFE  # temporarily turned off, then back on
    pg.pos = (100, 100)
    assert not real(500, 400) and len(calls) == 1  # leaves it alone if it isn't in the corner


# ------------------------------------------------------------------------------- login, quotes, clear item
def test_quoted_target_is_not_an_echo():
    # Advisor: 'Click the "Search or start a new Chat" box' -> the search box was taken for an echo and dropped.
    goal = 'Click the "Search or start a new Chat" box and type "Que Mal".'
    assert not is_goal_echo("q Search or start a new Chat", goal)
    assert not is_goal_echo("q Şearch or start a new Chat", "Click the “q Şearch or start a new Chat” box")
    assert is_goal_echo("Que Mal isimli kişiye amk mesajını gönder", "WhatsApp'ta Que Mal isimli kişiye “amk” "
                        "mesajını gönder")  # the user's own message is still an echo
    items = build_items([], [("q Search or start a new Chat", (0.04, 0.11, 0.11, 0.12)), ("Chats", (0.03, 0.06, 0.05, 0.07))],
                        goal)
    assert "q Search or start a new Chat" in [i.label for i in items]


class LoginPage(FakeScreen):
    def screen(self, goal=""):
        return Screen("WhatsApp", "https://web.whatsapp.com/", [
            Item("i1", "ax", "button", "Telefon numarası kullanarak giriş yapın", (0.4, 0.6, 0.6, 0.65), ref=1),
            Item("i2", "ax", "button", "İndir", (0.4, 0.7, 0.5, 0.75), ref=2)])


def test_login_screen_is_left_to_the_user():
    obs = LoginPage()
    res = ScreenAgent(obs, ProbBrain([{"probs": {"i1": 0.9, "i2": 0.1}}]), settle=0).run(
        "WhatsApp'ta Que Mal isimli kişiye “amk” mesajını gönder")
    assert res.status == "needs_login" and "giriş" in res.reason and obs.acts == []
    obs = LoginPage()  # if the user explicitly asked to log in, it may touch the page
    res = ScreenAgent(obs, ProbBrain([{"probs": {"i1": 0.9, "i2": 0.1}}, {"done": 0.95}]), settle=0).run(
        "WhatsApp'a telefon numarasıyla giriş yap")
    assert obs.acts and obs.acts[0][1] == "Telefon numarası kullanarak giriş yapın"


def test_login_stops_consult_too():
    obs = LoginPage()
    agent = ScreenAgent(obs, ProbBrain([{"probs": {"i1": 0.9, "i2": 0.1}}]), settle=0)
    agent.consult = lambda situation: pytest_fail("giriş ekranında danışılmaz")
    assert agent.run("mesajı gönder").status == "needs_login"


def pytest_fail(msg):
    raise AssertionError(msg)


class KindSplitBrain(ProbBrain):
    """Item is clear (0.95), action kind is unsure (click 0.35)."""

    def decide(self, state, questions, images=None):
        d = super().decide(state, questions, images)
        if "action" in d.answers:
            d.answers["action"] = Answer("choice", self.kind, self.kind_conf)
        return d


def test_clear_item_with_unsure_click_is_clicked():
    obs = Panel()
    brain = KindSplitBrain([{"probs": {"i2": 0.95, "i1": 0.05}}, {"done": 0.95}])
    brain.kind, brain.kind_conf = "click", 0.35
    res = ScreenAgent(obs, brain, settle=0).run("ayarları aç")
    assert obs.acts == [("click", "Ayarlar")] and res.status in ("done", "clicked")
    for kind, conf, probs in (("click", 0.35, {"i2": 0.6, "i1": 0.4}),  # the item is unsure too
                              ("scroll_down", 0.36, {"i2": 0.95, "i1": 0.05}),  # the most likely kind is not click
                              ("click", 0.2, {"i2": 0.95, "i1": 0.05})):  # click is very weak
        obs = Panel()
        brain = KindSplitBrain([{"probs": probs}])
        brain.kind, brain.kind_conf = kind, conf
        assert ScreenAgent(obs, brain, settle=0).run("ayarları aç").status == "needs_help" and obs.acts == []


def test_missing_target_window_stops_instead_of_using_the_foreground_one(monkeypatch):
    """Live recording (Nov 2): with no window titled 'Setup - Sonora version 0.47.0', the reflex carried on with the
    foreground window (War Thunder - In battle); the keys could have gone to the game."""
    import pytest

    from alice.reflex import observers
    from alice.tools import desktop
    from alice.tools.base import ToolError

    def missing(title):
        raise ToolError(f"'{title}' başlıklı pencere yok. Açık pencereler: War Thunder - In battle")

    monkeypatch.setattr(desktop, "focus_window", missing)
    obs = observers.DesktopObserver(ctx=None, window_title="Setup - Sonora")
    with pytest.raises(ToolError, match="pencere yok"):
        obs.screen("Next'e bas")


def test_read_only_goal_never_touches_the_screen():
    """Live recording (Oct 26): goal "Gece ışığı durumunu oku, herhangi bir düğmeye basma" (read the night light
    status, press no button), Jev chose to click the 'Off' text. With a goal that forbids touching, nothing is
    pressed; the consult reads what is on screen as the answer."""
    from alice.reflex.agent import no_touch_goal

    for g in ("Gece ışığı durumunu oku, herhangi bir düğmeye basma", "hiçbir şeye basıp ayarı değiştirme",
              "Mevcut durumu oku; hiçbir kontrole dokunma", "Read the list. Do not click anything.",
              "listeyi tıklamadan oku"):
        assert no_touch_goal(g), g
    for g in ("Kaydet düğmesine çift tıklama ile bas", "Ayarı değiştirme, sadece sayfayı aç", "Mesaj gönderme"):
        assert not no_touch_goal(g), g

    obs = FakeScreen()
    brain = Brain([{"action": "click", "item": "Uydurma bir savunma"}])
    res = ScreenAgent(obs, brain, translate=lambda g: g, settle=0).run("Listeyi oku, hiçbir şeye basma")
    assert res.status == "needs_help" and obs.acts == [] and "yasaklıyor" in res.reason

    obs = FakeScreen()
    agent = ScreenAgent(obs, Brain([{"action": "click", "item": "Uydurma bir savunma"}]), translate=lambda g: g, settle=0)
    agent.consult = lambda situation: {"answer": "Listede: Uydurma bir savunma"}
    res = agent.run("Listeyi oku, hiçbir şeye basma")
    assert res.status == "done" and obs.acts == [] and "Uydurma bir savunma" in res.reason


def test_typing_into_a_button_is_refused():
    """Live recording (Nov 9): in Chrome, Google's code box was not in the list; for "361819 yaz" (type 361819)
    the 'Next' text was picked, clicked, and the form was submitted without the code being typed anywhere."""
    from alice.reflex.agent import not_a_field

    class Page(FakeScreen):
        def screen(self, goal=""):
            return Screen("Account recovery", "desktop", [
                Item("i1", "ocr", "text", "Next", (0.5, 0.6, 0.55, 0.63)),
                Item("i2", "ocr", "text", "Enter code", (0.4, 0.4, 0.5, 0.43)),
                Item("i3", "ax", "Button", "Try another way", (0.3, 0.6, 0.45, 0.63), ref=3)])

    assert not_a_field(Item("i", "ocr", "text", "Next")) and not_a_field(Item("i", "ax", "Button", "Kod gir"))
    assert not not_a_field(Item("i", "ocr", "text", "Enter code")) and not not_a_field(Item("i", "ocr", "text", "Ara"))
    assert not not_a_field(Item("i", "ax", "Edit", "Next", editable=True))

    obs = Page()
    res = ScreenAgent(obs, Brain([{"action": "type_text", "item": "Next"}]), translate=lambda g: g, settle=0).run(
        "Enter code kutusuna 361819 yaz", inputs={"code": "361819"})
    assert res.status == "needs_help" and obs.acts == [] and "düğme" in res.reason

    obs = Page()  # if the text inside the field is picked, it is clicked and typed into as before
    ScreenAgent(obs, Brain([{"action": "type_text", "item": "Enter code"}, {"done": 0.95}]), translate=lambda g: g,
                settle=0).run("Enter code kutusuna 361819 yaz", inputs={"Enter code": "361819"})
    assert obs.acts and obs.acts[0][:2] == ("type_text", "Enter code")


def test_multi_part_goal_is_split_and_each_step_runs_on_its_own():
    """Live recording (Oct 26): "Not Defteri'nde 1'den 10'a yaz ve Farklı Kaydet ile ... kaydet" (write 1-10 in Notepad
    and save with Save As) was given as one goal; although the work was progressing, "bitti?" (done?) stayed at 1-6%
    and it kept clicking up to the step limit. A multi-part goal is split into steps, each with its own short goal."""
    from alice.reflex.agent import multi_part_goal

    assert multi_part_goal("Not Defteri'nde yeni belge aç, 1'den 10'a kadar yaz ve sonra Farklı Kaydet ile kaydet")
    assert multi_part_goal("Açık profil menüsünü Escape ile kapat, sonra Ctrl+K ile hızlı arama aç")
    assert multi_part_goal("Open the File menu; click Save As; type the name and press Save")
    assert not multi_part_goal("ekrandaki Install düğmesine tıkla")
    assert not multi_part_goal("Hesap Makinesi'nde 1234 × 5678 işlemini hesapla")
    assert not multi_part_goal("Ara ve Bul düğmesine tıkla")

    goal = "Önce ilk projeyi aç, sonra proje sayfasının açıldığını gör ve ardından orada bekle"
    obs = FakeScreen()
    brain = Brain([{"action": "click", "item": "Uydurma bir savunma"},  # step 1: clicked, the screen changed
                   {"done": 0.95}])  # step 2: already done
    agent = ScreenAgent(obs, brain, translate=lambda g: g, settle=0)
    asked = []
    agent.plan = lambda g: asked.append(g) or ["İlk projeyi aç", "Proje sayfasının açıldığını gör"]
    res = agent.run(goal)
    assert asked == [goal] and res.status == "steps_done" and "2 adımı da yapıldı" in res.reason
    assert [c[0]["goal"] for c in brain.calls] == ["İlk projeyi aç", "Proje sayfasının açıldığını gör"]
    assert "(plan 1/2) İlk projeyi aç" in res.log and "(plan 2/2) Proje sayfasının açıldığını gör" in res.log
    assert obs.acts == [("click", "Uydurma bir savunma", "", "", None)]

    # if a step can't be done, it stops there and says which one; later steps are not run
    obs = FakeScreen()
    brain = Brain([{"action": "click", "item": "Uydurma bir savunma"}, {"action": "click", "conf": 0.1}])
    agent = ScreenAgent(obs, brain, translate=lambda g: g, settle=0)
    agent.plan = lambda g: ["İlk projeyi aç", "Olmayan düğmeye bas", "Kaydet"]
    res = agent.run(goal)
    assert res.status == "needs_help" and "planın 2. adımı (2/3) yapılamadı: Olmayan düğmeye bas" in res.reason
    assert not any("(plan 3/3)" in line for line in res.log)


def test_short_or_keyboard_goals_are_not_planned():
    def boom(goal):
        raise AssertionError("plan çağrılmamalıydı")

    obs = FakeScreen()
    agent = ScreenAgent(obs, Brain([{"action": "click", "item": "Uydurma bir savunma"}, {"done": 0.95}]),
                        translate=lambda g: g, settle=0)
    agent.plan = boom
    assert agent.run("İlk projeyi aç").status == "clicked"  # single task: no extra model call

    # if the planner returns a single step or fails, the goal runs as one piece, as before
    for plan in (lambda g: ["hepsi tek adım"], lambda g: 1 / 0):
        obs = FakeScreen()
        brain = Brain([{"action": "click", "item": "Uydurma bir savunma"}, {"done": 0.95}])
        agent = ScreenAgent(obs, brain, translate=lambda g: g, settle=0)
        agent.plan = plan
        res = agent.run("Önce ilk projeyi aç, sonra proje sayfasının açıldığını gör ve ardından bekle")
        assert res.status in ("done", "clicked") and brain.calls[0][0]["goal"].startswith("Önce ilk projeyi aç")


def test_planner_reads_steps_from_the_model_and_verdict_asks_to_verify():
    from types import SimpleNamespace

    from alice.reflex.agent import make_planner
    from alice.tools.meta import reflex_verdict

    router = SimpleNamespace(chat=lambda role, msgs, **kw: SimpleNamespace(
        text='Tabii: {"steps": ["Dosya menüsünü aç", "Farklı kaydet düğmesine tıkla"]}'))
    assert make_planner(router)("x") == ["Dosya menüsünü aç", "Farklı kaydet düğmesine tıkla"]
    assert make_planner(SimpleNamespace(chat=lambda *a, **k: SimpleNamespace(text="bilmiyorum")))("x") == []
    assert "not verified" in reflex_verdict("steps_done")


def test_read_only_goal_also_blocks_key_presses():
    obs = FakeScreen()
    res = ScreenAgent(obs, Brain([{"action": "press_key", "key": "enter"}]), translate=lambda g: g, settle=0).run(
        "Listeyi oku, hiçbir tuşa basma")
    assert res.status == "needs_help" and obs.acts == []
