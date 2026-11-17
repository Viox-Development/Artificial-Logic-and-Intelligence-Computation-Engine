"""Feeds: poll a connected service and notify about new items. No network access; the connector call is faked."""

from __future__ import annotations

import time

import pytest

from alice.core import feeds as F
from alice.core.notify import Notification

TOML = """
[[feed]]
name = "discord_genel"
connector = "discord"
action = "messages"
args = { channel_id = "42" }
every_minutes = 5
id_field = "id"
title = "Discord · {author.username}"
body = "{content}"
topic = "discord"
metric = "feed.discord.yeni"

[[feed]]
name = "kapali"
connector = "steam"
action = "recent"
enabled = false
"""


@pytest.fixture
def feeds_file(tmp_path, monkeypatch):
    p = tmp_path / "feeds.toml"
    p.write_text(TOML, encoding="utf-8")
    monkeypatch.setattr(F, "feeds_path", lambda: p)
    monkeypatch.setattr(F, "state_path", lambda: tmp_path / "feeds-state.json")
    return p


def msg(i: str, who: str = "ahmet", text: str = "selam"):
    return {"id": i, "author": {"username": who}, "content": text}


def monitor(items, **kw):
    """Monitor with a fake connector: items is either a list or a list of lists that yields the next one on each call."""
    seq = list(items)

    def fetch(_feed):
        return seq.pop(0) if seq else []

    return F.FeedMonitor(fetch=fetch, **kw)


def test_tanim_okunur_ve_bozuk_olan_atlanir(tmp_path, monkeypatch):
    p = tmp_path / "feeds.toml"
    p.write_text(TOML + '\n[[feed]]\nname = "eksik"\n', encoding="utf-8")
    monkeypatch.setattr(F, "feeds_path", lambda: p)
    fs, errors = F.load_feeds()
    assert [f.name for f in fs] == ["discord_genel", "kapali"]
    assert errors and "zorunlu" in errors[0]
    assert fs[0].interval == 300 and fs[1].enabled is False


def test_doldurulmamis_sablon_akisi_calismaz(tmp_path, monkeypatch):
    # The "ornek" feed kept logging "DISCORD_BOT_TOKEN yok" (missing) every 5 minutes: KANAL_ID had never been filled in.
    p = tmp_path / "feeds.toml"
    p.write_text('[[feed]]\nname = "ornek"\nconnector = "discord"\naction = "messages"\n'
                 'args = { channel_id = "KANAL_ID" }\n[[feed]]\nname = "bolge"\nconnector = "x"\naction = "y"\n'
                 'args = { region = "EU" }\n', encoding="utf-8")
    monkeypatch.setattr(F, "feeds_path", lambda: p)
    fs, errors = F.load_feeds()
    assert [(f.name, f.enabled) for f in fs] == [("ornek", False), ("bolge", True)]  # visible but not polled
    assert any("doldurulmamış" in e for e in errors)


def test_dosya_yoksa_sorun_degil(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "feeds_path", lambda: tmp_path / "yok.toml")
    assert F.load_feeds() == ([], [])


def test_ilk_tur_bildirim_yapmaz(feeds_file):
    sent: list[Notification] = []
    m = monitor([[msg("1"), msg("2")]], notify=sent.append)
    rows = m.collect()
    assert sent == []                                  # silent on the first pass: old messages are not notified
    # Metric is 0: first-pass items aren't "new" (else a `yeni > 0` rule fires as soon as the feed is set up)
    assert ("feed.discord.yeni", 0.0, None) in rows


def test_yeni_oge_bildirilir_eskisi_tekrar_etmez(feeds_file):
    sent: list[Notification] = []
    m = monitor([[msg("1")], [msg("1"), msg("2", "ayse", "geliyorum")], [msg("1"), msg("2")]], notify=sent.append)
    m.collect()                       # first pass: silent
    m.state["discord_genel"]["last"] = 0   # let the interval elapse
    m.collect()
    assert len(sent) == 1
    assert sent[0].title == "Discord · ayse" and sent[0].body == "geliyorum" and sent[0].topic == "discord"
    m.state["discord_genel"]["last"] = 0
    m.collect()
    assert len(sent) == 1             # the same message is not notified twice


def test_suresi_gelmeyen_akis_yoklanmaz(feeds_file):
    calls = []

    def fetch(feed):
        calls.append(feed.name)
        return [msg("1")]

    m = F.FeedMonitor(fetch=fetch)
    m.collect()
    m.collect()  # 5 minutes have not passed
    assert calls == ["discord_genel"]


def test_kapali_akis_yoklanmaz(feeds_file):
    calls = []
    m = F.FeedMonitor(fetch=lambda f: calls.append(f.name) or [])
    m.collect()
    assert "kapali" not in calls


def test_cok_yeni_oge_varsa_bildirim_toplanir(feeds_file):
    sent: list[Notification] = []
    çok = [msg(str(i), "k", f"mesaj {i}") for i in range(12)]
    m = monitor([[msg("x")], çok], notify=sent.append)
    m.collect()
    m.state["discord_genel"]["last"] = 0
    m.collect()
    assert len(sent) == F.MAX_PER_TICK + 1            # last line: "N yeni öğe daha" (N more new items)
    assert "yeni öğe daha" in sent[-1].title


