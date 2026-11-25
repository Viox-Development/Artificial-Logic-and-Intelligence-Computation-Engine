"""The computer itself: lock, sleep, screen off, power plan, monitor brightness, Wake-on-LAN info.

Windows: Win32 calls, powercfg, DDC/CI and WMI. Linux: loginctl, systemctl, the display server's DPMS command
(xset on X11; KDE, GNOME, Sway and Hyprland on Wayland), powerprofilesctl and brightnessctl, whichever is installed.
macOS: pmset, a Control-Command-Q keystroke through osascript, and the `brightness` command (Homebrew).
Elsewhere, or when the needed tool is missing, it returns an understandable error.
"""

from __future__ import annotations

import os
import re
import shutil
import sys

from alice.home.net import run
from alice.i18n import _

# Windows' built-in power plans (names change with the system language, GUIDs do not).
PLAN_ALIASES = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": ("dengeli", "balanced"),
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": ("yüksek performans", "performans", "high performance", "performance"),
    "a1841308-3bc4-4ac8-ba48-0d6b2e1bf3f2": ("güç tasarrufu", "tasarruf", "power saver", "saver"),
    "e9a42b02-d5df-448d-aa00-03f14749eb61": ("nihai performans", "ultimate performance", "ultimate"),
    # Linux power-profiles-daemon profiles (their names are fixed, so they serve as the "guid").
    "balanced": ("dengeli", "balanced"),
    "performance": ("yüksek performans", "performans", "high performance", "performance"),
    "power-saver": ("güç tasarrufu", "tasarruf", "power saver", "saver"),
}


def _windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(_("this only works on Windows for now"))


def _mac() -> bool:
    return sys.platform == "darwin"


def _linux() -> bool:
    """True on Linux, False on Windows; any other system gets an understandable error (macOS is handled before)."""
    if sys.platform == "win32":
        return False
    if sys.platform.startswith("linux"):
        return True
    raise RuntimeError(_("this only works on Windows, Linux and macOS for now"))


def _need(tool: str, package: str = "") -> str:
    exe = shutil.which(tool)
    if not exe:
        raise RuntimeError((_("{tool} not found: install {package}", tool=tool, package=package) if package else
                            _("{tool} not found", tool=tool)))
    return exe


def _check(r, what: str) -> str:
    if r.returncode != 0:
        raise RuntimeError(f"{what}: " + ((r.stderr or r.stdout or "").strip() or _("exit code {code}", code=r.returncode)))
    return r.stdout or ""


def _linux_lock() -> str:
    tried = []
    for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        r = run([exe, *cmd[1:]])
        if r.returncode == 0:
            return _("computer locked")
        tried.append(f"{cmd[0]}: " + ((r.stderr or r.stdout or "").strip() or _("exit code {code}", code=r.returncode)))
    if not tried:
        raise RuntimeError(_("locking needs loginctl (systemd) or xdg-screensaver"))
    raise RuntimeError(_("could not lock the computer: {details}", details="; ".join(tried)))


def _dpms_commands() -> list[tuple[list[str], str]]:
    """Screen-off commands for the running display server, most specific first: [(command, how it comes back)]."""
    env = os.environ
    desktop = (env.get("XDG_CURRENT_DESKTOP") or "").upper()
    wayland = bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"
    input_wakes = _("the mouse or keyboard turns it back on")
    out: list[tuple[list[str], str]] = []
    if env.get("SWAYSOCK"):
        out.append((["swaymsg", "output * power off"], _("to turn it on: {command}", command="swaymsg 'output * power on'")))
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        out.append((["hyprctl", "dispatch", "dpms", "off"], _("to turn it on: {command}", command="hyprctl dispatch dpms on")))
    if wayland and "KDE" in desktop:
        out.append((["kscreen-doctor", "--dpms", "off"], input_wakes))
    if wayland and "GNOME" in desktop:
        out.append((["busctl", "--user", "set-property", "org.gnome.Mutter.DisplayConfig",
                     "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode", "i", "1"],
                    input_wakes))
    if env.get("DISPLAY") and not wayland:
        out.append((["xset", "dpms", "force", "off"], input_wakes))
    return out


