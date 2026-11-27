"""KAIROS: ALICE, which wakes up on its own in the background and keeps an eye on the state.

The core calls ``Kairos.tick()`` every turn. When the time comes (once the interval ALICE picked itself at its last
wake-up has passed), an agent with a clean context wakes up, sees the state at that moment and decides for itself:
  * most wake-ups end silently: if there is nothing to say, it does nothing;
  * if something is useful (a follow-up whose day has come but was never discussed, a PR that turned red, preparation
    for an upcoming event), it sends a single short notification or notes it in the day's log;
  * at the end it picks the next wake-up with ``kairos_sleep`` (often on a busy day, rarely on a quiet one).

Safety: the agent runs in plan mode, meaning it only reads, sends notifications and writes to the day's log; it
cannot change files, run commands or message anyone. The daily wake limit, quiet hours and game/flight mode (modes
that mute notifications) postpone a wake-up.

State is kept in ``~/.alice/kairos.json``: the next wake-up, today's wake count and a summary of recent wake-ups
(visible under Settings > Assistant).

config.toml:
  [kairos]
  enabled = false
  min_minutes = 20       # minimum gap between two wake-ups
  max_minutes = 240      # maximum
  daily_wakes = 16       # maximum wake-ups per day
  quiet_hours = "00:30-07:00"
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.tools.base import READ, Tool, ToolContext, ToolResult, obj

DEFAULTS: dict[str, Any] = {"enabled": False, "min_minutes": 20, "max_minutes": 240, "first_minutes": 10,
                            "daily_wakes": 16, "quiet_hours": "00:30-07:00", "permission_mode": "plan"}
HISTORY = 40
SILENT = ("sessiz", "silent", "yok", "-")


def settings(cfg: Any) -> dict[str, Any]:
    data = getattr(cfg, "data", None) or {}
    return {**DEFAULTS, **(data.get("kairos") or {})}


def state_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "kairos.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each writer uses its own temp file: when the core (tick/wake-up) and the app ("şimdi uyan" = wake now, on/off)
    # write at the same time, a shared "kairos.tmp" was getting lost from under one of them, and os.replace raised
    # FileNotFoundError (the wake-up's summary and the next wake-up time were being lost).
    fd, tmp = tempfile.mkstemp(prefix=".kairos-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=1))
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: the target is being read or changed by someone else at that moment
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # a leftover temp file is harmless; the error is raised below
        raise


def is_enabled(cfg: Any, st: dict[str, Any] | None = None) -> bool:
    """A choice in the state file (made from the app or a tool) overrides the one in config, so the core can
    be switched on and off without restarting."""
    st = load_state() if st is None else st
    if "enabled" in st:
        return bool(st["enabled"])
    return bool(settings(cfg).get("enabled"))


def set_enabled(cfg: Any, on: bool, path: Path | None = None) -> None:
    data = load_state(path)
    data["enabled"] = bool(on)
    if on and not data.get("next"):
        data["next"] = time.time() + float(settings(cfg)["first_minutes"]) * 60
    save_state(data, path)
    try:
        from alice.config import set_config_value

        set_config_value("kairos", "enabled", bool(on))
    except Exception:  # noqa: BLE001 - the state file is enough even if it cannot be written
        pass


def request_wake(path: Path | None = None) -> None:
    """From the app, "şimdi uyan" (wake now): pull the next wake-up to now (the core wakes on its next turn)."""
    data = load_state(path)
    data["next"] = time.time()
    data["forced"] = True
    save_state(data, path)


# ------------------------------------------------------------------------------------------- sleep tool
class KairosSleep(Tool):
    name = "kairos_sleep"
    description = ("KAIROS: choose the next wake-up and end this one. minutes: how many minutes from now (clamped to "
                   "the limits). Pick short when things are busy or something is expected, long when it is calm. "
                   "reason: why this interval.")
    parameters = obj({"minutes": {"type": "integer", "description": "Wake up after this many minutes."},
                      "reason": {"type": "string", "description": "Short reason."}}, ["minutes"])
    risk = READ

    def run(self, ctx: ToolContext, minutes: int, reason: str = "") -> ToolResult:
        ctx.state["kairos_sleep"] = (int(minutes or 0), str(reason or "")[:200])
        return ToolResult(f"Waking up in {minutes} minutes. Now write your one-line summary and finish.")


PROMPT = """You are on background watch (KAIROS). The user didn't call you; you woke up on your own. Time: {now}.

