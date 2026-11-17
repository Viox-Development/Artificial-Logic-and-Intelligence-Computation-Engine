"""Flight companion.

The F-16's own voice warnings already exist; ALICE does not repeat them. Instead it watches like a flight buddy:
  - Fuel: if the base is known, joker/bingo (by amount); if not, the remaining time from the average of the last minutes
  - Approach: short feedback on AoA and sink rate when gear is down, low and slow
  - Long periods of high G, chaff/flare running low
  - Short review after landing (+ heart rate if watch data is available)
  - Reminders set by the pilot: "10 bin feet'e gelince söyle" (tell me at 10,000 feet),
    "yakıt 1500 kiloya inince söyle" (tell me when fuel drops to 1500 kg)
  - On aircraft without a ground proximity warning: fast approach to the ground, flaps left open, empty external tanks

Independent of the source (DCS, War Thunder): all of them produce FlightState, FlightCompanion consumes it.
Units are SI: m, m/s, kg, degrees.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

KT = 0.514444    # 1 knot = m/s
FT = 0.3048
NM = 1852.0


# Turkish locative suffix for clock hours: 1'deki, 2'deki, 3'teki, 4'teki, 5'teki, 6'daki, 7'deki, 8'deki, 9'daki,
# 10'daki, 11'deki, 12'deki (set by last vowel and hard consonant of the number). "Saat 3'deki" (at clock 3) was wrong.
_CLOCK_EK = {1: "'deki", 2: "'deki", 3: "'teki", 4: "'teki", 5: "'teki", 6: "'daki", 7: "'deki", 8: "'deki",
             9: "'daki", 10: "'daki", 11: "'deki", 12: "'deki"}


_CLOCK_DEN = {1: "'den", 2: "'den", 3: "'ten", 4: "'ten", 5: "'ten", 6: "'dan", 7: "'den", 8: "'den", 9: "'dan",
              10: "'dan", 11: "'den", 12: "'den"}


def clock_from(clock: object) -> str:
    """"Saat 3'ten kilit" (Turkish ablative suffix; "3'den" was wrong)."""
    try:
        c = int(clock)
    except (TypeError, ValueError):
        return f"{clock}'den"
    return f"{c}{_CLOCK_DEN.get(c, chr(39) + 'den')}"


def clock_loc(clock: object) -> str:
    try:
        c = int(clock)
    except (TypeError, ValueError):
        return f"{clock}'deki"
    return f"{c}{_CLOCK_EK.get(c, chr(39) + 'deki')}"


@dataclass
class FlightState:
    t: float                         # simulation time (s)
    source: str = "dcs"
    aircraft: str = ""
    ias: float | None = None         # m/s
    tas: float | None = None
    alt: float | None = None         # meters above sea level
    agl: float | None = None         # meters above ground (None if unknown)
    vv: float | None = None          # vertical speed m/s
    aoa: float | None = None
    g: float | None = None
    fuel_kg: float | None = None
    burn_kgs: float | None = None    # instantaneous fuel burn kg/s (if known)
    chaff: int | None = None
    flare: int | None = None
    gear: float | None = None        # 0 = up, 1 = down
    x: float | None = None           # map coordinate m (DCS: x north, z east)
    z: float | None = None
    heading: float | None = None     # nose direction, degrees (true north)
    extra: dict[str, Any] = field(default_factory=dict)  # extra indicators from the game (see describe_extra)
    wall: float = field(default_factory=time.time)


@dataclass
class Profile:
    name: str
    internal_fuel_kg: float = 3000
    reserve_kg: float = 450          # fuel to be left at landing
    cruise_kg_nm: float = 5.0        # fuel per nautical mile on the way home (bingo calculation)
    aoa_on_speed: tuple[float, float] = (11.0, 15.0)
    approach_ias_max: float = 100.0  # m/s; below this with gear down it counts as an approach
    g_warn: float = 7.0
    g_warn_seconds: float = 8.0      # short hard turns are normal in a dogfight: only sustained high G counts
    chaff_low: int = 20
    flare_low: int = 10
    has_gpws: bool = False           # the aircraft has its own ground proximity warning: ALICE does not repeat it
    auto_flaps: bool = False         # automatic flaps: a "flap açık kaldı" (flaps left open) reminder is pointless


PROFILES = {
    "F-16C_50": Profile("F-16C", internal_fuel_kg=3249, reserve_kg=450, cruise_kg_nm=4.0, aoa_on_speed=(11.0, 15.0),
                        approach_ias_max=95, g_warn=7.5, has_gpws=True, auto_flaps=True),
    "FA-18C_hornet": Profile("F/A-18C", internal_fuel_kg=4900, reserve_kg=680, cruise_kg_nm=6.0,
                             aoa_on_speed=(7.4, 8.8),
                             approach_ias_max=80, g_warn=6.5, has_gpws=True, auto_flaps=True),
    "default": Profile("uçak"),
}


def profile_for(aircraft: str) -> Profile:
    base = PROFILES.get(aircraft) or PROFILES["default"]
    learned = learned_cruise(base.name if base.name != "uçak" else aircraft)
    if learned is None:
        return base
    from dataclasses import replace

    # so a few bad samples don't make bingo nonsense: clamp between half and double the manually entered value
    return replace(base, cruise_kg_nm=min(max(learned, base.cruise_kg_nm / 2), base.cruise_kg_nm * 2))


# --------------------------------------------------------------- cruise fuel learned from flights
# Fuel per mile used to be estimated by hand (F-16 4, F/A-18 6, others 5). The real value is extracted from the
# level, calm sections of recorded flights (G ~1, altitude near constant, no fuel jumps), and bingo uses it.
CRUISE_MIN_S = 60      # a cruise segment must last at least this long
CRUISE_MAX_S = 300
CRUISE_MIN_PARTS = 3   # with fewer than this, the manually entered value is kept
_learned_cache: dict[str, Any] = {}


def cruise_parts(series: dict[str, list]) -> list[float]:
    """Fuel per nautical mile (kg/nm) over a flight's cruise segments; IAS is roughly converted to TAS (+2% per 1000 ft)."""
    t, alt, g, ias, fuel = (series.get(k) or [] for k in ("t", "alt_ft", "g", "ias_kt", "fuel_kg"))
    out: list[float] = []
    start = 0
    nm = 0.0

    def close(i: int) -> None:
        if t[i] - t[start] >= CRUISE_MIN_S and nm > 3 and fuel[start] - fuel[i] > 0:
            out.append((fuel[start] - fuel[i]) / nm)

    n = min(len(t), len(alt), len(g), len(ias), len(fuel))
    for i in range(1, n):
        dt = t[i] - t[i - 1]
        ok = (fuel[i] is not None and fuel[i - 1] is not None and 0 < dt <= 10 and 0.6 <= (g[i] or 0) <= 1.6
              and (ias[i] or 0) > 150 and 0 <= fuel[i - 1] - fuel[i] <= 5 * dt  # excludes tank jettison or AB
              and abs(alt[i] - alt[start]) < 2000)
        if not ok:
            close(i - 1)
            start, nm = i, 0.0
            continue
        nm += ias[i] * (1 + 0.02 * max(alt[i], 0) / 1000) * dt / 3600
        if t[i] - t[start] >= CRUISE_MAX_S:
            close(i)
            start, nm = i, 0.0
    if n > 1 and start < n - 1:  # flight ended while cruising
        close(n - 1)
    return out


