"""PC control on Linux: loginctl, systemctl, DPMS commands, powerprofilesctl and brightnessctl (all faked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alice.app import remote
from alice.home import pc
from alice.tools.base import ToolContext, ToolError
from alice.tools.home import PC

PROFILES = """  performance:
    CpuDriver:\tintel_pstate
    Degraded:   no

* balanced:
    CpuDriver:\tintel_pstate
    PlatformDriver:\tplatform_profile

  power-saver:
    CpuDriver:\tintel_pstate
"""
BACKLIGHTS = ("intel_backlight,backlight,4800,50%,9600\n"
              "input3::capslock,leds,0,0%,1\n"
              "acpi_video0,backlight,7,70%,10\n")


def _ctx(tmp_path):
    from alice.config import load_config

    return ToolContext(cwd=Path(tmp_path), config=load_config())


class Fake:
    """Replaces pc.run: replies maps 'tool sub-command...' prefixes to (code, stdout, stderr)."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, cmd, timeout=15):
        joined = " ".join([Path(cmd[0]).name, *cmd[1:]])
        self.calls.append(joined)
        code, out, err = next((v for k, v in self.replies.items() if joined.startswith(k)), (0, "", ""))
        return subprocess.CompletedProcess(cmd, code, out, err)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "linux")
    for k in ("SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
              "DISPLAY"):
        monkeypatch.delenv(k, raising=False)
    state = {"have": set()}
    monkeypatch.setattr(pc.shutil, "which", lambda n: f"/usr/bin/{n}" if n in state["have"] else None)

    def make(have=(), replies=None):
        state["have"] = set(have)
        f = Fake(replies or {})
        monkeypatch.setattr(pc, "run", f)
        return f
    return make


def test_lock_uses_loginctl_then_falls_back_to_xdg_screensaver(linux):
    f = linux({"loginctl", "xdg-screensaver"})
    assert pc.lock() == "computer locked" and f.calls == ["loginctl lock-session"]
    f = linux({"loginctl", "xdg-screensaver"}, {"loginctl": (1, "", "Failed to issue method call")})
    assert pc.lock() == "computer locked"
    assert f.calls == ["loginctl lock-session", "xdg-screensaver lock"]
    linux({"loginctl"}, {"loginctl": (1, "", "No session")})
    with pytest.raises(RuntimeError, match="No session"):
        pc.lock()
    linux()
    with pytest.raises(RuntimeError, match="loginctl"):
        pc.lock()


@pytest.mark.parametrize("env,tool,expected", [
    ({"DISPLAY": ":0"}, "xset", "xset dpms force off"),
    ({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"}, "kscreen-doctor", "kscreen-doctor --dpms off"),
    ({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "busctl", "busctl --user set-property"),
    ({"SWAYSOCK": "/run/sway.sock", "WAYLAND_DISPLAY": "wayland-1"}, "swaymsg", "swaymsg output * power off"),
    ({"HYPRLAND_INSTANCE_SIGNATURE": "abc"}, "hyprctl", "hyprctl dispatch dpms off"),
])
def test_monitor_off_picks_the_display_servers_command(linux, monkeypatch, env, tool, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    f = linux({tool, "xset"})
    assert pc.monitor_off().startswith("screen turned off")
    assert f.calls[0].startswith(expected)


def test_monitor_off_errors(linux, monkeypatch):
    linux({"xset"})
    with pytest.raises(RuntimeError, match="isn't supported"):
        pc.monitor_off()  # no DISPLAY, no Wayland compositor we know
    monkeypatch.setenv("DISPLAY", ":0")
    linux()
    with pytest.raises(RuntimeError, match="xset"):
        pc.monitor_off()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    linux({"xset"})
    with pytest.raises(RuntimeError, match="isn't supported"):
        pc.monitor_off()  # xset does not turn off a Wayland screen


def test_suspend_and_hibernate_use_systemctl(linux, tmp_path):
    f = linux({"systemctl"})
    assert pc.suspend() == "going to sleep"
    assert pc.suspend(hibernate=True) == "hibernating"
    assert f.calls == ["systemctl suspend", "systemctl hibernate"]
    linux({"systemctl"}, {"systemctl hibernate": (1, "", "Not enough swap space for hibernation")})
    with pytest.raises(ToolError, match="swap"):
        PC().run(_ctx(tmp_path), action="hibernate")
    linux()
    with pytest.raises(RuntimeError, match="systemctl not found"):
        pc.suspend()


def test_power_profiles(linux, tmp_path):
    assert pc.parse_profiles(PROFILES) == [{"guid": "performance", "name": "performance", "active": False},
                                           {"guid": "balanced", "name": "balanced", "active": True},
                                           {"guid": "power-saver", "name": "power-saver", "active": False}]
    f = linux({"powerprofilesctl"}, {"powerprofilesctl list": (0, PROFILES, "")})
    assert PC().run(_ctx(tmp_path), action="plans").text == "  performance\n* balanced\n  power-saver"
    assert pc.set_plan("tasarruf") == "power plan: power-saver"
    assert pc.set_plan("performans") == "power plan: performance"
    assert [c for c in f.calls if " set " in c] == ["powerprofilesctl set power-saver",
                                                   "powerprofilesctl set performance"]
    with pytest.raises(RuntimeError, match="Available: performance, balanced, power-saver"):
        pc.set_plan("oyun")
    linux()
    with pytest.raises(RuntimeError, match="power-profiles-daemon"):
        pc.plans()


def test_brightness_with_brightnessctl(linux, tmp_path):
    f = linux({"brightnessctl"}, {"brightnessctl --list": (0, BACKLIGHTS, "")})
    assert pc.parse_backlights(BACKLIGHTS) == [{"device": "intel_backlight", "percent": 50},
                                               {"device": "acpi_video0", "percent": 70}]
    assert PC().run(_ctx(tmp_path), action="brightness").text == "1. intel_backlight: %50\n2. acpi_video0: %70"
    assert pc.brightness(140, 1) == "1. intel_backlight: %100"
    assert f.calls[-1] == "brightnessctl --device intel_backlight set 100%"
    with pytest.raises(RuntimeError, match="no screen 3"):
        pc.brightness(10, 3)
    linux({"brightnessctl"}, {"brightnessctl --list": (0, "input3::capslock,leds,0,0%,1\n", "")})
    with pytest.raises(RuntimeError, match="ddcutil"):
        pc.brightness()
    linux({"brightnessctl"}, {"brightnessctl --list": (0, BACKLIGHTS, ""),
                              "brightnessctl --device": (1, "", "Permission denied")})
    with pytest.raises(RuntimeError, match="Permission denied"):
        pc.brightness(30)


def test_still_windows_only_and_other_systems(linux, monkeypatch):
    linux()
    with pytest.raises(RuntimeError, match="only works on Windows"):
        pc.restart_in(0)
    monkeypatch.setattr(pc.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Windows and Linux"):
        pc.lock()


def test_phone_lock_and_sleep_work_on_linux(linux, monkeypatch):
    monkeypatch.setattr(remote, "IS_WIN", False)
    monkeypatch.setattr(remote.sys, "platform", "linux")
    f = linux({"loginctl", "systemctl"})
    assert remote.action({"action": "lock"}) == "computer locked"
    assert remote.action({"action": "sleep"}) == "going to sleep"
    assert f.calls == ["loginctl lock-session", "systemctl suspend"]
    linux()
    with pytest.raises(ValueError, match="loginctl"):
        remote.action({"action": "lock"})
    monkeypatch.setattr(remote.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="sadece Windows ve Linux"):
        remote.action({"action": "sleep"})
