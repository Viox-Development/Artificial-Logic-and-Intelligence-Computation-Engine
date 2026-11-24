"""BUDDY: a small companion unique to each user that changes on its own."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from alice import buddy
from tests.conftest import make_config


def t(h, d=13):
    return datetime(2026, 11, d, h, 0).timestamp()


def test_roll_is_deterministic_per_user_and_varies():
    a, b = buddy.roll("ayşe@pc"), buddy.roll("ayşe@pc")
    assert a == b
    rolls = [buddy.roll(f"kişi{i}@pc") for i in range(400)]
    assert len({r.species for r in rolls}) > 10
    counts = {r: sum(x.rarity == r for x in rolls) for r, *_ in buddy.RARITIES}
    assert counts["yaygın"] > counts["nadir"] > 0
    for r in rolls:
        peak, dump = max(r.stats.values()), min(r.stats.values())
        assert 1 <= dump < peak <= 100
        assert (r.hat == "yok") == (r.rarity == "yaygın")
        assert all("{e}" not in line for line in r.art())


def test_bones_are_not_read_from_file(tmp_path):
    p = tmp_path / "b.json"
    buddy.view(path=p, now=t(12))
    data = buddy._load(p)
    data["rarity"], data["species"] = "efsanevi", "ejder"
    buddy._save(data, p)
    v = buddy.view(path=p, now=t(12))
    real = buddy.roll()
    assert v["rarity"] == real.rarity and v["species"] == buddy.label(buddy.SPECIES_EN, real.species)


def test_decay_care_and_night_sleep(tmp_path):
    p = tmp_path / "b.json"
    v = buddy.view(path=p, now=t(12))
    assert v["name"] and v["food"] == 80
    v = buddy.view(path=p, now=t(22))  # 10 hours without care
    assert v["food"] == 40 and v["happy"] < 80 and v["energy"] == 50
    assert "ate happily" in buddy.act("feed", path=p, now=t(22))
    assert "is full" in buddy.act("feed", path=p, now=t(22) + 60)  # half an hour has not passed
    before = buddy.view(path=p, now=t(22))["happy"]
    buddy.act("pet", path=p, now=t(22))
    assert buddy.view(path=p, now=t(22))["happy"] == min(100, before + 15)
    night = buddy.view(path=p, now=t(3, 14))
    assert night["mood"] == "asleep" or night["energy"] > 50
    assert buddy.act("rename", "Fıstık", path=p) == "Its name is now Fıstık."


def test_on_turn_feeds_and_sometimes_comments(tmp_path, monkeypatch):
    p = tmp_path / "b.json"
    c = make_config({"brain": ["a:m"]})
    c.data["buddy"] = {"chatter": True}

    class Router:
        def __init__(self):
            self.n = 0

        def chat(self, role, messages, **kw):
            self.n += 1
            return SimpleNamespace(message={"content": "  Yine kod mu?  "})

    r = Router()
    buddy.view(path=p, now=t(12))
    assert buddy.on_turn(c, r, "selam", "merhaba", path=p, now=t(12), chance=1.0) == "Yine kod mu?"
    v = buddy.view(path=p, now=t(12))
    assert v["xp"] == 1 and v["say"] == "Yine kod mu?"
    # no new comment until 8 minutes pass, but it still gets fed
    assert buddy.on_turn(c, r, "a", "b", path=p, now=t(12) + 60, chance=1.0) == ""
    assert buddy.view(path=p, now=t(12) + 60)["xp"] == 2 and r.n == 1
    c.data["buddy"] = {"enabled": False}
    assert buddy.on_turn(c, r, "a", "b", path=p, now=t(13), chance=1.0) == ""
    assert buddy.view(path=p, now=t(13))["xp"] == 2


def test_tool_status_card():
    from alice.tools.base import ToolContext
    from alice.tools.companions import Buddy

    out = Buddy().run(ToolContext(cwd=".", config=make_config({"brain": ["a:m"]})), "status").text
    assert "Level" in out and buddy.label(buddy.SPECIES_EN, buddy.roll().species) in out
