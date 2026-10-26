"""Ekran ajanı (Jev + kod): öğe listesi, eylemler (tıkla/yaz/tuş/sürükle), doğrulama, görsel yedek, iz kaydı."""

from __future__ import annotations

import json

from PIL import Image

from alice.decider import Answer, Decision, Noul, _validate
from alice.reflex import perceive
from alice.reflex.agent import ScreenAgent, goal_keys
from alice.reflex.perceive import Item, Screen, build_items, is_goal_echo


# ------------------------------------------------------------------------------------------- algı
def test_build_items_merges_sources_drops_goal_echo_and_adds_context():
    ax = [Item("", "ax", "button", "Kaydet", (0.8, 0.02, 0.9, 0.06), ref=7),
          Item("", "ax", "Edit", "", (0.1, 0.9, 0.9, 0.95), editable=True, ref=8),
          Item("", "ax", "button", "", None)]  # adsız ve yazılamaz: atılır
    ocr = [("Kaydet", (0.81, 0.03, 0.88, 0.05)),  # düğmenin kendi yazısı: ayrı seçenek olmaz
           ("projeler kısmındaki ilk projeye tıkla", (0.4, 0.3, 0.7, 0.33)),  # kullanıcının mesajı
           ("Uydurma bir savunma", (0.02, 0.14, 0.15, 0.16)), ("Sil", (0.2, 0.14, 0.23, 0.16)),
           ("Proje genel olarak", (0.02, 0.2, 0.15, 0.22)), ("Sil", (0.2, 0.2, 0.23, 0.22))]
    items = build_items(ax, ocr, "Projeler kısmındaki ilk projeye tıkla")
    labels = [i.label for i in items]
    assert "projeler kısmındaki ilk projeye tıkla" not in labels  # hedefin yankısı
    assert labels.count("Kaydet") == 1 and next(i for i in items if i.label == "Kaydet").source == "ax+ocr"
    sil = [i for i in items if i.label == "Sil"]
    assert {i.context for i in sil} == {"in the row of 'Uydurma bir savunma'", "in the row of 'Proje genel olarak'"}
    assert [i.key for i in items] == [f"i{n}" for n in range(1, len(items) + 1)]
    kaydet = next(i for i in items if i.label == "Kaydet")
    assert kaydet.describe() == "button 'Kaydet' (top-right)"
    assert next(i for i in items if i.label == "Uydurma bir savunma").describe().startswith("text 'Uydurma")
    assert items[0].editable or "proje" in items[0].label.lower()  # alakalı/yazılabilir olanlar önde


def test_goal_echo_needs_most_words_from_goal():
    assert is_goal_echo("ilk projeye tıkla lütfen", "Projeler kısmındaki ilk projeye tıkla")
    assert not is_goal_echo("Projeler", "Projeler kısmındaki ilk projeye tıkla")  # kısa: hedefin kendisi olabilir
    assert not is_goal_echo("Uydurma bir savunma ve hav", "ilk projeye tıkla")


def test_ocr_lines_become_fractions():
    img = Image.new("RGB", (1000, 500))
    out = perceive.ocr_image(img, recognize=lambda im: [("Merhaba", (100, 50, 200, 25))])
    assert out == [("Merhaba", (0.1, 0.1, 0.3, 0.15))]


def test_goal_keys():
    assert goal_keys("ctrl+shift+n ile gizli pencere aç, sonra F11") == ["ctrl+shift+n", "f11"]


# ------------------------------------------------------------------------------------------- ajan
class FakeScreen:
    """İki ekranlı bir uygulama: liste -> proje sayfası. Kartlar sürüklenebilir, alan yazılabilir."""

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
    """Senaryo: her adımda ne cevaplanacağını sırayla veren sahte Jev."""

    supports_images = False
    names = ["fake"]

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def decide(self, state, questions, images=None):
        _validate(questions)  # gerçek motor gibi: tek seçenekli Choice reddedilir
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
    assert res.status == "needs_help" and "bulunamadı" in res.reason  # ekrana bakamıyor

    obs = FakeScreen()
    obs.image = Image.new("RGB", (1200, 800))
    brain = Brain([{"action": "click", "missing": 0.9, "visible": 0.95}, {"visible": 0.95}, {"done": 0.9}])
    brain.supports_images = True
    res = ScreenAgent(obs, brain, settle=0).run("dişli ikonuna bas ve ayarları aç")
    assert obs.acts[0][0] == "click_at" and res.status == "done"  # yakından bakılıp tıklandı
    assert "screen" in brain.calls[0][0] and brain.calls[0][2]  # görüntü de gitti


