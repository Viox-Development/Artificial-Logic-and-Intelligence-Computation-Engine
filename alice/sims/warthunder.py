"""War Thunder: reads the game's own published local indicators (http://127.0.0.1:8111).

/state gives flight values ("IAS, km/h", "H, m", "Ny", "AoA, deg", "Mfuel, kg"...), /indicators the aircraft type.
If the game is not open, the connection is refused; then it waits silently.
/map_obj.json gives the objects the player sees on the mini-map (enemy and friendly aircraft, and self), /map_info.json
the map's size in meters. From these, the bearing and distance of the nearest enemies are derived
(altitude is not on the map).

The game does not publish its RWR, lock or incoming missile through this interface (checked live on November 1: not in
/state, /indicators or /map_obj.json). Threats are derived from what is available instead:
  * the nose direction of an enemy aircraft on the map: if its nose points at you and you are in range,
    it can fire a missile
  * enemy SAMs, anti-aircraft guns and missile-armed ships on the map: announced when you enter their range
  * /hudmsg combat log: what the player shot down, and what shot them down or set them on fire. The player's name is
    learned on the first death (the "X (Su-30MKK)'i düşürdü" line right before dying) or given by
    [sims] warthunder_nick.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from typing import Any, Callable

import httpx

from alice.i18n import spoken
from alice.sims.flight import FlightState

URL = "http://127.0.0.1:8111"


def _f(d: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def to_state(state: dict[str, Any], indicators: dict[str, Any], t: float) -> FlightState | None:
    if not state.get("valid"):
        return None
    kmh = lambda v: None if v is None else v / 3.6  # noqa: E731
    gear = _f(state, "gear, %")
    return FlightState(t=t, source="warthunder", aircraft=str(indicators.get("type") or ""),
                       ias=kmh(_f(state, "IAS, km/h")), tas=kmh(_f(state, "TAS, km/h")), alt=_f(state, "H, m"),
                       agl=_f(indicators, "radio_altitude"), vv=_f(state, "Vy, m/s"), aoa=_f(state, "AoA, deg"),
                       g=_f(state, "Ny"), fuel_kg=_f(state, "Mfuel, kg"), gear=None if gear is None else gear / 100,
                       heading=_f(indicators, "compass", "compass1"), extra=_extra(state, indicators))


def _extra(st: dict[str, Any], ind: dict[str, Any]) -> dict[str, Any]:
    """Extra values from the game's local indicators (what is visible in the cockpit/HUD; no other-player info)."""
    fuel, fuel0 = _f(st, "Mfuel, kg"), _f(st, "Mfuel0, kg")
    pct = lambda v: None if v is None else v / 100  # noqa: E731
    e: dict[str, Any] = {
        "mach": _f(st, "M"),
        "pitch": _f(ind, "aviahorizon_pitch"),
        "bank": _f(ind, "aviahorizon_roll"),
        "radalt": None if _f(ind, "radio_altitude") is None else _f(ind, "radio_altitude") / 0.3048,
        "throttle": _f(st, "throttle 1, %"),
        "rpm": _f(st, "RPM throttle 1, %"),
        "oil_temp": _f(st, "oil temp 1, C"),
        "water_temp": _f(st, "water temp 1, C"),
        "flaps": pct(_f(st, "flaps, %")),
        "speedbrake": pct(_f(st, "airbrake, %")),
        "fuel_pct": None if not fuel0 or fuel is None else 100 * fuel / fuel0,
    }
    if e["rpm"] is None and _f(st, "RPM 1") is not None:
        e["rpm_abs"] = _f(st, "RPM 1")
    return {k: v for k, v in e.items() if v is not None}


def _enemy(color: str) -> bool:
    """On the mini-map enemies are red (like "#f40C00"), friends blue/green, the player yellow/white."""
    c = (color or "").lstrip("#")
    try:
        rr, gg, bb = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except (ValueError, IndexError):
        return False
    return rr > 180 and gg < 110 and bb < 110


# Enemy air defense on the map: icon -> (name spoken, approximate effective range in m)
GROUND_THREATS = {
    "SAM": ("SAM", 18000.0), "SPAA": ("anti-aircraft gun", 6000.0), "MissileDestroyer": ("missile destroyer", 18000.0),
    "Cruiser": ("cruiser", 15000.0), "Frigate": ("frigate", 12000.0), "Destroyer": ("destroyer", 8000.0),
}