def learn_cruise(folder: Path | None = None) -> dict[str, dict[str, float]]:
    """Cruise fuel per aircraft from all recorded flights; written to ~/.alice/flights/cruise.json."""
    folder = folder or flights_dir()
    parts: dict[str, list[float]] = {}
    for f in sorted(folder.glob("*.json")):
        if f.name == "cruise.json":
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("aircraft") and d.get("series"):
            parts.setdefault(d["aircraft"], []).extend(cruise_parts(d["series"]))
    result = {}
    for ac, xs in parts.items():
        xs = sorted(x for x in xs[-200:] if 0.5 <= x <= 40)  # recent flights
        if len(xs) >= CRUISE_MIN_PARTS:
            # Dogfights and afterburner segments push the value up; the trip home is flown economically: the lower third
            result[ac] = {"kg_nm": round(xs[len(xs) // 3], 2), "parts": len(xs)}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cruise.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    _learned_cache.clear()
    return result


def learned_cruise(aircraft: str, folder: Path | None = None) -> float | None:
    path = (folder or flights_dir()) / "cruise.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _learned_cache.get("mtime") != (path, mtime):
        try:
            _learned_cache.update(mtime=(path, mtime), data=json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
    entry = (_learned_cache.get("data") or {}).get(aircraft)
    return float(entry["kg_nm"]) if entry else None


def fmt_min(m: float) -> str:
    m = max(0, int(round(m)))
    return f"{m} dakika" if m < 120 else f"{m // 60} saat {m % 60} dakika"


@dataclass
class Debrief:
    aircraft: str
    source: str
    start_wall: float
    end_wall: float
    duration_s: float
    max_g: float
    max_ias_kt: float
    max_alt_ft: float
    fuel_used_kg: float | None
    min_fuel_kg: float | None
    approach_on_speed_pct: float | None
    max_sink_final_ms: float | None
    touchdown_vv_ms: float | None
    chaff_used: int | None
    flare_used: int | None
    hr: dict[str, float] | None = None
    series: dict[str, list] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"Uçuş {fmt_min(self.duration_s / 60)} sürdü."]
        parts.append(f"En yüksek {self.max_g:.1f} G, en yüksek hız {self.max_ias_kt:.0f} knot.")
        if self.fuel_used_kg:  # 0: born in the air or after a refuel, not worth mentioning
            parts.append(f"{self.fuel_used_kg:.0f} kilo yakıt kullandın, en az {self.min_fuel_kg:.0f} kiloya düştün.")
        if self.approach_on_speed_pct is not None:
            q = "iyiydi" if self.approach_on_speed_pct >= 70 else "biraz dalgalıydı" if self.approach_on_speed_pct >= 40 \
                else "hız kontrolü zayıftı"
            parts.append(f"Son yaklaşmanın yüzde {self.approach_on_speed_pct:.0f}'i on speed'di, {q}.")
        if self.touchdown_vv_ms is not None:
            v = abs(self.touchdown_vv_ms)
            feel = "yumuşaktı" if v < 1.5 else "normaldi" if v < 3 else "sertti"
            parts.append(f"Teker koyarken alçalma hızın saniyede {v:.1f} metre, {feel}.")
        if self.hr:
            parts.append(f"Nabzın ortalama {self.hr['avg']:.0f}, en yüksek {self.hr['max']:.0f}.")
        return " ".join(parts)


# ------------------------------------------------------------------------------- flight coaching
def load_history(folder: Path | None = None, exclude: float | None = None, limit: int = 80) -> list[dict[str, Any]]:
    """Recorded flights, oldest to newest (without series data). `exclude`: start time of the flight just saved."""
    out = []
    for f in sorted((folder or flights_dir()).glob("2*.json"))[-limit:]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if exclude is not None and abs(float(d.get("start_wall") or 0) - exclude) < 1:
            continue
        d.pop("series", None)
        out.append(d)
    return out


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def coach(d: Debrief, history: list[dict[str, Any]]) -> str:
    """Compares with previous flights in at most two sentences: progress/regression and one target for the next flight.

    Landings and approaches are compared with all flights; fuel and personal records only with the same aircraft.
    Stays silent if there is little history."""
    out: list[str] = []
    lands = [abs(h["touchdown_vv_ms"]) for h in history if h.get("touchdown_vv_ms") is not None][-5:]
    if d.touchdown_vv_ms is not None and len(lands) >= 3:
        v, avg = abs(d.touchdown_vv_ms), _avg(lands)
        if v < min(lands):
            out.append(f"Son {len(lands)} inişinin en yumuşağıydı (ortalaman {avg:.1f}).")
        elif v < avg - 0.3:
            out.append(f"Son {len(lands)} inişin ortalaması {avg:.1f}, bu {v:.1f}: gelişiyorsun.")
        elif v > avg + 0.5:
            out.append(f"Son {len(lands)} inişin ortalaması {avg:.1f}, bu {v:.1f}; bir dahakine son 50 feet'te "
                       "alçalmayı biraz daha kes.")
    appr = [h["approach_on_speed_pct"] for h in history if h.get("approach_on_speed_pct") is not None][-5:]
    if d.approach_on_speed_pct is not None and len(appr) >= 3 and len(out) < 2:
        p, avg = d.approach_on_speed_pct, _avg(appr)
        if p >= avg + 15:
            out.append(f"Yaklaşmada on speed oranın ortalamanın ({avg:.0f}) epey üstündeydi.")
        elif p <= avg - 15:
            out.append(f"Yaklaşma ortalamanın altındaydı (yüzde {avg:.0f} yerine {p:.0f}); hedef: AoA'yı on speed'de "
                       "tutup gazla düzelt.")
    same = [h for h in history if h.get("aircraft") == d.aircraft and (h.get("duration_s") or 0) >= 60]
    if len(out) < 2 and len(same) >= 3:
        if d.duration_s > max(h["duration_s"] for h in same):
            out.append(f"Bu uçakla en uzun uçuşundu ({fmt_min(d.duration_s / 60)}).")
        elif d.fuel_used_kg and all(h.get("fuel_used_kg") for h in same[-5:]):
            rate = d.fuel_used_kg / (d.duration_s / 60)
            avg = _avg([h["fuel_used_kg"] / (h["duration_s"] / 60) for h in same[-5:]])
            if rate < avg * 0.8:
                out.append(f"Dakikada {rate:.0f} kilo yaktın, bu uçaktaki ortalamandan ({avg:.0f}) belirgin az.")
            elif rate > avg * 1.3:
                out.append(f"Dakikada {rate:.0f} kilo yaktın, ortalaman {avg:.0f}: art yakıcıda çok kaldın.")
    return " ".join(out[:2])


class FlightCompanion:
    """Watches the FlightState stream and passes what to say via say(text, severity, emotion).

    If show(kind, **fields) is given, threats also go to the on-screen HUD as arrows and edge glows; the flight code
    does not know the HUD (see alice.hud.client.show)."""

    def __init__(self, say: Callable[[str, str, str], None], hr_stats: Callable[[float, float], dict | None] | None = None,
                 hr_now: Callable[[], float | None] | None = None,
                 on_debrief: Callable[[Debrief], None] | None = None,
                 approach_every: float = 4.0, show: Callable[..., Any] | None = None):
        self.say = say
        self.show = show or (lambda kind, **kw: None)
        self.hr_stats = hr_stats or (lambda a, b: None)
        self.hr_now = hr_now or (lambda: None)
        self.on_debrief = on_debrief or (lambda d: None)
        self.approach_every = approach_every
        # Threat sound times are not reset when the enemy dies or respawns: in War Thunder, the same ships and the same
        # anti-aircraft guns were counted again as "menzilindesin" on every spawn.
        self.threat_mem: dict[str, float] = {}
        self.coach_flags: set[str] = set()  # death patterns (alice/sims/coach.py): one tip per match
        self.calm = False                   # tilt (alice/mood.py): talk less
        self.reset()

    def reset(self) -> None:
        self.phase = "ground"            # ground | air | landing_roll
        self.profile = PROFILES["default"]
        self.aircraft = ""
        self.fuel_hist: deque[tuple[float, float]] = deque()
        self.burn_hist: deque[tuple[float, float]] = deque()
        self.home: tuple[float, float] | None = None
        self.home_alert_nm: float | None = None
        self.warn_seen: set[str] = set()
        self.heat_flags: set[str] = set()
        self.gear_last_warn = -1e9
        self.gear_fast_said = False
        self.wp_called: float | None = None
        self.weapons_last: dict[str, int] = {}
        self.gun_start: float | None = None
        self.gun_flag = False
        self.fuel_flags: set[str] = set()
        self.fuel_cand: dict[str, float] = {}     # when the warning condition was first seen (short spikes are ignored)
        self.endurance0: float | None = None      # total endurance at first measurement (min): took off low?
        self.said_at: dict[str, float] = {}       # key -> sim time it was last said
        self.info_last = -1e9
        self.g_since: float | None = None
        self.g_last_warn = -1e9
        self.cm_last: dict[str, int] = {}
        self.cm_flags: set[str] = set()
        self.appr_last_say = -1e9
        self.appr_last_kind = ""
        self.appr_cand, self.appr_cand_since = "", 0.0
        self.appr_samples: list[bool] = []
        self.max_sink_final: float | None = None
        self.slow_since: float | None = None
        self.last_state: FlightState | None = None
        self.takeoff: FlightState | None = None
        self.stats = {"max_g": 0.0, "max_ias": 0.0, "max_alt": 0.0, "min_fuel": None}
        self._g_prev = 0.0
        self.series: dict[str, list] = {"t": [], "wall": [], "alt_ft": [], "g": [], "ias_kt": [], "fuel_kg": []}
        self._series_t = -1e9
        self.restricted_said = False
        self._touchdown_vv: float | None = None
        self.alt_alert_ft: float | None = None   # one-time altitude reminder set by the pilot
        self.alt_alert_above: bool | None = None  # whether we were above the target when it was set
        self.fuel_alert_kg: float | None = None
        self.ground_last_warn = -1e9
        self.ground_armed = True                 # is the ground warning armed (silent until a safe return)
        self.ground_safe_since: float | None = None
        self.flaps_since: float | None = None
        self.flaps_said = False
        self.ext_last: float | None = None
        self.ext_said = False
        self.threat_seen: dict[str, float] = {}   # RWR emitter/lock/missile key -> last time it was seen
        self.threat_said: dict[str, float] = {}
        self.bandit_close = False                 # closest enemy within 5 km (re-armed at 7 km)
        self.nose_since: float | None = None      # War Thunder: start of enemy nose-on (can fire missiles)
        self.sam_inside: set[str] = set()         # air defense types we are within range of
        self.coached: set[str] = set()            # tips given in this session
        self.hud_on: dict[str, float] = {}        # HUD arrow id -> when this sighting started
        self.hud_seen: dict[str, float] = {}      # last seen (if it drops out and returns it is a new sighting)
        self.hud_sent: dict[str, float] = {}

    # ---------------------------------------------------------------- inputs
    def restricted(self) -> None:
        if not self.restricted_said:
            self.restricted_said = True
            self.say("Bu sunucu uçuş verisi paylaşımını kısıtlıyor. Yakıt ve yaklaşma takibi yapamam ama "
                     "kontrol listeleri, kneeboard ve sesli komutlar çalışıyor.", "info", "calm")

    def session_end(self) -> None:
        """Data cut off (mission ended). If still airborne, review the flight anyway."""
        if self.phase == "air" and self.last_state is not None:
            self._finish(self.last_state, landed=False)
        self.reset()

    def update(self, s: FlightState) -> None:
        if s.aircraft and s.aircraft != self.aircraft:
            self.aircraft = s.aircraft
            self.profile = profile_for(s.aircraft)
        prev = self.last_state
        if prev is not None and s.t + 5 < prev.t:  # mission restarted
            self.reset()
            prev = None
        self.last_state = s
        self._phase(s)
        if self.phase != "air":
            return
        self._record(s)
        self._home_alert(s)
        self._fuel(s)
        self._g(s)
        self._countermeasures(s)
        self._approach(s)
        self._warnings(s)
        self._engine_heat(s)
        self._gear_check(s)
        self._waypoint(s)
        self._weapons(s)
        self._pilot_alerts(s)
        self._ground(s)
        self._flaps(s)
        self._ext_tanks(s)
        self._contacts(s)
        self._hud(s)

    # ------------------------------------------------------------------- threats
    SIX_REPEAT = 45.0       # enemy-behind warning at most this often (in a turn fight it never stopped at 20 s intervals)
    SIX_RANGE = 2000.0
    MERGE_RANGE = 5000.0
    LOST_AFTER = 10.0       # a lock/missile not seen for this long counts as over
    NOSE_ASPECT = 12.0      # War Thunder: enemy nose narrowly on us at this angle, "can fire a missile"
    NOSE_RANGE = (2500.0, 14000.0)
    NOSE_HOLD = 2.0         # must last this long (the map refreshes once a second; a single reading can mislead)
    NOSE_REPEAT = 60.0
    SAM_REPEAT = 300.0      # same type of air defense (also when it respawns in the same match)
    SAM_ANY = 45.0          # any air defense (destroyer + frigate in a naval group are a single announcement)
    MERGE_REPEAT = 90.0     # "Düşman saat 2, 3 kilometre" (Enemy at 2 o'clock, 3 km)
    THREAT_GAP = 12.0       # min gap between enemy-behind, nose-on, closing and SAM sounds (not missiles/locks)
    HUD_FOLLOW = 20.0       # after a threat is seen, the arrow follows it for at most this long (even if muted)
    HUD_EVERY = 0.5         # while following, a new direction goes to the HUD at most this often
    HUD_GAP = 5.0           # if a threat is out of view this long and returns, it counts as a new sighting

    @staticmethod
    def _km(m: float) -> str:
        return f"{m:.0f} metre" if m < 1000 else f"{m / 1000:.1f} kilometre".replace(".", ",")

    @staticmethod
    def _km_short(m: float) -> str:
        return f"{m:.0f} m" if m < 1000 else f"{m / 1000:.1f} km".replace(".", ",")

    def _point(self, t: float, clock: Any, text: str, severity: str, key: str, edge: bool = False,
               ttl: float = 4.0) -> None:
        """Also show the announced threat on the HUD: an arrow in clock direction, and an edge glow if urgent."""
        self.hud_on[key] = self.hud_seen[key] = self.hud_sent[key] = t
        self._draw(clock, text, severity, key, edge, ttl)

    def _follow(self, t: float, clock: Any, text: str, severity: str, key: str, edge: bool = False) -> None:
        """The threat continues but the sound is silent (just said, or another threat is talking): the arrow still
        shows its direction (when turning, an enemy at 6 o'clock moves to 4 o'clock). For at most HUD_FOLLOW s from
        the start of the sighting; the short-lived arrow goes out on its own once the threat disappears."""
        if t - self.hud_seen.get(key, -1e9) > self.HUD_GAP:
            self.hud_on[key] = t
        self.hud_seen[key] = t
        if t - self.hud_on[key] > self.HUD_FOLLOW or t - self.hud_sent.get(key, -1e9) < self.HUD_EVERY:
            return
        self.hud_sent[key] = t
        self._draw(clock, text, severity, key, edge, 2.5)

    def _draw(self, clock: Any, text: str, severity: str, key: str, edge: bool, ttl: float) -> None:
        try:  # a HUD error must not break the announcement
            self.show("arrow", clock=clock, text=text, severity=severity, id=key, ttl=ttl)
            if edge:
                self.show("edge", clock=clock, severity=severity, id=f"{key}.edge", ttl=ttl)
        except Exception:  # noqa: BLE001
            pass

    def _label(self, text: str, severity: str, key: str, ttl: float) -> None:
        """Short text on the HUD (such as fuel); no position is given, the HUD draws it at the top center."""
        try:  # a HUD error must not break the announcement
            self.show("label", text=text, severity=severity, id=key, ttl=ttl)
        except Exception:  # noqa: BLE001
            pass

    def _hint(self, *keys: str) -> str:
        """Short tip matching a death pattern, once per session (coaching, not an announcement)."""
        from alice.sims.coach import HINTS

        for k in keys:
            if k in self.coach_flags and k not in self.coached:
                self.coached.add(k)
                return " " + HINTS[k]
        return ""

    MISSILE_REPEAT = 8.0    # s: missile warning at most this often (re-shouted when the same missile reappeared)
    LOCK_REPEAT = 30.0      # s: lock warning at most this often

    def _alert_ok(self, t: float, kind: str) -> bool:
        """Separate limit for missile/lock sounds (outside THREAT_GAP; missiles always win). Records time if allowed."""
        mem = self.__dict__.setdefault("alert_mem", {})
        if any(v > t + 5 for v in mem.values()):  # new mission: sim time went back to the start
            mem.clear()
        every = self.MISSILE_REPEAT if kind == "missile" else self.LOCK_REPEAT
        if t - mem.get(kind, -1e9) < every:
            return False
        mem[kind] = t
        return True

    def _may_say(self, t: float, key: str, every: float) -> bool:
        """Threat sound permission: this key at most once per 'every' s, and at least THREAT_GAP from any other threat sound.
        If allowed, records the time. Missiles and locks do not go through this; they are always said."""
        m = self.threat_mem
        if any(v > t + 5 for v in m.values()):  # new mission: sim time went back to the start
            m.clear()
        gap = self.THREAT_GAP * (2 if self.calm else 1)  # talk less when tilted
        if t - m.get(key, -1e9) < every or t - m.get("threat", -1e9) < gap:
            return False
        m[key] = m["threat"] = t
        return True

    def _contacts(self, s: FlightState) -> None:
        """RWR (DCS) and mini-map (War Thunder) contacts: missile, lock, new radar, enemy behind and closing in.

        The F-16's RWR already beeps; ALICE says what it doesn't: the clock direction and the radar's name."""
        cs = s.extra.get("contacts")
        if not isinstance(cs, list):
            return
        t = s.t
        for c in cs:
            if c.get("kind") != "radar":
                continue
            base = f"{c.get('id')}|{c.get('name')}"
            for key, cond in ((f"m|{base}", c.get("missile")), (f"l|{base}", c.get("lock")), (f"s|{base}", True)):
                if not cond:
                    continue
                fresh = t - self.threat_seen.get(key, -1e9) > self.LOST_AFTER
                self.threat_seen[key] = t
                clock, name = c.get("clock"), c.get("name") or "radar"
                if not fresh:
                    if key[0] == "m":
                        self._follow(t, clock, f"FÜZE · {name}", "critical", "missile", edge=True)
                    elif key[0] == "l":
                        self._follow(t, clock, f"KİLİT · {name}", "warn", "lock")
                    continue
                if key[0] in "ml" and not self._alert_ok(t, "missile" if key[0] == "m" else "lock"):
                    # War Thunder threats are read off the screen; when they cut out and came back they counted as "new":
                    # 30 min: 16 "Füze atıldı", 13 "Kilitlendin" (7 Nov). Sound is limited; arrow and glow continue.
                    if key[0] == "m":
                        self._point(t, clock, f"FÜZE · {name}", "critical", "missile", edge=True)
                    else:
                        self._point(t, clock, f"KİLİT · {name}", "warn", "lock")
                elif key[0] == "m" and c.get("vision"):  # seen on screen: type (and sometimes direction) unknown
                    self.say(f"Füze! Saat {clock}. Chaff at, dön!" if clock else "Füze atıldı! Chaff at, dön!",
                             "critical", "urgent")
                    self._point(t, clock, "FÜZE", "critical", "missile", edge=True)
                elif key[0] == "m":
                    self.say(f"Füze! Saat {clock}, {name}. Chaff at, dön!", "critical", "urgent")
                    self._point(t, clock, f"FÜZE · {name}", "critical", "missile", edge=True)
                elif key[0] == "l":
                    self.say(f"Saat {clock_from(clock)} kilit: {name}." if clock and name != "radar" else
                             (f"Saat {clock_from(clock)} kilitlendin." if clock else "Kilitlendin!"),
                             "warn", self._urgent())
                    self._point(t, clock, f"KİLİT · {name}", "warn", "lock")
                elif name != "?" and t - self.threat_said.get(name, -1e9) > 120:
                    self.threat_said[name] = t
                    # A radar seen on screen has no type, sometimes no direction: it said "Yeni radar: radar, saat None".
                    text = (f"Yeni radar: {name}, saat {clock}." if name != "radar" and clock else
                            f"Yeni radar: {name}." if name != "radar" else
                            f"Yeni radar, saat {clock}." if clock else "")
                    if text:
                        self._info(s, text)
                break  # if there is a missile, the lock/new radar is not said separately
        self._ground_threats(s, [c for c in cs if c.get("kind") == "sam" and c.get("dist_m") is not None])
        planes = [c for c in cs if c.get("kind") == "aircraft" and c.get("dist_m") is not None]
        if not planes:
            self.bandit_close = False
            self.nose_since = None
            return
        if self._nose_on(s, planes):
            return
        near = planes[0]
        six = next((c for c in planes if c["dist_m"] <= self.SIX_RANGE and c.get("clock") in (4, 5, 6, 7, 8)), None)
        if six is not None and self._may_say(t, "six", self.SIX_REPEAT):
            self.say(f"Arkanda düşman, saat {six['clock']}, {self._km(six['dist_m'])}!"
                     + self._hint("low_turn_six", "six"), "warn", "urgent")
            self._point(t, six["clock"], f"ARKANDA · {self._km_short(six['dist_m'])}", "critical", "six", edge=True)
            self.bandit_close = True
            return
        if six is not None:
            self._follow(t, six["clock"], f"ARKANDA · {self._km_short(six['dist_m'])}", "critical", "six", edge=True)
        if near["dist_m"] <= self.MERGE_RANGE and not self.bandit_close:
            self.bandit_close = True
            if self._may_say(t, "merge", self.MERGE_REPEAT):
                self._info(s, f"Düşman saat {near['clock']}, {self._km(near['dist_m'])}.", "serious")
        elif near["dist_m"] > self.MERGE_RANGE + 2000:
            self.bandit_close = False

    def _nose_on(self, s: FlightState, planes: list[dict]) -> bool:
        """War Thunder has no RWR: an enemy with its nose on us inside missile range is the closest cue. True if said."""
        lo, hi = self.NOSE_RANGE
        hot = [c for c in planes if c.get("aspect") is not None and c["aspect"] <= self.NOSE_ASPECT
               and lo <= c["dist_m"] <= hi]
        if not hot:
            self.nose_since = None
            return False
        if self.nose_since is None:
            self.nose_since = s.t
        if s.t - self.nose_since < self.NOSE_HOLD:
            return False
        if not self._may_say(s.t, "nose", self.NOSE_REPEAT):
            c = hot[0]
            self._follow(s.t, c["clock"], f"BURNU SANA · {self._km_short(c['dist_m'])}", "warn", "nose")
            return False
        c = hot[0]
        self.say(f"Saat {clock_loc(c['clock'])} düşman burnunu sana çevirdi, {self._km(c['dist_m'])}. Füze gelebilir!",
                 "warn", self._urgent())
        self._point(s.t, c["clock"], f"BURNU SANA · {self._km_short(c['dist_m'])}", "warn", "nose")
        return True

    def _ground_threats(self, s: FlightState, sams: list[dict]) -> None:
        """Once when an enemy SAM, AA gun or missile ship enters range; re-armed after moving 3 km away."""
        near: dict[str, dict] = {}
        for c in sams:
            if c["name"] not in near or c["dist_m"] < near[c["name"]]["dist_m"]:
                near[c["name"]] = c
        for name in list(self.sam_inside):
            c = near.get(name)
            if c is None or c["dist_m"] > float(c.get("range_m") or 0) + 3000:
                self.sam_inside.discard(name)
        fresh = []
        for name, c in near.items():
            if name in self.sam_inside:
                self._follow(s.t, c["clock"], f"{name} · {self._km_short(c['dist_m'])}", "warn", f"sam:{name}")
                continue
            if c["dist_m"] > float(c.get("range_m") or 0):
                continue
            self.sam_inside.add(name)
            if s.t - self.threat_mem.get(f"sam:{name}", -1e9) >= self.SAM_REPEAT:
                fresh.append(c)
        if fresh and self._may_say(s.t, "sam", self.SAM_ANY):
            for f in fresh:  # ships entering range at the same time (naval group) are not announced separately
                self.threat_mem[f"sam:{f['name']}"] = s.t
            c = min(fresh, key=lambda f: f["dist_m"])
            name = c["name"]
            self.say(f"{name[0].upper() + name[1:]} menzilindesin: saat {c['clock']}, {self._km(c['dist_m'])}."
                     + self._hint("sam"),
                     "warn", "serious")
            self._point(s.t, c["clock"], f"{name} · {self._km_short(c['dist_m'])}", "warn", f"sam:{name}")

    def _hud(self, s: FlightState) -> None:
        """War Thunder combat log: kills you made, and hits on you."""
        for kind, who in s.extra.get("hud") or []:
            if kind == "kill":
                self.say(f"Düşürdün: {who}.", "info", "cheerful")
                try:  # a HUD error must not break the announcement
                    self.show("card", title="Düşürdün", text=str(who), severity="info", ttl=4.0, id="kill")
                except Exception:  # noqa: BLE001
                    pass
            elif kind == "fire":
                self.say(f"Yanıyorsun! {who} vurdu.", "critical", "urgent")
            elif kind == "hit":
                self.say(f"Ağır hasar aldın, {who} vurdu!", "warn", "urgent")
            elif kind == "killed":
                self.say(f"Seni {who} düşürdü.", "info", "serious")

    # ------------------------------------------------------------------- phase
    def _airborne(self, s: FlightState) -> bool:
        if s.agl is not None:
            return s.agl > 15 and (s.ias or 0) > 40
        return (s.ias or 0) > 60

    def _phase(self, s: FlightState) -> None:
        if self.phase == "ground":
            if self._airborne(s):
                self.phase = "air"
                self.takeoff = s
                if s.x is not None and s.z is not None:
                    self.home = (s.x, s.z)
            return
        stopped = (s.ias or 0) < 30 and (s.agl is None or s.agl < 5)
        if stopped:
            self.slow_since = self.slow_since if self.slow_since is not None else s.t
            if s.t - self.slow_since >= 5:
                # Stopping with the gear up is a crash, not a landing: in War Thunder the wreck stopped after each death
                # and made it say "Uçuş 2 dakika sürdü..." (1 Nov, 21 times)
                self._finish(s, landed=s.gear is None or s.gear >= 0.9)
                home = self.home
                self.reset()
                self.home = home
        else:
            self.slow_since = None

    def _record(self, s: FlightState) -> None:
        st = self.stats
        g = s.g or 0
        st["max_g"] = max(st["max_g"], min(g, self._g_prev))  # a single-sample spike (crash, death) is not counted
        self._g_prev = g
        st["max_ias"] = max(st["max_ias"], s.ias or 0)
        st["max_alt"] = max(st["max_alt"], s.alt or 0)
        if s.fuel_kg is not None:
            st["min_fuel"] = s.fuel_kg if st["min_fuel"] is None else min(st["min_fuel"], s.fuel_kg)
        if s.t - self._series_t >= 1.0:
            self._series_t = s.t
            ser = self.series
            ser["t"].append(round(s.t, 1))
            ser["wall"].append(round(s.wall, 1))
            ser["alt_ft"].append(round((s.alt or 0) / FT))
            ser["g"].append(round(s.g or 0, 2))
            ser["ias_kt"].append(round((s.ias or 0) / KT))
            ser["fuel_kg"].append(None if s.fuel_kg is None else round(s.fuel_kg))

    # ------------------------------------------------------------------- fuel
    FUEL_WINDOW = 240.0   # s: consumption is measured from the actual fuel drop over this window
    FUEL_MIN_SPAN = 60.0  # s: measurements shorter than this are not treated as estimates
    MAX_BURN = 40.0       # kg/s

    def burn_rate(self) -> float | None:
        """Average consumption over the last ~4 minutes (kg/s), from the drop in the fuel gauge.

        The game's instantaneous consumption jumps several times on afterburner (some modules count one engine twice);
        going by it, saying "X dakika kaldı" meant a warning on every maneuver. Only drops are summed:
        moving fuel from an external to an internal tank makes a small jump on the gauge; it must not skew consumption."""
        h = self.fuel_hist
        if len(h) >= 2 and h[-1][0] - h[0][0] >= self.FUEL_MIN_SPAN:
            # No engine burns more than 40 kg per second: such a drop is a tank jettison, not consumption.
            used = sum(d for a, b in zip(h, list(h)[1:])
                       if 0 < (d := a[1] - b[1]) <= self.MAX_BURN * max(b[0] - a[0], 0.2))
            if used > 0:
                return used / (h[-1][0] - h[0][0])
        if len(self.burn_hist) >= 5:  # the game's value until enough measurements build up (only for the status line)
            return sum(b for _, b in self.burn_hist) / len(self.burn_hist)
        return None

    def measured(self) -> bool:
        h = self.fuel_hist
        return len(h) >= 2 and h[-1][0] - h[0][0] >= self.FUEL_MIN_SPAN

    def home_distance(self, s: FlightState) -> float | None:
        if self.home is None or s.x is None or s.z is None:
            return None
        return math.hypot(s.x - self.home[0], s.z - self.home[1])

    def home_nav(self, s: FlightState) -> dict[str, float] | None:
        """Bearing to home (degrees from true north), distance (nm) and the turn needed (+ right, - left)."""
        if self.home is None or s.x is None or s.z is None:
            return None
        dx, dz = self.home[0] - s.x, self.home[1] - s.z  # DCS: x north, z east
        out = {"bearing": math.degrees(math.atan2(dz, dx)) % 360, "nm": math.hypot(dx, dz) / NM}
        if s.heading is not None:
            out["turn"] = (out["bearing"] - s.heading + 540) % 360 - 180
        return out

    def fuel_status(self, s: FlightState) -> dict[str, float] | None:
        burn = self.burn_rate()
        if s.fuel_kg is None or not burn or burn <= 0:
            return None
        left_min = s.fuel_kg / burn / 60
        out = {"left_min": left_min, "burn_kgs": burn}
        bingo = self.bingo_kg(s)
        if bingo is not None:
            nm = self.home_distance(s) / NM
            out.update(home_nm=nm, home_min=nm * NM / max(s.tas or s.ias or 0, 200.0) / 60, bingo_kg=bingo,
                       margin_kg=s.fuel_kg - bingo, margin_min=(s.fuel_kg - bingo) / burn / 60)
        return out

    def bingo_kg(self, s: FlightState) -> float | None:
        """Fuel needed to fly home at cruise and land with reserve. Like real bingo, it looks at the amount, not the
        instantaneous rate, so that going to afterburner does not say "eve dönme vakti". Not calculated if the
        fuel figure is an estimate (unknown aircraft, rate giving module)."""
        dist = self.home_distance(s)
        if dist is None or s.fuel_kg is None or s.extra.get("fuel_guess"):
            return None
        return self.profile.cruise_kg_nm * dist / NM + self.profile.reserve_kg

    def joker_kg(self) -> float:
        return max(300.0, 0.15 * self.profile.internal_fuel_kg)

    def _home_alert(self, s: FlightState) -> None:
        """'Tell me when 10 miles from home' (flight alert_home): say it once when the distance drops below."""
        if self.home_alert_nm is None:
            return
        nav = self.home_nav(s)
        if nav and nav["nm"] <= self.home_alert_nm:
            self.home_alert_nm = None
            self.say(f"Üsse {nav['nm']:.0f} mil kaldı.", "warn", "calm")

    def _fuel(self, s: FlightState) -> None:
        if s.fuel_kg is None:
            return
        if self.fuel_hist and s.fuel_kg > self.fuel_hist[-1][1] + 150:
            # Refueled or fuel added in the air: re-measure consumption. Warnings are kept; they re-arm on their own.
            # once it truly clears (the re-arm margins below).
            self.fuel_hist.clear()
            self.burn_hist.clear()
        self.fuel_hist.append((s.t, s.fuel_kg))
        while self.fuel_hist and s.t - self.fuel_hist[0][0] > self.FUEL_WINDOW:
            self.fuel_hist.popleft()
        if s.burn_kgs is not None and s.burn_kgs > 0:
            self.burn_hist.append((s.t, s.burn_kgs))
            while self.burn_hist and s.t - self.burn_hist[0][0] > 120:
                self.burn_hist.popleft()
        bingo = self.bingo_kg(s)
        if bingo is not None:
            self._bingo(s, bingo)
            return
        st = self.fuel_status(s) if self.measured() else None
        if not st:
            return
        left = st["left_min"]
        if self.endurance0 is None and self.takeoff is not None:
            self.endurance0 = (s.t - self.takeoff.t) / 60 + left
        pct = s.extra.get("fuel_pct")
        if isinstance(pct, (int, float)) and pct > 35:
            return  # afterburner at 3000 kg said "4 dakika yeter": no minute warning until the tank drops
        for limit, flag, sev in ((10, "low10", "warn"), (5, "low5", "critical")):
            if left > limit + 8:
                self.fuel_flags.discard(flag)  # refueled or fuel dropped back: re-arm
            if limit == 10 and (self.endurance0 or 99) < 25:
                continue  # already took off low on fuel (common in War Thunder): the 10-minute warning is just noise
            if self._held(flag, left <= limit, s.t, 20) and flag not in self.fuel_flags:
                self.fuel_flags.update({flag} | ({"low10"} if flag == "low5" else set()))
                self.say(f"Yakıt yaklaşık {fmt_min(left)} yeter.", sev, self._urgent())
                self._label(f"YAKIT ~{max(0, round(left))} dk", sev, "fuel", 6.0)

    def _bingo(self, s: FlightState, bingo: float) -> None:
        """Bingo: fuel left equals the way home plus reserve. Joker: a margin before bingo, reported once."""
        fuel, joker = s.fuel_kg, self.joker_kg()
        if fuel > bingo + joker * 1.5:
            self.fuel_flags -= {"joker", "bingo"}
        elif fuel > bingo + joker * 0.5:
            self.fuel_flags.discard("bingo")
        nav = self.home_nav(s) or {}
        where = f"üs {nav.get('nm', 0):.0f} mil, {nav.get('bearing', 0):03.0f} derece"
        if self._held("bingo", fuel <= bingo, s.t, 10) and "bingo" not in self.fuel_flags:
            self.fuel_flags.update({"bingo", "joker"})
            self.say(f"Bingo. Şimdi eve dön, {where}.", "critical", self._urgent())
        elif self._held("joker", fuel <= bingo + joker, s.t, 10) and "joker" not in self.fuel_flags:
            self.fuel_flags.add("joker")
            self.say(f"Joker. Bingo'ya {max(0.0, fuel - bingo):.0f} kilo var, {where}.", "warn", self._urgent())

    def _held(self, key: str, cond: bool, t: float, secs: float) -> bool:
        """Has the condition held continuously for at least secs seconds (tank transfers and spikes are ignored)."""
        if not cond:
            self.fuel_cand.pop(key, None)
            return False
        return t - self.fuel_cand.setdefault(key, t) >= secs

    def _urgent(self) -> str:
        """If the heart rate is high, ALICE speaks calmly but clearly, not urgently."""
        hr = self.hr_now()
        return "calm" if hr is not None and hr >= 120 else "urgent"

    # ------------------------------------------------------- automatic warnings
    OIL_MAX, WATER_MAX = 105.0, 115.0  # War Thunder: above these the engine burns (varies by aircraft)

    # Lights not counted as warnings: the autopilot kept saying "Uyarı: otopilot açık" every time it was switched on.
    LIGHTS_IGNORED = {"AutopilotOn"}
    # Lights that flash once a second in a dogfight: never on an aircraft with its own warning, otherwise once a minute.
    LIGHTS_FLICKER = {"StallSignalization"}
    WARN_REPEAT = 120.0  # s: a light that goes out and lights up again is not announced again before this time passes

    def _warnings(self, s: FlightState) -> None:
        """DCS warning/failure lights: announce a newly lit light; not every time a flashing light flashes."""
        raw = s.extra.get("warnings")
        if raw is None:
            return
        now = {w for w in str(raw).split(",") if w} - self.LIGHTS_IGNORED
        if self.profile.has_gpws:
            now -= self.LIGHTS_FLICKER
        new = sorted(now - self.warn_seen)
        self.warn_seen = now
        new = [w for w in new if s.t - self.said_at.get("light:" + w, -1e9) >= (
            60.0 if w in self.LIGHTS_FLICKER else self.WARN_REPEAT)]
        if len(new) > 1 and "MasterWarning" in new:
            new.remove("MasterWarning")  # the actual failure name is already being announced
        if new:
            for w in new:
                self.said_at["light:" + w] = s.t
            names = ", ".join(warning_name(w) for w in new[:3])
            self.say(f"Uyarı: {names}.", "critical", self._urgent())

    HEAT_REPEAT = 180.0

    def _engine_heat(self, s: FlightState) -> None:
        for key, limit, label in (("oil_temp", self.OIL_MAX, "Yağ"), ("water_temp", self.WATER_MAX, "Su")):
            v = s.extra.get(key)
            if v is None:
                continue
            # A temperature oscillating around the limit must not be announced each time: re-arm once it has cooled and
            # 3 minutes have passed.
            if v >= limit and key not in self.heat_flags and s.t - self.said_at.get(key, -1e9) >= self.HEAT_REPEAT:
                self.heat_flags.add(key)
                self.said_at[key] = s.t
                self.say(f"{label} sıcaklığı {v:.0f} derece, motor ısınıyor. Gazı azalt ya da radyatörü aç.",
                         "warn", self._urgent())
            elif v < limit - 15:
                self.heat_flags.discard(key)

    INFO_GAP = 15.0  # s: minimum gap between two informational announcements (waypoint, ammunition, tank)

    def _info(self, s: FlightState, text: str, emotion: str = "calm") -> None:
        """Minor announcement: must not stack on the previous one; if it is crowded, skip it without saying it."""
        if self.calm or s.t - self.info_last < self.INFO_GAP:  # no minor announcements while tilted
            return
        self.info_last = s.t
        self.say(text, "info", emotion)

    def _pilot_alerts(self, s: FlightState) -> None:
        """'Tell me at 10,000 feet' / 'tell me when fuel drops to 1500 kg': once, when the threshold is crossed."""
        if self.alt_alert_ft is not None and s.alt is not None:
            ft = s.alt / FT
            if self.alt_alert_above is None:
                self.alt_alert_above = ft > self.alt_alert_ft
            elif (ft <= self.alt_alert_ft) == self.alt_alert_above:
                self.say(f"{self.alt_alert_ft:,.0f} feet.".replace(",", "."), "info", "calm")
                self.alt_alert_ft = self.alt_alert_above = None
        if self.fuel_alert_kg is not None and s.fuel_kg is not None and s.fuel_kg <= self.fuel_alert_kg:
            self.say(f"Yakıt {self.fuel_alert_kg:.0f} kiloya indi.", "warn", "calm")
            self.fuel_alert_kg = None

    GROUND_TTI = 5.0     # s: warn if we will hit the ground within this time
    GROUND_REPEAT = 8.0  # s: minimum gap between two warnings (even if re-armed)
    GROUND_SAFE_AGL = 300.0  # m: above this, or
    GROUND_SAFE_VV = -2.0    # m/s: descending slower than this counts as safe
    GROUND_REARM = 3.0       # s: time spent in the safe state before the warning is re-armed

    def _ground(self, s: FlightState) -> None:
        """Fast approach to the ground (on aircraft without their own GPWS). Silent during the landing approach.
        Must not repeat when low in a fight (13 times in 10 minutes in War Thunder): after saying it, the aircraft
        does not repeat until it has been in a safe state for at least GROUND_REARM s."""
        if self.profile.has_gpws or s.agl is None or s.vv is None:
            return
        if s.agl > self.GROUND_SAFE_AGL or s.vv > self.GROUND_SAFE_VV:
            self.ground_safe_since = s.t if self.ground_safe_since is None else self.ground_safe_since
            if s.t - self.ground_safe_since >= self.GROUND_REARM:
                self.ground_armed = True
        else:
            self.ground_safe_since = None
        if s.vv >= -8 or s.agl > 600:
            return
        if (s.gear or 0) > 0.9 and (s.ias or 1e9) < self.profile.approach_ias_max * 1.2:
            return  # gear down, at approach speed: coming in to land
        if s.agl / -s.vv < self.GROUND_TTI and self.ground_armed \
                and s.t - self.ground_last_warn >= self.GROUND_REPEAT:
            self.ground_last_warn, self.ground_armed, self.ground_safe_since = s.t, False, None
            self.say("Yere yaklaşıyorsun, burnu kaldır!", "critical", "urgent")

    def _flaps(self, s: FlightState) -> None:
        """Flaps left open after takeoff: if fast and high for 20 s, remind once."""
        flaps = s.extra.get("flaps")
        if self.profile.auto_flaps or flaps is None:
            return
        if flaps < 0.1:
            self.flaps_since, self.flaps_said = None, False
            return
        cruising = (s.gear is None or s.gear < 0.1) and (s.ias or 0) > self.profile.approach_ias_max * 1.4 \
            and (s.agl is None or s.agl > 600)
        if not cruising:
            self.flaps_since = None
            return
        self.flaps_since = s.t if self.flaps_since is None else self.flaps_since
        if s.t - self.flaps_since >= 20 and not self.flaps_said:
            self.flaps_said = True
            self.say("Flaplar hâlâ açık, toplamayı unutma.", "warn", "serious")

    def _ext_tanks(self, s: FlightState) -> None:
        """If the external tanks drained slowly (not jettisoned), say so once."""
        ext = s.extra.get("fuel_ext_kg")
        if ext is None:
            return
        last, self.ext_last = self.ext_last, ext
        if ext > 50:
            self.ext_said = False
        elif last is not None and 0.5 < last < 60 and ext <= 0.5 and not self.ext_said:
            self.ext_said = True
            self._info(s, "Dış tanklar boşaldı.")

    def _gear_check(self, s: FlightState) -> None:
        if s.gear is not None and s.ias is not None and (s.gear < 0.1 or s.ias < 140):
            self.gear_fast_said = False
        if s.gear is None or s.ias is None or s.t - self.gear_last_warn < 30:
            return
        agl = s.agl if s.agl is not None else s.extra.get("radalt", 0) * FT if s.extra.get("radalt") else None
        if s.gear < 0.1 and agl is not None and agl < 300 and s.ias < 85 and (s.vv or 0) < -2:
            self.gear_last_warn = s.t
            self.say("Teker kapalı! Alçak ve yavaşsın.", "critical", "urgent")
        elif s.gear > 0.9 and s.ias > 150 and not self.gear_fast_said:
            self.gear_last_warn = s.t
            self.gear_fast_said = True  # once, until the gear retracts or we slow down
            self.say(f"Teker açık ve hızın {s.ias / 0.514444:.0f} knot, tekeri kapat ya da yavaşla.", "warn", "serious")

    def _waypoint(self, s: FlightState) -> None:
        e = s.extra
        if e.get("wp_x") is None or e.get("wp_z") is None:
            return
        bn = _bearing_nm(s, e["wp_x"], e["wp_z"])
        num = e.get("wp_num")
        if bn is None:
            return
        if bn[1] <= 2 and self.wp_called != num:
            self.wp_called = num
            label = f"{int(num)} numaralı waypoint'e" if num is not None else "Waypoint'e"
            self._info(s, f"{label} {bn[1]:.0f} mil.")

    def _weapons(self, s: FlightState) -> None:
        now = parse_weapons(s.extra.get("weapons"))
        if s.extra.get("weapons") is not None or self.weapons_last:
            for name, before in self.weapons_last.items():
                after = now.get(name, 0)
                if after < before:
                    if after == 1:
                        self._info(s, f"Son {name}.", "serious")
                    elif after == 0:
                        self._info(s, f"{name} bitti.", "serious")
            self.weapons_last = now
        gun = s.extra.get("gun")
        if gun is not None:
            if self.gun_start is None or gun > self.gun_start:
                self.gun_start, self.gun_flag = gun, False
            elif self.gun_start > 0 and gun <= self.gun_start * 0.2 and not self.gun_flag:
                self.gun_flag = True
                self._info(s, f"Top mermisi azaldı, {int(gun)} kaldı.", "serious")

    # --------------------------------------------------------------------- G
    def _g(self, s: FlightState) -> None:
        p = self.profile
        if s.g is None or s.source == "warthunder":
            # War Thunder's Ny value is noisy (48 G at death) and the game shows blackouts itself: in a dogfight
            # saying "11 G üstündesin" every 3 minutes was only annoying.
            return
        if s.g >= p.g_warn:
            self.g_since = s.t if self.g_since is None else self.g_since
            if s.t - self.g_since >= p.g_warn_seconds and s.t - self.g_last_warn > 180:
                self.g_last_warn = s.t
                self.say(f"Uzun süredir {s.g:.0f} G üstündesin, biraz rahatla.", "warn", "serious")
        else:
            self.g_since = None

    # ---------------------------------------------------------------- chaff/flare
    def _countermeasures(self, s: FlightState) -> None:
        for kind, name, low in (("chaff", "Chaff", self.profile.chaff_low), ("flare", "Flare", self.profile.flare_low)):
            n = getattr(s, kind)
            if n is None:
                continue
            last = self.cm_last.get(kind)
            self.cm_last[kind] = n
            if last is None:
                continue
            if n > last:  # refilled
                self.cm_flags.discard(f"{kind}_low")
                self.cm_flags.discard(f"{kind}_out")
                continue
            if n == 0 and last > 0 and f"{kind}_out" not in self.cm_flags:
                self.cm_flags.add(f"{kind}_out")
                self.say(f"{name} bitti.", "warn", "serious")
            elif 0 < n <= low < last and f"{kind}_low" not in self.cm_flags:
                self.cm_flags.add(f"{kind}_low")
                self.say(f"{name} azaldı, {n} kaldı.", "warn", "serious")

    # --------------------------------------------------------------- approach
    def in_approach(self, s: FlightState) -> bool:
        low = s.agl is not None and 1.0 < s.agl < 600  # approach ends once the wheels touch the ground
        slow = 30 < (s.ias or 1e9) < self.profile.approach_ias_max
        # Speeding up with gear still down just after takeoff is not an approach: aircraft without radar altitude (War Thunder)
        # "Hızlısın." (You're fast) was said after every takeoff (1 Nov, 21 times)
        departing = (self.takeoff is not None and s.t - self.takeoff.t < 60) or (s.vv is not None and s.vv > 3)
        return (s.gear or 0) > 0.9 and slow and (low or s.agl is None) and not departing

    APPR_MARGIN = 2.0   # degrees: don't say "hızlı/yavaş" (fast/slow) until out of the on-speed band by this much
    APPR_STABLE = 1.5   # s: don't say it until the state has lasted this long (momentary swings are ignored)
    APPR_REPEAT = 15.0  # s: repeat the same call no sooner than this
    APPR_GAP = 3.0      # s: minimum gap between two calls

    def _approach_kind(self, s: FlightState) -> str:
        lo, hi = self.profile.aoa_on_speed
        final = s.agl is not None and s.agl < 300
        if final and s.vv is not None and s.vv < -7:
            return "sink"
        if s.aoa > hi + self.APPR_MARGIN:
            return "slow"  # approaching stall
        if s.aoa < lo - self.APPR_MARGIN and (s.ias or 0) >= 0.85 * self.profile.approach_ias_max:
            return "fast"  # approaching the approach speed limit
        if lo <= s.aoa <= hi:
            return "on"
        return "band"  # just outside the band: nothing to say

    def _approach(self, s: FlightState) -> None:
        if not self.in_approach(s) or s.aoa is None:
            self.appr_last_kind = ""
            self.appr_cand = ""
            return
        lo, hi = self.profile.aoa_on_speed
        if s.agl is not None and s.agl < 300:
            self.appr_samples.append(lo <= s.aoa <= hi)
            if s.vv is not None:
                self.max_sink_final = max(self.max_sink_final or 0.0, -s.vv)
        kind = self._approach_kind(s)
        if kind != self.appr_cand:
            self.appr_cand, self.appr_cand_since = kind, s.t
        stable = s.t - self.appr_cand_since >= self.APPR_STABLE or kind == "sink"
        if not stable or kind == "band" or s.t - self.appr_last_say < self.APPR_GAP:
            return
        last = self.appr_last_kind
        if kind == "on":
            say_it = last in ("fast", "slow", "sink")  # only after a correction, once
        else:
            say_it = kind != last or s.t - self.appr_last_say >= self.APPR_REPEAT
        if not say_it:
            return
        text = APPROACH[kind]
        self.appr_last_say, self.appr_last_kind = s.t, kind
        self.say(text, "info", "calm" if kind == "on" else "serious")

    # ------------------------------------------------------------ post-flight
    SPEAK_DEBRIEF_S = 600  # a flight that did not end in a landing and is shorter than this is not reviewed aloud

    def _finish(self, s: FlightState, landed: bool) -> None:
        t0 = self.takeoff
        if t0 is None:
            return
        fuel_used = None
        if t0.fuel_kg is not None and s.fuel_kg is not None:
            fuel_used = max(0.0, t0.fuel_kg - s.fuel_kg)
        cm_used = {}
        for kind in ("chaff", "flare"):
            a, b = getattr(t0, kind), getattr(s, kind)
            cm_used[kind] = (a - b) if a is not None and b is not None and a >= b else None
        pct = 100 * sum(self.appr_samples) / len(self.appr_samples) if len(self.appr_samples) >= 5 else None
        touchdown = self._touchdown_vv if landed else None
        d = Debrief(
            aircraft=self.profile.name if self.profile.name != "uçak" else (self.aircraft or "uçak"),
            source=s.source, start_wall=t0.wall, end_wall=s.wall, duration_s=s.t - t0.t,  # game time (excluding pauses)
            max_g=self.stats["max_g"], max_ias_kt=self.stats["max_ias"] / KT, max_alt_ft=self.stats["max_alt"] / FT,
            fuel_used_kg=fuel_used, min_fuel_kg=self.stats["min_fuel"], approach_on_speed_pct=pct,
            max_sink_final_ms=self.max_sink_final, touchdown_vv_ms=touchdown, chaff_used=cm_used["chaff"],
            flare_used=cm_used["flare"], hr=self.hr_stats(t0.wall, s.wall), series=self.series)
        if d.duration_s < 60:
            return
        self.on_debrief(d)
        if not landed and d.duration_s < self.SPEAK_DEBRIEF_S:
            return  # saved to the log and page; don't talk after every death in War Thunder
        try:
            tip = coach(d, load_history(exclude=d.start_wall))
        except Exception:  # noqa: BLE001 - still give the review even without a comparison
            tip = ""
        self.say(d.summary() + (" " + tip if tip else ""), "info", "calm")

    def note_touchdown(self, s: FlightState) -> None:
        """Save the last vertical speed at the moment the wheels touch down (agl ~0)."""
        self._touchdown_vv = s.vv


class TouchdownTracker:
    """Catches the moment agl drops to zero and reports it to the companion (called before update)."""

    def __init__(self, companion: FlightCompanion):
        self.c = companion
        self.prev: FlightState | None = None

    def feed(self, s: FlightState) -> None:
        p = self.prev
        if (p is not None and self.c.phase == "air" and p.agl is not None and s.agl is not None
                and p.agl > 0.5 >= s.agl and p.vv is not None):
            self.c.note_touchdown(p)
        self.prev = s
        self.c.update(s)


# ---------------------------------------------------------------- report
def _bearing_nm(s: FlightState, x: float, z: float) -> tuple[float, float] | None:
    if s.x is None or s.z is None:
        return None
    dx, dz = x - s.x, z - s.z
    return math.degrees(math.atan2(dz, dx)) % 360, math.hypot(dx, dz) / NM


# Turkish names for the DCS MCP warning lights (unknown names are read as they are).
WARNING_NAMES = {
    "LeftEngineFailure": "sol motor arızası", "RightEngineFailure": "sağ motor arızası",
    "HydraulicsFailure": "hidrolik arızası", "ACSFailure": "otopilot arızası", "AutopilotFailure": "otopilot arızası",
    "AutopilotOn": "otopilot açık", "CannonFailure": "top arızası", "StallSignalization": "stall",
    "LeftMainPumpFailure": "sol yakıt pompası arızası", "RightMainPumpFailure": "sağ yakıt pompası arızası",
    "LeftWingPumpFailure": "sol kanat pompası arızası", "RightWingPumpFailure": "sağ kanat pompası arızası",
    "RadarFailure": "radar arızası", "EOSFailure": "optik sistem arızası", "MLWSFailure": "füze uyarı sistemi arızası",
    "ECMFailure": "karıştırıcı arızası", "GearFailure": "teker arızası", "MFDFailure": "ekran arızası",
    "RWSFailure": "radar uyarı alıcısı arızası", "HUDFailure": "HUD arızası", "LeftTailPlaneFailure": "sol kuyruk hasarı",
    "RightTailPlaneFailure": "sağ kuyruk hasarı", "LeftAileronFailure": "sol eleron hasarı",
    "RightAileronFailure": "sağ eleron hasarı", "CanopyOpen": "kanopi açık", "FuelTankDamage": "yakıt tankı hasarı",
    "MasterWarning": "ana uyarı", "LeftEngineFire": "sol motor yangını", "RightEngineFire": "sağ motor yangını",
}


APPROACH = {"sink": "Çok hızlı alçalıyorsun.", "slow": "Yavaşsın, stall'a yaklaşıyorsun, güç ver.",
            "fast": "Hızlısın.", "on": "On speed, iyi."}


def callouts() -> list[tuple[str, str]]:
    """Fixed phrases spoken in flight, and their tones. Audio is generated in advance and stored on disk: in flight the
    graphics card is busy with the game, so the voice would come from the cloud (1-2 s delay per warning, quota);
    cached audio plays instantly."""
    fixed = [("Yere yaklaşıyorsun, burnu kaldır!", "urgent"), ("Teker kapalı! Alçak ve yavaşsın.", "urgent"),
             ("Flaplar hâlâ açık, toplamayı unutma.", "serious"), ("Dış tanklar boşaldı.", "calm")]
    fixed += [(text, "calm" if kind == "on" else "serious") for kind, text in APPROACH.items()]
    # Single-light warnings (the most common case); most critical first.
    names = sorted(WARNING_NAMES.values(), key=lambda n: ("yangın" not in n, "hasar" in n, n))
    return fixed + [(f"Uyarı: {n}.", "urgent") for n in names]


def warning_name(key: str) -> str:
    return WARNING_NAMES.get(key) or re.sub(r"(?<!^)(?=[A-Z])", " ", key).lower()


def band_name(mhz: float) -> str:
    if 225 <= mhz <= 400:
        return "UHF"
    if 108 <= mhz < 118:
        return "NAV (VOR/ILS)"
    if 118 <= mhz <= 174:
        return "VHF"
    if 30 <= mhz < 88:
        return "FM"
    return "frekans"


def parse_weapons(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (text or "").split(","):
        m = re.match(r"\s*(.+?)\s+x(\d+)\s*$", part)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out


def describe_extra(s: FlightState) -> list[str]:
    """Turn the extra indicators into short Turkish lines (only the values the game gives for that aircraft)."""
    e = s.extra
    out: list[str] = []
    bits = []
    if e.get("mach") is not None:
        bits.append(f"Mach {e['mach']:.2f}")
    if e.get("mag_heading") is not None:
        bits.append(f"manyetik yön {e['mag_heading']:03.0f}°")
    elif s.heading is not None:
        bits.append(f"yön {s.heading:03.0f}°")
    if e.get("pitch") is not None and e.get("bank") is not None:
        bits.append(f"burun {e['pitch']:+.0f}°, yatış {e['bank']:+.0f}°")
    if s.vv is not None:
        bits.append(f"dikey hız {s.vv / FT * 60:+,.0f} ft/dk")
    if e.get("radalt") is not None:
        bits.append(f"radar irtifa {e['radalt']:,.0f} ft")
    if bits:
        out.append("  " + ", ".join(bits))
    eng = []
    if e.get("rpm") is not None:
        eng.append(f"devir %{e['rpm']:.0f}")
    if e.get("throttle") is not None:
        eng.append(f"gaz %{e['throttle']:.0f}")
    if e.get("egt") is not None:
        eng.append(f"motor sıcaklığı {e['egt']:.0f}°C")
    if e.get("oil_temp") is not None:
        eng.append(f"yağ {e['oil_temp']:.0f}°C")
    if e.get("water_temp") is not None:
        eng.append(f"su {e['water_temp']:.0f}°C")
    if e.get("fuel_pct") is not None:
        eng.append(f"yakıt %{e['fuel_pct']:.0f}")
    if eng:
        out.append("  motor: " + ", ".join(eng))
    cfg = []
    for k, label in (("flaps", "flap"), ("speedbrake", "hız freni"), ("hook", "kanca")):
        v = e.get(k)
        if v is not None and v > 0.05:
            cfg.append(f"{label} %{v * 100:.0f}" if v <= 1 else f"{label} %{v:.0f}")
    if s.gear is not None:
        cfg.append("teker aşağı" if s.gear > 0.9 else "teker yukarı" if s.gear < 0.1 else "teker hareket halinde")
    if cfg:
        out.append("  " + ", ".join(cfg))
    if e.get("wind_kt") is not None and e["wind_kt"] >= 1:
        out.append(f"  rüzgar {e['wind_from']:03.0f}° yönünden {e['wind_kt']:.0f} kt")
    if e.get("wp_x") is not None and e.get("wp_z") is not None:
        bn = _bearing_nm(s, e["wp_x"], e["wp_z"])
        if bn:
            num = f" {int(e['wp_num'])}" if e.get("wp_num") is not None else ""
            spd = s.tas or s.ias
            eta = f", varış ~{bn[1] * NM / spd / 60:.0f} dk" if spd and spd > 20 else ""
            out.append(f"  sıradaki waypoint{num}: {bn[0]:03.0f}° (gerçek), {bn[1]:.1f} nm{eta}")
    if e.get("ils_glide") is not None and e.get("ils_side") is not None and (e["ils_glide"] or e["ils_side"]):
        g, sd = e["ils_glide"], e["ils_side"]
        out.append(f"  ILS: süzülüş {'yukarıda' if g > 0 else 'aşağıda'} ({g:+.2f}), "
                   f"merkez {'sağda' if sd > 0 else 'solda'} ({sd:+.2f})")
    if e.get("weapons") or e.get("gun") is not None:
        w = e.get("weapons") or "yok"
        gun = f", top mermisi {int(e['gun'])}" if e.get("gun") is not None else ""
        out.append(f"  silahlar: {w}{gun}")
    if e.get("warnings"):
        out.append(f"  UYARI / ARIZA: {e['warnings']}")
    if e.get("radios"):
        out.append("  telsiz/seyrüsefer frekansları: " + ", ".join(f"{band_name(float(f))} {f}" for f in
                                                               str(e["radios"]).split(",") if f))
    if e.get("mode"):
        out.append(f"  sistem modu: {e['mode']}")
    return out


def flights_dir() -> Path:
    return alice_home() / "flights"


def _svg_line(xs: list[float], ys: list, w: int = 720, h: int = 140, color: str = "var(--a)", label: str = "") -> str:
    pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pts) < 2:
        return ""
    x0, x1 = pts[0][0], pts[-1][0]
    lo, hi = min(y for _, y in pts), max(y for _, y in pts)
    if hi - lo < 1e-9:  # constant value: center the line
        lo, hi = lo - 1, hi + 1
    span_x, span_y = (x1 - x0) or 1, hi - lo
    path = " ".join(f"{'M' if i == 0 else 'L'}{(x - x0) / span_x * (w - 50) + 45:.1f},"
                    f"{h - 18 - (y - lo) / span_y * (h - 30):.1f}" for i, (x, y) in enumerate(pts))
    return (f'<figure><figcaption>{label}</figcaption><svg viewBox="0 0 {w} {h}" role="img" aria-label="{label}">'
            f'<text x="0" y="14" class="ax">{hi:.0f}</text><text x="0" y="{h - 18}" class="ax">{lo:.0f}</text>'
            f'<line x1="45" y1="{h - 18}" x2="{w - 5}" y2="{h - 18}" class="base"/>'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.8"/></svg></figure>')


def save_debrief(d: Debrief, hr_series: list[tuple[float, float]] | None = None, out: Path | None = None) -> Path:
    out = out or flights_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(d.start_wall))
    (out / f"{stamp}.json").write_text(json.dumps(asdict(d), ensure_ascii=False), encoding="utf-8")
    ser = d.series
    charts = [_svg_line(ser["t"], ser["alt_ft"], label="İrtifa (ft)"), _svg_line(ser["t"], ser["g"], label="G"),
              _svg_line(ser["t"], ser["ias_kt"], label="Hız (knot)"),
              _svg_line(ser["t"], ser["fuel_kg"], label="Yakıt (kg)")]
    if hr_series and ser.get("wall"):
        # Heart rate comes with wall-clock time; move it onto the flight's game-time axis.
        w0, t0 = ser["wall"][0], ser["t"][0]
        charts.append(_svg_line([t0 + (t - w0) for t, _ in hr_series], [v for _, v in hr_series], color="var(--hr)",
                                label="Nabız"))
    rows = [("Süre", fmt_min(d.duration_s / 60)), ("En yüksek G", f"{d.max_g:.1f}"),
            ("En yüksek hız", f"{d.max_ias_kt:.0f} kt"), ("En yüksek irtifa", f"{d.max_alt_ft:,.0f} ft")]
    if d.fuel_used_kg is not None:
        rows.append(("Kullanılan yakıt", f"{d.fuel_used_kg:.0f} kg"))
    if d.approach_on_speed_pct is not None:
        rows.append(("Yaklaşma on speed", f"%{d.approach_on_speed_pct:.0f}"))
    if d.touchdown_vv_ms is not None:
        rows.append(("Teker koyma", f"{abs(d.touchdown_vv_ms):.1f} m/s ({abs(d.touchdown_vv_ms) / FT * 60:.0f} fpm)"))
    for k, n in (("Chaff", d.chaff_used), ("Flare", d.flare_used)):
        if n:
            rows.append((f"Kullanılan {k.lower()}", str(n)))
    if d.hr:
        rows.append(("Nabız", f"ort {d.hr['avg']:.0f}, en yüksek {d.hr['max']:.0f}"))
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    when = time.strftime("%d.%m.%Y %H:%M", time.localtime(d.start_wall))
    page = out / f"{stamp}.html"
    page.write_text(f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Uçuş {when}</title><style>
:root{{--bg:#faf9f7;--fg:#222;--mut:#777;--line:#e4e1dc;--a:#c6613f;--hr:#3f7fc6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b1a;--fg:#eee;--mut:#999;--line:#333;--a:#d97757;--hr:#6fa3e0}}}}
body{{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:24px 16px;max-width:760px}}
th{{text-align:left;color:var(--mut);font-weight:400;padding:4px 16px 4px 0}} figure{{margin:18px 0}}
figcaption{{color:var(--mut);font-size:13px}} svg{{width:100%;height:auto}} .ax{{fill:var(--mut);font-size:11px}}
.base{{stroke:var(--line)}}
</style></head><body><h1>{d.aircraft} · {when}</h1><p>{d.summary()}</p><table>{table}</table>{''.join(charts)}
</body></html>""", encoding="utf-8")
    return page