def _linux_monitor_off() -> str:
    cands = _dpms_commands()
    for cmd, back in cands:
        exe = shutil.which(cmd[0])
        if exe:
            _check(run([exe, *cmd[1:]]), _("could not turn the screen off"))
            return _("screen turned off ({how})", how=back)
    if not cands:
        raise RuntimeError(_("turning the screen off isn't supported on this desktop (needs X11, KDE, GNOME, Sway or "
                             "Hyprland)"))
    raise RuntimeError(_("turning the screen off needs one of: {tools}", tools=", ".join(c[0][0] for c in cands)))


def _mac_lock() -> str:
    # Control-Command-Q locks at once but needs the Accessibility permission; otherwise the display sleeps, which locks
    # when macOS asks for the password right after sleep (the default).
    osa = shutil.which("osascript")
    if osa and run([osa, "-e", 'tell application "System Events" to keystroke "q" using {control down, command down}'
                    ]).returncode == 0:
        return _("computer locked")
    _check(run([_need("pmset"), "displaysleepnow"]), _("could not lock the computer"))
    return _("screen turned off (it locks when macOS asks for the password after sleep)")


def lock() -> str:
    if _mac():
        return _mac_lock()
    if _linux():
        return _linux_lock()
    import ctypes

    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
    return _("computer locked")


def monitor_off() -> str:
    if _mac():
        _check(run([_need("pmset"), "displaysleepnow"]), _("could not turn the screen off"))
        return _("screen turned off (the mouse or keyboard turns it back on)")
    if _linux():
        return _linux_monitor_off()
    import ctypes

    # SendMessage can hang while broadcasting; PostMessage does not wait. SC_MONITORPOWER, 2 = off.
    ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)  # type: ignore[attr-defined]
    return _("screen turned off (the mouse or keyboard turns it back on)")


def suspend(hibernate: bool = False) -> str:
    if _mac():
        if hibernate:
            raise RuntimeError(_("macOS can't hibernate on request; it decides on its own (pmset hibernatemode)"))
        _check(run([_need("pmset"), "sleepnow"]), _("could not start sleep"))
        return _("going to sleep")
    if _linux():
        exe = _need("systemctl", "systemd")
        what = _("could not hibernate (swap or a kernel setting may be missing)") if hibernate else _("could not start sleep")
        _check(run([exe, "hibernate" if hibernate else "suspend"]), what)
        return _("hibernating") if hibernate else _("going to sleep")
    import ctypes

    ok = ctypes.windll.powrprof.SetSuspendState(bool(hibernate), True, False)  # type: ignore[attr-defined]
    if not ok:
        raise RuntimeError(_("Windows refused the sleep request") + (_(" (hibernation may be off: powercfg /hibernate on)")
                                                                if hibernate else ""))
    return _("hibernating") if hibernate else _("going to sleep")


def restart_in(minutes: float = 0) -> str:
    _windows()
    sec = max(0, int(minutes * 60))
    r = run(["shutdown", "/r", "/t", str(sec)])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or _("could not restart"))
    return _("restarting in {s} s (cancel: media cancel_shutdown)", s=sec) if sec else _("restarting")


# ---------- power plan ----------

PLAN_LINE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s+\((.+?)\)"
                       r"(\s*\*)?")


def parse_plans(text: str) -> list[dict]:
    """`powercfg /list` output (in any language) → [{guid, name, active}]."""
    return [{"guid": m.group(1).lower(), "name": m.group(2).strip(), "active": bool(m.group(3))}
            for m in PLAN_LINE.finditer(text)]


def find_plan(plans: list[dict], wanted: str) -> dict | None:
    w = wanted.strip().casefold()
    for p in plans:
        if p["guid"] == w or p["name"].casefold() == w:
            return p
    for p in plans:
        if w in p["name"].casefold() or w in PLAN_ALIASES.get(p["guid"], ()):
            return p
    for p in plans:
        if any(w in a or a in w for a in PLAN_ALIASES.get(p["guid"], ())):
            return p
    return None


PROFILE_LINE = re.compile(r"^(\*)?\s*([A-Za-z][\w-]*):\s*$")


def parse_profiles(text: str) -> list[dict]:
    """`powerprofilesctl list` output → [{guid, name, active}]; the profile name is used as the guid."""
    out = []
    for line in text.splitlines():
        if m := PROFILE_LINE.match(line):
            out.append({"guid": m.group(2), "name": m.group(2), "active": bool(m.group(1))})
    return out


