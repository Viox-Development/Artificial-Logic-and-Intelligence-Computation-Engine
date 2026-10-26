"""Oyun oturumu: değişen bölgeler, OCR önbelleği, yeni öğeler, notlar, tur özetleri ve game aracı."""

from __future__ import annotations

from types import SimpleNamespace

from PIL import Image, ImageDraw

from alice.reflex import perceive
from alice.reflex.game import GameSession, code_lessons, discover, game_key, reflect
from alice.reflex.perceive import Item, changed_regions, mark_fresh, ocr_changed


def frame(*blocks):
    """1600x900 siyah kare, verilen (x0, y0, x1, y1) bloklar beyaz."""
    img = Image.new("RGB", (1600, 900))
    d = ImageDraw.Draw(img)
    for b in blocks:
        d.rectangle(b, fill="white")
    return img


# ------------------------------------------------------------------------------------------- algı
def test_changed_regions_finds_only_what_changed():
    a = frame((10, 10, 300, 60))
    assert changed_regions(a, a.copy()) == []
    assert changed_regions(None, a) is None and changed_regions(a, a.resize((800, 450))) is None
    b = frame((10, 10, 300, 60), (1400, 820, 1500, 860))  # sağ altta yeni bir düğme
    (reg,) = changed_regions(a, b)
    assert reg[0] <= 1400 / 1600 and reg[2] >= 1500 / 1600 and reg[1] <= 820 / 900 and reg[3] == 1.0
    assert reg[0] > 0.8  # sol üst aynı kaldı: bölgeye girmez
    c = frame((10, 10, 300, 60), (1400, 820, 1500, 860), (700, 400, 760, 420))
    assert len(changed_regions(b, c)) == 1 and changed_regions(a, c)[0][1] < 0.5  # iki ayrı bölge, yukarıdaki önce