def test_unsure_action_kind_escalates():
    res = ScreenAgent(FakeScreen(), Brain([{"action": "click", "conf": 0.2}]), settle=0).run("x")
    assert res.status == "needs_help" and "emin değil" in res.reason


# ------------------------------------------------------------------------------------------- eylemler
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
    assert calls[0] == ("ui", 3, "click", None)  # erişilebilirlik üzerinden
    assert calls[1] == ("click", (250, 125), {})  # pencere (100,50) + 0.15*1000, 0.15*500
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


# ------------------------------------------------------------------------------------- gerçek Windows'ta bulunanlar
def test_words_match_turkish_capital_i():
    from alice.reflex.perceive import words

    assert words("ALİCE uygulaması") == ["alice", "uygulamasi"] and words("ALICE") == ["alice"]


def test_button_named_in_goal_is_not_an_echo():
    # Unciv ana menüsü: hedef "start a new game" iken "Start new game" düğmesi yankı sanılıp atılıyordu.
    assert not is_goal_echo("Start new game", "start a new game")
    items = build_items([], [("Start new game", (0.3, 0.5, 0.5, 0.55)), ("Load game", (0.3, 0.6, 0.5, 0.65))],
                        "start a new game")
    assert items[0].label == "Start new game"
    longer = [("Click on the Start new game button", (0.1, 0.1, 0.5, 0.12))]
    assert build_items([], longer, "click on the start new game button") == []
    assert len(build_items([], longer, "click on the start new game button", drop_echo=False)) == 1  # oyun penceresi


def test_user_message_echo_is_dropped_even_when_goal_was_rephrased():
    msg = "Şuanda reflex katmanı ile ekrana bakıp projeler kısmındaki ilk projeye tıkla"
    goal = "ALICE uygulamasında Projeler bölümündeki ilk projeye tıkla"
    assert not is_goal_echo(msg, goal)  # beyin modeli hedefi yeniden yazınca örtüşme %60'ın altında
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
    assert is_phantom_tab("Button", "New Tab", (6, 30, 36, 70), doc)  # sayfadan birkaç piksel taşıyor
    assert not is_phantom_tab("Button", "New Tab", (6, 0, 36, 25), doc)  # gerçek tarayıcı: şerit sayfanın üstünde
    assert not is_phantom_tab("Button", "Kaydet", (14, 30, 40, 70), doc)
    # Not Defteri + aşağı taşan Kaydet penceresi; ekranın dışı kırpılır.
    assert window_union([(590, 640, 2130, 1194), (596, 640, 1796, 1360)], (-1920, 0, 3440, 1440)) == \
        (590, 640, 1540, 720)
    assert window_union([(3000, 1400, 3600, 1600)], (0, 0, 3440, 1440)) is None  # ekranda 40 piksel kaldı
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
    assert res.status == "needs_help"  # FakeScreen sürüklemede ekranı değiştirmiyor: işe yaradığı belli değil

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
    assert obs.acts == [("type_text", "Address Bar", "C:\\Users", "", None)]  # ağaçtaki 'Desktop'a değil
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

    obs.page = "project"  # ekranda tek öğe: 'item' sorusu da sorulmaz
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

    class Op:  # IAsyncOperation gibi: coroutine değil, sadece await edilebilir
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

    class Addr:  # Gezgin'in adres çubuğu: yazılanı kabul eder, sonra eski adrese döner
        def set_edit_text(self, t):
            calls.append(("set", t))

        def get_value(self):
            return "C:\\Windows"

        def click_input(self):
            calls.append(("click",))

    class Doc(Addr):  # değeri okunamıyor: yazılanın tuttuğu varsayılır, iki kez yazılmaz
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
    observers.DesktopObserver(ctx, None, "ALICE'te ilk projeye tıkla")  # hedef ALICE'in kendisiyse olur
    observers.DesktopObserver(ctx, "Not Defteri", "merhaba yaz")  # pencere verildiyse olur

    ctx.state["agent"] = type("A", (), {"messages": [
        {"role": "system", "content": "s"}, {"role": "user", "content": "ilk projeye tıkla"},
        {"role": "user", "content": "devam et", "_nudge": True}]})()
    assert _last_user_text(ctx) == ("ilk projeye tıkla",)
    ctx.state["agent"].messages.append({"role": "user", "_display": "ikinci projeye tıkla",
                                        "content": "ikinci projeye tıkla\n\n[hatırlanan] Uydurma bir savunma sanayi"})
    assert _last_user_text(ctx) == ("ikinci projeye tıkla",)  # hafıza eki yankı sayılmasın


