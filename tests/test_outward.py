"""Outward actions (SEND): a message sent to someone else is asked about even in auto mode, rejected in plan mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from alice.apps import find
from alice.connect.manifest import discover
from alice.connect.tool import ConnectTool
from alice.permissions import Permissions
from alice.tools.apps import AppTool
from alice.tools.base import SEND, Tool, ToolContext
from tests.conftest import make_config


class Sender(Tool):
    name = "sender"
    risk = SEND


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


@pytest.mark.parametrize("mode,expected", [("plan", "deny"), ("ask", "ask"), ("auto", "ask"), ("yolo", "allow")])
def test_send_her_modda_dogru_karar(ctx, mode, expected):
    assert Permissions(mode).decide(Sender(), {}, ctx) == expected


def test_oturumda_hep_izin_ver_send_icin_de_calisir(ctx):
    p = Permissions("auto")
    p.always_allow.add("sender")
    assert p.decide(Sender(), {}, ctx) == "allow"


def test_confirm_outward_kapatilabilir(workdir):
    cfg = make_config({"brain": ["a:m"]}, confirm_outward=False)
    c = ToolContext(cwd=workdir, config=cfg)
    assert Permissions("auto").decide(Sender(), {}, c) == "allow"
    assert Permissions("plan").decide(Sender(), {}, c) == "deny"  # plan mode still does not touch it


def test_discord_mesaj_gondermek_send_spotify_calmak_degil(ctx):
    ms, _ = discover()
    discord, spotify = ConnectTool(ms["discord"]), ConnectTool(ms["spotify"])
    assert discord.risk_for({"action": "send"}) == SEND
    assert discord.risk_for({"action": "messages"}) == "network"
    assert spotify.risk_for({"action": "play"}) == "write"   # controlling my own player: nothing goes to someone else
    assert Permissions("auto").decide(discord, {"action": "send"}, ctx) == "ask"
    assert Permissions("auto").decide(spotify, {"action": "play"}, ctx) == "allow"


def test_whatsapp_gonder_send_okumak_degil(ctx):
    t = AppTool()
    assert t.risk_for({"action": "flow", "name": "whatsapp", "flow": "send"}) == SEND
    assert t.risk_for({"action": "flow", "name": "wp", "flow": "send"}) == SEND   # alias too
    assert t.risk_for({"action": "flow", "name": "whatsapp", "flow": "read"}) == "computer"
    assert t.risk_for({"action": "flow", "name": "gmail", "flow": "compose"}) == SEND
    assert t.risk_for({"action": "flow", "name": "instagram", "flow": "dm"}) == SEND
    assert Permissions("auto").decide(t, {"action": "flow", "name": "whatsapp", "flow": "send"}, ctx) == "ask"


def test_gozetimsiz_ajan_mesaj_gonderemez(ctx):
    """Background task (user not present): cannot ask questions, so SEND is rejected."""
    from alice.agent import NullUI

    assert NullUI(approve="n").ask_permission("discord", "mesaj", SEND) == "n"


def test_yerlesik_tariflerde_sends_tanimli_akislara_isaret_eder():
    for name in ("whatsapp", "instagram", "gmail", "discord_web"):
        app = find(name)
        assert app.sends and all(f in app.flows for f in app.sends), name


def test_kullanici_tarifi_sends_ekleyebilir(monkeypatch, tmp_path):
    from alice import apps

    f = tmp_path / "apps.toml"
    f.write_text('[benim]\nkind = "web"\nurl = "https://a.example"\nsends = ["post"]\n'
                 '[benim.flows]\npost = "{metin} yaz ve paylaş"\n', encoding="utf-8")
    monkeypatch.setattr(apps, "user_file", lambda: f)
    assert AppTool().risk_for({"action": "flow", "name": "benim", "flow": "post"}) == SEND
    assert Path(f).exists()
