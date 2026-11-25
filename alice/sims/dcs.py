"""DCS World connection.

  install()       -> Puts alice_export.lua in Saved Games\\DCS*\\Scripts, adds one line to Export.lua;
                     puts the wingman hook in Scripts\\Hooks (see alice/sims/wingmen.py)
  DCSListener     -> receives the data the script sends over UDP and turns it into FlightState
  read_mission()  -> extracts the player's group, frequency and waypoints from the .miz mission file
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.i18n import _, speech_language, spoken
from alice.sims.flight import FT, NM, PROFILES, FlightState, profile_for

PORT = 41230
SCRIPT = Path(__file__).with_name("alice_export.lua")
EXPORT_LINE = ("pcall(function() local aliceLfs = require('lfs'); "
               "dofile(aliceLfs.writedir() .. 'Scripts/alice_export.lua') end) -- ALICE")


def saved_games_dirs() -> list[Path]:
    """DCS's Saved Games folders (stable and open beta)."""
    base = Path(os.environ.get("USERPROFILE", Path.home())) / "Saved Games"
    return [p for p in (base / "DCS", base / "DCS.openbeta") if p.is_dir()]


WINGMEN_PORT = 41231
HOOK = Path(__file__).with_name("alice_wingmen_hook.lua")


def _export_lua(port: int) -> str:
    return SCRIPT.read_text(encoding="utf-8").replace('"127.0.0.1", 41230,', f'"127.0.0.1", {port},')


def _lua_str(text: str) -> str:
    """Python text -> safe Lua string literal (quoted and backslash-escaped; control characters dropped)."""
    clean = "".join(ch for ch in str(text) if ch >= " " and ch != "\x7f")[:60]
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def hook_lua(port: int = WINGMEN_PORT, names: list[str] | None = None) -> str:
    """Content of the wingman hook to install: port and optional fixed group list ([sims] dcs_wingmen)."""
    table = "{" + ", ".join(_lua_str(n) for n in (names or []) if str(n).strip()) + "}"
    return (HOOK.read_text(encoding="utf-8")
            .replace("local ALICE_PORT = 41231", f"local ALICE_PORT = {int(port)}", 1)
            .replace("local ALICE_NAMES = {}", f"local ALICE_NAMES = {table}", 1))


def install(dirs: list[Path] | None = None, port: int = PORT, wingmen_port: int = WINGMEN_PORT,
            wingmen: list[str] | None = None) -> list[Path]:
    """Install the script and wingman hook. Doesn't re-add the line if Export.lua has it. Returns installed folders."""
    done = []
    lua, hook = _export_lua(port), hook_lua(wingmen_port, wingmen)
    for d in dirs if dirs is not None else saved_games_dirs():
        scripts = d / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "alice_export.lua").write_text(lua, encoding="utf-8")
        (scripts / "Hooks").mkdir(exist_ok=True)
        (scripts / "Hooks" / HOOK.name).write_text(hook, encoding="utf-8")
        export = scripts / "Export.lua"
        text = export.read_text(encoding="utf-8", errors="replace") if export.exists() else ""
        if "alice_export.lua" not in text:
            export.write_text(text.rstrip("\n") + ("\n" if text else "") + EXPORT_LINE + "\n", encoding="utf-8")
        done.append(d)
    return done


def ensure_installed(dirs: list[Path] | None = None, port: int = PORT, wingmen_port: int = WINGMEN_PORT,
                     wingmen: list[str] | None = None) -> list[Path]:
    """Install if missing or if the script/hook changed (the core calls this on startup). Returns installed folders."""
    lua, hook = _export_lua(port), hook_lua(wingmen_port, wingmen)
    read = lambda p: p.read_text(encoding="utf-8", errors="replace") if p.exists() else None  # noqa: E731
    todo = []
    for d in dirs if dirs is not None else saved_games_dirs():
        export = read(d / "Scripts" / "Export.lua")
        ok = (read(d / "Scripts" / "alice_export.lua") == lua and export is not None and "alice_export.lua" in export
              and read(d / "Scripts" / "Hooks" / HOOK.name) == hook)
        if not ok:
            todo.append(d)
    return install(todo, port, wingmen_port, wingmen) if todo else []


def uninstall(dirs: list[Path] | None = None) -> list[Path]:
    done = []
    for d in dirs if dirs is not None else saved_games_dirs():
        export = d / "Scripts" / "Export.lua"
        if export.exists():
            lines = export.read_text(encoding="utf-8", errors="replace").splitlines()
            kept = [ln for ln in lines if "alice_export.lua" not in ln]
            if len(kept) != len(lines):
                export.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                done.append(d)
        for script in (d / "Scripts" / "alice_export.lua", d / "Scripts" / "Hooks" / HOOK.name):
            if script.exists():
                script.unlink()
                if d not in done:
                    done.append(d)
    return done


