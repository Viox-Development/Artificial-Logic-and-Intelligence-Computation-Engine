"""Post-action observation: what actually changed on screen (new window, error box, UAC) is passed to the model."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alice.tools import outcome
from alice.tools.base import COMPUTER, READ, Tool, ToolResult


@pytest.fixture
def screen(monkeypatch):
    """Fake desktop: the test sets the windows and dialog texts."""
    s = SimpleNamespace(wins={1: {"title": "Opera", "cls": "Chrome_WidgetWin_1", "pid": 5}}, texts={}, fg=1,
                        uac=False, after=None)
    monkeypatch.setattr(outcome, "IS_WIN", True)
    monkeypatch.setattr(outcome, "windows", lambda: dict(s.wins))
    monkeypatch.setattr(outcome, "dialog_text", lambda h: s.texts.get(h, ""))
    monkeypatch.setattr(outcome, "foreground", lambda: s.fg)
    monkeypatch.setattr(outcome, "uac_open", lambda: s.uac)
    return s


def test_new_error_box_is_reported_with_its_text(screen):
    w = outcome.Watch()
    screen.wins[7] = {"title": "Sonora-Setup.exe", "cls": "#32770", "pid": 9}
    screen.texts[7] = "Windows 'C:/Users/k/Downloads/Sonora-Setup.exe' öğesini bulamıyor. | Tamam"
    screen.fg = 7
    note = w.note(sleep=lambda s: None)
    assert "new dialog 'Sonora-Setup.exe'" in note and "bulamıyor" in note
    assert "Enter/OK blindly" in note


def test_dialog_already_in_front_is_mentioned_again(screen):
    screen.wins[8] = {"title": "Farklı Kaydet", "cls": "#32770", "pid": 5}
    screen.fg = 8
    note = outcome.Watch().note(sleep=lambda s: None)  # the box was already open and is still in front
    assert "a dialog is open in front 'Farklı Kaydet'" in note


def test_open_without_any_window_is_not_assumed_success(screen):
    slept = []
    note = outcome.Watch(opening=True).note(sleep=slept.append)
    assert "no new window appeared" in note and "don't assume" in note
    assert sum(slept) == pytest.approx(outcome.OPEN_WAIT)


def test_uac_prompt_is_handed_to_the_user(screen):
    w = outcome.Watch()
    screen.uac = True
    assert "permission screen (UAC)" in w.note(sleep=lambda s: None)


def test_quiet_when_nothing_changed(screen):
    assert outcome.Watch().note(sleep=lambda s: None) == ""


def test_agent_appends_what_happened_on_screen(screen, monkeypatch):
    from alice import agent as agent_mod

    class Clicker(Tool):
        name = "ui_act"
        description = "d"
        parameters = {"type": "object", "properties": {}}
        risk = COMPUTER

        def run(self, ctx, **kw):
            screen.wins[9] = {"title": "Hata", "cls": "#32770", "pid": 3}
            screen.texts[9] = "Dosya bulunamadı | Tamam"
            return ToolResult("tıklandı")

    class Reader(Clicker):
        name = "ui_elements"
        risk = READ

        def run(self, ctx, **kw):
            return ToolResult("liste")

    monkeypatch.setattr(outcome.time, "sleep", lambda s: None)
    a = object.__new__(agent_mod.Agent)
    a.config = SimpleNamespace(agent={})
    a.cancel = SimpleNamespace(is_set=lambda: False)
    assert a._watch(Reader(), {}) is None  # read-only: no observation, no delay
    w = a._watch(Clicker(), {"action": "click"})
    Clicker().run(None)
    assert "Dosya bulunamadı" in w.note(sleep=lambda s: None)
    a.config = SimpleNamespace(agent={"observe_outcomes": False})
    assert a._watch(Clicker(), {}) is None
