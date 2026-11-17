"""Now playing: reads the Windows media session, controls that exact app, and shows track changes on the HUD."""

from __future__ import annotations

from alice.extras import nowplaying
from alice.tools.base import ToolContext
from tests.conftest import make_config


def test_app_names_are_readable():
    assert nowplaying.app_name("sonora.exe") == "Sonora"
    assert nowplaying.app_name("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "Spotify"
    assert nowplaying.app_name("C:/x/Foo.exe") == "Foo"


def test_describe():
    n = {"app": "Sonora", "title": "Dünyanın Sonuna Doğmuşum", "artist": "maNga", "status": "paused"}
    assert nowplaying.describe(n) == "maNga – Dünyanın Sonuna Doğmuşum (Sonora, duraklatıldı)"
    assert "çalan bir şey yok" in nowplaying.describe(None)


def test_watcher_announces_only_track_changes_while_playing():
    shown = []
    track = {"title": "A", "artist": "X", "status": "playing", "app": "Spotify"}
    w = nowplaying.Watcher(shown.append, read=lambda: dict(track))
    assert not w.tick()  # a song already playing at startup is not announced
    assert not w.tick()  # same song
    track["title"] = "B"
    assert w.tick() and shown[-1]["title"] == "B"
    track.update(title="C", status="paused")
    assert not w.tick()  # do not announce while paused


def test_media_tool_controls_the_playing_app_and_reports_result(monkeypatch, tmp_path):
    from alice.extras import media
    from alice.tools import extras

    calls = []
    monkeypatch.setattr(nowplaying, "control", lambda a: calls.append(a) or True)
    monkeypatch.setattr(nowplaying, "now", lambda: {"app": "Sonora", "title": "B", "artist": "X", "status": "playing"})
    monkeypatch.setattr(media, "key", lambda *a: (_ for _ in ()).throw(AssertionError("tuşa düşmemeli")))
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)
    ctx = ToolContext(cwd=tmp_path, config=make_config({"brain": ["a:m"]}))
    assert extras.Media().run(ctx, "next").text == "tamam; şimdi: X – B (Sonora, çalıyor)"
    assert calls == ["next"]
    assert extras.Media().run(ctx, "now").text == "X – B (Sonora, çalıyor)"
    monkeypatch.setattr(nowplaying, "control", lambda a: False)  # no session: falls back to the media key
    monkeypatch.setattr(media, "key", lambda a, t=1: "tamam")
    assert extras.Media().run(ctx, "play_pause").text == "tamam"