def to_state(d: dict[str, Any], internal_fuel_kg: float | None = None) -> FlightState:
    """Convert the dict sent by the script into a FlightState."""
    aircraft = str(d.get("aircraft") or "")
    fuel = None
    fi, fe = d.get("fuel_int"), d.get("fuel_ext")
    if isinstance(fi, (int, float)):
        cap = internal_fuel_kg or profile_for(aircraft).internal_fuel_kg
        # Some modules report kg, others a fraction of a full tank (0-1).
        fuel = fi * cap if fi <= 1.5 else fi
        if isinstance(fe, (int, float)) and fe > 0:
            fuel += fe * cap if fe <= 1.5 else fe
    ext_kg = None
    if isinstance(fe, (int, float)) and fe >= 0:
        ext_kg = fe * (internal_fuel_kg or profile_for(aircraft).internal_fuel_kg) if fe <= 1.5 else fe
    # Ratio-reporting module, capacity unknown: kg is an estimate; minutes stay ratio-independent and bingo won't fire.
    guess = isinstance(fi, (int, float)) and fi <= 1.5 and not internal_fuel_kg and aircraft not in PROFILES
    num = lambda k: float(d[k]) if isinstance(d.get(k), (int, float)) else None  # noqa: E731
    return FlightState(t=float(d.get("t") or 0), source="dcs", aircraft=aircraft, ias=num("ias"), tas=num("tas"),
                       alt=num("alt"), agl=num("agl"), vv=num("vv"), aoa=num("aoa"), g=num("g"), fuel_kg=fuel,
                       burn_kgs=num("burn"), chaff=int(d["chaff"]) if isinstance(d.get("chaff"), (int, float)) else None,
                       flare=int(d["flare"]) if isinstance(d.get("flare"), (int, float)) else None,
                       gear=num("gear"), x=num("x"), z=num("z"),
                       heading=math.degrees(num("heading")) % 360 if num("heading") is not None else None,
                       extra=_extra(d, num) | ({"fuel_ext_kg": ext_kg} if ext_kg is not None else {})
                       | ({"fuel_guess": True} if guess else {}))


def _extra(d: dict[str, Any], num: Callable[[str], float | None]) -> dict[str, Any]:
    deg = lambda k: None if num(k) is None else math.degrees(num(k))  # noqa: E731
    e: dict[str, Any] = {"mach": num("mach"), "pitch": deg("pitch"), "bank": deg("bank"),
                         "mag_heading": None if num("mag_heading") is None else deg("mag_heading") % 360,
                         "radalt": None if num("radalt") is None else num("radalt") / FT,
                         "flaps": num("flaps"), "speedbrake": num("speedbrake"), "hook": num("hook"),
                         "wp_x": num("wp_x"), "wp_z": num("wp_z"), "wp_num": num("wp_num"),
                         "ils_glide": num("ils_glide"), "ils_side": num("ils_side"), "gun": num("gun")}
    rpms = [v for v in (num("rpm_l"), num("rpm_r")) if v]
    if rpms:
        e["rpm"] = max(rpms)
    egts = [v for v in (num("egt_l"), num("egt_r")) if v]
    if egts:
        e["egt"] = max(egts)
    wx, wz = num("wind_x"), num("wind_z")
    if wx is not None and wz is not None:
        e["wind_kt"] = math.hypot(wx, wz) / 0.514444
        e["wind_from"] = (math.degrees(math.atan2(wz, wx)) + 180) % 360  # direction the wind blows from
    for k in ("weapons", "warnings", "mode", "radios", "ack"):
        if isinstance(d.get(k), str) and d[k].strip("/ "):
            e[k] = d[k]
    if isinstance(d.get("rwr"), str):
        e["contacts"] = parse_rwr(d["rwr"])
    return {k: v for k, v in e.items() if v is not None}


def parse_rwr(text: str) -> list[dict[str, Any]]:
    """RWR line -> contacts. Bearing off the nose in radians (right positive) -> clock (12 = ahead)."""
    out = []
    for part in text.split(";"):
        f = part.split("|")
        if len(f) < 4:
            continue
        try:
            az = math.degrees(float(f[1]))
        except ValueError:
            continue
        sig = f[2].lower()
        out.append({"id": f[0], "clock": clock_of(az), "name": f[3].strip() or "?", "lock": "lock" in sig,
                    "missile": "missile" in sig, "dist_m": None, "kind": "radar"})
    return out


