"""Discord kanal izinleri: ses kanallarında ve talep kanallarında görsel atılabilsin; var olan ayar korunsun."""

from __future__ import annotations

import json

import httpx
import pytest

from alice.tools.base import NETWORK, WRITE, ToolContext, ToolError
from alice.tools.discord_admin import PERMS, Discord, DiscordAdmin, merged, select_channels
from tests.conftest import make_config

G = "100"
MEDIA = PERMS["attach_files"] | PERMS["embed_links"]
CHANNELS = [
    {"id": "1", "type": 4, "name": "Sesli Sohbet", "position": 0},
    {"id": "2", "type": 2, "name": "Genel Ses", "parent_id": "1", "position": 1,
     "permission_overwrites": [{"id": G, "type": 0, "allow": str(PERMS["view_channel"]),
                                "deny": str(PERMS["attach_files"])}]},
    {"id": "3", "type": 2, "name": "Oyun", "parent_id": "1", "position": 2,
     "permission_overwrites": [{"id": G, "type": 0, "allow": str(MEDIA), "deny": "0"}]},
    {"id": "4", "type": 13, "name": "Sahne", "position": 3},
    {"id": "5", "type": 4, "name": "Talepler", "position": 4},
    {"id": "6", "type": 0, "name": "ticket-0001", "parent_id": "5", "position": 5},
    {"id": "7", "type": 0, "name": "genel", "position": 6},
]


def fake(puts, status=None, guilds=None):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bot tok"
        p = req.url.path.removeprefix("/api/v10")
        if status:
            return httpx.Response(status, json={"message": "no"})
        if p == "/users/@me/guilds":
            return httpx.Response(200, json=guilds if guilds is not None else [{"id": G, "name": "Sunucu"}])
        if p == f"/guilds/{G}/channels":
            return httpx.Response(200, json=CHANNELS)
        if p == f"/guilds/{G}/roles":
            return httpx.Response(200, json=[{"id": G, "name": "@everyone"}, {"id": "55", "name": "Üye"}])
        if req.method == "PUT":
            puts.append((p, json.loads(req.content)))
            return httpx.Response(204)
        return httpx.Response(404)
    return lambda token: Discord(token, client=httpx.Client(transport=httpx.MockTransport(handler)),
                                 sleep=lambda s: None)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    return ToolContext(cwd=tmp_path, config=make_config({"brain": ["a:m"]}))


def test_merge_keeps_other_bits_and_lifts_deny():
    allow, deny, same = merged(CHANNELS[1]["permission_overwrites"], G, MEDIA)
    assert allow == PERMS["view_channel"] | MEDIA and deny == 0 and not same
    assert merged(CHANNELS[2]["permission_overwrites"], G, MEDIA)[2]  # zaten tamam
    assert merged([], G, MEDIA) == (MEDIA, 0, False)


def test_select_voice_and_ticket_channels():
    assert [c["id"] for c in select_channels(CHANNELS, ["voice", "stage"])] == ["2", "3", "4"]
    # talep kategorisinin kendisi de (yeni açılan talepler ayarı oradan kopyalar) ve altındaki kanallar
    assert [c["id"] for c in select_channels(CHANNELS, ["text"], "talep|ticket")] == ["5", "6"]


def test_preview_then_apply(ctx):
    puts = []
    tool = DiscordAdmin(make=fake(puts))
    out = tool.run(ctx, action="allow", kinds=["voice", "stage"]).text
    assert "2 kanal değişecek, 1 kanal zaten tamam" in out and "apply=true" in out and puts == []
    assert tool.risk_for({"action": "allow"}) == NETWORK
    assert tool.risk_for({"action": "allow", "apply": True}) == WRITE
    out = tool.run(ctx, action="allow", kinds=["voice", "stage"], apply=True).text
    assert "2 kanalda verildi" in out
    assert puts == [(f"/channels/2/permissions/{G}", {"type": 0, "allow": str(PERMS["view_channel"] | MEDIA),
                                                       "deny": "0"}),
                    (f"/channels/4/permissions/{G}", {"type": 0, "allow": str(MEDIA), "deny": "0"})]


def test_named_role_and_errors(ctx, monkeypatch):
    puts = []
    tool = DiscordAdmin(make=fake(puts))
    tool.run(ctx, action="allow", match="talep|ticket", kinds=["text"], role="üye", apply=True)
    assert [p for p, _ in puts] == ["/channels/5/permissions/55", "/channels/6/permissions/55"]
    with pytest.raises(ToolError, match="rol yok"):
        tool.run(ctx, action="allow", kinds=["voice"], role="Yok")
    with pytest.raises(ToolError, match="kinds"):
        tool.run(ctx, action="allow")
    with pytest.raises(ToolError, match="Rolleri Yönet"):
        DiscordAdmin(make=fake([], status=403)).run(ctx, action="servers")
    with pytest.raises(ToolError, match="guild_id"):
        DiscordAdmin(make=fake([], guilds=[{"id": "1", "name": "a"}, {"id": "2", "name": "b"}])).run(
            ctx, action="channels")
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    with pytest.raises(ToolError, match="DISCORD_BOT_TOKEN"):
        tool.run(ctx, action="servers")


def test_channels_listing(ctx):
    out = DiscordAdmin(make=fake([])).run(ctx, action="channels").text
    assert "voice: Genel Ses (id 2) · kategori Sesli Sohbet" in out
