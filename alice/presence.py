"""Greeting upon returning to the computer (Jarvis's "Welcome back sir, two things today" moment).

The core checks keyboard/mouse idle time on every turn. When returning after a long absence (default 90 min),
if there is something to say, it gives a short spoken briefing:
  * due follow-ups ("How was your math exam?"), reminders, unfinished work
  * remaining calendar for today (if Google Calendar is connected)
  * unread important emails (if Gmail is connected)
If there is nothing to say, it just gives a short greeting on the first return; otherwise it stays silent.
If the first greeting is before noon, it switches to the morning summary: weather, today's schedule ([brief] days), remaining notes from yesterday,
and pending issues from the health report are also added. When the computer is turned on in the morning (core just started, absence
could not be measured), this summary is also spoken once; the day it was spoken is kept in a file so ALICE does not repeat it even if restarted. The text is written quickly by the model with your
personality (fallback sentence if no model). Then you can reply for a few seconds without saying "Hey Alice"; the conversation
is also written to memory, and Live sees it in "previous conversation". If flight/game mode silences notifications, the greeting is also silenced.

config.toml:
  [greeting]
  enabled = true
  away_minutes = 90      # considered away after this long
  min_gap_hours = 3      # minimum between two greetings
  quiet_hours = "00:30-07:00"   # no greeting during these hours
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable

DEFAULTS = {"enabled": True, "away_minutes": 90, "min_gap_hours": 3, "quiet_hours": "00:30-07:00",
            "morning_until": 13}  # morning summary if first greeting is before this hour


def idle_seconds() -> float | None:
    """Time elapsed since last keyboard/mouse usage. None if unmeasurable."""
    try:
        if sys.platform == "win32":
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
                return None
            millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime  # type: ignore[attr-defined]
            return max(0.0, (millis & 0xFFFFFFFF) / 1000.0)
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "HIDIdleTime" in line:
                    return int(line.split("=")[-1].strip()) / 1e9
            return None
        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) / 1000.0 if out.returncode == 0 else None
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def settings(cfg) -> dict[str, Any]:
    return {**DEFAULTS, **((getattr(cfg, "data", None) or {}).get("greeting") or {})}


def part_of_day(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "Günaydın"
    if 12 <= h < 18:
        return "İyi günler"
    return "İyi akşamlar"


def _away_text(seconds: float) -> str:
    h = seconds / 3600
    if h >= 20:
        return "dünden beri"
    if h >= 1.5:
        return f"yaklaşık {round(h)} saattir"
    return f"{int(seconds // 60)} dakikadır"


# ------------------------------------------------------------------------------------------- briefing
def today_events(cfg, now: datetime, client: Any = None) -> list[str]:
    """Remaining events for today ("15:00 Dentist"), if Google Calendar is connected."""
    try:
        from alice.connect import runner
        from alice.connect.manifest import discover

        m = discover(cfg)[0].get("google_calendar")
        if m is None or not runner.ready(m)[0]:
            return []
        start = now.astimezone()
        end = start.replace(hour=23, minute=59, second=0)
        data = runner.call(m, m.actions["upcoming"], {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                                                       "maxResults": 8}, client=client, cache=False)
    except Exception:  # noqa: BLE001 - calendar hint; briefing still works if unreachable
        return []
    out = []
    for ev in (data or {}).get("items") or []:
        st = (ev.get("start") or {}).get("dateTime") or ""
        when = st[11:16] if len(st) >= 16 else "gün boyu"
        if ev.get("summary"):
            out.append(f"{when} {ev['summary']}")
    return out


def unread_mail(cfg) -> list[str]:
    """Unread important emails in connected Gmail accounts ("school: Professor — Project submission")."""
    try:
        from alice.tools.gmail import important_unread

        return important_unread(cfg, limit=5)
    except Exception:  # noqa: BLE001
        return []


def morning_info(cfg, now: datetime) -> dict[str, Any]:
    """Additional parts of the morning summary: weather, today's schedule, remaining notes from yesterday, health report pending issue."""
    from alice.extras import brief

    data = (getattr(cfg, "data", None) or {})
    b = dict(data.get("brief") or {})
    out: dict[str, Any] = {}
    try:
        out["weather"] = brief.weather(str(b.get("city") or ""), auto=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        g = brief.gather(b, now=now, weather_fn=lambda c: "")
        if b.get("days"):
            out["program"] = g.get("today", "")
        out["notes"] = g.get("dünden kalanlar") or []
    except Exception:  # noqa: BLE001
        pass
    try:
        from alice import health_report

        pending = (json.loads(health_report.state_path().read_text(encoding="utf-8")).get("pending") or {})
        if pending.get("body"):
            out["health"] = pending["body"]
    except (OSError, ValueError):
        pass  # no health report pending
    return out


def briefing(mem, cfg, away: float, now: datetime, morning: dict[str, Any] | None = None) -> dict[str, Any]:
    due = mem.due_followups(now.timestamp()) if mem is not None else []
    return {"greeting": part_of_day(now), "away": _away_text(away), "followups": due,
            "events": today_events(cfg, now), "mail": unread_mail(cfg), "morning": morning}


def has_news(b: dict[str, Any]) -> bool:
    return bool(b["followups"] or b["events"] or b["mail"])


COMPOSE_PROMPT = """The user is back at the computer (they were away for {away}). Welcome them by voice, short \
and natural. Your personality is below; speak as that character. Rules: Turkish, at most 3 short sentences, no \
lists/markdown/emoji, write times as they are spoken ("üçte"). Start with a greeting like "{greeting}" (use their name \
if you know it). Ask about or remind them of the follow-ups naturally; if there are several things, put the most \
important first and gather the rest in one sentence. If there is nothing to say, just give a short greeting. Don't \
make anything up: use only what is below.

Personality (summary):
{persona}

What is known about the user:
{profile}

Follow-ups (to ask about / to remind / unfinished):
{followups}
The rest of today's calendar:
{events}
Unread important emails:
{mail}{morning}"""

MORNING_PROMPT = """

This is the first welcome of the day: give a short morning summary (at most 5 sentences). Order: weather (one sentence; say if an umbrella or a coat is needed), what is on today (schedule and calendar), then follow-ups and emails, last what is left from yesterday and ALICE's own problem.
Weather: {weather}
Today's schedule: {program}
Notes left from yesterday: {notes}
ALICE's own problem (health report): {health}"""


def compose(b: dict[str, Any], mem=None, router=None) -> str:
    fups = "\n".join(f"- ({f.kind}) {f.text}" for f in b["followups"]) or "(none)"
    events = "\n".join(f"- {e}" for e in b["events"]) or "(none)"
    mail = "\n".join(f"- {m}" for m in b["mail"]) or "(none)"
    mo = b.get("morning")
    morning = MORNING_PROMPT.format(weather=mo.get("weather") or "(none)", program=mo.get("program") or "(none)",
                                    notes="; ".join(mo.get("notes") or []) or "(none)",
                                    health=mo.get("health") or "(none)") if mo else ""
    if router is not None:
        try:
            from alice.config import personality_text

            profile = mem.profile(chars=1200, recent=0) if mem is not None else ""
            prompt = COMPOSE_PROMPT.format(away=b["away"], greeting=b["greeting"], persona=personality_text()[:1500]
                                           or "(default: calm, short, warm)", profile=profile[:1500] or "(none)",
                                           followups=fups, events=events, mail=mail, morning=morning)
            res = router.chat("fast", [{"role": "user", "content": prompt}], temperature=0.7,
                              max_tokens=380 if mo else 220)
            text = (res.message.get("content") or "").strip()
            if text:
                return text
        except Exception:  # noqa: BLE001 - fallback sentence if no model
            pass
    parts = [f"{b['greeting']}, hoş geldin."]
    if mo and mo.get("weather"):
        parts.append(mo["weather"] + ".")
    if b["followups"]:
        parts.append(b["followups"][0].text)
    if b["events"]:
        parts.append(f"Bugün takviminde {len(b['events'])} şey var, ilki {b['events'][0]}.")
    if b["mail"]:
        parts.append(f"{len(b['mail'])} önemli e-postan var.")
    return " ".join(parts)


# ------------------------------------------------------------------------------------------- watcher
class Greeter:
    """tick() on every core turn; prepares and speaks greeting in the background upon return from long absence."""

    def __init__(self, cfg, notify: Callable[[Any], bool], memory: Callable[[], Any] = lambda: None,
                 router: Callable[[], Any] = lambda: None, idle: Callable[[], float | None] = idle_seconds,
                 clock: Callable[[], datetime] = datetime.now,
                 morning: Callable[[Any, datetime], dict[str, Any]] = morning_info, state_path: Any = None):
        self.cfg = cfg
        self.notify = notify
        self.memory = memory
        self.router = router
        self.idle = idle
        self.clock = clock
        self.voice: Any = None  # VoiceAssistant: allow replying without saying "Hey Alice" after greeting
        self.away_since: float | None = None
        self.last_greet = 0.0
        self.morning = morning
        if state_path is None:
            from alice.config import alice_home

            state_path = alice_home() / "greeting-state.json"
        self.state_path = state_path
        try:
            self.last_day = str(json.loads(state_path.read_text(encoding="utf-8")).get("last_day") or "")
        except (OSError, ValueError, AttributeError):
            self.last_day = ""
        self._started = False
        self._busy = threading.Lock()

    def tick(self) -> bool:
        """Starts greeting and returns True if return is detected."""
        s = settings(self.cfg)
        if not s.get("enabled", True):
            return False
        idle = self.idle()
        if idle is None:
            return False
        now = time.time()
        if not self._started:
            self._started = True
            # Computer turned on in the morning: core just started, absence could not be measured but first summary of the day not done yet
            today = self.clock()
            if (idle < 300 and self.last_day != today.strftime("%Y-%m-%d") and 6 <= today.hour < int(s["morning_until"])
                    and self._allowed(s, now)):
                threading.Thread(target=self._greet_bg, args=(8 * 3600,), daemon=True, name="alice-greeting").start()
                return True
        if idle >= float(s["away_minutes"]) * 60:
            if self.away_since is None:
                self.away_since = now - idle
            return False
        if self.away_since is None or idle > 60:
            return False
        away, self.away_since = now - self.away_since, None
        if not self._allowed(s, now):
            return False
        threading.Thread(target=self._greet_bg, args=(away,), daemon=True, name="alice-greeting").start()
        return True

    def _allowed(self, s: dict[str, Any], now: float) -> bool:
        if now - self.last_greet < float(s["min_gap_hours"]) * 3600:
            return False
        from alice.core.modes import in_hours, parse_hours

        span = parse_hours(str(s.get("quiet_hours") or ""))
        return not (span and in_hours(self.clock(), span))

    def _greet_bg(self, away: float) -> None:
        """Background greeting: do not let thread crash silently when memory is closed (core shutting down) or summary cannot be prepared; greeting is skipped this time."""
        try:
            self.greet(away)
        except Exception:  # noqa: BLE001
            pass

    def greet(self, away: float) -> str:
        if not self._busy.acquire(blocking=False):
            return ""
        try:
            now = self.clock()
            mem = self.memory()
            first_today = self.last_day != now.strftime("%Y-%m-%d")
            mo = None
            if first_today and now.hour < int(settings(self.cfg)["morning_until"]):
                try:
                    mo = self.morning(self.cfg, now)
                except Exception:  # noqa: BLE001 - greet even without morning extras
                    mo = None
            b = briefing(mem, self.cfg, away, now, mo)
            if not has_news(b) and not first_today:
                return ""
            text = compose(b, mem, self.router())
            from alice.core.notify import Notification

            if not self.notify(Notification("Hoş geldin", text, "info", "presence", kind="info",
                                            emotion="cheerful")):
                return ""  # mode silenced: do not count, can be retried later
            self.last_greet = time.time()
            self.last_day = now.strftime("%Y-%m-%d")
            try:
                self.state_path.write_text(json.dumps({"last_day": self.last_day}), encoding="utf-8")
            except (OSError, AttributeError):
                pass  # the greeting is still counted for this process
            self._after(mem, b, text)
            return text
        finally:
            self._busy.release()

    def _after(self, mem, b: dict[str, Any], text: str) -> None:
        if mem is not None:
            try:
                mem.record("(bilgisayarın başına döndü)", text, "karşılama")
                for f in b["followups"]:
                    if f.kind in ("ask", "remind"):
                        mem.done_followup(f.id)  # asked; answer comes in conversation
            except Exception:  # noqa: BLE001
                pass
        va = self.voice
        if va is not None:
            try:
                va.speaker.wait(40)
                va.armed_until = max(getattr(va, "armed_until", 0.0), time.monotonic() + float(va.follow_up))
            except Exception:  # noqa: BLE001
                pass
