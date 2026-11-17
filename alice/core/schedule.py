"""Scheduled tasks.

A sentence like "Her pazar gece Unity projelerimi yedekle" ("back up my Unity projects every Sunday night") is
turned by the fast model into a cron expression and a task for ALICE, shown to the user, and saved once approved
(~/.alice/tasks.json). The service runs the tasks that are due on every turn. A task missed while the computer
was off is caught up once at startup if catch_up is on.

Cron: "dakika saat gün ay haftanın_günü" (minute hour day month weekday); *, a-b, a,b, */n are supported. Weekday
is 0-6 (0 = Sunday, 7 is also Sunday). If both day and weekday are restricted, matching either one is enough.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from alice.config import alice_home

_SAVE_LOCK = threading.Lock()


def write_json_atomic(path: Path, data: Any) -> None:
    """Write to a temporary file first, then replace the target: a crash, power cut, or two threads writing at
    the same time must not leave half-written (corrupt) JSON; a corrupt file used to crash the core on every startup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SAVE_LOCK:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def read_json_list(path: Path) -> list[Any] | None:
    """Read a JSON list; None if the file is corrupt (the caller keeps what it has so the core doesn't crash)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "[]")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = ["dakika", "saat", "gün", "ay", "haftanın günü"]
_DOW = ["pazar", "pazartesi", "salı", "çarşamba", "perşembe", "cuma", "cumartesi"]


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron 5 alanlı olmalı (dakika saat gün ay haftanın_günü): {expr!r}")
        self.expr = " ".join(parts)
        self.fields = [self._parse(p, lo, hi, name) for p, (lo, hi), name in zip(parts, _RANGES, _NAMES)]
        self.fields[4] = {d % 7 for d in self.fields[4]}
        self.dom_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    @staticmethod
    def _parse(part: str, lo: int, hi: int, name: str) -> set[int]:
        out: set[int] = set()
        for item in part.split(","):
            step = 1
            if "/" in item:
                item, s = item.split("/", 1)
                step = int(s)
                if step <= 0:
                    raise ValueError(f"{name}: adım pozitif olmalı")
            if item == "*":
                a, b = lo, hi
            elif "-" in item:
                a, b = (int(x) for x in item.split("-", 1))
            else:
                a = b = int(item)
                if step > 1:
                    b = hi
            if a < lo or b > hi or a > b:
                raise ValueError(f"{name} alanı {lo}-{hi} aralığında olmalı: {part!r}")
            out.update(range(a, b + 1, step))
        return out

    def matches(self, dt: datetime) -> bool:
        mi, h, dom, mon, dow = self.fields
        if dt.minute not in mi or dt.hour not in h or dt.month not in mon:
            return False
        dom_ok = dt.day in dom
        dow_ok = (dt.isoweekday() % 7) in dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, dt: datetime) -> datetime:
        """The first matching minute after dt."""
        t = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=366 * 5)
        while t < limit:
            if t.month not in self.fields[3]:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.fields[1]:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute in self.fields[0]:
                return t
            t += timedelta(minutes=1)
        raise ValueError(f"cron hiç eşleşmiyor: {self.expr}")

    def _day_ok(self, t: datetime) -> bool:
        dom_ok = t.day in self.fields[2]
        dow_ok = (t.isoweekday() % 7) in self.fields[4]
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def describe(self) -> str:
        """A rough Turkish description, for display on the confirmation screen."""
        mi, h, dom, mon, dow = self.expr.split()
        when = f"{int(h):02d}:{int(mi):02d}" if mi.isdigit() and h.isdigit() else f"dakika={mi} saat={h}"
        days = []
        if dow != "*":
            days.append(", ".join(_DOW[d] for d in sorted(self.fields[4])))
        if dom != "*":
            days.append(f"ayın {dom}. günü")
        if mon != "*":
            days.append(f"ay={mon}")
        return f"{' / '.join(days) or 'her gün'} {when}"


@dataclass
class ScheduledTask:
    cron: str
    prompt: str                     # task given to ALICE
    description: str = ""           # the user's own words
    catch_up: bool = True           # if missed while the computer was off, run it once at startup
    notify: bool = True
    once: bool = False              # one-time ("yarın 17:00", "5 dakika sonra" = "in 5 minutes"): deleted once it runs
    enabled: bool = True
    created: float = field(default_factory=time.time)
    last_run: float | None = None
    last_result: str = ""
    pending: bool = False           # "run now" was requested
    project: str = ""               # if set: start this project template as an autonomous project, not the prompt
    cwd: str = ""                   # project: working folder (empty: the template's)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        # Fail right away if invalid. A date that never comes, like "30 Şubat" (30 February), is rejected before
        # it is saved; otherwise due() would blow up every turn and stop all the other tasks.
        Cron(self.cron).next_after(datetime.now())

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScheduledTask":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def next_run(self, after: float | None = None) -> datetime:
        base = after if after is not None else (self.last_run or self.created)
        return Cron(self.cron).next_after(datetime.fromtimestamp(base))

    def summary(self) -> str:
        try:
            nxt = self.next_run(time.time()).strftime("%d.%m %H:%M") if self.enabled else "kapalı"
        except ValueError:
            nxt = "hiç gelmiyor"
        last = datetime.fromtimestamp(self.last_run).strftime("%d.%m %H:%M") if self.last_run else "hiç"
        kind = "tek sefer, çalışınca kendiliğinden silinir; " if self.once else ""
        what = f"proje şablonu '{self.project}'" if self.project else (self.description or self.prompt[:60])
        return (f"{self.id}: [{self.cron}] {Cron(self.cron).describe()} -> {what} "
                f"({kind}sonraki: {nxt}, son: {last})")


def start_project(task: ScheduledTask, starter: Any) -> str:
    """Start a due project task: an autonomous project with the template's goal, settings and agents. starter: the
    app's create_project (wired up in the app so the project appears on the Projects page). Returns a result line."""
    from alice.team_store import list_templates

    if starter is None:
        return "proje başlatılamadı: uygulama çekirdekte açık değil"
    tpl = next((t for t in list_templates() if str(t.get("name", "")).lower() == task.project.lower()), None)
    if tpl is None:
        return f"proje başlatılamadı: '{task.project}' adında şablon yok"
    goal = str(tpl.get("goal") or "") + ("\n\n" + task.prompt if task.prompt.strip() else "")
    try:
        pid = starter({**tpl, "goal": goal, "cwd": task.cwd or tpl.get("cwd") or ""})
    except Exception as e:  # noqa: BLE001 - folder missing, PR branch failed, etc.: reported as the result
        return f"proje başlatılamadı: {str(e)[:300]}"
    if not pid:
        return "proje başlatılamadı: uygulama hazır değil"
    return f"'{task.project}' şablonundan proje başladı ({pid}); Projeler sayfasında izlenebilir"


