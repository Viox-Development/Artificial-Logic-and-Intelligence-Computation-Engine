from __future__ import annotations

import pytest

from alice.decider import Answer, Decision
from alice.reflex.loop import ReflexLoop, rank
from alice.reflex.observers import Element, Observation


class FakeObserver:
    kind = "fake"

    def __init__(self):
        self.page = "home"
        self.actions = []

    def observe(self):
        if self.page == "home":
            els = [Element(1, "a", "Haberler", False), Element(2, "input[search]", "Ara", True),
                   Element(3, "button", "Ayarlar", False)]
        elif self.page == "settings":
            els = [Element(4, "a", "Gizlilik", False), Element(5, "a", "Geri", False)]
        else:
            els = [Element(6, "h1", "Gizlilik ayarları", False)]
        return Observation(self.page, f"https://x/{self.page}", els)

    def click(self, element_id):
        self.actions.append(("click", element_id))
        self.page = {3: "settings", 4: "privacy"}.get(element_id, self.page)

    def type(self, element_id, text, submit):
        self.actions.append(("type", element_id, text))

    def scroll(self, down):
        self.actions.append(("scroll", down))

    def back(self):
        self.actions.append(("back",))

    def press_enter(self):
        self.actions.append(("enter",))


class ScriptedDecider:
    names = ["fake"]

    def __init__(self, picks):
        self.picks = list(picks)
        self.states = []

    def decide(self, state, questions):
        self.states.append((state, questions))
        label, conf, done = self.picks.pop(0)
        assert label in questions["next"].criteria, f"{label} seçeneklerde yok"
        return Decision({"next": Answer("choice", label, conf), "done": Answer("noul", done, 1.0)}, "fake", 0.01)


def test_navigates_to_goal():
    obs = FakeObserver()
    dec = ScriptedDecider([("click_3", 0.9, 0.0), ("click_4", 0.95, 0.0), ("done", 0.9, 0.95)])
    res = ReflexLoop(obs, dec, settle=0).run("Ayarlar > Gizlilik sayfasını aç")
    assert res.status == "done" and obs.actions == [("click", 3), ("click", 4)]
    assert "click_3" in res.log[0] or "Ayarlar" in res.log[0]


def test_low_confidence_escalates():
    res = ReflexLoop(FakeObserver(), ScriptedDecider([("click_1", 0.2, 0.0)]), settle=0).run("x")
    assert res.status == "needs_help" and "düşük güven" in res.reason


def test_typing_uses_inputs_then_writer():
    obs = FakeObserver()
    dec = ScriptedDecider([("type_2", 0.9, 0.0), ("press_enter", 0.9, 0.0), ("done", 0.9, 0.9)])
    res = ReflexLoop(obs, dec, settle=0).run("ara: hava durumu", inputs={"ara": "hava durumu ankara"})
    assert ("type", 2, "hava durumu ankara") in obs.actions and ("enter",) in obs.actions
    assert res.status == "done"

    obs2 = FakeObserver()
    dec2 = ScriptedDecider([("type_2", 0.9, 0.0), ("done", 0.9, 0.9)])
    ReflexLoop(obs2, dec2, text_writer=lambda goal, field: "üretilen", settle=0).run("bir şey ara")
    assert ("type", 2, "üretilen") in obs2.actions


def test_repeated_action_detected():
    dec = ScriptedDecider([("scroll_down", 0.9, 0.0)] * 5)
    res = ReflexLoop(FakeObserver(), dec, settle=0).run("x")
    assert res.status == "needs_help" and "tekrar" in res.reason


def test_rank_prefers_goal_words():
    els = [Element(1, "a", "Haberler", False), Element(2, "a", "Gizlilik ayarları", False),
           Element(3, "input", "", True)]
    top = rank(els, "gizlilik ayarlarını aç", 2, set())
    assert top[0].id == 2


def test_low_confidence_click_on_element_named_in_goal_proceeds():
    obs = FakeObserver()
    dec = ScriptedDecider([("click_3", 0.45, 0.0), ("click_4", 0.95, 0.0), ("done", 0.9, 0.95)])
    res = ReflexLoop(obs, dec, settle=0).run("ekrandaki Ayarlar düğmesine tıkla, sonra Gizlilik")
    assert obs.actions[0] == ("click", 3) and res.status == "done"
    # element whose name is not in the goal: still falls back to the brain
    res = ReflexLoop(FakeObserver(), ScriptedDecider([("click_1", 0.45, 0.0)]), settle=0).run("ayarları aç")
    assert res.status == "needs_help"


