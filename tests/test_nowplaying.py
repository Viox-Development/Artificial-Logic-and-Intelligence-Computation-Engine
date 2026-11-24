"""Now playing: reads the Windows media session, controls that exact app, and shows track changes on the HUD."""

from __future__ import annotations

import subprocess

import pytest

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
    assert extras.Media().run(ctx, "next").text == "done; now: X – B (Sonora, çalıyor)"
    assert calls == ["next"]
    assert extras.Media().run(ctx, "now").text == "X – B (Sonora, çalıyor)"
    monkeypatch.setattr(nowplaying, "control", lambda a: False)  # no session: falls back to the media key
    monkeypatch.setattr(media, "key", lambda a, t=1: "tamam")
    assert extras.Media().run(ctx, "play_pause").text == "tamam"


# ---------- Linux: MPRIS through playerctl ----------


@pytest.fixture
def playerctl(monkeypatch):
    """Fake playerctl on Linux: replies maps the first argument to (returncode, stdout)."""
    monkeypatch.setattr(nowplaying.sys, "platform", "linux")
    monkeypatch.setattr(nowplaying.shutil, "which", lambda n: "/usr/bin/playerctl" if n == "playerctl" else None)
    calls = []

    def make(replies):
        def run(cmd, **kw):
            calls.append(cmd[1:])
            code, out = replies.get(cmd[1], (1, ""))
            return subprocess.CompletedProcess(cmd, code, out, "" if code == 0 else "No players found")

        monkeypatch.setattr(nowplaying.subprocess, "run", run)
        return calls
    return make


def test_linux_now_reads_playerctl_metadata(playerctl):
    sep = nowplaying.SEP
    calls = playerctl({"metadata": (0, sep.join(["spotify", "Playing", "Sen Ağlama", "maNga", "Sen Ağlama"]) + "\n")})
    n = nowplaying.now()
    assert n == {"app": "Spotify", "title": "Sen Ağlama", "artist": "maNga", "album": "Sen Ağlama",
                 "status": "playing"}
    assert calls == [["metadata", "--format", nowplaying.NOW_FORMAT]]
    assert nowplaying.describe(n) == "maNga – Sen Ağlama (Spotify, çalıyor)"


def test_linux_now_without_player_or_playerctl_is_none(playerctl, monkeypatch):
    playerctl({})  # "No players found", exit code 1
    assert nowplaying.now() is None
    assert nowplaying.parse_playerctl("garbage") is None
    monkeypatch.setattr(nowplaying.shutil, "which", lambda n: None)
    assert nowplaying.now() is None and nowplaying.control("next") is False


def test_linux_control_maps_actions_to_playerctl(playerctl):
    calls = playerctl({"play-pause": (0, ""), "next": (0, ""), "pause": (1, "")})
    assert nowplaying.control("play_pause") is True
    assert nowplaying.control("next") is True
    assert nowplaying.control("pause") is False  # no player: the caller falls back to the media keys
    assert nowplaying.control("bogus") is False
    assert calls == [["play-pause"], ["next"], ["pause"]]