def test_hata_akisi_oldurmez_ve_kaydedilir(feeds_file):
    notes: list[str] = []

    def fetch(_f):
        raise RuntimeError("401 yetki")

    m = F.FeedMonitor(fetch=fetch, emit=notes.append)
    assert m.collect() == []
    assert "401" in m.state["discord_genel"]["error"]
    assert notes and "401" in notes[0]
    m.state["discord_genel"]["last"] = 0
    m.collect()
    assert len(notes) == 1  # the same error is not rewritten on every pass


def test_ajan_yeni_ogede_uyanir(feeds_file, tmp_path, monkeypatch):
    p = tmp_path / "feeds.toml"
    p.write_text(TOML.replace('metric = "feed.discord.yeni"', 'agent = "Bunlari ozetle"'), encoding="utf-8")
    prompts: list[str] = []
    m = monitor([[msg("1")], [msg("1"), msg("2", "ayse", "toplantı var")]], run_agent=prompts.append)
    m.collect()
    m.state["discord_genel"]["last"] = 0
    m.collect()
    assert prompts and "Bunlari ozetle" in prompts[0] and "toplantı var" in prompts[0]


def test_ajan_patlarsa_akis_surer(feeds_file, tmp_path, monkeypatch):
    p = tmp_path / "feeds.toml"
    p.write_text(TOML.replace('metric = "feed.discord.yeni"', 'agent = "bak"'), encoding="utf-8")
    notes: list[str] = []

    def patla(_p):
        raise RuntimeError("model yok")

    m = monitor([[msg("1")], [msg("1"), msg("2")]], run_agent=patla, emit=notes.append)
    m.collect()
    m.state["discord_genel"]["last"] = 0
    m.collect()
    assert any("ajan çalıştırılamadı" in n for n in notes)


def test_durum_diske_yazilir_ve_okunur(feeds_file):
    sent: list[Notification] = []
    m = monitor([[msg("1")]], notify=sent.append)
    m.collect()
    yeni = monitor([[msg("1"), msg("2")]], notify=sent.append)   # like a new process: reads the state from disk
    assert "discord_genel" in yeni.state
    yeni.state["discord_genel"]["last"] = 0
    yeni.collect()
    assert len(sent) == 1 and sent[0].title.endswith("ahmet")   # message 1 was not notified again


def test_hatirlanan_kimlik_sayisi_sinirli(feeds_file):
    m = monitor([[msg(str(i)) for i in range(F.MAX_SEEN + 50)]])
    m.collect()
    assert len(m.state["discord_genel"]["seen"]) == F.MAX_SEEN


def test_sablon_ic_ice_alan_ve_name(feeds_file):
    feed = F.load_feeds()[0][0]
    assert F.fill("{author.username}: {content}", msg("1", "ali", "naber"), feed) == "ali: naber"
    assert F.fill("{name} · {yok}", {}, feed).strip() == "discord_genel ·"


def test_kimliksiz_oge_icerikten_ayirt_edilir(feeds_file):
    sent: list[Notification] = []
    m = monitor([[{"content": "a"}], [{"content": "a"}, {"content": "b"}]], notify=sent.append)
    m.collect()
    m.state["discord_genel"]["last"] = 0
    m.collect()
    assert len(sent) == 1 and sent[0].body == "b"


def test_cekirdek_akislari_gozlemci_olarak_ekler(feeds_file, monkeypatch, tmp_path):
    from alice.core.service import Core
    from alice.core.store import Store
    from tests.conftest import make_config

    core = Core(make_config({"brain": ["a:m"]}), store=Store(tmp_path / "s.db"))
    assert any(getattr(mon, "name", "") == "feeds" for mon in core.monitors)
    core.store.close()


def test_akis_yoksa_gozlemci_bos_durur_sonradan_eklenen_devreye_girer(tmp_path, monkeypatch):
    """A feed added from chat must run without restarting the core: the monitor is attached even while empty."""
    from alice.core.service import Core
    from alice.core.store import Store
    from tests.conftest import make_config

    p = tmp_path / "sonra.toml"
    monkeypatch.setattr(F, "feeds_path", lambda: p)
    monkeypatch.setattr(F, "state_path", lambda: tmp_path / "st.json")
    core = Core(make_config({"brain": ["a:m"]}), store=Store(tmp_path / "s.db"))
    mon = next(m for m in core.monitors if getattr(m, "name", "") == "feeds")
    assert mon.feeds == [] and mon.collect() == []     # does nothing while empty
    p.write_text(TOML, encoding="utf-8")
    calls = []
    mon._fetch = lambda f: calls.append(f.name) or []
    mon.collect()
    assert calls == ["discord_genel"]                  # reloaded itself when the file changed
    core.store.close()


def test_interval_en_az_bir_dakika():
    assert F.Feed(name="a", connector="b", action="c", every_minutes=0.1).interval == 60.0
    assert F.Feed(name="a", connector="b", action="c", every_minutes=30).interval == 1800.0
    assert time.time() > 0  # feed time follows the real clock
