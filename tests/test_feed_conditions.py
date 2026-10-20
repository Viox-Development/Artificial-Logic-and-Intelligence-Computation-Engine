"""Akış koşulları ve teslim zamanlaması: when/escalate, change modu, özet, sessiz saat, bekleme."""

from __future__ import annotations

from datetime import datetime

import pytest

from alice.core import feeds as F
from alice.core.conditions import Condition, check, haystack, parse_condition, tr_lower
from alice.core.notify import Notification


# ------------------------------------------------------------------------------ koşul dili
def ok(cond: dict, item, *rendered, ask=None):
    c = parse_condition(cond)
    return check(c, item, haystack(item, *rendered), ask)


def test_turkce_kucuk_harf():
    assert tr_lower("İSTANBUL IŞIK") == "istanbul ışık"
    assert ok({"any": ["istanbul"]}, {"t": "İSTANBUL'a geldim"})[0]
    assert ok({"any": ["ışık"]}, {"t": "IŞIK yandı"})[0]


def test_any_all_none():
    item = {"content": "Hayat DCS güncellemesi çıktı", "author": {"username": "bot_haber"}}
    assert ok({"any": ["hayat", "@everyone"]}, item)[0]
    assert not ok({"any": ["ahmet"]}, item)[0]
    assert ok({"all": ["dcs", "güncelleme"]}, item)[0]
    assert ok({"all": ["dcs", "yama"]}, item) == (False, "geçmiyor: yama")
    assert ok({"none": ["bot"]}, item) == (False, "istenmeyen kelime: bot")   # iç içe alanlar da aranır


def test_regex_buyuk_kucuk_duyarsiz():
    assert ok({"regex": r"\b(acil|hemen)\b"}, {"c": "ACİL bak"})[0]   # Türkçe büyük harf: 'İ' -> 'i'
    assert ok({"regex": r"\bacil\b"}, {"c": "bu acil"})[0]
    assert not ok({"regex": r"\bacil\b"}, {"c": "acilen değil, aciliyet"})[0]
    with pytest.raises(ValueError, match="regex geçersiz"):
        parse_condition({"regex": "(("})


def test_fields_esitlik_liste_ve_bos_degil():
    p = {"personaname": "Ahmet", "gameextrainfo": "DCS World"}
    assert ok({"fields": {"personaname": ["ahmet", "ayşe"]}}, p)[0]
    assert not ok({"fields": {"personaname": "mehmet"}}, p)[0]
    assert ok({"fields": {"gameextrainfo": "*"}}, p)[0]
    assert ok({"fields": {"gameextrainfo": "*"}}, {"personaname": "x"}) == (False, "gameextrainfo boş")


def test_min_max_sayisal():
    v = {"statistics": {"viewCount": "15000"}}
    assert ok({"min": {"statistics.viewCount": 1000}}, v)[0]
    assert not ok({"min": {"statistics.viewCount": 50000}}, v)[0]
    assert not ok({"max": {"statistics.viewCount": 10}}, v)[0]
    assert not ok({"min": {"yok": 1}}, v)[0]                   # alan yoksa geçmez
    with pytest.raises(ValueError, match="sayı olmalı"):
        parse_condition({"min": {"a": "çok"}})


def test_ask_en_son_sorulur_ve_hata_gecirir():
    asked = []

    def ask(q, text):
        asked.append(q)
        return "toplantı" in text

    assert ok({"any": ["x"], "ask": "acil mi?"}, {"c": "y"}, ask=ask)[0] is False
    assert asked == []                                       # ucuz koşul eledi, model hiç çağrılmadı
    assert ok({"ask": "acil mi?"}, {"c": "toplantı 5'te"}, ask=ask)[0]
    assert ok({"ask": "acil mi?"}, {"c": "naber"}, ask=ask)[0] is False

    def bozuk(q, text):
        raise RuntimeError("model yok")

    assert ok({"ask": "acil mi?"}, {"c": "naber"}, ask=bozuk) == (True, "karar motoru cevap vermedi, geçirildi")
    assert ok({"ask": "acil mi?"}, {"c": "naber"})[0]        # karar motoru hiç yoksa da geçer


def test_bilinmeyen_alan_reddedilir():
    with pytest.raises(ValueError, match="bilinmeyen alan: icerir"):
        parse_condition({"icerir": ["x"]})


def test_bos_kosul_her_seyi_gecirir():
    assert Condition().empty()
    assert check(Condition(), {"a": 1}, "")[0]


# ------------------------------------------------------------------------------ akış motoru
def write(tmp_path, monkeypatch, toml: str):
    p = tmp_path / "feeds.toml"
    p.write_text(toml, encoding="utf-8")
    monkeypatch.setattr(F, "feeds_path", lambda: p)
    monkeypatch.setattr(F, "state_path", lambda: tmp_path / "feeds-state.json")


