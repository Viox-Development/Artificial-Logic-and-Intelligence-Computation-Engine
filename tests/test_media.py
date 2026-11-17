from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from alice.extras import media


@pytest.fixture
def env(monkeypatch):
    """Fake the platform, command execution and key presses."""
    st = SimpleNamespace(runs=[], presses=[], ok=True, which="/usr/bin/playerctl")

    def fake_run(cmd):
        st.runs.append(cmd)
        return st.ok(cmd) if callable(st.ok) else st.ok

    monkeypatch.setattr(media, "_run", fake_run)
    monkeypatch.setattr(media, "_press", lambda vk, times=1: st.presses.append((vk, times)))
    monkeypatch.setattr(media.shutil, "which", lambda name: st.which)

    def platform(p):
        monkeypatch.setattr(media.sys, "platform", p)

    st.platform = platform
    return st


# --------------------------------------------------------------------- _run
def test_run_true_only_on_zero_exit(monkeypatch):
    monkeypatch.setattr(media.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0))
    assert media._run(["x"]) is True
    monkeypatch.setattr(media.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=3))
    assert media._run(["x"]) is False


@pytest.mark.parametrize("exc", [FileNotFoundError("x"), subprocess.TimeoutExpired("x", 10)])
def test_run_false_on_errors(monkeypatch, exc):
    def boom(cmd, **kw):
        raise exc

    monkeypatch.setattr(media.subprocess, "run", boom)
    assert media._run(["x"]) is False


# ---------------------------------------------------------------------- key
def test_key_unknown_action(env):
    with pytest.raises(ValueError, match="bilinmeyen medya tuşu"):
        media.key("rewind")


def test_key_windows_presses_vk(env):
    env.platform("win32")
    assert media.key("next", 3) == "tamam"
    assert env.presses == [(0xB0, 3)]
    assert env.runs == []


@pytest.mark.parametrize("action, cmd", [
    ("play_pause", ["playerctl", "play-pause"]),
    ("next", ["playerctl", "next"]),
    ("previous", ["playerctl", "previous"]),
    ("stop", ["playerctl", "stop"]),
    ("mute", ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]),
])
def test_key_linux_commands(env, action, cmd):
    env.platform("linux")
    assert media.key(action) == "tamam"
    assert env.runs == [cmd]


def test_key_linux_volume_steps(env):
    env.platform("linux")
    assert media.key("volume_up", 5) == "tamam"
    assert media.key("volume_down") == "tamam"
    assert env.runs == [["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"],
                        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-2%"]]


def test_key_linux_missing_tools(env):
    env.platform("linux")
    env.which = None
    assert media.key("next") == "playerctl yok"
    assert env.runs == []  # not run when playerctl is missing
    env.ok = False
    assert media.key("mute") == "pactl yok"
    assert media.key("volume_up") == "pactl yok"
    env.which = "/usr/bin/playerctl"
    assert media.key("play_pause") == "playerctl yok"  # command failed


def test_key_macos(env):
    env.platform("darwin")
    assert media.key("volume_up", 2) == "tamam"
    assert "+ 12)" in env.runs[-1][2]
    assert media.key("volume_down") == "tamam"
    assert "+ -6)" in env.runs[-1][2]
    assert media.key("mute") == "tamam"
    assert "muted not" in env.runs[-1][2]
    assert media.key("play_pause") == "tamam"
    assert env.runs[-1] == ["osascript", "-e", 'tell application "Music" to playpause']
    n = len(env.runs)
    assert media.key("next") == "yapılamadı"  # not supported on macOS, no command is run
    assert len(env.runs) == n
    env.ok = False
    assert media.key("mute") == "yapılamadı"


# --------------------------------------------------------------- set_volume
@pytest.mark.parametrize("given, want", [(50, 50), (150, 100), (-5, 0), (33.7, 33), ("70", 70)])
def test_set_volume_clamps_linux(env, given, want):
    env.platform("linux")
    assert media.set_volume(given) == f"ses %{want}"
    assert env.runs == [["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{want}%"]]


def test_set_volume_linux_fail(env):
    env.platform("linux")
    env.ok = False
    assert media.set_volume(10) == "pactl yok"


def test_set_volume_macos(env):
    env.platform("darwin")
    assert media.set_volume(40) == "ses %40"
    assert env.runs == [["osascript", "-e", "set volume output volume 40"]]
    env.ok = False
    assert media.set_volume(40) == "yapılamadı"


def test_set_volume_windows_without_pycaw_uses_keys(env, monkeypatch):
    env.platform("win32")
    import builtins

    real_import = builtins.__import__

    def no_pycaw(name, *a, **kw):
        if name.startswith(("pycaw", "comtypes")):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pycaw)
    assert media.set_volume(37) == "ses yaklaşık %37"
    assert env.presses == [(0xAE, 50), (0xAF, round(37 / 2))]


# ----------------------------------------------------------------- shutdown
def test_shutdown_windows(env):
    env.platform("win32")
    assert media.shutdown_in(1.5) == "bilgisayar 1.5 dakika sonra kapanacak"
    assert env.runs == [["shutdown", "/s", "/t", "90"]]


def test_shutdown_windows_negative_clamped(env):
    env.platform("win32")
    media.shutdown_in(-3)
    assert env.runs == [["shutdown", "/s", "/t", "0"]]


@pytest.mark.parametrize("minutes, arg", [(30, "+30"), (0, "+1"), (2.6, "+3")])
def test_shutdown_unix_minutes(env, minutes, arg):
    env.platform("linux")
    media.shutdown_in(minutes)
    assert env.runs == [["shutdown", "-h", arg]]


def test_shutdown_failure(env):
    env.platform("linux")
    env.ok = False
    assert media.shutdown_in(10) == "kapatma zamanlayıcısı kurulamadı"


@pytest.mark.parametrize("plat, cmd", [("win32", ["shutdown", "/a"]), ("linux", ["shutdown", "-c"]),
                                       ("darwin", ["shutdown", "-c"])])
def test_cancel_shutdown(env, plat, cmd):
    env.platform(plat)
    assert media.cancel_shutdown() == "kapatma iptal edildi"
    assert env.runs == [cmd]
    env.ok = False
    assert media.cancel_shutdown() == "iptal edilecek kapatma yok"


# ------------------------------------------------------------------ pause_in
class FakeTimer:
    made: list = []

    def __init__(self, interval, fn):
        self.interval, self.fn = interval, fn
        self.daemon = False
        self.started = self.cancelled = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def test_pause_in_schedules_and_replaces(env, monkeypatch):
    FakeTimer.made = []
    monkeypatch.setattr(media.threading, "Timer", FakeTimer)
    monkeypatch.setattr(media, "_timers", {})
    assert media.pause_in(30) == "müzik 30 dakika sonra duracak"
    first = FakeTimer.made[0]
    assert first.interval == 1800 and first.daemon and first.started
    assert media.pause_in(0.5) == "müzik 0.5 dakika sonra duracak"
    second = FakeTimer.made[1]
    assert first.cancelled and not second.cancelled
    assert media._timers["pause"] is second
    # When the timer fires, play_pause is pressed
    env.platform("win32")
    second.fn()
    assert env.presses == [(media.VK["play_pause"], 1)]


def test_vk_and_playerctl_tables_consistent():
    assert set(media.PLAYERCTL) <= set(media.VK)
