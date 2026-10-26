"""Oyun oturumu: değişen bölgeler, OCR önbelleği, yeni öğeler, notlar, tur özetleri ve game aracı."""

from __future__ import annotations

import json
from types import SimpleNamespace

from PIL import Image, ImageDraw

from alice.reflex import perceive
from alice.reflex.game import STARTER_NOTES, GameSession, game_key, summarize_turn
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


# ------------------------------------------------------------------------------------------- oturum
def test_session_notes_turns_and_context(tmp_path):
    assert game_key(" Unciv ") == "unciv" and game_key("Slay the Spire!") == "slay-the-spire"
    s = GameSession.open(tmp_path, "Unciv")
    assert s.notes() == STARTER_NOTES["unciv"] and s.turn == 0
    assert s.add_note("Settlers should found the capital on turn 1.")
    assert not s.add_note("settlers should found the capital on turn 1.")  # aynısı
    for n in range(5):
        s.record(f"goal {n}", "done", 2, f"summary {n}")
    ctx = s.context()
    assert ctx.startswith("Game: Unciv.") and "Settlers should found" in ctx
    assert "turn 5: summary 4" in ctx and "summary 1" not in ctx  # son üç tur
    again = GameSession.open(tmp_path, "unciv", "Unciv 4.")
    assert again.turn == 5 and again.title == "Unciv 4." and again.notes()[-1].startswith("Settlers")
    assert GameSession.open(tmp_path, "Bilinmeyen Oyun").notes() == []


def test_summarize_turn():
    got = {}

    def chat(messages):
        got["msgs"] = messages
        return "Ended turn 3; a new city screen\nopened."

    out = summarize_turn(chat, "end the turn", "done", ["1. clicked text 'Next turn'"], ["Pick construction"])
    assert out == "Ended turn 3; a new city screen opened."
    assert "Pick construction" in got["msgs"][1]["content"] and "clicked text 'Next turn'" in got["msgs"][1]["content"]

    def broken(messages):
        raise RuntimeError("kota")

    assert summarize_turn(broken, "end the turn", "needs_help", ["1. pressed enter"], []) == \
        "end the turn -> needs_help; last: 1. pressed enter"
    assert summarize_turn(None, "g", "done", [], []) == "g -> done"
    assert summarize_turn(lambda m: "The", "end the turn", "done", [], []) == "end the turn -> done"  # yarım cevap


# ------------------------------------------------------------------------------------------- araç
def test_game_tool_start_turn_note_end(monkeypatch, tmp_path):
    import alice.config as config
    import alice.reflex.agent as agent_mod
    import alice.reflex.observers as obs_mod
    from alice.reflex.loop import ReflexResult
    from alice.tools.base import ToolContext
    from alice.tools.game import STATE_KEY, Game

    monkeypatch.setattr(config, "alice_home", lambda: tmp_path)
    made = []
    monkeypatch.setattr(obs_mod, "DesktopObserver", lambda ctx, title, goal, diff: made.append((title, diff)) or
                        SimpleNamespace(title=title))
    runs = []

    class FakeAgent:
        def __init__(self, observer, *a, **k):
            self.observer = observer
            self.last = SimpleNamespace(items=[Item("i1", "ocr", "text", "Pick a tech", fresh=True),
                                               Item("i2", "ocr", "text", "Gold: 10")])

        def run(self, goal, max_steps, notes):
            runs.append((self.observer, goal, notes))
            return ReflexResult("done", 2, "hedefe ulaşıldı", ["1. clicked text 'Next turn'"], "Unciv", "desktop")

    monkeypatch.setattr(agent_mod, "ScreenAgent", FakeAgent)
    monkeypatch.setattr(agent_mod, "make_translator", lambda router: None)
    router = SimpleNamespace(chat=lambda role, msgs, **k: SimpleNamespace(text="Turn ended; a tech must be picked."))
    ctx = ToolContext(cwd=tmp_path, config=None, router=router, decider=object())
    tool = Game()
    assert tool.risk_for({"action": "status"}) == "read" and tool.risk_for({"action": "turn"}) == tool.risk

    out = tool.run(ctx, "start", game="Unciv").text
    assert "Next turn" in out and "0 tur" in out
    assert "eklendi" in tool.run(ctx, "note", note="Capital is on the river.").text
    r1 = tool.run(ctx, "turn", goal="end the turn")
    r2 = tool.run(ctx, "turn", goal="pick Pottery")
    assert made == [("Unciv", True)]  # gözlemci turlar arasında aynı: neyin değiştiği turdan tura görülür
    assert runs[0][0] is runs[1][0] and "Capital is on the river." in runs[0][2]
    assert "turn 1: Turn ended; a tech must be picked." in runs[1][2]  # önceki turun özeti bağlamda
    assert "tur 2" in r2.text and "özet: Turn ended" in r1.text and "SONUÇ: hedef yapıldı." in r1.text
    rows = [json.loads(x) for x in (tmp_path / "games" / "unciv" / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["goal"] for r in rows] == ["end the turn", "pick Pottery"] and rows[0]["status"] == "done"
    assert "2 tur" in tool.run(ctx, "status").text
    assert "toplam 2 tur" in tool.run(ctx, "end").text and STATE_KEY not in ctx.state