BASE = """
[[feed]]
name = "d"
connector = "discord"
action = "messages"
every_minutes = 1
title = "{author.username}"
body = "{content}"
"""


def msg(i, who="ahmet", text="selam"):
    return {"id": str(i), "author": {"username": who}, "content": text}


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += minutes * 60


def run(m, batches, clock, step=1.0):
    """Her yoklamada sıradaki listeyi döndür, aralarında zamanı ilerlet."""
    for _ in batches:
        m.collect()
        clock.advance(step)


def monitor(batches, clock, **kw):
    seq = list(batches)
    return F.FeedMonitor(fetch=lambda f: seq.pop(0) if seq else (seq and seq[-1]) or [], clock=clock, **kw)


def at(hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, 20, hour, minute).timestamp()


def test_when_kosulu_bildirimleri_suzer(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + '[feed.when]\nany = ["hayat"]\nnone = ["bot"]\n')
    sent: list[Notification] = []
    clock = Clock(at(12))
    m = monitor([[msg(0)], [msg(0), msg(1, text="hayat bakar mısın"), msg(2, text="naber"),
                            msg(3, "bot", "hayat haberleri")]], clock, notify=sent.append)
    run(m, range(2), clock)
    assert [n.body for n in sent] == ["hayat bakar mısın"]


def test_escalate_kritik_yapar_ve_sessiz_saati_deler(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + 'quiet = "23:00-08:00"\n[feed.escalate]\nregex = "\\\\bacil\\\\b"\n')
    sent: list[Notification] = []
    clock = Clock(at(2))   # gece 2: sessiz saat
    m = monitor([[msg(0)], [msg(0), msg(1, text="acil ara"), msg(2, text="günaydın")]], clock, notify=sent.append)
    run(m, range(2), clock)
    assert [(n.body, n.severity) for n in sent] == [("acil ara", "critical")]   # acil olan beklemedi
    assert m.state["d"]["held"][0]["body"] == "günaydın"                      # diğeri sabaha kaldı


def test_sessiz_saat_bitince_ozet_gelir(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + 'quiet = "23:00-08:00"\n')
    sent: list[Notification] = []
    clock = Clock(at(7, 50))
    m = monitor([[msg(0)], [msg(0), msg(1, text="a"), msg(2, text="b")]], clock, notify=sent.append)
    run(m, range(2), clock)
    assert sent == []
    clock.t = at(8, 1)
    m.collect()                                  # yeni öğe yok ama özetin vakti geldi
    assert len(sent) == 1 and sent[0].title == "d: 2 yeni"
    assert "ahmet — a" in sent[0].body and "ahmet — b" in sent[0].body
    assert m.state["d"]["held"] == []


def test_gece_yarisini_gecen_sessiz_aralik():
    f = F.Feed(name="x", connector="c", action="a", quiet="23:00-08:00")
    assert F.in_quiet(f, at(23, 30)) and F.in_quiet(f, at(3)) and not F.in_quiet(f, at(8)) and not F.in_quiet(f, at(15))
    g = F.Feed(name="x", connector="c", action="a", quiet="13:00-14:00")
    assert F.in_quiet(g, at(13, 15)) and not F.in_quiet(g, at(14))


def test_bozuk_sessiz_saat_yuklemede_soylenir(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + 'quiet = "gece"\n')
    feeds, errors = F.load_feeds()
    assert feeds == [] and "quiet" in errors[0]


def test_digest_tek_tek_degil_ozet_halinde(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + "digest_minutes = 30\n")
    sent: list[Notification] = []
    clock = Clock(at(12))
    m = monitor([[msg(0)], [msg(0), msg(1)], [msg(0), msg(1), msg(2)]], clock, notify=sent.append)
    run(m, range(3), clock, step=10)
    assert sent == []                                    # 20 dk geçti, özet daha gelmedi
    clock.t = at(12, 45)
    m.collect()
    assert len(sent) == 1 and sent[0].title == "d: 2 yeni"


def test_bekleme_suresinde_gelenler_toplanir(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + "cooldown_minutes = 10\n")
    sent: list[Notification] = []
    clock = Clock(at(12))
    m = monitor([[msg(0)], [msg(0), msg(1, text="ilk")], [msg(0), msg(1), msg(2, text="ikinci")],
                 [msg(0), msg(1), msg(2), msg(3, text="üçüncü")]], clock, notify=sent.append)
    run(m, range(4), clock, step=2)
    assert [n.body for n in sent] == ["ilk"]            # sonrakiler bekleme süresinde
    clock.advance(10)
    m.collect()
    assert sent[-1].title == "d: 2 yeni" and "ikinci" in sent[-1].body and "üçüncü" in sent[-1].body