# ------------------------------------------------------------------------------------------- klavye
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
    assert calls[0] == ("type", "1234*5678=")  # alan seçmeden öndeki pencereye
    assert calls[1:6] == [("keyDown", ("shift",)), ("keyDown", ("w",)), ("sleep", 2), ("keyUp", ("w",)),
                          ("keyUp", ("shift",))]  # birlikte basılı, ters sırayla bırakılır
    assert calls[6] == ("hotkey", ("ctrl", "shift", "s"))  # aynı anda


def test_symbols_are_sent_as_unicode_not_layout_keys(monkeypatch):
    """Türkçe Q klavyede pyautogui '1234*5678=' yazınca '*' kayboldu (ABD düzenine göre tuşa basıyor)."""
    from alice.tools import computer

    calls = []

    class PG:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append((name, a))

    monkeypatch.setattr(computer, "_pyautogui", lambda: PG())
    monkeypatch.setattr(computer, "send_unicode", lambda t: calls.append(("unicode", t)) or True)
    computer.type_text("1234*5678=")
    computer.type_text("merhaba 42")
    assert calls == [("unicode", "1234*5678="), ("write", ("merhaba 42",))]  # harf/rakam her düzende aynı
    monkeypatch.setattr(computer, "send_unicode", lambda t: False)  # Windows dışı: eski yol
    calls.clear()
    computer.type_text("a*b")
    assert calls == [("write", ("a*b",))]


def test_reflex_refuses_wrong_foreground_window(monkeypatch):
    """Başlık verilmedi, hedef Not Defteri'ni söylüyor ama önde Hesap Makinesi var: 1-10 hesap makinesine yazılmıştı."""
    import pytest

    from alice.reflex import observers
    from alice.reflex.observers import app_name, named_window
    from alice.tools.base import ToolError

    assert app_name("*sayilar.txt, Not Defteri") == "Not Defteri"
    assert app_name("refleks-test - File Explorer") == "File Explorer"
    wins = ["Hesap Makinesi", "*1, Not Defteri", "cizim.png - Paint"]
    goal = "Not Defteri'ne 1'den 10'a kadar sayıları yaz"
    assert named_window(goal, "Hesap Makinesi", wins) == "Not Defteri"
    assert named_window(goal, "*1, Not Defteri", wins) == ""  # zaten önde
    assert named_window("1234 çarpı 5678 hesapla", "Hesap Makinesi", wins) == ""  # başka pencere adı yok
    monkeypatch.setattr(observers, "foreground_title", lambda: "Hesap Makinesi")
    monkeypatch.setattr(observers, "_open_windows", lambda: wins)
    with pytest.raises(ToolError, match="title='Not Defteri'"):
        observers.DesktopObserver(None, None, goal)
    observers.DesktopObserver(None, "Not Defteri", goal)  # başlık verildiyse sorun yok


def test_long_or_multiline_text_is_pasted(monkeypatch):
    """Not Defteri'ne 1-10 satır satır tuşlanınca dosyada sadece '9, 10' kalmıştı: çok satırlı metin panodan."""
    import pyperclip

    from alice.tools import computer

    calls, board = [], {"v": "eski pano"}

    class PG:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append((name, a))

    monkeypatch.setattr(computer, "_pyautogui", lambda: PG())
    monkeypatch.setattr(computer.time, "sleep", lambda s: None)
    monkeypatch.setattr(pyperclip, "paste", lambda: board["v"])
    monkeypatch.setattr(pyperclip, "copy", lambda t: board.update(v=t) or calls.append(("copy", (t,))))
    monkeypatch.setattr(computer, "send_unicode", lambda t: False)  # testte gerçek girdi gönderilmesin
    computer.type_text("1234*5678=")
    assert calls == [("write", ("1234*5678=",))]  # kısa tek satır: panoya gitmez (hesap makinesi)
    calls.clear()
    computer.type_text("1\n2\n3")
    assert calls[:2] == [("copy", ("1\n2\n3",)), ("hotkey", ("ctrl", "v"))] and board["v"] == "eski pano"


