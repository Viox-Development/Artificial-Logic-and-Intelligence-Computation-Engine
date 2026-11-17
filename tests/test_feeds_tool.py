"""Creating feeds from chat: the feeds tool and editing feeds.toml without touching its comments."""

from __future__ import annotations

import json
import sys

import pytest

from alice.core import feedfile
from alice.core import feeds as F
from alice.tools.base import ToolContext, ToolError
from alice.tools.core import Feeds
from tests.conftest import make_config

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@pytest.fixture
def ffile(tmp_path, monkeypatch):
    p = tmp_path / "feeds.toml"
    monkeypatch.setattr(F, "feeds_path", lambda: p)
    monkeypatch.setattr(feedfile, "feeds_path", lambda: p)
    monkeypatch.setattr(F, "state_path", lambda: tmp_path / "st.json")
    return p


@pytest.fixture
def ctx(workdir, monkeypatch):
    monkeypatch.setenv("STEAM_API_KEY", "k")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


USER_FILE = """# Benim akışlarım — bu yorum kalmalı

[[feed]]
name = "elle"
connector = "discord"
action = "messages"
args = { channel_id = "1" }
# kanal 1'i izliyorum
[feed.when]
any = ["hayat"]
"""


# ------------------------------------------------------------------------------ file
def test_ekle_yorumlari_korur_ve_gecerli_toml_uretir(ffile):
    ffile.write_text(USER_FILE, encoding="utf-8")
    feedfile.add_feed({"name": "yeni", "connector": "steam", "action": "profile",
                       "args": {"steamids": "765"}, "mode": "change", "watch": ["gameextrainfo"],
                       "id_field": "steamid", "title": "{personaname} oyuna girdi",
                       "when": {"fields": {"gameextrainfo": "*"}}, "escalate": {"regex": r"\b(dcs)\b"}})
    text = ffile.read_text(encoding="utf-8")
    assert "# Benim akışlarım — bu yorum kalmalı" in text and "# kanal 1'i izliyorum" in text
    feeds, errors = F.load_feeds()
    assert not errors and [f.name for f in feeds] == ["elle", "yeni"]
    assert feeds[1].when.fields == {"gameextrainfo": ["*"]} and feeds[1].escalate.regex == r"\b(dcs)\b"


def test_ayni_ad_ve_gecersiz_akis_reddedilir(ffile):
    ffile.write_text(USER_FILE, encoding="utf-8")
    with pytest.raises(ValueError, match="zaten var"):
        feedfile.add_feed({"name": "elle", "connector": "discord", "action": "messages"})
    with pytest.raises(ValueError, match="watch"):
        feedfile.add_feed({"name": "x", "connector": "steam", "action": "profile", "mode": "change"})
    with pytest.raises(ValueError, match="bilinmeyen alan"):
        feedfile.add_feed({"name": "y", "connector": "d", "action": "m", "when": {"icerir": ["a"]}})
    assert ffile.read_text(encoding="utf-8") == USER_FILE      # the broken add did not touch the file at all


def test_sil_ve_ac_kapa_sadece_ilgili_blogu_degistirir(ffile):
    ffile.write_text(USER_FILE, encoding="utf-8")
    feedfile.add_feed({"name": "ikinci", "connector": "discord", "action": "messages", "args": {"channel_id": "2"}})
    assert feedfile.set_enabled("elle", False)
    feeds = {f.name: f for f in F.load_feeds()[0]}
    assert feeds["elle"].enabled is False and feeds["ikinci"].enabled is True
    assert feeds["elle"].when.any == ["hayat"]                 # sub-table was not broken
    assert feedfile.set_enabled("elle", True)
    assert {f.name: f.enabled for f in F.load_feeds()[0]}["elle"] is True
    assert feedfile.remove_feed("ikinci") and not feedfile.remove_feed("ikinci")
    assert [f.name for f in F.load_feeds()[0]] == ["elle"]
    assert "# Benim akışlarım" in ffile.read_text(encoding="utf-8")


def test_dosya_yokken_ekleme_olusturur(ffile):
    feedfile.add_feed({"name": "ilk", "connector": "discord", "action": "messages"})
    assert [f.name for f in F.load_feeds()[0]] == ["ilk"]


