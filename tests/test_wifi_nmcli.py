"""Wi-Fi on Linux: nmcli terse output parsing and the commands sent (nmcli is replaced with a fake)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alice.home import wifi
from alice.tools.base import ToolContext, ToolError
from alice.tools.home import Wifi

LIST = (
    " :Komsu_2G:40:WPA2:6:130 Mbit/s\n"
    "*:Ev\\:Agi 5G:87:WPA2 WPA3:36:540 Mbit/s\n"
    " :Komsu_2G:62:WPA2:11:130 Mbit/s\n"
    " :Kafe::--:1:54 Mbit/s\n"
    " ::20:WPA2:1:54 Mbit/s\n"
)
PROFILES = "Ev:Agi 5G:802-11-wireless\nWired connection 1:802-3-ethernet\nOkul:802-11-wireless\nlo:loopback\n"
PROFILES_ESCAPED = "Ev\\:Agi 5G:802-11-wireless\nWired connection 1:802-3-ethernet\nOkul:802-11-wireless\n"
DEVICES = "eth0:ethernet:Wired connection 1\nwlp2s0:wifi:Ev\\:Agi 5G\nlo:loopback:lo\n"


def _ctx(tmp_path):
    from alice.config import load_config

    return ToolContext(cwd=Path(tmp_path), config=load_config())


class FakeNmcli:
    def __init__(self, replies, fail=()):
        self.replies, self.fail, self.calls = replies, fail, []

    def __call__(self, cmd, timeout=15):
        self.calls.append(cmd[1:])
        joined = " ".join(cmd[1:])
        if any(f in joined for f in self.fail):
            return subprocess.CompletedProcess(cmd, 4, "", "Error: No network with SSID 'X' found.\n")
        out = next((v for k, v in self.replies.items() if k in joined), "")
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def nmcli(monkeypatch):
    monkeypatch.setattr(wifi.sys, "platform", "linux")
    monkeypatch.setattr(wifi.shutil, "which", lambda name: "/usr/bin/nmcli" if name == "nmcli" else None)

    def make(replies=None, fail=()):
        f = FakeNmcli(replies or {}, fail)
        monkeypatch.setattr(wifi, "run", f)
        return f
    return make


def test_split_terse_unescapes_colons_and_backslashes():
    assert wifi.split_terse("*:Ev\\:Agi:87") == ["*", "Ev:Agi", "87"]
    assert wifi.split_terse("a\\\\b::c") == ["a\\b", "", "c"]


def test_parse_nm_status_uses_in_use_marker():
    s = wifi.parse_nm_status(LIST)
    assert s["connected"] and s["ssid"] == "Ev:Agi 5G" and s["signal"] == 87
    assert ("Security", "WPA2 WPA3") in s["fields"] and ("Channel", "36") in s["fields"]
    assert wifi.parse_nm_status(LIST.replace("*", " ")) == {"connected": False, "ssid": "", "signal": None,
                                                            "fields": []}


def test_parse_nm_networks_keeps_strongest_ap_and_marks_open():
    nets = wifi.parse_nm_networks(LIST)
    assert nets == [
        {"ssid": "Ev:Agi 5G", "signal": 87, "auth": "WPA2 WPA3"},
        {"ssid": "Komsu_2G", "signal": 62, "auth": "WPA2"},
        {"ssid": "", "signal": 20, "auth": "WPA2"},
        {"ssid": "Kafe", "signal": 0, "auth": "open"},
    ]


def test_parse_nm_profiles_and_devices():
    assert wifi.parse_nm_profiles(PROFILES_ESCAPED) == ["Ev:Agi 5G", "Okul"]
    assert wifi.parse_nm_devices(DEVICES) == [("wlp2s0", "Ev:Agi 5G")]
    assert wifi.parse_nm_devices("wlan1:wifi:--\nwlan0:wifi:Ev\n") == [("wlan0", "Ev"), ("wlan1", "")]


def test_tool_status_scan_saved_on_linux(nmcli, tmp_path):
    f = nmcli({"device wifi list": LIST, "connection show": PROFILES_ESCAPED})
    ctx = _ctx(tmp_path)
    assert Wifi().run(ctx, action="status").text.startswith("Connected: Ev:Agi 5G, signal 87%\nSSID: Ev:Agi 5G")
    assert Wifi().run(ctx, action="scan").text.splitlines()[:2] == ["Ev:Agi 5G: %87 WPA2 WPA3", "Komsu_2G: %62 WPA2"]
    assert Wifi().run(ctx, action="saved").text == "Ev:Agi 5G\nOkul"
    assert f.calls[0][-2:] == ["--rescan", "no"] and f.calls[1][-2:] == ["--rescan", "yes"]
    assert all(c[0] == "-t" for c in f.calls)


def test_connect_known_network_brings_profile_up(nmcli):
    f = nmcli({"connection show": PROFILES_ESCAPED})
    assert wifi.connect("Okul") == "connecting to 'Okul'"
    assert f.calls[-1] == ["connection", "up", "id", "Okul"]


def test_connect_new_network_with_password_and_open_network(nmcli):
    f = nmcli({"connection show": "", "device wifi list": LIST})
    wifi.connect("Komsu_2G", "s3cret")
    assert f.calls[-1] == ["device", "wifi", "connect", "Komsu_2G", "password", "s3cret"]
    wifi.connect("Kafe")
    assert f.calls[-1] == ["device", "wifi", "connect", "Kafe"]
    with pytest.raises(RuntimeError, match="give the password too"):
        wifi.connect("Komsu_2G")


def test_connect_error_is_reported_without_password(nmcli, tmp_path):
    nmcli({"connection show": "", "device wifi list": LIST}, fail=("device wifi connect",))
    with pytest.raises(ToolError) as e:
        Wifi().run(_ctx(tmp_path), action="connect", ssid="Komsu_2G", password="gizli123")
    assert "No network" in str(e.value) and "gizli123" not in str(e.value)


def test_disconnect_and_radio(nmcli, tmp_path):
    f = nmcli({"DEVICE,TYPE,CONNECTION": DEVICES})
    ctx = _ctx(tmp_path)
    assert Wifi().run(ctx, action="disconnect").text == "Wi-Fi disconnected"
    assert f.calls[-1] == ["device", "disconnect", "wlp2s0"]
    assert Wifi().run(ctx, action="radio_off").text == "Wi-Fi turned off"
    assert f.calls[-1] == ["radio", "wifi", "off"]
    assert Wifi().run(ctx, action="radio_on").text == "Wi-Fi turned on"
    assert Wifi().risk_for({"action": "radio_off"}) == "computer"

    f = nmcli({"DEVICE,TYPE,CONNECTION": "wlan0:wifi:--\n"})
    assert wifi.disconnect() == "Wi-Fi'a zaten bağlı değil."
    assert len(f.calls) == 1
    nmcli({"DEVICE,TYPE,CONNECTION": "eth0:ethernet:Wired\n"})
    with pytest.raises(RuntimeError, match="aygıtı bulunamadı"):
        wifi.disconnect()


def test_missing_nmcli_gives_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(wifi.sys, "platform", "linux")
    monkeypatch.setattr(wifi.shutil, "which", lambda name: None)
    with pytest.raises(ToolError, match="NetworkManager"):
        Wifi().run(_ctx(tmp_path), action="status")


def test_other_platforms_fail_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(wifi.sys, "platform", "darwin")
    with pytest.raises(ToolError, match="Windows and Linux"):
        Wifi().run(_ctx(tmp_path), action="scan")


def test_radio_is_linux_only(monkeypatch):
    monkeypatch.setattr(wifi.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="only works on Linux"):
        wifi.radio(False)