def plans() -> list[dict]:
    if _mac():
        raise RuntimeError(_("macOS has no power plans to switch (Low Power Mode: System Settings → Battery)"))
    if _linux():
        exe = _need("powerprofilesctl", "power-profiles-daemon")
        return parse_profiles(_check(run([exe, "list"]), _("could not read the power profiles")))
    return parse_plans(run(["powercfg", "/list"]).stdout)


def set_plan(wanted: str) -> str:
    all_plans = plans()
    p = find_plan(all_plans, wanted)
    if not p:
        raise RuntimeError(_("there is no power plan called '{name}'. Available: {plans}", name=wanted,
                              plans=", ".join(x["name"] for x in all_plans)))
    if _linux():
        exe = _need("powerprofilesctl", "power-profiles-daemon")
        _check(run([exe, "set", p["guid"]]), _("could not change the power profile"))
        return _("power plan: {name}", name=p["name"])
    r = run(["powercfg", "/setactive", p["guid"]])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or _("could not change the power plan"))
    return _("power plan: {name}", name=p["name"])


# ---------- brightness ----------

VCP_BRIGHTNESS = 0x10


def _physical_monitors() -> list[tuple[object, str]]:
    """External monitors that speak DDC/CI: [(handle, description)]. Call _release when done."""
    import ctypes
    import ctypes.wintypes as wintypes

    class PHYSICAL_MONITOR(ctypes.Structure):
        _fields_ = [("handle", wintypes.HANDLE), ("desc", ctypes.c_wchar * 128)]

    user32, dxva2 = ctypes.windll.user32, ctypes.windll.dxva2  # type: ignore[attr-defined]
    hmons: list = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                              wintypes.LPARAM)
    cb = proc(lambda h, _dc, _r, _l: hmons.append(h) or True)
    user32.EnumDisplayMonitors(None, None, cb, 0)
    out = []
    for h in hmons:
        n = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(h, ctypes.byref(n)) or not n.value:
            continue
        arr = (PHYSICAL_MONITOR * n.value)()
        if dxva2.GetPhysicalMonitorsFromHMONITOR(h, n.value, arr):
            out.extend((m.handle, m.desc) for m in arr)
    return out


def _release(mons: list[tuple[object, str]]) -> None:
    import ctypes

    for h, _desc in mons:
        ctypes.windll.dxva2.DestroyPhysicalMonitor(h)  # type: ignore[attr-defined]


def _wmi_brightness(level: int | None) -> str:
    """The laptop's built-in screen does not speak DDC/CI, so its brightness comes from WMI."""
    if level is None:
        ps = "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
    else:
        ps = ("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | Invoke-CimMethod "
              f"-MethodName WmiSetBrightness -Arguments @{{Brightness=[byte]{level}; Timeout=[uint32]0}} | Out-Null; "
              "'ok'")
    r = run(["powershell", "-NoProfile", "-Command", ps])
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(_("could not set the brightness: the monitor doesn't support DDC/CI or it is off (turn DDC/CI on "
                             "in the monitor's own menu)"))
    return _("built-in screen {pct}%", pct=out.split()[0] if level is None else level)