def test_agent_types_on_keyboard_in_one_step_and_reports_the_result():
    """Hesap makinesinde tuşlara tek tek tıklatıp adım sınırına takılıyordu; sonucu okumak için de ayrı tur gerekiyordu."""

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
    # Ana model metni verdiyse ilk adımda Jev'e sorulmadan yazılır (Jev "İki" tuşuna tıklamayı seçmişti).
    assert res.status == "done" and obs.acts == [("type_keys", None, "1234*5678=", "", None)]
    assert len(brain.calls) == 1 and "type_keys" in brain.calls[0][1]["action"].criteria
    assert "Press ctrl+shift+s" in brain.calls[0][1]["key"].criteria.values()  # aracın verdiği kombinasyon
    report = res.report()
    assert "ekranda başa göre çıkan: Gösterim 7.006.652" in report and "kaybolan: Gösterim 0" in report
    assert brain.calls[0][0]["recent_actions"][0].startswith("typed '1234*5678=' on the keyboard -> screen changed")

    # Önceki girdi temizlenir: önceki işlemden kalan "2"nin arkasına yazılmıştı (212345678).
    obs = Calc()
    brain = Brain([{"done": 0.95}])
    ScreenAgent(obs, brain, settle=0).run("1234 çarpı 5678", text="1234*5678=", press_first=["escape"])
    assert obs.acts[:2] == [("press_key", None, "", "escape", None), ("type_keys", None, "1234*5678=", "", None)]
    assert brain.calls[0][0]["recent_actions"][0].startswith("pressed escape, then typed '1234*5678='")

    # Jev kendisi klavyeyle yazmayı seçerse metin hedeften üretilir.
    obs = Calc()
    brain = Brain([{"action": "type_keys"}, {"done": 0.95}])
    res = ScreenAgent(obs, brain, text_writer=lambda goal, where: "1234*5678=", settle=0).run("1234 çarpı 5678")
    assert res.status == "done" and obs.acts[0][:3] == ("type_keys", None, "1234*5678=")


# ------------------------------------------------------------------------------- harness ilkeleri (jev-mcp gibi)
class Panel(FakeScreen):
    """Üç düğmeli ekran; 'Kaydet' bir şey yapmaz, 'Gönder' gönderir, 'Ayarlar' ayar ekranını açar."""

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
    """Her adımda verilen olasılıklarla öğe seçen sahte Jev; ikinci bakış sorusunu da cevaplar."""

    supports_images = False

    def __init__(self, steps, second=None):
        self.steps, self.second, self.calls = list(steps), second, []

    def decide(self, state, questions, images=None):
        self.calls.append((state, questions))
        if set(questions) == {"item"}:  # ikinci bakış
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
    agent.allow_irreversible = True  # kullanıcı açıkça istedi
    assert agent.run("mesajı gönder").status == "done" and obs.acts == [("click", "Gönder")]


def test_candidate_that_did_nothing_is_not_picked_again():
    obs = Panel()
    brain = ProbBrain([{"probs": {"i1": 0.7, "i2": 0.3}}, {"probs": {"i1": 0.7, "i2": 0.3}}, {"done": 0.95}])
    res = ScreenAgent(obs, brain, settle=0).run("ayarları aç")
    assert obs.acts == [("click", "Kaydet"), ("click", "Ayarlar")] and res.status in ("done", "clicked")
    assert brain.calls[1][0]["tried_here_without_effect"] == ["Button 'Kaydet' (bottom-left)"]  # kodun gerçeği


def test_second_look_when_unsure():
    obs = Panel()
    brain = ProbBrain([{"probs": {"i1": 0.45, "i2": 0.4, "i3": 0.15}}, {"done": 0.95}], second="i2")
    ScreenAgent(obs, brain, settle=0).run("ayarları aç")
    second = [q for _, q in brain.calls if set(q) == {"item"}]
    assert len(second) == 1 and set(second[0]["item"].criteria) == {"i1", "i2", "i3"}  # en iyi adaylar
    assert obs.acts == [("click", "Ayarlar")]  # iki bakışın ortalaması


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
    assert agent.run("x").status == "needs_help"  # danışman vazgeçerse ana modele döner
    # Okuma hedefi: cevap ekranda görünüyorsa danışman cevabı verir, refleks onu hedef sanıp denemez.
    agent = ScreenAgent(Panel(), ProbBrain([{"probs": {"i1": 1.0}}] * 2), settle=0)
    agent.consult = lambda situation: {"next": "", "answer": "Off: Night light is off"}
    res = agent.run("gece ışığı açık mı bak")
    assert res.status == "done" and "Off: Night light is off" in res.reason
