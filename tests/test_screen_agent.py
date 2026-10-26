"""Ekran ajanı (Jev + kod): öğe listesi, eylemler (tıkla/yaz/tuş/sürükle), doğrulama, görsel yedek, iz kaydı."""

from __future__ import annotations

import json

from PIL import Image

from alice.decider import Answer, Decision, Noul
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