def parse_backlights(text: str) -> list[dict]:
    """`brightnessctl --list --machine-readable` → backlight devices [{device, percent}] (LEDs are skipped)."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 5 and parts[1] == "backlight":
            pct = parts[3].rstrip("%")
            out.append({"device": parts[0], "percent": int(pct) if pct.isdigit() else None})
    return out


def _linux_brightness(level: int | None, monitor: int | None) -> str:
    exe = _need("brightnessctl", "brightnessctl")
    devs = parse_backlights(_check(run([exe, "--list", "--machine-readable"]), _("could not read the brightness")))
    if not devs:
        raise RuntimeError(_("no screen backlight found (external monitors need DDC/CI for brightness: ddcutil)"))
    lines = []
    for i, d in enumerate(devs, 1):
        if monitor and i != monitor:
            continue
        if level is None:
            lines.append(f"{i}. {d['device']}: %{d['percent']}" if d["percent"] is not None else f"{i}. {d['device']}")
        else:
            _check(run([exe, "--device", d["device"], "set", f"{level}%"]), _("could not set the brightness"))
            lines.append(f"{i}. {d['device']}: %{level}")
    if not lines:
        raise RuntimeError(_("there is no screen {n}; there are {count}", n=monitor, count=len(devs)))
    return "\n".join(lines)


def parse_mac_brightness(text: str) -> list[float]:
    """`brightness -l` → brightness of each display, 0-1 ("display 0: brightness 0.750000")."""
    return [float(m) for m in re.findall(r"display \d+: brightness ([0-9.]+)", text)]


def _mac_brightness(level: int | None, monitor: int | None) -> str:
    exe = _need("brightness", "brightness (brew install brightness)")
    if level is not None:
        args = [exe, *(["-d", str(monitor - 1)] if monitor else []), f"{level / 100:.2f}"]
        _check(run(args), _("could not set the brightness"))
        return f"{monitor or 1}. %{level}" if monitor else f"%{level}"
    vals = parse_mac_brightness(_check(run([exe, "-l"]), _("could not read the brightness")))
    if not vals:
        raise RuntimeError(_("no display with adjustable brightness found"))
    return "\n".join(f"{i}. %{round(v * 100)}" for i, v in enumerate(vals, 1) if not monitor or i == monitor)


def brightness(level: int | None = None, monitor: int | None = None) -> str:
    """If level is None, read it; otherwise set it to 0-100. monitor: 1-based index; if empty, all monitors."""
    if level is not None:
        level = max(0, min(100, int(level)))
    if _mac():
        return _mac_brightness(level, monitor)
    if _linux():
        return _linux_brightness(level, monitor)
    import ctypes
    import ctypes.wintypes as wintypes

    mons = _physical_monitors()
    lines = []
    try:
        dxva2 = ctypes.windll.dxva2  # type: ignore[attr-defined]
        for i, (h, desc) in enumerate(mons, 1):
            if monitor and i != monitor:
                continue
            cur, mx = wintypes.DWORD(), wintypes.DWORD()
            if not dxva2.GetVCPFeatureAndVCPFeatureReply(h, VCP_BRIGHTNESS, None, ctypes.byref(cur), ctypes.byref(mx)):
                continue
            top = mx.value or 100
            if level is None:
                lines.append(f"{i}. {desc or _('monitor')}: %{round(cur.value * 100 / top)}")
            elif dxva2.SetVCPFeature(h, VCP_BRIGHTNESS, wintypes.DWORD(round(level * top / 100))):
                lines.append(f"{i}. {desc or _('monitor')}: %{level}")
    finally:
        _release(mons)
    if lines:
        return "\n".join(lines)
    return _wmi_brightness(level)


# ---------- Wake-on-LAN ----------

def wol_info() -> str:
    """Information and status needed to wake this computer from another device."""
    _windows()
    from alice.home.net import local_ip

    ip = local_ip()
    nics = run(["powershell", "-NoProfile", "-Command",
                "Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | "
                "ForEach-Object { \"$($_.Name)|$($_.InterfaceDescription)|$($_.MacAddress)|$($_.MediaType)\" }"]
               ).stdout.strip().splitlines()
    armed = run(["powercfg", "/devicequery", "wake_armed"]).stdout.strip()
    lines = [f"IP: {ip}", _("Broadcast address: {address}", address=ip.rsplit(".", 1)[0] + ".255")]
    for n in nics:
        name, desc, mac, media = (n.split("|") + ["", "", "", ""])[:4]
        lines.append(_("Network card: {name} ({desc}) MAC {mac} {media}", name=name, desc=desc, mac=mac.replace("-", ":"),
                      media=media))
    lines.append(_("Devices that can wake it: {devices}", devices=", ".join(armed.splitlines()) or _("none")))
    lines.append(_("Note: WoL works reliably over a cable (Ethernet); most Wi-Fi cards don't hear the packet while "
                   "off. 'Wake on LAN / Power On By PCI-E' must be on in the BIOS, 'Allow a magic packet to wake "
                   "the computer' must be ticked on the network card's Power Management tab in Windows, and Fast "
                   "Startup must be off."))
    return "\n".join(lines)
