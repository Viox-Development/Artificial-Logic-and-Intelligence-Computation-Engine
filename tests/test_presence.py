from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace

from alice.memory import LongMemory
from alice.presence import Greeter, compose, part_of_day


def cfg(**greeting):
    return SimpleNamespace(data={"greeting": {"quiet_hours": "", **greeting}})


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_part_of_day():
    assert part_of_day(datetime(2026, 10, 22, 8)) == "Günaydın"
    assert part_of_day(datetime(2026, 10, 22, 14)) == "İyi günler"
    assert part_of_day(datetime(2026, 10, 22, 21)) == "İyi akşamlar"


def test_greets_after_long_absence_with_followups():
    mem = LongMemory(":memory:", {})
    now = datetime(2026, 10, 22, 17, 30)  # follow-up time is also fixed, so the test does not depend on today's date
    fid = mem.add_followup("Matematik sınavının nasıl geçtiğini sor.", now.timestamp() - 3600, "ask")
    sent = []
    idle = iter([6000.0, 6100.0, 3.0, 2.0])
    voice = SimpleNamespace(speaker=SimpleNamespace(wait=lambda t: True), follow_up=5.0, armed_until=0.0)
    g = Greeter(cfg(), lambda n: sent.append(n) or True, memory=lambda: mem, idle=lambda: next(idle),
                clock=lambda: now)
    g.voice = voice
    assert not g.tick() and not g.tick()  # uzakta
    assert g.tick()  # came back
    assert wait_for(lambda: sent)
    text = sent[0].body
    assert text.startswith("İyi günler") and "Matematik" in text and sent[0].topic == "presence"
    assert wait_for(lambda: voice.armed_until > time.monotonic())  # answers without needing "Hey Alice"
    assert mem.due_followups() == [] and [f.id for f in mem.followups(include_done=True)] == [fid]
    assert mem.recent(1)[0].channel == "karşılama"  # Live sees it in the most recent conversation
    assert not g.tick()  # does not greet again


def test_silent_without_news_after_first_greeting_and_respects_gap_and_quiet():
    sent = []
    g = Greeter(cfg(), lambda n: sent.append(n) or True, idle=lambda: 0.0, clock=lambda: datetime(2026, 10, 22, 10),
                morning=lambda c, n: {})
    assert g.greet(7200) .startswith("Günaydın")  # first return of the day: short greeting
    assert g.greet(7200) == ""  # same day, nothing to say: stay quiet
    assert len(sent) == 1
    g2 = Greeter(cfg(quiet_hours="00:00-23:59"), lambda n: True, idle=lambda: 0.0,
                 clock=lambda: datetime(2026, 10, 22, 12))  # a run between 23:59-00:00 used to fall outside quiet hours
    g2.away_since = time.time() - 9000
    assert not g2.tick()  # quiet hours
    g3 = Greeter(cfg(), lambda n: True, idle=lambda: 0.0)
    g3.away_since, g3.last_greet = time.time() - 9000, time.time() - 60
    assert not g3.tick()  # greeted just now


def test_muted_mode_does_not_count():
    g = Greeter(cfg(), lambda n: False, idle=lambda: 0.0, clock=lambda: datetime(2026, 10, 22, 10),
                morning=lambda c, n: {})
    assert g.greet(7200) == "" and g.last_greet == 0.0


def test_compose_uses_model_with_fallback():
    class Router:
        def chat(self, role, msgs, **k):
            assert role == "fast" and "Matematik" in msgs[0]["content"]
            return SimpleNamespace(message={"content": "Akşamlar Hayat. Sınav nasıl geçti?"})

    mem = LongMemory(":memory:", {})
    mem.add_followup("Matematik sınavını sor.", time.time() - 60, "ask")
    b = {"greeting": "İyi akşamlar", "away": "3 saattir", "followups": mem.due_followups(), "events": ["15:00 Dişçi"],
         "mail": []}
    assert compose(b, mem, Router()) == "Akşamlar Hayat. Sınav nasıl geçti?"
    text = compose(b, mem, None)
    assert "İyi akşamlar" in text and "Dişçi" in text


def test_morning_summary_on_startup_once_per_day(tmp_path):
    sent = []
    st = tmp_path / "g.json"
    info = {"weather": "İstanbul: şu an 12°C, yağmurlu", "notes": ["faturayı öde"]}
    mk = lambda hour: Greeter(cfg(), lambda n: sent.append(n) or True, idle=lambda: 5.0,  # noqa: E731
                              clock=lambda: datetime(2026, 10, 22, hour), morning=lambda c, n: info, state_path=st)
    g = mk(8)
    assert g.tick()  # computer opened in the morning: the day's first summary, without measuring absence
    assert wait_for(lambda: sent)
    assert sent[0].body.startswith("Günaydın") and "yağmurlu" in sent[0].body
    assert not mk(9).tick()  # ALICE restarted: does not say it again the same day (stored in the file)
    assert not Greeter(cfg(), lambda n: True, idle=lambda: 5.0, clock=lambda: datetime(2026, 10, 23, 15),
                       morning=lambda c, n: info, state_path=st).tick()  # afternoon start: no morning summary
    b = compose({"greeting": "Günaydın", "away": "", "followups": [], "events": [], "mail": [], "morning": info})
    assert "yağmurlu" in b


def test_background_greeting_swallows_errors():
    from alice.presence import Greeter

    class Boom(Greeter):
        def __init__(self):
            pass

        def greet(self, away):
            raise RuntimeError("hafıza kapalı")

    Boom()._greet_bg(10.0)  # no uncaught error should be left in the thread