def test_ocr_changed_rereads_only_changed_regions():
    img = frame()
    seen = []

    def recognize(im):
        seen.append(im.size)
        return [("Next turn", (0, 0, im.width // 2, im.height // 2))]

    prev = [("Gold: 10", (0.0, 0.0, 0.1, 0.03)), ("Pick a tech", (0.85, 0.9, 0.95, 0.95))]
    out = ocr_changed(img, prev, [(0.875, 0.888, 1.0, 1.0)], recognize)
    assert ("Gold: 10", (0.0, 0.0, 0.1, 0.03)) in out  # değişmeyen satır önbellekten
    assert all(t != "Pick a tech" for t, _ in out)  # bölgeye değen eski satır atıldı, yeniden okundu
    assert len(seen) == 1 and seen[0][0] < 400  # sadece küçük bir parça okundu (bölge eski satırı da kapsıyor)
    new = next(r for t, r in out if t == "Next turn")
    assert new[0] >= 0.85 and new[1] >= 0.88
    assert ocr_changed(img, prev, [], recognize) == prev and len(seen) == 1  # hiçbir şey değişmedi: OCR yok
    ocr_changed(img, prev, [(0.0, 0.0, 0.8, 0.8)], recognize)  # yarıdan fazlası değişti: hepsi baştan
    assert seen[-1] == (1600, 900)
    ocr_changed(img, prev, None, recognize)  # ilk kare
    assert seen[-1] == (1600, 900)


def test_mark_fresh_and_describe():
    a = [Item("i1", "ocr", "text", "Gold: 10"), Item("i2", "ocr", "text", "Next turn")]
    seen = mark_fresh(a, None)
    assert not any(i.fresh for i in a)
    b = [Item("i1", "ocr", "text", "Gold: 10", (0.0, 0.0, 0.1, 0.05)),
         Item("i2", "ocr", "text", "Pick a tech", (0.85, 0.9, 0.95, 0.95))]
    mark_fresh(b, seen)
    assert [i.fresh for i in b] == [False, True]
    assert b[1].describe() == "text 'Pick a tech' (bottom-right; new)"
    assert b[1].describe(False) == "text 'Pick a tech' (bottom-right)"  # geçmiş satırlarında işaret yok


def test_desktop_observer_diff_mode_reads_changes_only(monkeypatch):
    from alice.reflex import observers
    from alice.tools import desktop

    frames = [frame((100, 100, 400, 140)), frame((100, 100, 400, 140)),
              frame((100, 100, 400, 140), (1300, 800, 1550, 860))]
    calls = []

    def recognize(im):
        calls.append(im.size)
        if im.size == (1600, 900):
            return [("Gold: 10", (100, 100, 300, 40))]
        return [("Next turn", (0, 0, im.width, im.height // 2))]

    monkeypatch.setattr(perceive, "_recognize_windows", recognize)
    monkeypatch.setattr(observers, "ocr_available", lambda: (True, ""))
    monkeypatch.setattr(desktop, "collect_ui_elements", lambda ctx, title, limit=200, **k: ("Unciv", []))
    monkeypatch.setattr(desktop, "focus_window", lambda title: title)
    obs = observers.DesktopObserver(ctx=None, window_title="Unciv", diff=True)
    monkeypatch.setattr(obs, "_target_hwnd", lambda: 0)
    monkeypatch.setattr(obs, "_window", lambda: 0)
    monkeypatch.setattr(obs, "snapshot", lambda hwnd=0: frames.pop(0))
    first = obs.screen("end the turn")
    assert [i.label for i in first.items] == ["Gold: 10"] and not first.items[0].fresh and calls == [(1600, 900)]
    second = obs.screen("end the turn")
    assert [i.label for i in second.items] == ["Gold: 10"] and len(calls) == 1 and obs.changed == []
    third = obs.screen("end the turn")
    assert len(calls) == 2 and calls[-1][0] < 1600  # sadece değişen köşe okundu
    labels = {i.label: i.fresh for i in third.items}
    assert labels == {"Gold: 10": False, "Next turn": True}


def test_image_change_counts_when_text_stays_the_same():
    """Solitaire: desteye tıklayınca kart açılıyor ama OCR kart yüzünü okumuyor; yazı imzası aynı kalınca "ekran
    değişmedi" denip tekrar koruması durduruyordu."""
    from alice.decider import Answer, Decision
    from alice.reflex.agent import ScreenAgent
    from alice.reflex.perceive import Screen, image_changed

    base = frame((100, 100, 210, 250))
    card = frame((100, 100, 210, 250), (250, 100, 360, 250))  # yanda yeni bir kart
    caret = base.copy()
    caret.paste((255, 255, 255), (800, 400, 802, 420))  # yanıp sönen imleç
    assert image_changed(base, card) and not image_changed(base, caret) and not image_changed(None, base)

    class Table:
        n = 0

        def screen(self, goal=""):
            img = frame((100, 100, 210, 250), *[(250 + 120 * k, 100, 360 + 120 * k, 250) for k in range(self.n)])
            return Screen("Solitaire", "desktop", [Item("i1", "ocr", "text", "Puan: 5", (0.9, 0.0, 1.0, 0.05)),
                                                   Item("i2", "ocr", "text", "Yeni", (0.1, 0.95, 0.2, 1.0))], image=img)

        def act(self, action, item=None, **k):
            self.n += 1

    class Brain:
        supports_images = False

        def decide(self, state, questions, images=None):
            return Decision({"action": Answer("choice", "click", 0.9), "done": Answer("noul", 0.05, 0.9),
                             "missing": Answer("noul", 0.05, 0.9), "key": Answer("choice", "enter", 0.9),
                             "item": Answer("choice", "i1", 0.9), "drop": Answer("choice", "i2", 0.9)}, "fake", 0.01)

    agent = ScreenAgent(Table(), Brain(), settle=0)
    agent.track_changes = True
    res = agent.run("draw cards", max_steps=3)
    assert res.status == "max_steps"  # durdurulmadı: her tıklama kart açtı
    assert all(e["changed"] for e in agent.evidence) and agent.evidence[0]["action"].endswith("screen changed")
    # Yazısız değişimin yeri kanıtta: yansıtma 'menülerde kayboldun' diye uydurmasın.
    assert agent.evidence[0]["changed_places"] == ["top-left"]
    from alice.reflex.game import evidence_lines

    assert evidence_lines(agent.evidence[:1])[0].endswith("| picture changed at: top-left")

    class Seeing(Brain):
        supports_images = True
        seen = []

        def decide(self, state, questions, images=None):
            self.seen.append((dict(state), dict(images or {})))
            return super().decide(state, questions, images)

    class TableWithClick(Table):
        def click_at(self, fx, fy):
            self.n += 1

    agent = ScreenAgent(TableWithClick(), Seeing(), settle=0)
    agent.track_changes = True
    agent.run("draw cards", max_steps=2)
    first, second = Seeing.seen[0], Seeing.seen[1]
    assert "screen_at_start" not in first[0] and second[0]["screen_at_start"] in second[1]  # önce/sonra görüntüsü

    class Compares(Brain):
        supports_images = True
        asked = []

        def decide(self, state, questions, images=None):
            self.asked.append("changed_as_asked" in questions)
            d = super().decide(state, questions, images)
            if "changed_as_asked" in questions:  # "tamamen bitti mi" düşük, görsel kıyas yüksek
                d.answers["changed_as_asked"] = Answer("noul", 0.8, 0.9)  # canlıda kart çekilince görülen
            return d

    agent = ScreenAgent(TableWithClick(), Compares(), settle=0)
    agent.track_changes = True
    res = agent.run("draw one card", max_steps=5)
    assert Compares.asked == [False, True] and res.status == "done" and res.steps == 1
    # Değişen bölge küçükse Jev'e o bölgenin önce/sonra kırpıntısı gider, soru da onlara bakar.
    state, images = Seeing.seen[1]
    assert images[state["changed_part_now"]] and images[state["changed_part_at_start"]]
    from alice.reflex.perceive import changed_crops

    before, after = changed_crops(base, card)
    assert before.size == after.size and before.width < base.width // 3
    assert changed_crops(base, base.copy()) is None and changed_crops(base, frame((0, 0, 1599, 899))) is None
    clock = frame((100, 100, 210, 250), (250, 100, 360, 250), (1540, 10, 1590, 30))  # kart + sağ üstte saat
    before, after = changed_crops(base, clock)
    assert after.width < 600  # saate kadar uzanmadı, kartın etrafı


def test_back_and_forth_loop_is_stopped():
    """Unciv: 'Pick a tech!' tech ekranını açıyor, 'Close' kapatıyor; her tıklama ekranı değiştirdiği için eski koruma
    15 adım boyunca durmadı."""
    from alice.decider import Answer, Decision
    from alice.reflex.agent import ScreenAgent
    from alice.reflex.perceive import Screen

    class Flip:
        page = "map"
        acts = 0

        def screen(self, goal=""):
            label = "Pick a tech!" if self.page == "map" else "Close"
            return Screen(self.page, "desktop", [Item("i1", "ocr", "text", label, (0.0, 0.0, 0.1, 0.05)),
                                                 Item("i2", "ocr", "text", "Gold", (0.5, 0.0, 0.6, 0.05))])

        def act(self, action, item=None, **k):
            self.acts += 1
            self.page = "tech" if self.page == "map" else "map"

    class Brain:
        supports_images = False

        def decide(self, state, questions, images=None):
            return Decision({"action": Answer("choice", "click", 0.9), "done": Answer("noul", 0.05, 0.9),
                             "missing": Answer("noul", 0.05, 0.9), "key": Answer("choice", "enter", 0.9),
                             "item": Answer("choice", "i1", 0.9), "drop": Answer("choice", "i2", 0.9)}, "fake", 0.01)

    obs = Flip()
    res = ScreenAgent(obs, Brain(), settle=0).run("pick a tech", max_steps=15)
    assert res.status == "needs_help" and "döngü" in res.reason and obs.acts == 4


def test_agent_gives_jev_changes_since_start_and_keeps_evidence():
    """Oyundan bağımsız 'bitti mi?': tur sayacı gibi değerler 'kaybolan/çıkan' olarak görünür."""
    from alice.decider import Answer, Decision
    from alice.reflex.agent import ScreenAgent
    from alice.reflex.perceive import Screen

    class Board:
        turn = 1

        def screen(self, goal=""):
            return Screen("Game", "desktop", [Item("i1", "ocr", "text", f"Turn {self.turn}", (0.9, 0.0, 1.0, 0.05)),
                                              Item("i2", "ocr", "text", "End turn", (0.9, 0.9, 1.0, 0.95))])

        def act(self, action, item=None, **k):
            self.turn += 1

    class Brain:
        supports_images = False
        states = []

        def decide(self, state, questions, images=None):
            self.states.append((state, questions))
            done = 0.9 if "changes_since_start" in state else 0.05
            return Decision({"action": Answer("choice", "click", 0.9), "done": Answer("noul", done, 0.9),
                             "missing": Answer("noul", 0.05, 0.9), "key": Answer("choice", "enter", 0.9),
                             "item": Answer("choice", "i2", 0.9), "drop": Answer("choice", "i1", 0.9)}, "fake", 0.01)

    agent = ScreenAgent(Board(), Brain(), settle=0)
    agent.track_changes = True
    res = agent.run("end the turn", notes=lambda scr: f"seen {len(scr.items)} items")
    assert res.status == "done"
    first, second = Brain.states[0][0], Brain.states[1][0]
    assert "changes_since_start" not in first and first["notes"] == "seen 2 items"  # notlar ekrana göre seçiliyor
    assert second["changes_since_start"] == {"appeared": ["Turn 2"], "disappeared": ["Turn 1"]}
    assert "changes_since_start" in Brain.states[0][1]["done"].criteria["true"]
    assert agent.evidence == [{"action": "clicked text 'End turn' (bottom-right) -> screen changed", "changed": True,
                               "appeared": ["Turn 2"], "disappeared": ["Turn 1"], "changed_places": []}]
    plain = ScreenAgent(Board(), Brain(), settle=0)  # oyun dışında reflex eskisi gibi
    plain.run("end the turn", max_steps=2)
    assert "changes_since_start" not in Brain.states[-1][0] and plain.evidence == []


# ------------------------------------------------------------------------------------------- oturum
def test_session_notes_turns_and_context(tmp_path):
    assert game_key(" Unciv ") == "unciv" and game_key("Slay the Spire!") == "slay-the-spire"
    s = GameSession.open(tmp_path, "Kart Oyunu")
    assert s.notes() == [] and s.lessons() == [] and s.turn == 0  # koda yazılı hazır not yok
    assert s.add_note("The user wants an aggressive strategy.")
    assert not s.add_note("the user wants an aggressive strategy.")  # aynısı
    for n in range(5):
        s.record(f"goal {n}", "done", 2, f"summary {n}")
    ctx = s.context()
    assert ctx.startswith("Game: Kart Oyunu.") and "aggressive strategy" in ctx
    assert "turn 5: summary 4" in ctx and "summary 1" not in ctx  # son üç tur
    again = GameSession.open(tmp_path, "kart oyunu", "Kart")
    assert again.turn == 5 and again.title == "Kart"


def test_lessons_add_confirm_contradict_and_relevance(tmp_path):
    s = GameSession.open(tmp_path, "X")
    out = s.apply({"add": [{"trigger": "", "do": "Never quit to the main menu.", "kind": "rule"},
                           {"trigger": "Exit City", "do": "Leave the city screen before ending the turn."}]},
                  source="prior")
    assert out["add"] == 2 and all(r["source"] == "prior" and r["score"] == 0 for r in s.lessons())
    a, b = s.lessons()
    assert s.apply({"add": [{"trigger": "Exit City", "do": "leave the city screen before ending the turn."}]})["add"] == 0
    s.apply({"confirm": [b["id"]], "contradict": [a["id"]]})  # yanlış çıkan prior ders düşer
    (left,) = s.lessons()
    assert left["id"] == b["id"] and left["score"] == 1 and left["source"] == "seen"
    s.apply({"edit": [{"id": b["id"], "do": "Click 'Exit City' first."}],
             "add": [{"do": f"lesson {n}", "kind": "nonsense"} for n in range(6)]})
    assert len(s.lessons()) == 4 and s.lessons()[0]["do"] == "Click 'Exit City' first."  # turda en fazla 3 yeni
    # Keşif bir keresinde "istenirse hesabınla giriş yap" yazmıştı: oyun oturumu kullanıcı adına onay/giriş yapmaz.
    assert s.apply({"add": [{"do": "Log in with your Microsoft account if prompted."},
                            {"do": "Accept the privacy terms to continue."},
                            {"do": "Avoid clicking ads or the Buy button."}]})["add"] == 1
    assert s.lessons()[1]["kind"] == "ui"
    # Tetik ekranda görülen bir yazı olmalı: modelin kendi cümlesi ("clicked something ...") hiçbir ekrana uymaz.
    out = s.apply({"add": [{"trigger": "clicked something visible on screen at left-top", "do": "Aim better."},
                           {"trigger": "Gold: 10", "do": "Gold is shown at the top."}]},
                  seen_texts=["Gold: 10", "Exit City"])
    assert out["add"] == 1 and s.lessons()[-1]["trigger"] == "Gold: 10"
    rel = s.relevant(["Gold: 10", "Exit City"])
    assert {r["trigger"] for r in rel[:2]} == {"Gold: 10", "Exit City"}  # ekrana uyanlar tetiksizlerden önce
    assert all(r["trigger"] != "Exit City" for r in s.relevant(["Gold: 10"]))  # ekranda yoksa gelmez
    ctx = s.context(["Exit City"])
    assert "When 'Exit City' is on screen: Click 'Exit City' first." in ctx


def test_code_lessons_from_clicks_that_changed_nothing():
    ev = [{"action": "clicked text 'Pick a construction' (top-left) -> nothing changed on screen", "changed": False},
          {"action": "clicked text 'Pick a construction' (top-left) -> nothing changed on screen", "changed": False},
          {"action": "clicked text 'Gold' (top-left) -> nothing changed on screen", "changed": False},
          {"action": "clicked text 'Warrior' (middle-left) -> screen changed", "changed": True}]
    (lesson,) = code_lessons(ev)["add"]
    assert lesson["trigger"] == "Pick a construction" and "changes nothing" in lesson["do"]
    loop = [{"action": "clicked Button 'KLONDIKE' (middle-left) -> screen changed", "changed": True}] * 3 + \
        [{"action": "ekranda görülen yere tıklandı", "changed": False}]
    (lesson,) = code_lessons(loop)["add"]
    assert lesson["trigger"] == "KLONDIKE" and "back and forth" in lesson["do"]


def test_reflect_uses_evidence_and_falls_back():
    got = {}

    def chat(messages):
        got["msgs"] = messages
        return ('```json\n{"summary": "Ended the turn; the counter moved to Turn 2.", '
                '"add": [{"trigger": "End turn", "do": "End turn advances the counter.", "kind": "ui"}], '
                '"confirm": ["l1"]}\n```')

    ev = [{"action": "clicked text 'End turn' -> screen changed", "changed": True, "appeared": ["Turn 2"],
           "disappeared": ["Turn 1"]}]
    summary, ops = reflect(chat, "G", "end the turn", "done", "hedefe ulaşıldı", ev,
                           [{"id": "l1", "trigger": "", "do": "Turns end with 'End turn'.", "source": "prior"}])
    assert summary == "Ended the turn; the counter moved to Turn 2." and ops["confirm"] == ["l1"]
    sent = got["msgs"][1]["content"]
    assert "appeared: Turn 2 | disappeared: Turn 1" in sent and "l1: Turns end with 'End turn'. (unverified)" in sent
    assert reflect(lambda m: "The", "G", "g", "done", "", [], [])[0] == "g -> done"  # yarım cevap

    def broken(messages):
        raise RuntimeError("kota")

    assert reflect(broken, "G", "g", "needs_help", "", ev, []) == \
        ("g -> needs_help; last: 1. clicked text 'End turn' -> screen changed | appeared: Turn 2 | disappeared: Turn 1",
         {})
    assert reflect(None, "G", "g", "done", "", [], []) == ("g -> done", {})


def test_discover_writes_prior_lessons_from_first_screen():
    got = {}

    def chat(messages):
        got["msgs"] = messages
        return '{"add": [{"trigger": "Start new game", "do": "Start a game from here.", "kind": "ui"}]}'

    ops = discover(chat, "Some Game", ["Start new game", "Options"])
    assert ops["add"][0]["trigger"] == "Start new game" and "- Options" in got["msgs"][1]["content"]
    assert discover(lambda m: "bilmiyorum", "G", []) == {} and discover(None, "G", []) == {}


# ------------------------------------------------------------------------------------------- araç
def test_game_tool_discovers_plays_and_learns(monkeypatch, tmp_path):
    import alice.config as config
    import alice.reflex.agent as agent_mod
    import alice.reflex.observers as obs_mod
    from alice.reflex.loop import ReflexResult
    from alice.tools.base import ToolContext
    from alice.tools.game import STATE_KEY, Game

    monkeypatch.setattr(config, "alice_home", lambda: tmp_path)
    made = []

    class Obs:
        def __init__(self, ctx, title, goal, diff):
            made.append((title, diff))

        def screen(self, goal=""):
            return SimpleNamespace(items=[Item("i1", "ocr", "text", "Start new game"), Item("i2", "ocr", "text", "Exit")])

    monkeypatch.setattr(obs_mod, "DesktopObserver", Obs)
    runs = []

    class FakeAgent:
        def __init__(self, observer, *a, **k):
            self.observer = observer
            self.track_changes = False
            self.evidence = []
            self.last = SimpleNamespace(items=[Item("i1", "ocr", "text", "Exit")])

        def run(self, goal, max_steps, notes):
            screen = SimpleNamespace(items=[Item("i1", "ocr", "text", "Start new game")])
            runs.append((self.observer, goal, notes(screen), self.track_changes))
            self.evidence = [{"action": "clicked text 'Exit' (top) -> nothing changed on screen", "changed": False}] * 2
            return ReflexResult("needs_help", 2, "aynı eylem iki kez ekranı değiştirmedi", [], "G", "desktop")

    monkeypatch.setattr(agent_mod, "ScreenAgent", FakeAgent)
    monkeypatch.setattr(agent_mod, "make_translator", lambda router: None)

    def chat(role, msgs, **k):
        if "never played" in msgs[0]["content"]:  # keşif
            return SimpleNamespace(text='{"add": [{"trigger": "Start new game", "do": "Begin here."}]}')
        return SimpleNamespace(text='{"summary": "Tried Exit twice; nothing happened.", "confirm": ["l1"]}')

    ctx = ToolContext(cwd=tmp_path, config=None, router=SimpleNamespace(chat=chat), decider=object())
    tool = Game()
    assert tool.risk_for({"action": "status"}) == "read" and tool.risk_for({"action": "turn"}) == tool.risk
    out = tool.run(ctx, "start", game="Hiç Görülmemiş Oyun").text
    assert "1 başlangıç dersi" in out and "Begin here. (unverified)" in out
    r = tool.run(ctx, "turn", goal="start a game")
    assert made == [("Hiç Görülmemiş Oyun", True)]  # keşifteki gözlemci turda da kullanılıyor
    observer, goal, notes, tracked = runs[0]
    assert tracked and "When 'Start new game' is on screen: Begin here." in notes
    assert "özet: Tried Exit twice" in r.text and "+1 yeni, 1 doğrulandı" in r.text  # kodun dersi + modelin onayı
    lessons = {x["do"]: x for x in GameSession.open(tmp_path, "Hiç Görülmemiş Oyun").lessons()}
    assert lessons["Begin here."]["source"] == "seen"
    assert any(d.startswith("Clicking 'Exit' changes nothing") for d in lessons)
    assert "2 ders" in tool.run(ctx, "status").text
    assert "toplam 1 tur" in tool.run(ctx, "end").text and STATE_KEY not in ctx.state

