"""macOS backends: the command output parsers and that each call goes to the macOS tool."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alice.core import monitors
from alice.extras import nowplaying
from alice.home import pc, wifi


def _fake(monkeypatch, module, outputs):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        for key, out in outputs.items():
            if key in " ".join(cmd):
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_pc_on_macos(monkeypatch):
    calls = _fake(monkeypatch, pc, {"brightness -l": "display 0: brightness 0.750000\ndisplay 1: brightness 0.5\n"})
    assert pc.monitor_off().startswith("screen turned off") and calls[-1][-1] == "displaysleepnow"
    assert pc.suspend() == "going to sleep" and calls[-1][-1] == "sleepnow"
    with pytest.raises(RuntimeError, match="hibernate"):
        pc.suspend(hibernate=True)
    with pytest.raises(RuntimeError, match="power plans"):
        pc.plans()
    assert pc.lock() == "computer locked" and "keystroke" in calls[-1][-1]
    assert pc.brightness() == "1. %75\n2. %50"
    assert pc.brightness(40, 2) == "2. %40" and calls[-1][-3:] == ["-d", "1", "0.40"]


def test_wifi_on_macos(monkeypatch):
    calls = _fake(monkeypatch, wifi, {
        "-listallhardwareports": "Hardware Port: Ethernet\nDevice: en1\n\nHardware Port: Wi-Fi\nDevice: en0\n",
        "-getairportnetwork": "Current Wi-Fi Network: EvAgi\n",
        "-listpreferredwirelessnetworks": "Preferred networks on en0:\n\tEvAgi\n\tOkul\n",
        "system_profiler": ("Wi-Fi:\n\n      Interfaces:\n        en0:\n          Current Network Information:\n"
                            "            EvAgi:\n              Security: WPA2 Personal\n"
                            "              Signal / Noise: -50 dBm / -90 dBm\n"
                            "          Other Local Wi-Fi Networks:\n            Komsu:\n"
                            "              Security: WPA2 Personal\n              Signal / Noise: -80 dBm / -92 dBm\n"),
    })
    assert wifi.status().startswith("Connected: EvAgi")
    assert wifi.profiles() == ["EvAgi", "Okul"]
    assert wifi.scan() == [{"ssid": "EvAgi", "signal": 100, "auth": "WPA2 Personal"},
                           {"ssid": "Komsu", "signal": 40, "auth": "WPA2 Personal"}]
    assert wifi.connect("Okul", "sifre") == "connecting to 'Okul'" and calls[-1][-3:] == ["en0", "Okul", "sifre"]
    assert wifi.radio(False) == "Wi-Fi turned off" and calls[-1][-2:] == ["en0", "off"]
    with pytest.raises(RuntimeError, match="no Wi-Fi disconnect"):
        wifi.disconnect()


def test_networksetup_error_printed_with_exit_0(monkeypatch):
    _fake(monkeypatch, wifi, {"-listallhardwareports": "Hardware Port: Wi-Fi\nDevice: en0\n",
                              "-setairportnetwork": "Could not find network Yok.\n"})
    with pytest.raises(RuntimeError, match="Could not find network"):
        wifi.connect("Yok")


def test_foreground_app_on_macos(monkeypatch):
    monkeypatch.setattr(monitors.sys, "platform", "darwin")
    monkeypatch.setattr(monitors.shutil, "which", lambda name: "/usr/bin/lsappinfo")
    out = {"front": "ASN:0x0-0x1a01a:\n", "info": '"LSDisplayName"="Safari"\n'}
    monkeypatch.setattr(monitors, "_run", lambda cmd, timeout=5: out["front" if cmd[1] == "front" else "info"])
    assert monitors.foreground_process() == "Safari"
    assert monitors.parse_lsappinfo_name("") is None


def test_now_playing_on_macos(monkeypatch):
    monkeypatch.setattr(nowplaying.sys, "platform", "darwin")
    sent = []

    def osa(script):
        sent.append(script)
        if script.startswith('if application "Spotify"'):
            return nowplaying.SEP.join(["Spotify", "playing", "Sen Ağlama", "maNga", "Albüm"]) + "\n"
        return ""

    monkeypatch.setattr(nowplaying, "_osascript", osa)
    assert nowplaying.now() == {"app": "Spotify", "title": "Sen Ağlama", "artist": "maNga", "album": "Albüm",
                                "status": "playing"}
    assert nowplaying.control("next") and sent[-1] == 'tell application "Spotify" to next track'
    assert "is running" in nowplaying.mac_now_script("Music")