class TaskStore:
    def __init__(self, path: Path | None = None):
        self.path = path or alice_home() / "tasks.json"
        self.tasks: list[ScheduledTask] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.tasks = []
            return
        data = read_json_list(self.path)
        if data is None:  # corrupt file: keep what we have
            return
        tasks = []
        for d in data:
            try:
                tasks.append(ScheduledTask.from_dict(d))
            except (TypeError, ValueError, AttributeError):
                continue
        self.tasks = tasks

    def save(self) -> None:
        write_json_atomic(self.path, [asdict(t) for t in self.tasks])

    def add(self, task: ScheduledTask) -> ScheduledTask:
        self.tasks = [t for t in self.tasks if t.id != task.id] + [task]
        self.save()
        return task

    def get(self, task_id: str) -> ScheduledTask | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def remove(self, task_id: str) -> bool:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        if len(self.tasks) != before:
            self.save()
            return True
        return False

    def due(self, now: float | None = None, grace: float = 600) -> list[tuple[ScheduledTask, bool]]:
        """Due tasks as (task, is_catch_up). The last_run of missed tasks with catch_up off is quietly moved
        forward so they wait for their next time."""
        now = time.time() if now is None else now
        out = []
        changed = False
        missed: list[str] = []  # missed one-time tasks that won't be caught up: deleted
        for t in self.tasks:
            if t.pending:
                out.append((t, False))
                continue
            if not t.enabled:
                continue
            try:
                nxt = t.next_run().timestamp()
            except ValueError:  # a date that never comes: skip this task, don't stop the others
                continue
            if nxt > now:
                continue
            late = now - nxt > grace
            if late and not t.catch_up:
                if t.once:
                    # If it stays in the list, cron regenerates the same day next year and the one-time task runs then.
                    missed.append(t.id)
                else:
                    t.last_run = now
                changed = True
                continue
            out.append((t, late))
        if missed:
            self.tasks = [t for t in self.tasks if t.id not in missed]
        if changed:
            self.save()
        return out

    def mark_run(self, task: ScheduledTask, result: str, now: float | None = None) -> None:
        task.last_run = time.time() if now is None else now
        task.last_result = result[:2000]
        task.pending = False
        self.save()


# ------------------------------------------------------- natural language -> task
PARSE_PROMPT = """Convert the user's request into a scheduled task for a desktop assistant (ALICE) that can run
shell commands, edit files, browse the web and use the computer. Now: {now}.
Reply ONLY with JSON:
{{"cron": "<5-field cron: minute hour day-of-month month day-of-week (0=Sunday)>",
  "prompt": "<complete, self-contained Turkish instruction ALICE will execute unattended at that time; include paths/details the user gave>",
  "catch_up": <true if it should still run once after the computer was off at the scheduled time>,
  "once": <true for a one-time request ("yarın", "5 dakika sonra", a specific date); false if it repeats ("her gün")>}}
For a one-time request put the exact minute, hour, day and month in the cron.
"Gece" without an hour means 23:00, "sabah" means 08:00, "akşam" means 19:00.
User request: {text}"""


def parse_task(text: str, router) -> ScheduledTask:
    from alice.decider import extract_json

    msgs = [{"role": "user", "content": PARSE_PROMPT.format(now=datetime.now().strftime("%Y-%m-%d %A %H:%M"),
                                                             text=text)}]
    res = router.chat("fast", msgs, temperature=0, max_tokens=600)
    data = extract_json(res.text)
    if not isinstance(data, dict) or "cron" not in data or "prompt" not in data:
        raise ValueError(f"model görev döndürmedi: {res.text[:200]}")
    return ScheduledTask(cron=str(data["cron"]), prompt=str(data["prompt"]), description=text,
                         catch_up=bool(data.get("catch_up", True)), once=bool(data.get("once", False)))