def contacts(objs: list[dict[str, Any]], info: dict[str, Any], limit: int = 6,
             heading: float | None = None) -> list[dict[str, Any]]:
    """Mini-map objects -> nearest enemy aircraft (clock relative to the player's direction of travel), then enemy
    air defense within range (kind="sam").

    For aircraft, "aspect": the angle between the enemy's nose and the direction from it to you
    (0 = nose pointing straight at you)."""
    from alice.sims.dcs import clock_of

    me = next((o for o in objs if o.get("icon") == "Player"), None)
    lo, hi = info.get("map_min") or [0, 0], info.get("map_max") or [0, 0]
    w, h = float(hi[0]) - float(lo[0]), float(hi[1]) - float(lo[1])
    if me is None or w <= 0 or h <= 0:
        return []
    hx, hy = float(me.get("dx") or 0), float(me.get("dy") or 0)
    if heading is None:  # no compass: use the arrow's direction on the map
        heading = math.degrees(math.atan2(hx * w, -hy * h)) % 360 if (hx or hy) else 0.0
    planes, ground = [], []
    for o in objs:
        kind, icon = o.get("type"), str(o.get("icon") or "")
        if o is me or not _enemy(str(o.get("color") or "")):
            continue
        if kind != "aircraft" and not (kind == "ground_model" and icon in GROUND_THREATS):
            continue
        ex, ey = (float(o.get("x") or 0) - float(me["x"])) * w, (float(o.get("y") or 0) - float(me["y"])) * h
        dist = math.hypot(ex, ey)
        bearing = math.degrees(math.atan2(ex, -ey)) % 360
        if kind == "aircraft":
            aspect = None
            ox, oy = float(o.get("dx") or 0), float(o.get("dy") or 0)
            if (ox or oy) and dist > 0:
                nose = math.degrees(math.atan2(ox * w, -oy * h))
                aspect = abs((nose - (bearing + 180) + 180) % 360 - 180)
            planes.append({"id": "", "clock": clock_of(bearing - heading), "dist_m": dist, "kind": "aircraft",
                           "name": icon or spoken("aircraft"), "lock": False, "missile": False, "aspect": aspect})
        else:
            name, rng = GROUND_THREATS[icon]
            name = spoken(name)
            if dist <= rng + 15000:
                ground.append({"id": "", "clock": clock_of(bearing - heading), "dist_m": dist, "kind": "sam",
                               "name": name, "range_m": rng, "lock": False, "missile": False})
    planes.sort(key=lambda c: c["dist_m"])
    ground.sort(key=lambda c: c["dist_m"] - c["range_m"])
    return planes[:limit] + ground[:3]


_HUD = re.compile(r"^(?P<a>.+?) \((?P<at>[^)]*)\) (?P<verb>shot down|destroyed|critically damaged|severely damaged|"
                  r"set afire) (?P<b>.+?)(?: \((?P<bt>[^)]*)\))?$")


def _norm(t: str) -> str:
    """'␗Su-30MKK' / 'su_30mkk' -> 'su30mkk'."""
    return re.sub(r"[^0-9a-z]", "", (t or "").lower())


def parse_hud(msg: str) -> dict[str, str] | None:
    """Combat log line -> {a, at, verb, b, bt}. Missile/bomb kills ("... destroyed weapons/...") are not counted."""
    m = _HUD.match((msg or "").strip())
    if not m or m["b"].startswith("weapons/"):
        return None
    return {k: (m[k] or "").strip() for k in ("a", "at", "verb", "b", "bt")}


def _type_name(t: str) -> str:
    return re.sub(r"^[^0-9A-Za-z]+", "", t or "").strip() or spoken("enemy")


def _attacker(d: dict[str, str]) -> str:
    """The attacker's type. For AI air defense the type is generic, like "TEL" (launcher); then the name
    ("SAMP/T") is clearer."""
    t = _type_name(d["at"])
    return d["a"].strip() if t.upper() in ("TEL", "DÜŞMAN", "ENEMY", "AI") and d["a"].strip() else t


def hud_events(msgs: list[dict[str, Any]], nick: str) -> list[tuple[str, str]]:
    """Combat log entries involving the player -> [(type, opposing side's type)]:
    kill | hit | fire | killed (shot you down)."""
    out: list[tuple[str, str]] = []
    if not nick:
        return out
    for m in msgs:
        d = parse_hud(str(m.get("msg") or ""))
        if not d:
            continue
        if d["a"] == nick and d["verb"] in ("shot down", "destroyed") and d["b"] != nick:
            out.append(("kill", _type_name(d["bt"] or d["b"])))
        elif d["b"] == nick and d["a"] != nick:
            if d["verb"] in ("shot down", "destroyed"):
                out.append(("killed", _attacker(d)))  # the real cause of death: it was never read before
            elif d["verb"] in ("critically damaged", "severely damaged"):
                out.append(("hit", _attacker(d)))
            elif d["verb"] == "set afire":
                out.append(("fire", _attacker(d)))
    return out


def learn_nick(recent: list[dict[str, Any]], aircraft: str) -> str | None:
    """If a single player was shot down by this aircraft type in the records just before death,
    that player is the one."""
    me = _norm(aircraft)
    if not me:
        return None
    names = set()
    for m in recent:
        d = parse_hud(str(m.get("msg") or ""))
        if d and d["verb"] in ("shot down", "destroyed") and d["bt"]:
            t = _norm(d["bt"])
            if t and (t.startswith(me) or me.startswith(t)):
                names.add(d["b"])
    return names.pop() if len(names) == 1 else None