def test_change_modu_oyuna_girince_haber_verir(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, """
[[feed]]
name = "arkadaslar"
connector = "steam"
action = "profile"
mode = "change"
watch = ["gameextrainfo"]
id_field = "steamid"
every_minutes = 1
title = "{personaname} oyuna girdi"
body = "{gameextrainfo} (önce: {prev.gameextrainfo})"
[feed.when]
fields = { gameextrainfo = "*" }
""")
    sent: list[Notification] = []
    clock = Clock(at(20))
    p = lambda game: [{"steamid": "1", "personaname": "Ahmet", "gameextrainfo": game},  # noqa: E731
                      {"steamid": "2", "personaname": "Ayşe", "gameextrainfo": ""}]
    m = monitor([p(""), p(""), p("DCS World"), p("DCS World"), p("")], clock, notify=sent.append)
    run(m, range(5), clock)
    assert [n.title for n in sent] == ["Ahmet oyuna girdi"]     # çıkış (boşa dönme) koşul yüzünden bildirilmedi
    assert sent[0].body.startswith("DCS World")


def test_change_modu_watch_ister(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + 'mode = "change"\n')
    feeds, errors = F.load_feeds()
    assert feeds == [] and "watch" in errors[0]


def test_change_modunda_changed_alani(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE.replace('body = "{content}"', 'body = "{changed}"')
          + 'mode = "change"\nwatch = ["content", "author.username"]\n')
    sent: list[Notification] = []
    clock = Clock(at(12))
    m = monitor([[msg(1, text="a")], [msg(1, text="b")]], clock, notify=sent.append)
    run(m, range(2), clock)
    assert sent[0].body == "content"


def test_ask_karar_motoruna_gider(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + '[feed.when]\nask = "Bu mesaj bana bir şey soruyor mu?"\n')

    class Answer:
        def __init__(self, v):
            self.value = v

    class FakeDecider:
        def __init__(self):
            self.states = []

        def decide(self, state, questions):
            self.states.append((state, questions["notify"].instructions))
            return {"notify": Answer(1.0 if "?" in state else 0.0)}

    dec = FakeDecider()
    sent: list[Notification] = []
    clock = Clock(at(12))
    m = monitor([[msg(0)], [msg(0), msg(1, text="yarın gelir misin?"), msg(2, text="tamam")]], clock,
                notify=sent.append, decider=dec)
    run(m, range(2), clock)
    assert [n.body for n in sent] == ["yarın gelir misin?"]
    assert dec.states[0][1] == "Bu mesaj bana bir şey soruyor mu?" and "discord.messages" in dec.states[0][0]


def test_explain_neden_gecmedigini_soyler(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + '[feed.when]\nany = ["hayat"]\n[feed.escalate]\nany = ["acil"]\n')
    m = F.FeedMonitor(fetch=lambda f: [msg(1, text="hayat acil"), msg(2, text="hayat"), msg(3, text="naber")])
    v = m.explain(m.feeds[0])
    assert [(x.ok, x.severity) for x in v] == [(True, "critical"), (True, "info"), (False, "info")]
    assert v[2].reason.startswith("hiçbiri geçmiyor")
    assert m.state == {} or "d" not in m.state or "seen" not in m.state["d"]   # test komutu durumu değiştirmez


def test_metrik_kosulu_gecen_sayiyi_yazar(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + 'metric = "feed.d.onemli"\n[feed.when]\nany = ["hayat"]\n')
    clock = Clock(at(12))
    m = monitor([[msg(0)], [msg(0), msg(1, text="hayat"), msg(2, text="x")]], clock)
    m.collect()
    clock.advance(1)
    assert ("feed.d.onemli", 1.0, None) in m.collect()


def test_describe_okunur_ozet(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, BASE + 'quiet = "23:00-08:00"\ndigest_minutes = 60\n[feed.when]\nany = ["hayat"]\n')
    d = F.load_feeds()[0][0].describe()
    assert "discord.messages" in d and "biri: hayat" in d and "sessiz 23:00-08:00" in d and "60 dk'da bir özet" in d


def test_ornek_sablon_gecerli_ve_regex_kelime_siniri():
    """alice feeds --new şablonu yüklenebilmeli; regex TOML kaçışıyla bozulmamalı (\\b -> geri silme olmasın)."""
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib
    from alice.core.cli import FEED_TEMPLATE

    feed = F.parse_feed(tomllib.loads(FEED_TEMPLATE)["feed"][0])
    assert feed.escalate.regex == r"\b(acil|hemen)\b"
    assert check(feed.escalate, {"c": "ACİL"}, haystack({"c": "ACİL"}))[0]
    assert not check(feed.escalate, {"c": "aciliyet"}, haystack({"c": "aciliyet"}))[0]