def test_named_target_repeated_on_page_still_counts():
    from alice.reflex.loop import _named_in_goal

    els = [Element(1, "a", "Wish Me Luck", False), Element(2, "button", "Play Wish Me Luck", False),
           Element(3, "a", "Lucky Day", False)]
    by_key = {"click_1": els[0]}
    assert _named_in_goal("click_1", by_key, els, "ekrandaki Wish Me Luck şarkısını aç")
    els.append(Element(4, "a", "Wish You Were Here", False))
    by_key = {"click_4": els[3]}
    assert not _named_in_goal("click_4", by_key, els, "ekrandaki Wish Me Luck şarkısını aç")


class LoginObserver(FakeObserver):
    def observe(self):
        return Observation("WhatsApp", "https://web.whatsapp.com/",
                           [Element(1, "button", "Telefon numarası kullanarak giriş yapın", False),
                            Element(2, "input[tel]", "WhatsApp'a giriş yapmak için telefon numaranızı yazın", True)])


def test_login_controls_are_left_to_the_user():
    for pick in ("click_1", "type_2"):
        obs = LoginObserver()
        res = ReflexLoop(obs, ScriptedDecider([(pick, 0.9, 0.0)]), settle=0).run("Que Mal'a selam yaz")
        assert res.status == "needs_login" and obs.actions == []
    obs = LoginObserver()  # touched only if the goal is to log in
    ReflexLoop(obs, ScriptedDecider([("click_1", 0.9, 0.0), ("done", 0.9, 0.95)]), settle=0).run(
        "telefon numarasıyla giriş yap")
    assert obs.actions == [("click", 1)]


def test_desktop_reflex_frames_click_on_hud(monkeypatch):
    """Reflex is now the first tool for screen work: draw a ring on the HUD before clicking (without waiting)."""
    from alice.hud import views
    from alice.reflex.observers import DesktopObserver
    from alice.reflex.perceive import Item
    from alice.tools import computer as comp

    seen, clicks = [], []
    monkeypatch.setattr(views, "click_target", lambda x, y, action="click", wait=True: seen.append((x, y, action, wait)))
    monkeypatch.setattr(comp, "_pyautogui", lambda: type("PG", (), {"rightClick": staticmethod(
        lambda *a: clicks.append(a))})())
    obs = DesktopObserver.__new__(DesktopObserver)
    obs._rect = (100, 50, 1000, 800)
    item = Item("k", "ocr", "text", "Görünüm", rect=(0.4, 0.4, 0.6, 0.5))
    obs.act("right_click", item)
    assert seen == [(600, 410, "right_click", False)] and clicks == [(600, 410)]
    with pytest.raises(ValueError, match="yeri bilinmiyor"):  # not a HUD error, the action's own error
        obs.act("right_click", Item("n", "ocr", "text", "yeri yok"))
    assert len(seen) == 1


def test_desktop_reflex_survives_uia_com_error(monkeypatch):
    """Live: UIA COM error (0x80040201) dropped the whole reflex with "ekran okunamadı"; it should continue with OCR."""
    import alice.reflex.observers as ob
    from alice.tools import desktop

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError(-2147220991, "An event was unable to invoke any of the subscribers")

    monkeypatch.setattr(desktop, "collect_ui_elements", boom)
    monkeypatch.setattr(ob, "foreground_title", lambda: "Program Manager")
    monkeypatch.setattr(ob, "ocr_available", lambda: (False, ""))
    monkeypatch.setattr(ob, "focused_control", lambda: None, raising=False)
    monkeypatch.setattr(ob.time, "sleep", lambda s: None)
    obs = ob.DesktopObserver.__new__(ob.DesktopObserver)
    obs.ctx, obs.title, obs.goal, obs.diff, obs.echoes, obs._unparked, obs._rect = None, None, "x", False, (), True, None
    obs._prev_img, obs._prev_lines = None, []
    monkeypatch.setattr(obs, "snapshot", lambda hwnd=0: None, raising=False)
    monkeypatch.setattr(obs, "_target_hwnd", lambda: 0, raising=False)
    monkeypatch.setattr(obs, "_window", lambda: 0, raising=False)
    scr = obs.screen("simgeleri gizle")
    assert scr.title == "Program Manager" and len(calls) == 2  # bir kez yeniden denendi