def _nick_path():
    from alice.config import alice_home

    return alice_home() / "warthunder.json"


def load_nick() -> str:
    try:
        return str(json.loads(_nick_path().read_text(encoding="utf-8")).get("nick") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def save_nick(nick: str) -> None:
    try:
        _nick_path().write_text(json.dumps({"nick": nick}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # the nickname is asked again next time


class WarThunderPoller:
    def __init__(self, on_state: Callable[[FlightState], None], on_end: Callable[[], None] = lambda: None,
                 on_metrics: Callable[[list], None] = lambda rows: None, hz: float = 5,
                 client: httpx.Client | None = None, running: Callable[[], bool] = lambda: True,
                 nick: str | None = None):
        self.on_state = on_state
        self.on_end = on_end
        self.on_metrics = on_metrics
        self.interval = 1 / hz
        self.client = client or httpx.Client(base_url=URL, timeout=1.0)
        self.running = running  # is the game open (from the process list); not polled while closed
        self.active = False
        self._stop = threading.Event()
        self._metrics_at = 0.0
        self._map_at = 0.0
        self._map_info: dict[str, Any] = {}
        self._contacts: list[dict[str, Any]] = []
        self._t0 = time.monotonic()
        self._fixed_nick = bool(nick)  # if set in the config, do not replace it with a learned one
        self.nick = nick or load_nick()
        self._hud_last: int | None = None  # last combat log entry read; None: first read (old ones are not announced)
        self._hud_recent: deque = deque(maxlen=40)  # (time received, entry): for learning the player's name on death
        self._hud_new: list[tuple[str, str]] = []
        self._aircraft = ""

    def start(self) -> "WarThunderPoller":
        threading.Thread(target=self._loop, daemon=True, name="alice-warthunder").start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> FlightState | None:
        try:
            st = self.client.get("/state").json()
            ind = self.client.get("/indicators").json()
        except (httpx.HTTPError, ValueError):
            if self.active:
                self.active = False
                self.on_end()
            return None
        s = to_state(st, ind, time.monotonic() - self._t0)
        if s is None:
            if self.active:  # hangar / death / match end
                self.active = False
                self._death()
                self.on_end()
            return None
        self.active = True
        self._aircraft = s.aircraft or self._aircraft
        if time.monotonic() - self._map_at >= 1.0:
            self._map_at = time.monotonic()
            self._contacts = self._read_map(s.heading)
            self._read_hud()
        s.extra["contacts"] = self._contacts
        if self._hud_new:
            s.extra["hud"], self._hud_new = self._hud_new, []
        self.on_state(s)
        if time.monotonic() - self._metrics_at >= 1.0:
            self._metrics_at = time.monotonic()
            self.on_metrics([(f"wt.{k}", v, None) for k, v in (
                ("ias_kmh", s.ias and s.ias * 3.6), ("alt_m", s.alt), ("g", s.g), ("fuel.kg", s.fuel_kg))
                if v is not None])
        return s

    def _read_map(self, heading: float | None = None) -> list[dict[str, Any]]:
        try:
            objs = self.client.get("/map_obj.json").json()
            if not self._map_info.get("valid") or self._map_info.get("_at", 0) < time.monotonic() - 60:
                self._map_info = {**self.client.get("/map_info.json").json(), "_at": time.monotonic()}
        except (httpx.HTTPError, ValueError):
            return []
        return contacts(objs, self._map_info, heading=heading) if isinstance(objs, list) else []

    def _read_hud(self) -> None:
        try:
            r = self.client.get("/hudmsg", params={"lastEvt": 0, "lastDmg": self._hud_last or 0}).json()
        except (httpx.HTTPError, ValueError):
            return
        dmg = [m for m in (r.get("damage") or []) if isinstance(m, dict)] if isinstance(r, dict) else []
        first = self._hud_last is None
        fresh = [m for m in dmg if first or int(m.get("id") or 0) > (self._hud_last or 0)]
        self._hud_last = max([int(m.get("id") or 0) for m in dmg] + [self._hud_last or 0])
        now = time.monotonic()
        for m in fresh:
            self._hud_recent.append((now, m))
        if not first:
            self._hud_new += hud_events(fresh, self.nick)

    def _death(self) -> None:
        """The player died or left: if the name is unknown, learn it from the combat log of the last few seconds."""
        if self._fixed_nick:
            return
        self._read_hud()
        now = time.monotonic()
        nick = learn_nick([m for at, m in self._hud_recent if now - at <= 8], self._aircraft)
        if nick and nick != self.nick:
            self.nick = nick
            save_nick(nick)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.running():
                self.poll_once()
                self._stop.wait(self.interval)
            else:
                if self.active:
                    self.active = False
                    self.on_end()
                self._stop.wait(5)