def clock_of(rel_deg: float) -> int:
    """Angle off the nose (degrees, right positive) -> clock position 1-12."""
    c = round((rel_deg % 360) / 30) % 12
    return 12 if c == 0 else c


class DCSListener:
    """Forwards UDP data to the companion and (once a second) to the store."""

    def __init__(self, on_state: Callable[[FlightState], None], on_restricted: Callable[[], None] = lambda: None,
                 on_end: Callable[[], None] = lambda: None, on_metrics: Callable[[list], None] = lambda rows: None,
                 port: int = PORT, timeout: float = 10.0):
        self.on_state = on_state
        self.on_restricted = on_restricted
        self.on_end = on_end
        self.on_metrics = on_metrics
        self.port = port
        self.timeout = timeout
        self.last: FlightState | None = None
        self.active = False
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._metrics_at = 0.0

    def start(self) -> "DCSListener":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.settimeout(1.0)
        threading.Thread(target=self._loop, daemon=True, name="alice-dcs").start()
        return self

    @property
    def bound_port(self) -> int:
        return self._sock.getsockname()[1] if self._sock else self.port

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()

    def handle(self, data: dict[str, Any]) -> None:
        if data.get("event") == "stop":
            self._end()
            return
        if data.get("event") == "start":
            return
        self.active = True
        if data.get("restricted"):
            self.on_restricted()
            return
        s = to_state(data)
        self.last = s
        self.on_state(s)
        if time.monotonic() - self._metrics_at >= 1.0:
            self._metrics_at = time.monotonic()
            rows = [(f"dcs.{k}", v, None) for k, v in (
                ("ias_kt", s.ias and s.ias / 0.514444), ("alt_ft", s.alt and s.alt / FT), ("g", s.g), ("aoa", s.aoa),
                ("fuel.kg", s.fuel_kg), ("chaff", s.chaff), ("flare", s.flare)) if v is not None]
            self.on_metrics(rows)

    def _end(self) -> None:
        if self.active:
            self.active = False
            self.on_end()

    def _loop(self) -> None:
        last_rx = time.monotonic()
        while not self._stop.is_set():
            try:
                raw, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                if self.active and time.monotonic() - last_rx > self.timeout:
                    self._end()
                continue
            except OSError:
                return
            last_rx = time.monotonic()
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(data, dict):
                try:
                    self.handle(data)
                except Exception:  # if a packet is malformed, don't let the listener die
                    pass


# ------------------------------------------------------------ mission file
class LuaError(ValueError):
    pass