def test_toml_degerleri():
    assert feedfile.toml_value(r"\bacil\b") == r"'\bacil\b'"       # regex: literal string, no escaping
    assert feedfile.toml_value('tırnak "var"') == '"tırnak \\"var\\""'
    assert feedfile.toml_value({"author.username": ["a"]}) == '{ "author.username" = ["a"] }'
    assert feedfile.toml_value(5.0) == "5" and feedfile.toml_value(0.5) == "0.5" and feedfile.toml_value(True) == "true"
    assert tomllib.loads("x = " + feedfile.toml_value({"a": [1, "b"], "c": {"d": False}}))["x"] == \
        {"a": [1, "b"], "c": {"d": False}}


# ------------------------------------------------------------------------------ tool
def test_arac_akis_ekler_listeler_siler(ffile, ctx):
    t = Feeds()
    out = t.run(ctx, action="add", name="ahmet_oyun", connector="steam", connector_action="profile",
                args='{"steamids": "765"}', mode="change", watch=["gameextrainfo"], id_field="steamid",
                title="{personaname} oyuna girdi", body="{gameextrainfo}",
                when=json.dumps({"fields": {"gameextrainfo": "*"}}), quiet="23:00-08:00",
                description="Ahmet Steam'de oyuna girince haber ver").text
    assert "akış eklendi: ahmet_oyun" in out and "ilk tur sessizdir" in out
    listed = t.run(ctx, action="list").text
    assert "ahmet_oyun" in listed and "değişince: gameextrainfo" in listed and "sessiz 23:00-08:00" in listed
    assert t.run(ctx, action="disable", name="ahmet_oyun").text == "kapatıldı"
    assert "(kapalı)" in t.run(ctx, action="list").text
    assert t.run(ctx, action="remove", name="ahmet_oyun").text == "silindi"
    assert t.run(ctx, action="list").text == "akış yok"


def test_arac_yanlis_baglanti_ve_eksik_parametreyi_soyler(ffile, ctx):
    t = Feeds()
    with pytest.raises(ToolError, match="bağlantı yok: slack"):
        t.run(ctx, action="add", name="x", connector="slack", connector_action="m")
    with pytest.raises(ToolError, match="Olanlar: .*messages"):
        t.run(ctx, action="add", name="x", connector="discord", connector_action="mesajlar")
    with pytest.raises(ToolError, match="eksik: channel_id"):
        t.run(ctx, action="add", name="x", connector="discord", connector_action="messages")
    with pytest.raises(ToolError, match="gönderiyor"):
        t.run(ctx, action="add", name="x", connector="discord", connector_action="send", args='{"channel_id": "1"}')
    with pytest.raises(ToolError, match="geçerli JSON değil"):
        t.run(ctx, action="add", name="x", connector="discord", connector_action="messages", args="{bozuk")
    assert not ffile.exists()


def test_connectors_sadece_okuyan_eylemleri_alanlariyla_gosterir(ctx):
    out = Feeds().run(ctx, action="connectors").text
    assert "steam (hazır)" in out and "profile(steamids*)" in out and "gameextrainfo" in out
    assert "send(" not in out          # a sending action cannot be given to a feed


def test_test_action_ornek_ogeleri_gosterir(ctx, monkeypatch):
    monkeypatch.setattr("alice.connect.runner.call",
                        lambda m, act, args, **k: {"response": {"players": [{"personaname": "Ahmet", "steamid": "1"}]}})
    out = Feeds().run(ctx, action="test_action", connector="steam", connector_action="profile",
                      args={"steamids": "1"}).text
    assert out.startswith("1 öğe") and '"personaname": "Ahmet"' in out


def test_risk_ajanli_akis_onay_ister():
    t = Feeds()
    assert t.risk_for({"action": "list"}) == "read"
    assert t.risk_for({"action": "add", "name": "x"}) == "write"
    assert t.risk_for({"action": "add", "agent": "özetle"}) == "danger"   # an unattended agent wakes up
    assert t.risk_for({"action": "test_action"}) == "network"


def test_akis_araci_otomasyon_grubunda_ve_cumleyle_acilir():
    from alice.tools.groups import group_of, groups_for_text

    assert group_of("feeds") == "automation"
    assert "automation" in groups_for_text("Ahmet Steam'de oyuna girince haber ver")
