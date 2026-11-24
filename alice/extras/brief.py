"""Morning brief: weather, whether today is internship (staj) or school, leftovers from yesterday, overnight tasks.

It only gathers the data; ALICE does the telling (once installed as a scheduled task, it arrives in the morning
as a notification/voice). Weather comes from wttr.in (no key needed).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from alice.i18n import _, pick
import httpx

DAYS = ["pazartesi", "salı", "çarşamba", "perşembe", "cuma", "cumartesi", "pazar"]

BRIEF_PROMPT = ("Prepare my morning brief: call the report tool with kind=morning and tell me the result as a warm, short "
                "(at most 6 sentences) morning summary in my language. First the day and the weather, then what's on "
                "today, then what happened overnight and what's left from yesterday.")


def weather(city: str, client: httpx.Client | None = None, auto: bool = False) -> str:
    """If auto=True and there is no city, the location is found from the IP."""
    if not city and not auto:
        return _("set [brief] city in the config for the weather")
    c = client or httpx.Client(timeout=8)
    try:
        d = c.get(f"https://wttr.in/{city}", params={"format": "j1", "lang": "tr"}).json()
    except (httpx.HTTPError, ValueError) as e:
        return _("could not get the weather ({error})", error=e.__class__.__name__)
    finally:
        if client is None:  # so the sockets of a client we opened don't pile up in the kernel
            c.close()
    try:
        cur = d["current_condition"][0]
        desc = (cur.get("lang_tr") or cur.get("weatherDesc") or [{"value": ""}])[0]["value"]
        today = d["weather"][0]
        rain = max(int(h.get("chanceofrain", 0)) for h in today.get("hourly", [{}]))
        if not city:
            area = (d.get("nearest_area") or [{}])[0]
            city = ((area.get("areaName") or [{"value": "buralar"}])[0]["value"])
        return _("{city}: now {now}°C, {desc}; today low {low}°C, high {high}°C, chance of rain up to {rain}%",
                 city=city, now=cur["temp_C"], desc=desc.lower(), low=today["mintempC"], high=today["maxtempC"], rain=rain)
    except (KeyError, IndexError, ValueError):
        return _("could not read the weather")


def gather(cfg: dict[str, Any], store=None, tasks=None, now: datetime | None = None,
           weather_fn=weather) -> dict[str, Any]:
    from alice.extras import notes

    now = now or datetime.now()
    day = DAYS[now.weekday()]
    schedule = cfg.get("days", {})
    today = schedule.get(day) or schedule.get(now.strftime("%A").lower()) or ""
    tomorrow = schedule.get(DAYS[(now.weekday() + 1) % 7], "")
    evening = (now - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0).timestamp()
    yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out: dict[str, Any] = {"date": f"{now:%d.%m.%Y}, {pick(now.strftime('%A'), day)}", "today": today or _("nothing on the schedule"),
                           "tomorrow": tomorrow, "weather": weather_fn(cfg.get("city", ""))}
    if tasks is not None:
        ran = [t for t in tasks.tasks if t.last_run and t.last_run >= evening and t.last_result
               and "brief" not in t.id]
        out["tasks_overnight"] = [f"{t.description or t.prompt[:50]}: {t.last_result[:200]}" for t in ran]
    if store is not None:
        evs = [e for e in store.events(since=evening, limit=50) if e.kind in ("alert", "flight", "error")]
        out["overnight"] = [f"{datetime.fromtimestamp(e.ts):%H:%M} {e.body or e.title}"[:160] for e in evs[:8]]
    left = [n for n in notes.search(since=yesterday) if n.ts < now.replace(hour=0, minute=0).timestamp()]
    left += [n for n in notes.search("yarın", since=yesterday - 86400) if n not in left]
    out["left_from_yesterday"] = [n.text for n in left[:8]]
    return out


# Labels for render(): the data keys are English identifiers, shown in the interface language.
LABELS = {"date": "date", "today": "today", "tomorrow": "tomorrow", "weather": "weather",
          "tasks_overnight": "tasks that ran overnight", "overnight": "what happened overnight",
          "left_from_yesterday": "left from yesterday"}


def render(data: dict[str, Any]) -> str:
    lines = []
    for k, v in data.items():
        label = _(LABELS.get(k, k))
        if isinstance(v, list):
            lines.append(f"{label}: " + ("; ".join(v) if v else _("none")))
        elif v:
            lines.append(f"{label}: {v}")
    return "\n".join(lines)


def install(at: str = "08:00", weekdays_only: bool = False):
    from alice.core.schedule import ScheduledTask, TaskStore

    h, m = (int(x) for x in at.split(":"))
    task = ScheduledTask(cron=f"{m} {h} * * {'1-5' if weekdays_only else '*'}", prompt=BRIEF_PROMPT,
                         description=_("morning brief"), catch_up=False, created=time.time(), id="brief")
    return TaskStore().add(task)