def parse_lua_table(text: str) -> Any:
    """Converts Lua tables such as `mission = { ... }` in a DCS mission file into Python.
    Data syntax only: tables, strings, numbers, true/false/nil, comments."""
    i = 0
    n = len(text)

    def skip():
        nonlocal i
        while i < n:
            if text[i].isspace():
                i += 1
            elif text.startswith("--", i):
                if text.startswith("--[[", i):
                    end = text.find("]]", i)
                    i = n if end < 0 else end + 2
                else:
                    end = text.find("\n", i)
                    i = n if end < 0 else end + 1
            else:
                return

    def string():
        nonlocal i
        q = text[i]
        i += 1
        out = []
        while i < n and text[i] != q:
            c = text[i]
            if c == "\\" and i + 1 < n:
                nxt = text[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n"}.get(nxt, nxt))
                i += 2
                continue
            out.append(c)
            i += 1
        i += 1
        return "".join(out)

    def long_string():
        nonlocal i
        m = re.match(r"\[(=*)\[", text[i:])
        close = "]" + m.group(1) + "]"
        start = i + m.end()
        end = text.find(close, start)
        i = end + len(close)
        return text[start:end]

    def value():
        nonlocal i
        skip()
        c = text[i]
        if c == "{":
            return table()
        if c in "\"'":
            return string()
        if c == "[" and re.match(r"\[=*\[", text[i:]):
            return long_string()
        m = re.match(r"-?(0x[0-9a-fA-F]+|\d+\.?\d*(e[+-]?\d+)?|\.\d+(e[+-]?\d+)?)", text[i:], re.I)
        if m:
            i += m.end()
            tok = m.group(0)
            if "x" in tok.lower():
                return int(tok, 16)
            return float(tok) if any(ch in tok for ch in ".eE") else int(tok)
        for word, val in (("true", True), ("false", False), ("nil", None)):
            if text.startswith(word, i):
                i += len(word)
                return val
        raise LuaError(f"beklenmeyen karakter {c!r} (konum {i})")

    def table():
        nonlocal i
        i += 1
        out: dict[Any, Any] = {}
        auto = 1
        while True:
            skip()
            if text[i] == "}":
                i += 1
                break
            if text[i] == "[" and not re.match(r"\[=*\[", text[i:]):
                i += 1
                key = value()
                skip()
                i += 1  # ]
                skip()
                i += 1  # =
                out[key] = value()
            else:
                m = re.match(r"([A-Za-z_]\w*)\s*=(?!=)", text[i:])
                if m:
                    i += m.end()
                    out[m.group(1)] = value()
                else:
                    out[auto] = value()
                    auto += 1
            skip()
            if text[i] in ",;":
                i += 1
        return out

    skip()
    m = re.match(r"([A-Za-z_]\w*)\s*=", text[i:])
    if m:
        i += m.end()
    return value()


def lua_list(t: Any) -> list:
    if isinstance(t, dict):
        return [t[k] for k in sorted(k for k in t if isinstance(k, int))]
    return list(t or [])


@dataclass
class Waypoint:
    name: str
    x: float
    y: float
    alt_ft: float
    speed_kt: float | None = None
    kind: str = ""


@dataclass
class MissionInfo:
    name: str
    aircraft: str
    group: str
    frequency_mhz: float | None
    waypoints: list[Waypoint] = field(default_factory=list)
    description: str = ""
    theatre: str = ""

    def legs(self) -> list[tuple[Waypoint, float, float]]:
        """(waypoint, distance in nm from the previous point, true bearing in degrees)."""
        out = []
        prev = None
        for wp in self.waypoints:
            if prev is None:
                out.append((wp, 0.0, 0.0))
            else:
                dx, dy = wp.x - prev.x, wp.y - prev.y  # x is north, y is east
                out.append((wp, math.hypot(dx, dy) / NM, (math.degrees(math.atan2(dy, dx)) + 360) % 360))
            prev = wp
        return out


def read_mission(path: Path) -> MissionInfo:
    with zipfile.ZipFile(path) as z:
        mission = parse_lua_table(z.read("mission").decode("utf-8", errors="replace"))
        dictionary: dict = {}
        for name in z.namelist():
            if name.endswith("l10n/DEFAULT/dictionary"):
                dictionary = parse_lua_table(z.read(name).decode("utf-8", errors="replace"))
    resolve = lambda v: dictionary.get(v, v) if isinstance(v, str) else (v or "")  # noqa: E731
    found = None
    for side in ("blue", "red", "neutrals"):
        for country in lua_list(mission.get("coalition", {}).get(side, {}).get("country")):
            for cat in ("plane", "helicopter"):
                for group in lua_list(country.get(cat, {}).get("group")):
                    for unit in lua_list(group.get("units")):
                        if unit.get("skill") in ("Client", "Player"):
                            found = found or (group, unit, unit.get("skill") == "Player")
                            if unit.get("skill") == "Player":
                                found = (group, unit, True)
    if found is None:
        raise ValueError("no player or client aircraft found in the mission")
    group, unit, _is_player = found
    wps = []
    for i, p in enumerate(lua_list(group.get("route", {}).get("points"))):
        name = resolve(p.get("name")) or (spoken("Takeoff") if i == 0 else f"WP{i}")
        wps.append(Waypoint(str(name), float(p.get("x", 0)), float(p.get("y", 0)), float(p.get("alt", 0)) / FT,
                            float(p["speed"]) / 0.514444 if p.get("speed") else None, str(p.get("type", ""))))
    return MissionInfo(name=Path(path).stem, aircraft=str(unit.get("type", "")), group=str(resolve(group.get("name"))),
                       frequency_mhz=float(group["frequency"]) if group.get("frequency") else None, waypoints=wps,
                       description=str(resolve(mission.get("descriptionText", "")))[:1500],
                       theatre=str(mission.get("theatre", "")))


def latest_mission(dirs: list[Path] | None = None) -> Path | None:
    """Most recently modified .miz (missions and multiplayer downloads)."""
    cands = []
    for d in dirs if dirs is not None else saved_games_dirs():
        for sub in ("Missions", "MissionEditor", "Tracks", "Temp"):
            p = d / sub
            if p.is_dir():
                cands += list(p.rglob("*.miz"))
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


COMMANDS = {"gear": "teker", "hook": "kanca", "flaps": "flap", "airbrake": "hava freni", "flare": "flare",
            "chaff": "chaff"}
_seq = [0]


def send_command(kind: str, arg: str, port: int = PORT) -> int:
    """Send to the script's command channel (port+1); the returned sequence number arrives in its "ack" field."""
    if kind == "cmd" and arg not in COMMANDS:
        raise ValueError(f"command not allowed: {arg}")
    _seq[0] += 1
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(f"{_seq[0]}|{kind}|{arg}".encode(), ("127.0.0.1", port + 1))
    return _seq[0]


def current_mission(dirs: list[Path] | None = None) -> Path | None:
    """Currently loaded mission: the last "loadMission <path>" line in the DCS log (usually Temp/DCS/tempMission.miz).
    If the log has none, the most recently modified mission."""
    for d in dirs if dirs is not None else saved_games_dirs():
        log = d / "Logs" / "dcs.log"
        try:
            with log.open("rb") as f:
                f.seek(max(0, log.stat().st_size - 2_000_000))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        found = re.findall(r"loadMission (.+?\.miz)\s*$", tail, re.M)
        if found and Path(found[-1].strip()).is_file():
            return Path(found[-1].strip())
    return latest_mission(dirs)


def briefing_text(m: MissionInfo) -> str:
    """Pre-takeoff spoken briefing: mission, aircraft, route (first two legs and total), radio, description start."""
    parts = [spoken("Mission: {name}.", name=m.name) + (" " + spoken("You're in the {group} group.", group=m.group)
                                                         if m.group else "")]
    legs = m.legs()
    if len(legs) > 1:
        total = sum(d for _wp, d, _brg in legs[1:])
        parts.append(spoken("The route has {n} points, {nm} miles in total.", n=len(legs) - 1, nm=f"{total:.0f}"))
        for wp, dist, brg in legs[1:3]:
            alt = f"{wp.alt_ft:,.0f}".replace(",", "." if speech_language() == "tr" else ",")
            parts.append(spoken("{name}: {deg} degrees, {nm} miles, {alt} feet.", name=wp.name, deg=f"{brg:03.0f}",
                                nm=f"{dist:.0f}", alt=alt))
    if m.frequency_mhz:
        parts.append(spoken("Group frequency {mhz}.", mhz=f"{m.frequency_mhz:g}"))
    desc = re.sub(r"\s+", " ", m.description or "").strip()
    if desc:
        first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0]
        parts.append(spoken("Mission description: {text}", text=first[:220]))
    return " ".join(parts)


