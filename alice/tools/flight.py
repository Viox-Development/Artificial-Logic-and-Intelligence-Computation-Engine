"""Flight tool (one tool, light on context): live status, last debrief, checklists, kneeboard."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

ACTIONS = ["status", "cockpit", "brief", "alert_home", "alert_alt", "alert_fuel", "alerts_clear", "last_debrief", "checklist_list", "checklist_start", "checklist_next", "checklist_prev",
           "checklist_stop", "kneeboard_build", "kneeboard_note", "kneeboard_clear", "kneeboard_show"]


class Flight(Tool):
    name = "flight"
    description = ("DCS / War Thunder flight: live state, spoken alerts, checklists, kneeboard.\n"
                   "- When asked about speed, altitude, fuel, heading, distance to base or weapons, call status EVERY "
                   "TIME; don't repeat an old answer.\n"
                   "- NEVER make up information that status doesn't have or marks as UNKNOWN (BİLİNMİYOR), and don't "
                   "guess it from general knowledge; say 'the game doesn't provide this'.\n"
                   "- Only promise alerts that alert_home/alt/fuel can set up ('tell me at X miles' -> alert_home).\n"
                   "- Requests like 'gear down', 'drop flares', 'set the radio to 251' -> cockpit (DCS only).\n"
                   "- During a checklist by voice, read only the returned step; on 'ok/continue' ('tamam/devam') call "
                   "checklist_next.")
    parameters = obj({
        "action": {"type": "string", "enum": ACTIONS,
                   "description": "status: live fuel, minutes left, bearing/distance to base, how many degrees to "
                                  "turn, nearest enemies/RWR | cockpit: apply command in DCS | brief: read the loaded "
                                  "DCS mission's briefing | alert_home: tell me when nm miles from base | alert_alt: "
                                  "tell me at ft altitude | alert_fuel: tell me when fuel drops to kg | alerts_clear "
                                  "| last_debrief: debrief of the last flight | checklist_list | checklist_start: "
                                  "start the name list | checklist_next/prev/stop | kneeboard_note: note text | "
                                  "kneeboard_build: build a kneeboard from the mission | kneeboard_show | "
                                  "kneeboard_clear."},
        "name": {"type": "string", "description": "For checklist_start: the list name, in Turkish: kalkış (takeoff), "
                                                  "kalkış sonrası (after takeoff), iniş (landing), iniş sonrası "
                                                  "(after landing), motor çalıştırma (engine start), motor arızası "
                                                  "(engine failure), savaş girişi (fight's on), savaş çıkışı (fight's "
                                                  "off)."},
        "text": {"type": "string", "description": "For kneeboard_note: the note."},
        "mission": {"type": "string", "description": "For kneeboard_build: path to the .miz. Empty: the last mission."},
        "nm": {"type": "number", "description": "For alert_home: nautical miles."},
        "ft": {"type": "number", "description": "For alert_alt: feet."},
        "kg": {"type": "number", "description": "For alert_fuel: kilograms."},
        "command": {"type": "string", "enum": ["gear_up", "gear_down", "flaps", "airbrake", "hook", "flare", "chaff",
                                               "radio"],
                    "description": "For cockpit. flaps/airbrake/hook toggle; radio: set to the mhz frequency."},
        "mhz": {"type": "number", "description": "For cockpit radio: frequency, e.g. 251 or 124.5."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action", "")
        return WRITE if a in ("kneeboard_build", "kneeboard_note", "kneeboard_clear") else READ

    def run(self, ctx: ToolContext, action: str, name: str = "", text: str = "", mission: str = "",
            nm: float | None = None, ft: float | None = None, kg: float | None = None, command: str = "",
            mhz: float | None = None) -> ToolResult:
        if action in ("cockpit", "brief"):
            from alice.sims import hub

            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            if action == "brief":
                hub.ACTIVE._briefed = ""
                return ToolResult(hub.ACTIVE.brief_mission() or "yüklü görev bulunamadı")
            if not command:
                raise ToolError("command gerekli")
            return ToolResult(hub.ACTIVE.cockpit(command, _number(mhz)))
        if action.startswith("alert"):
            from alice.sims import hub

            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            if action == "alerts_clear":
                return ToolResult(hub.ACTIVE.clear_alerts())
            name, value, call = {"alert_home": ("nm", nm, hub.ACTIVE.alert_home),
                                 "alert_alt": ("ft", ft, hub.ACTIVE.alert_altitude),
                                 "alert_fuel": ("kg", kg, hub.ACTIVE.alert_fuel)}[action]
            value = _number(value)
            if not value or value <= 0:
                raise ToolError(f"{name} gerekli")
            return ToolResult(call(value))
        if action.startswith("checklist_"):
            return self._checklist(ctx, action[len("checklist_"):], name)
        if action.startswith("kneeboard_"):
            return self._kneeboard(action[len("kneeboard_"):], mission, text)
        return self._status(action)

    # ----------------------------------------------------------- status
    @staticmethod
    def _status(action: str) -> ToolResult:
        from alice.sims import hub
        from alice.sims.flight import Debrief, flights_dir

        if action == "status":
            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            return ToolResult(hub.ACTIVE.status())
        if hub.ACTIVE is not None and hub.ACTIVE.last_debrief:
            d, page = hub.ACTIVE.last_debrief
            return ToolResult(f"{d.summary()}\nRapor: {page}")
        files = sorted(flights_dir().glob("*.json")) if flights_dir().is_dir() else []
        if not files:
            return ToolResult("kayıtlı uçuş yok")
        d = Debrief(**json.loads(files[-1].read_text(encoding="utf-8")))
        return ToolResult(f"{d.summary()}\nRapor: {files[-1].with_suffix('.html')}")

    # -------------------------------------------------- checklists
    @staticmethod
    def _checklist(ctx: ToolContext, action: str, name: str) -> ToolResult:
        from alice.sims.checklists import Runner, find, load_all

        if action == "list":
            return ToolResult("\n".join(f"{cl.key}: {cl.title} ({len(cl.steps)} adım)" for cl in load_all().values())
                              or "kontrol listesi yok")
        if action == "start":
            cl = find(name)
            if cl is None:
                raise ToolError(f"liste bulunamadı: {name}")
            ctx.state["checklist"] = Runner(cl)
            return ToolResult(f"{cl.title}. {ctx.state['checklist'].current()}")
        run = ctx.state.get("checklist")
        if run is None:
            raise ToolError("açık kontrol listesi yok (önce checklist_start)")
        if action == "stop":
            ctx.state.pop("checklist")
            return ToolResult("kontrol listesi kapatıldı")
        text = {"next": run.next, "prev": run.prev}[action]()
        if run.done:
            ctx.state.pop("checklist")
        return ToolResult(text)

    # -------------------------------------------------------- kneeboard
    @staticmethod
    def _kneeboard(action: str, mission: str, text: str) -> ToolResult:
        from alice.sims import kneeboard as kb
        from alice.sims.dcs import latest_mission, read_mission, saved_games_dirs

        if action == "show":
            return ToolResult("\n".join(f"{n['time']} {n['text']}" for n in kb.load_notes()) or "not yok")
        if action == "note":
            if not text:
                raise ToolError("text gerekli")
            kb.add_note(text)
        elif action == "clear":
            kb.clear_notes()
        dirs = saved_games_dirs()
        if not dirs:
            raise ToolError("DCS Saved Games klasörü bulunamadı")
        info = None
        path = Path(mission).expanduser() if mission else latest_mission(dirs)
        if path is not None and path.exists():
            try:
                info = read_mission(path)
            except (ValueError, KeyError, OSError) as e:
                if action == "build":
                    raise ToolError(f"görev okunamadı: {e}") from e
        written = kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        what = f"görev: {info.name}, {len(info.waypoints)} waypoint" if info else "görev yok"
        return ToolResult(f"kneeboard güncellendi ({len(written)} sayfa; {what}).")


class DcsWingman(Tool):
    name = "dcs_wingman"
    description = ("Voice orders to friendly AI wingmen in a DCS single-player mission: attack/suppress, rejoin, hold "
                   "(orbit), return to base. E.g. 'İkinci, sağdaki SAM'i bastır' (Two, suppress the SAM on the "
                   "right), 'Üçüncü eve dön' (Three, go home), 'hepsi toplan' (all, rejoin).\n"
                   "- Put the user's words in command as they are; ALICE parses them. Or fill in "
                   "verb/index/target/direction.\n"
                   "- Order: the player is the lead (1); İkinci (Two) = the nearest wingman, Üçüncü (Three) = the "
                   "next, 0 = all.\n"
                   "- The result shows up as text on screen in the game (e.g. 'İkinci: SAM'e saldırıyor'); here it "
                   "only says it was sent. Nothing happens if DCS is closed or you are on someone else's multiplayer "
                   "server.")
    parameters = obj({
        "command": {"type": "string", "description": "The user's words, e.g. 'İkinci, sağdaki SAM'i bastır'. If "
                                                     "given, empty fields are taken from it."},
        "verb": {"type": "string", "enum": ["attack", "rejoin", "hold", "rtb"],
                 "description": "attack: attack a target | rejoin: follow the player | hold: orbit where it is | rtb: "
                                "land at the nearest friendly base."},
        "index": {"type": "integer", "description": "Wingman order: 2 = İkinci/Two (nearest), 3 = Üçüncü/Three, 0 = "
                                                    "all."},
        "target": {"type": "string", "enum": ["sam", "air", "ground", "ship", "any"],
                   "description": "For attack: target type: sam (air defense), air (aircraft), ground, ship, any."},
        "direction": {"type": "string", "enum": ["right", "left", "front", "behind", "any"],
                      "description": "For attack: the target's direction relative to the player's nose."},
    })
    risk = READ

    def run(self, ctx: ToolContext, command: str = "", verb: str = "", index: Any = None, target: str = "",
            direction: str = "") -> ToolResult:
        from alice.sims import wingmen as wm

        cmd = wm.parse(command) if command else None
        if cmd is None:
            if not verb:
                raise ToolError("komut anlaşılmadı: verb (attack/rejoin/hold/rtb) ver ya da command'ı netleştir")
            cmd = wm.Command(verb)
        if verb:
            cmd.verb = verb
        n = _number(index)
        if n is not None:
            cmd.index = int(n)
        cmd.target = target or cmd.target
        cmd.direction = direction or cmd.direction
        port, _ = wm.settings((ctx.config.data.get("sims", {}) if ctx and ctx.config else {}) or {})
        try:
            ok = wm.send(cmd, port)
        except ValueError as e:
            raise ToolError(str(e)) from e
        if not ok:
            return ToolResult(f"gönderilemedi ({cmd.describe()}): yerel UDP hatası")
        return ToolResult(f"gönderildi: {cmd.describe()}. Sonuç DCS'te ekranda yazı olarak görünür.")


TOOLS = [Flight(), DcsWingman()]


def _number(value: Any) -> float | None:
    """The model sometimes sends a number as text ("10", "12,5", "10 mil" = 10 miles); convert it, else None."""
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value)
    m = re.search(r"-?\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*(?![.,]?\d)", text)  # "5.000", "12,000" (thousands separators)
    if m:
        return float(re.sub(r"[.,]", "", m.group()))
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None
