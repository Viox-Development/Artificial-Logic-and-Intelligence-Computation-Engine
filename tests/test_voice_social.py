"""Tests for microphone-in-use detection in ``alice.voice.social`` (#449).

The pactl and Windows registry paths are faked so every test runs on any OS with
no browser, PulseAudio or real registry access.
"""

from __future__ import annotations

import subprocess
import sys
import types

from alice.voice import social


def _pactl_output() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        returncode=0,
        stdout=(
            "Source Output #42\n"
            "\tapplication.process.binary = \"discord\"\n"
            "\tapplication.name = \"Discord\"\n"
            "\nSource Output #43\n"
            "\tapplication.process.binary = \"zoom\"\n"
        ),
    )


def test_mic_users_linux_parses_pactl(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _pactl_output())
    assert social.mic_users() == ["discord", "zoom"]


def test_mic_users_linux_falls_back_when_pactl_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for failure in (FileNotFoundError("pactl"), subprocess.TimeoutExpired("pactl", 3)):

        def fail(*a, _failure=failure, **kw):
            raise _failure

        monkeypatch.setattr(subprocess, "run", fail)
        assert social.mic_users() == []

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: types.SimpleNamespace(returncode=1, stdout=""))
    assert social.mic_users() == []


def test_mic_users_is_empty_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert social.mic_users() == []


def test_mic_users_windows_registry_only_reports_open_streams(monkeypatch):
    class FakeKey:
        def __init__(self, names=(), values=None):
            self.names = list(names)
            self.values = values or {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeWinreg:
        HKEY_CURRENT_USER = 1

        def __init__(self):
            self.nonpackaged = social.MIC_KEY + "\\NonPackaged"
            self.roots = {
                self.nonpackaged: FakeKey(names=["C:#Users#a#Discord.exe", "C:#Users#a#Teams.exe"]),
                social.MIC_KEY: FakeKey(names=["5319275A.WhatsAppDesktop_x", "OtherApp_x"]),
            }
            self.values = {
                "C:#Users#a#Discord.exe": {"LastUsedTimeStart": 1, "LastUsedTimeStop": 0},
                "C:#Users#a#Teams.exe": {"LastUsedTimeStart": 1, "LastUsedTimeStop": 42},
                "5319275A.WhatsAppDesktop_x": {"LastUsedTimeStart": 1, "LastUsedTimeStop": 0},
                "OtherApp_x": {"LastUsedTimeStart": 0, "LastUsedTimeStop": 0},
            }

        def OpenKey(self, root, path):
            if path in self.roots:
                return self.roots[path]
            if path in self.values:
                return FakeKey(values=self.values[path])
            raise OSError("missing key")

        def EnumKey(self, key, index):
            if index >= len(key.names):
                raise OSError("no more keys")
            return key.names[index]

        def QueryValueEx(self, key, name):
            if name not in key.values:
                raise OSError("missing value")
            return (key.values[name], 0)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg())
    assert social.mic_users() == [
        "C:#Users#a#Discord.exe",
        "5319275A.WhatsAppDesktop_x",
    ]


def test_social_watch_reason_uses_mics_games_and_cache(monkeypatch):
    current = ["discord.exe"]
    watch = social.SocialWatch(mics=lambda: current, procs=lambda: set(), every=100.0)
    assert "Discord" in watch.reason()
    current = []
    assert "Discord" in watch.reason()  # cached

    games = social.SocialWatch(mics=lambda: [], procs=lambda: {"dcs.exe"}, every=0)
    assert "DCS" in games.reason()

    disabled = social.SocialWatch(mics=lambda: ["discord.exe"], procs=lambda: set(), every=0)
    disabled.enabled = False
    assert disabled.reason() == ""
