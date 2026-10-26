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
    now = datetime(2026, 10, 22, 17, 30)  # takip saati de sabit saatten: test günün tarihine bağlı kalmasın
    fid = mem.add_followup("Matematik sınavının nasıl geçtiğini sor.", now.timestamp() - 3600, "ask")
    sent = []
    idle = iter([6000.0, 6100.0, 3.0, 2.0])
    voice = SimpleNamespace(speaker=SimpleNamespace(wait=lambda t: True), follow_up=5.0, armed_until=0.0)
    g = Greeter(cfg(), lambda n: sent.append(n) or True, memory=lambda: mem, idle=lambda: next(idle),
                clock=lambda: now)
    g.voice = voice
    assert not g.tick() and not g.tick()  # uzakta
    assert g.tick()  # döndü
    assert wait_for(lambda: sent)
    text = sent[0].body
    assert text.startswith("İyi günler") and "Matematik" in text and sent[0].topic == "presence"
    assert wait_for(lambda: voice.armed_until > time.monotonic())  # "Hey Alice" demeden cevap
    assert mem.due_followups() == [] and [f.id for f in mem.followups(include_done=True)] == [fid]
    assert mem.recent(1)[0].channel == "karşılama"  # Live az önceki konuşmada görür
    assert not g.tick()  # tekrar karşılamaz


def test_silent_without_news_after_first_greeting_and_respects_gap_and_quiet():
    sent = []
    g = Greeter(cfg(), lambda n: sent.append(n) or True, idle=lambda: 0.0, clock=lambda: datetime(2026, 10, 22, 10))
    assert g.greet(7200) .startswith("Günaydın")  # günün ilk dönüşü: kısa selam
    assert g.greet(7200) == ""  # aynı gün, söyleyecek bir şey yok: sus
    assert len(sent) == 1
    g2 = Greeter(cfg(quiet_hours="00:00-23:59"), lambda n: True, idle=lambda: 0.0,
                 clock=lambda: datetime(2026, 10, 22, 12))  # 23:59-00:00 arası koşunca sessiz saat dışında kalıyordu
    g2.away_since = time.time() - 9000
    assert not g2.tick()  # sessiz saatler
    g3 = Greeter(cfg(), lambda n: True, idle=lambda: 0.0)
    g3.away_since, g3.last_greet = time.time() - 9000, time.time() - 60
    assert not g3.tick()  # az önce karşıladı


def test_muted_mode_does_not_count():
    g = Greeter(cfg(), lambda n: False, idle=lambda: 0.0, clock=lambda: datetime(2026, 10, 22, 10))
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