# ---------------------------------------------------------------- commands
def dcs_cmd(argv: list[str]) -> int:
    """alice dcs install | uninstall | status | kneeboard [mission.miz]"""
    from rich.console import Console

    console = Console(highlight=False)
    sub = argv[0] if argv else "status"
    dirs = saved_games_dirs()
    if sub in ("install", "uninstall") and not dirs:
        console.print("[red]" + _("DCS Saved Games folder not found") + "[/] " + _("(DCS must have been opened once)"))
        return 1
    if sub == "install":
        from alice.config import load_config
        from alice.sims.wingmen import settings as wingmen_settings

        wport, wnames = wingmen_settings(load_config().data.get("sims", {}) or {})
        for d in install(dirs, wingmen_port=wport, wingmen=wnames):
            console.print("[green]" + _("installed:") + f"[/] {d / 'Scripts' / 'alice_export.lua'}")
            console.print("[green]" + _("installed:") + f"[/] {d / 'Scripts' / 'Hooks' / HOOK.name} " + _("(wingman commands)"))
        console.print(_("Restart DCS. The data is handled while the core is running (alice core run)."))
        return 0
    if sub == "uninstall":
        for d in uninstall(dirs):
            console.print("[green]" + _("removed:") + f"[/] {d}")
        return 0
    if sub == "kneeboard":
        from alice.sims import kneeboard as kb

        path = Path(argv[1]) if len(argv) > 1 else latest_mission(dirs)
        info = read_mission(path) if path else None
        written = kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        console.print("[green]" + _("{n} pages written", n=len(written)) + "[/]" + (f" ({info.name})" if info else ""))
        return 0
    for d in dirs:
        installed = (d / "Scripts" / "alice_export.lua").exists()
        console.print(f"{d}: " + ("[green]" + _("installed") + "[/]" if installed else "[yellow]" + _("not installed (alice dcs install)") + "[/]"))
    if not dirs:
        console.print("[yellow]" + _("DCS Saved Games folder not found") + "[/]")
    m = latest_mission(dirs)
    if m:
        console.print(_("last mission: {path}", path=m))
    return 0