Your job: look over the state and act only if there is something that is really useful to the user RIGHT NOW.
Most wake-ups should end without doing anything. A needless notification is the worst outcome: it bothers the user.

Useful examples: a follow-up or reminder that is due but hasn't been talked about; something on a watched PR the user
needs to look at; preparation needed for an event starting soon; an observation worth remembering later (write it to
the day's log with memory action=log instead of notifying).
Don't: notify the same thing twice (look at the previous wake-ups), try to start a conversation, say something minor
while the user is playing a game or at night, or notify something you are guessing and aren't sure of.

Your tools are for reading (calendar, email, memory, web, PR status). To notify, use `notify` (at most once, 1-2
short sentences, in Turkish, the user's language). You can't do anything that changes things.

Before finishing, always choose the next wake-up with `kairos_sleep` ({lo}-{hi} minutes). Make your last answer one
line, in Turkish: a summary of what you did, or only "sessiz" if you did nothing.

# Current state
{context}"""


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


class Kairos:
    """Core calls tick() on its turn; when the time comes, it starts a wake-up in the background."""

    def __init__(self, cfg: Any, store: Any = None, memory: Callable[[], Any] = lambda: None,
                 policy: Callable[[], Any] = lambda: None, idle: Callable[[], float | None] | None = None,
                 runner: Callable[[str], tuple[str, Any]] | None = None, clock: Callable[[], float] = time.time,
                 path: Path | None = None):
        self.cfg = cfg
        self.store = store
        self.memory = memory
        self.policy = policy
        if idle is None:
            from alice.presence import idle_seconds

            idle = idle_seconds
        self.idle = idle
        self.runner = runner or self._run_agent
        self.clock = clock
        self.path = path
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- gate
    def tick(self) -> bool:
        s = settings(self.cfg)
        st = load_state(self.path)
        if not is_enabled(self.cfg, st):
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        now = self.clock()
        nxt = float(st.get("next") or 0)
        if not nxt:  # first start: not right away, but a little later
            st["next"] = now + float(s["first_minutes"]) * 60
            save_state(st, self.path)
            return False
        if now < nxt:
            return False
        forced = bool(st.get("forced"))
        why = "" if forced else self._blocked(s, st, now)
        if why:
            st["next"] = now + (30 * 60 if why != "günlük sınır" else self._until_tomorrow(now))
            st["skipped"] = why
            save_state(st, self.path)
            return False
        st["forced"] = False
        st["next"] = now + float(s["max_minutes"]) * 60  # even if a wake-up is cut short, don't let it loop
        save_state(st, self.path)
        self._thread = threading.Thread(target=self.wake, daemon=True, name="alice-kairos")
        self._thread.start()
        return True

    def _blocked(self, s: dict[str, Any], st: dict[str, Any], now: float) -> str:
        from alice.core.modes import in_hours, parse_hours

        span = parse_hours(str(s.get("quiet_hours") or ""))
        if span and in_hours(datetime.fromtimestamp(now), span):
            return "sessiz saatler"
        pol = self.policy()
        if pol is not None and not pol.allows("info"):
            return f"mod: {getattr(pol, 'mode', '')}"
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if st.get("day") == today and int(st.get("wakes") or 0) >= int(s["daily_wakes"]):
            return "günlük sınır"
        return ""

    @staticmethod
    def _until_tomorrow(now: float) -> float:
        d = datetime.fromtimestamp(now)
        nxt = d.replace(hour=7, minute=30, second=0, microsecond=0)
        if nxt <= d:
            nxt = datetime.fromtimestamp(nxt.timestamp() + 86400)
        return nxt.timestamp() - now

    # ---------------------------------------------------------------- wake-up
    def context(self, now: float, st: dict[str, Any]) -> str:
        rows: list[str] = []
        idle = None
        try:
            idle = self.idle()
        except Exception:  # noqa: BLE001
            pass
        if idle is not None:
            rows.append(f"User: {'at the computer' if idle < 300 else f'away for {int(idle // 60)} minutes'}")
        pol = self.policy()
        if pol is not None:
            rows.append(f"Mode: {getattr(pol, 'mode', '?')}")
        mem = self.memory()
        if mem is not None:
            try:
                due = mem.due_followups(now)
                soon = [f for f in mem.followups() if f.due and now < f.due <= now + 2 * 86400]
                if due:
                    rows.append("Follow-ups due:\n" + "\n".join(f"- [{f.kind}] {f.text}" for f in due[:8]))
                if soon:
                    rows.append("Coming up (2 days):\n" + "\n".join(f"- {_fmt_ts(f.due)} {f.text}" for f in soon[:8]))
            except Exception:  # noqa: BLE001
                pass
            try:
                start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp()
                log = mem.notes.logs_since(start)
                if log.strip():
                    rows.append("Today's log (last part):\n" + log[-1500:])
            except Exception:  # noqa: BLE001
                pass
        if self.store is not None:
            try:
                since = float(st.get("last") or now - 6 * 3600)
                evs = [e for e in self.store.events(since=since, limit=25) if e.topic != "kairos"]
                if evs:
                    rows.append("Events since the last wake-up:\n" + "\n".join(
                        f"- {_fmt_ts(e.ts)} [{e.severity}] {e.title}{': ' + e.body[:160] if e.body else ''}"
                        for e in evs[-15:]))
            except Exception:  # noqa: BLE001
                pass
        try:
            from alice import prwatch

            if prwatch.store_path().exists() and prwatch.load():
                rows.append("Watched PRs:\n" + prwatch.describe()[:1200])
        except Exception:  # noqa: BLE001
            pass
        hist = st.get("history") or []
        if hist:
            rows.append("Your previous wake-ups:\n" + "\n".join(f"- {_fmt_ts(h['ts'])}: {h.get('summary', '')}"
                                                         for h in hist[-6:]))
        return "\n\n".join(rows) or "(nothing worth noting)"

    def wake(self) -> str:
        s = settings(self.cfg)
        now = self.clock()
        st = load_state(self.path)
        lo, hi = int(s["min_minutes"]), int(s["max_minutes"])
        prompt = PROMPT.format(now=datetime.fromtimestamp(now).strftime("%A %d.%m.%Y %H:%M"), lo=lo, hi=hi,
                               context=self.context(now, st))
        summary, sleep = "", None
        try:
            summary, sleep = self.runner(prompt)
        except Exception as e:  # noqa: BLE001 - one wake-up's error must not end the watch
            summary = f"hata: {str(e)[:160]}"
        minutes, reason = sleep if sleep else ((lo + hi) // 2, "seçilmedi")
        minutes = max(lo, min(hi, int(minutes or lo)))
        summary = " ".join((summary or "").split())[:300] or "sessiz"
        st = load_state(self.path)  # the app may have said "şimdi uyan" while the wake-up was running
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if st.get("day") != today:
            st["day"], st["wakes"] = today, 0
        st["wakes"] = int(st.get("wakes") or 0) + 1
        st["last"] = now
        if not st.get("forced"):
            st["next"] = self.clock() + minutes * 60
        st.pop("skipped", None)
        st["history"] = ((st.get("history") or []) + [{"ts": now, "summary": summary, "sleep": minutes,
                                                       "reason": reason}])[-HISTORY:]
        save_state(st, self.path)
        if self.store is not None and summary.lower().strip(" .") not in SILENT:
            try:
                self.store.add_event("info", "KAIROS", summary, topic="kairos")
            except Exception:  # noqa: BLE001
                pass
        return summary

    def _run_agent(self, prompt: str) -> tuple[str, Any]:
        """Agent with a clean context in plan mode, using the kairos_sleep tool. (summary, (minutes, reason) | None)"""
        from alice.agent import Agent, NullUI
        from alice.decider import Decider
        from alice.permissions import Permissions
        from alice.providers.router import Router
        from alice.tools import all_tools, cleanup

        router = Router(self.cfg, background=True)
        decider = Decider.from_config(self.cfg, router)
        ctx = ToolContext(cwd=Path.home(), config=self.cfg, router=router, decider=decider)
        ctx.state["tool_groups"] = {"notify"}  # notify tool visible from the start; others via load_tools
        mode = str(settings(self.cfg).get("permission_mode") or "plan")
        tools = [t for t in all_tools(self.cfg) if t.name != "enter_plan_mode"] + [KairosSleep()]
        agent = Agent(self.cfg, router, tools, ctx, Permissions(mode), NullUI(approve="n"), decider)
        try:
            text = agent.run_turn(prompt)
        finally:
            cleanup(ctx)
            decider.close()
            router.close()
        return text or "", ctx.state.get("kairos_sleep")


def view(path: Path | None = None) -> dict[str, Any]:
    """For the app: whether it is on, the next wake-up, today's count and the recent wake-ups."""
    st = load_state(path)
    return {"next": st.get("next"), "wakes": st.get("wakes", 0), "day": st.get("day", ""),
            "skipped": st.get("skipped", ""), "history": list(reversed(st.get("history") or []))[:15]}
