"""Tasks ("Görevler"): standing responsibilities ALICE carries out in the background.

A task is a responsibility written in plain language ("watch my repo and draft replies to new issues") plus a
trigger that says when to look at it. It is built on the parts the core already has, not beside them:

  * triggers reuse the core's own machinery: ``schedule`` uses the cron of scheduled jobs (alice/core/schedule.py),
    ``metric`` uses the rule check over the store (alice/core/rules.py), ``event`` matches events the monitors and
    other modules write to the store (e.g. the flight debrief), and ``github`` / ``folder`` watch a source for new
    items;
  * the core's tick calls ``TaskRunner.tick``; runs happen in a background thread with the same unattended agent the
    scheduled jobs use, but with the task's own model role, tool groups and permission level;
  * every task has its own memory (notes it keeps between runs), state (trigger cursors) and run log;
  * when a task needs a decision it calls ``task_ask``: that creates an approval in a single inbox. The notifier
    sends it as a desktop toast and as a warning event, which the phone app's poller turns into a push
    notification. Approve / reject / reply is written back to the same database by the app (desktop or phone);
    the core picks the answer up on its next tick and runs the follow-up.

Permission levels (``Task.permission``):
  read  - reads only; anything that changes something is refused and must go through ``task_ask`` (default)
  act   - normal file edits and commands run; outward or dangerous steps are still refused (approval needed)
  full  - everything runs without asking (the user explicitly allowed it for this task)
An approved action runs once in a follow-up with everything allowed: the user's yes is the permission.

Storage is SQLite (~/.alice/standing-tasks.db) because the app process (API) and the core process (runner) both
write to it; WAL mode keeps that safe.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home
from alice.tools.base import READ, Tool, ToolContext, ToolResult, obj

TRIGGERS = ("manual", "schedule", "metric", "event", "github", "folder")
PERMISSIONS = ("read", "act", "full")
ROLES = ("brain", "fast", "code", "vision")
SILENT = {"sessiz", "silent", ""}
LOG_KEEP = 300          # log rows kept per task
MEMORY_KEEP = 30        # notes kept per task
MIN_GAP = 60.0          # s: a task never starts twice within this gap (a noisy trigger must not loop)
SOURCE_EVERY = 300.0    # s: default polling interval for github / folder sources


def db_path() -> Path:
    return alice_home() / "standing-tasks.db"


# --------------------------------------------------------------------------- model
@dataclass
class Task:
    name: str
    responsibility: str
    trigger: dict[str, Any] = field(default_factory=lambda: {"type": "manual"})
    tools: list[str] = field(default_factory=list)   # tool groups (alice/tools/groups.py); empty: core tools only
    permission: str = "read"
    role: str = "brain"
    notify: bool = True                               # send the run summary as a notification
    enabled: bool = True
    template: str = ""
    created: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        self.responsibility = str(self.responsibility or "").strip()
        if not self.name or not self.responsibility:
            raise ValueError("görevin adı ve sorumluluğu olmalı")
        if self.permission not in PERMISSIONS:
            raise ValueError(f"geçersiz izin seviyesi: {self.permission}")
        if not isinstance(self.trigger, dict):
            raise ValueError("tetik bir nesne olmalı")
        check_trigger(self.trigger)
        self.tools = [str(t) for t in (self.tools or []) if str(t).strip()]
        self.role = str(self.role or "brain")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def check_trigger(t: dict[str, Any]) -> None:
    """Raise ValueError if the trigger cannot work, so a broken task is refused when saved, not on every tick."""
    kind = t.get("type", "manual")
    if kind not in TRIGGERS:
        raise ValueError(f"geçersiz tetik: {kind}")
    if kind == "schedule":
        from alice.core.schedule import Cron

        Cron(str(t.get("cron", ""))).next_after(datetime.now())
    elif kind == "metric":
        from alice.core.rules import OPS

        if not t.get("metric") or t.get("op") not in OPS:
            raise ValueError("ölçüm tetiği için metric ve op gerekli")
        float(t.get("threshold", 0))
    elif kind == "event":
        if not (t.get("kind") or t.get("topic") or t.get("match")):
            raise ValueError("olay tetiği için kind, topic ya da match gerekli")
        if t.get("match"):
            re.compile(str(t["match"]))
    elif kind == "github":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", str(t.get("repo", ""))):
            raise ValueError("GitHub tetiği için repo 'sahip/depo' biçiminde olmalı")
    elif kind == "folder":
        if not str(t.get("path", "")).strip():
            raise ValueError("klasör tetiği için path gerekli")


def describe_trigger(t: dict[str, Any]) -> str:
    kind = t.get("type", "manual")
    if kind == "schedule":
        from alice.core.schedule import Cron

        try:
            return Cron(str(t.get("cron", ""))).describe()
        except ValueError:
            return str(t.get("cron", ""))
    if kind == "metric":
        dur = float(t.get("duration") or 0)
        return f"{t.get('metric')} {t.get('op')} {float(t.get('threshold', 0)):g}" + (
            f", {int(dur // 60)} dk boyunca" if dur >= 60 else "")
    if kind == "event":
        parts = [str(t[k]) for k in ("kind", "topic", "match") if t.get(k)]
        return "olay: " + " · ".join(parts)
    if kind == "github":
        return f"{t.get('repo')} deposunda yeni {'PR' if t.get('what') == 'pulls' else 'issue'}"
    if kind == "folder":
        return f"{t.get('path')} klasörüne yeni dosya"
    return "elle"


@dataclass
class Approval:
    id: str
    task_id: str
    ts: float
    title: str
    body: str = ""
    action: str = ""          # what will be done if approved (an instruction for the follow-up run)
    status: str = "pending"   # pending | approved | rejected | replied | done | failed
    reply: str = ""
    resolved: float | None = None
    result: str = ""


# --------------------------------------------------------------------------- storage
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}',
                                  updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, ts REAL NOT NULL,
                                kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_log_task ON log(task_id, id);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, ts REAL NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', reply TEXT NOT NULL DEFAULT '', resolved REAL, result TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_appr_status ON approvals(status, ts);
"""


class TaskBook:
    """Tasks, their state/memory, run log and the approvals inbox. Safe to use from the app and the core at once."""

    def __init__(self, path: Path | None = None):
        self.path = path or db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass  # WAL is only faster; the default journal mode works too (e.g. on some network drives)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "TaskBook":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount

    # ---- tasks
    def list(self) -> list[Task]:
        out = []
        for (data,) in self._q("SELECT data FROM tasks ORDER BY json_extract(data, '$.created')"):
            try:
                out.append(Task.from_dict(json.loads(data)))
            except (TypeError, ValueError):
                continue  # a broken row must not hide the others
        return out

    def get(self, task_id: str) -> Task | None:
        row = self._q("SELECT data FROM tasks WHERE id = ?", (task_id,))
        if not row:
            return None
        try:
            return Task.from_dict(json.loads(row[0][0]))
        except (TypeError, ValueError):
            return None

    def save(self, task: Task) -> Task:
        Task.from_dict(asdict(task))  # validate again (fields may have been changed after construction)
        data = json.dumps(asdict(task), ensure_ascii=False)
        with self._lock:
            self._db.execute("INSERT INTO tasks(id, data, updated) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                             "data = excluded.data, updated = excluded.updated", (task.id, data, time.time()))
            self._db.commit()
        return task

    def remove(self, task_id: str) -> bool:
        with self._lock:
            n = self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount
            self._db.execute("DELETE FROM log WHERE task_id = ?", (task_id,))
            self._db.execute("UPDATE approvals SET status = 'rejected', reply = 'görev silindi', resolved = ? "
                             "WHERE task_id = ? AND status = 'pending'", (time.time(), task_id))
            self._db.commit()
        return n > 0

    def state(self, task_id: str) -> dict[str, Any]:
        row = self._q("SELECT state FROM tasks WHERE id = ?", (task_id,))
        try:
            return json.loads(row[0][0]) if row else {}
        except ValueError:
            return {}

    def set_state(self, task_id: str, **change: Any) -> dict[str, Any]:
        """Merge keys into the task's state (read-modify-write under one lock)."""
        with self._lock:
            st = self.state(task_id)
            st.update(change)
            self._db.execute("UPDATE tasks SET state = ? WHERE id = ?", (json.dumps(st, ensure_ascii=False), task_id))
            self._db.commit()
        return st

    def remember(self, task_id: str, note: str) -> None:
        note = " ".join(str(note or "").split())[:500]
        if not note:
            return
        with self._lock:
            mem = list(self.state(task_id).get("memory") or [])
            mem.append({"ts": time.time(), "text": note})
            self.set_state(task_id, memory=mem[-MEMORY_KEEP:])

    def forget(self, task_id: str) -> None:
        self.set_state(task_id, memory=[])

    # ---- log
    def log(self, task_id: str, kind: str, text: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO log(task_id, ts, kind, text) VALUES (?, ?, ?, ?)",
                             (task_id, time.time(), kind, str(text)[:4000]))
            self._db.execute("DELETE FROM log WHERE task_id = ? AND id NOT IN (SELECT id FROM log WHERE task_id = ? "
                             "ORDER BY id DESC LIMIT ?)", (task_id, task_id, LOG_KEEP))
            self._db.commit()

    def entries(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._q("SELECT ts, kind, text FROM log WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit))
        return [{"ts": r[0], "kind": r[1], "text": r[2]} for r in rows]

    # ---- approvals
    def ask(self, task_id: str, title: str, body: str = "", action: str = "") -> Approval:
        a = Approval(uuid.uuid4().hex[:10], task_id, time.time(), str(title)[:200], str(body)[:4000],
                     str(action)[:4000])
        self._x("INSERT INTO approvals(id, task_id, ts, title, body, action, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (a.id, a.task_id, a.ts, a.title, a.body, a.action, a.status))
        return a

    def approvals(self, status: str | None = "pending", task_id: str | None = None, limit: int = 100) -> list[Approval]:
        sql, args = "SELECT id, task_id, ts, title, body, action, status, reply, resolved, result FROM approvals " \
                    "WHERE 1 = 1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if task_id:
            sql += " AND task_id = ?"
            args.append(task_id)
        rows = self._q(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit))
        return [Approval(*r) for r in rows]

    def approval(self, approval_id: str) -> Approval | None:
        rows = self._q("SELECT id, task_id, ts, title, body, action, status, reply, resolved, result FROM approvals "
                       "WHERE id = ?", (approval_id,))
        return Approval(*rows[0]) if rows else None

    def answer(self, approval_id: str, decision: str, reply: str = "") -> Approval:
        """The user's answer: approve | reject | reply. Only a pending approval can be answered (an approval answered
        on the phone must not be answered again on the desktop)."""
        status = {"approve": "approved", "reject": "rejected", "reply": "replied"}.get(decision)
        if status is None:
            raise ValueError(f"geçersiz cevap: {decision}")
        if status == "replied" and not str(reply).strip():
            raise ValueError("cevap boş olamaz")
        n = self._x("UPDATE approvals SET status = ?, reply = ?, resolved = ? WHERE id = ? AND status = 'pending'",
                    (status, str(reply)[:4000], time.time(), approval_id))
        a = self.approval(approval_id)
        if a is None:
            raise KeyError(approval_id)
        if not n:
            raise ValueError("bu onay zaten cevaplanmış")
        word = {"approved": "Onaylandı", "rejected": "Reddedildi"}.get(status, "Cevap")
        self.log(a.task_id, "answer", f"{word}: {a.title}" + (f" — {reply}" if reply else ""))
        return a

    def answered(self) -> list[Approval]:
        """Answered approvals the core has not acted on yet."""
        return [a for s in ("approved", "replied", "rejected") for a in self.approvals(s, limit=50)]

    def claim(self, approval_id: str, from_status: str) -> bool:
        """Move an answered approval to 'running' so two cores (or two ticks) never act on it twice."""
        return self._x("UPDATE approvals SET status = 'running' WHERE id = ? AND status = ?",
                       (approval_id, from_status)) > 0

    def finish(self, approval_id: str, ok: bool, result: str) -> None:
        self._x("UPDATE approvals SET status = ?, result = ? WHERE id = ?",
                ("done" if ok else "failed", str(result)[:2000], approval_id))


# --------------------------------------------------------------------------- triggers
@dataclass
class Fire:
    reason: str                 # why it runs now, shown to the agent and in the log
    items: list[str] = field(default_factory=list)  # new things (issues, files) for the agent to handle


def _github_fetch(repo: str, what: str, cfg: Any = None) -> list[dict[str, Any]]:
    """Newest open issues (or PRs) of a repo: [{id, number, title, url, body}]."""
    import httpx

    from alice.tools import github

    headers = {"Accept": "application/vnd.github+json"}
    token = next(iter(github._token_candidates(cfg)), None)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    path = "pulls" if what == "pulls" else "issues"
    r = httpx.get(f"{github.API}/repos/{repo}/{path}", params={"state": "open", "sort": "created",
                                                               "direction": "desc", "per_page": 20},
                  headers=headers, timeout=15)
    r.raise_for_status()
    out = []
    for it in r.json():
        if path == "issues" and it.get("pull_request"):
            continue
        out.append({"id": int(it["number"]), "number": it["number"], "title": it.get("title") or "",
                    "url": it.get("html_url") or "", "body": (it.get("body") or "")[:1500],
                    "author": (it.get("user") or {}).get("login", "")})
    return out


class Triggers:
    """Decides whether a task should run now. Cursors (last seen issue, files seen) live in the task state, so a
    restart neither repeats old items nor loses new ones. The first look at a source only records what is there."""

    def __init__(self, book: TaskBook, store: Any = None, mode: Callable[[], str] = lambda: "normal",
                 github_fetch: Callable[..., list[dict[str, Any]]] | None = None, cfg: Any = None):
        self.book, self.store, self.mode, self.cfg = book, store, mode, cfg
        self.github_fetch = github_fetch or (lambda repo, what: _github_fetch(repo, what, cfg))

    def check(self, task: Task, now: float) -> Fire | None:
        t = task.trigger
        kind = t.get("type", "manual")
        st = self.book.state(task.id)
        if st.get("run_now"):
            self.book.set_state(task.id, run_now=False)
            return Fire("elle çalıştırıldı")
        fn = getattr(self, f"_{kind}", None)
        return fn(task, t, st, now) if fn else None

    def _schedule(self, task: Task, t: dict, st: dict, now: float) -> Fire | None:
        from alice.core.schedule import Cron

        base = st.get("last_fire") or st.get("armed") or task.created
        if not st.get("armed"):
            self.book.set_state(task.id, armed=now)
        nxt = Cron(str(t["cron"])).next_after(datetime.fromtimestamp(base)).timestamp()
        if nxt > now:
            return None
        if now - nxt > 600 and not t.get("catch_up", True):
            self.book.set_state(task.id, last_fire=now)  # missed while off: wait for the next time
            return None
        return Fire(f"zamanı geldi ({describe_trigger(t)})")

    def _metric(self, task: Task, t: dict, st: dict, now: float) -> Fire | None:
        if self.store is None:
            return None
        import fnmatch

        from alice.core.rules import Rule, RuleEngine

        cooldown = float(t.get("cooldown", 6 * 3600))
        if now - float(st.get("last_fire") or 0) < cooldown:
            return None
        rule = Rule(metric=str(t["metric"]), op=str(t["op"]), threshold=float(t.get("threshold", 0)), message="",
                    duration=float(t.get("duration") or 0))
        if any(c in rule.metric for c in "*?["):
            keys = [k for k in self.store.keys(since=now - 3600) if fnmatch.fnmatchcase(k, rule.metric)]
        else:
            keys = [rule.metric]
        engine = RuleEngine.__new__(RuleEngine)
        engine.store = self.store
        for key in keys:
            value = engine.check(rule, key, now)
            if value is not None:
                return Fire(f"{key} = {value:g} ({describe_trigger(t)})")
        return None

    def _event(self, task: Task, t: dict, st: dict, now: float) -> Fire | None:
        if self.store is None:
            return None
        since = st.get("event_since")
        if since is None:  # first look: only events from now on
            self.book.set_state(task.id, event_since=now)
            return None
        rx = re.compile(str(t["match"]), re.I) if t.get("match") else None
        hits = []
        newest = float(since)
        for e in reversed(self.store.events(since=float(since) + 1e-6, limit=200)):
            newest = max(newest, e.ts)
            if t.get("kind") and e.kind != t["kind"]:
                continue
            if t.get("topic") and e.topic != t["topic"]:
                continue
            if rx and not rx.search(f"{e.title}\n{e.body}"):
                continue
            hits.append(f"{e.title}: {e.body}".strip(": "))
        if newest > float(since):
            self.book.set_state(task.id, event_since=newest)
        return Fire(f"olay geldi ({describe_trigger(t)})", hits) if hits else None

    def _due_poll(self, t: dict, st: dict, now: float) -> bool:
        every = max(60.0, float(t.get("every_minutes", SOURCE_EVERY / 60)) * 60)
        return now - float(st.get("polled") or 0) >= every

    def _github(self, task: Task, t: dict, st: dict, now: float) -> Fire | None:
        if not self._due_poll(t, st, now):
            return None
        self.book.set_state(task.id, polled=now)
        items = self.github_fetch(str(t["repo"]), str(t.get("what", "issues")))
        seen = st.get("gh_last")
        top = max((int(i["id"]) for i in items), default=int(seen or 0))
        if seen is None:
            self.book.set_state(task.id, gh_last=top)
            return None
        new = sorted((i for i in items if int(i["id"]) > int(seen)), key=lambda i: int(i["id"]))
        if not new:
            return None
        self.book.set_state(task.id, gh_last=top)
        lines = [f"#{i['number']} {i['title']} ({i.get('author', '')}) {i.get('url', '')}\n{i.get('body', '')}".strip()
                 for i in new]
        return Fire(f"{t['repo']} deposunda {len(new)} yeni kayıt", lines)

    def _folder(self, task: Task, t: dict, st: dict, now: float) -> Fire | None:
        if not self._due_poll({"every_minutes": t.get("every_minutes", 1)}, st, now):
            return None
        self.book.set_state(task.id, polled=now)
        folder = Path(str(t["path"])).expanduser()
        if not folder.is_dir():
            return None
        pattern = str(t.get("pattern") or "*")
        names = sorted(p.name for p in folder.glob(pattern) if not p.name.startswith((".", "~$"))
                       and not p.name.endswith((".crdownload", ".part", ".tmp")))
        seen = st.get("seen")
        self.book.set_state(task.id, seen=names[-1000:])
        if seen is None:
            return None
        new = [n for n in names if n not in set(seen)]
        return Fire(f"{folder} klasörüne {len(new)} yeni dosya", [str(folder / n) for n in new]) if new else None


# --------------------------------------------------------------------------- agent tools


class TaskAsk(Tool):
    name = "task_ask"
    description = ("Background task: ask the user to approve something that needs their decision (they get a "
                   "notification and approve, reject or reply from the phone or the desktop). Use this instead "
                   "of doing it yourself for every irreversible or outgoing step (writing a comment, sending a "
                   "message, deleting a file, buying something). title: a short question. body: everything the "
                   "user needs to see to decide (draft text included). action: exactly what to do if approved "
                   "(an instruction to yourself, detailed). Write title and body in Turkish.")
    parameters = obj({"title": {"type": "string"}, "body": {"type": "string"}, "action": {"type": "string"}},
                     ["title", "action"])
    risk = READ

    def run(self, ctx: ToolContext, title: str, action: str, body: str = "") -> ToolResult:
        asks = ctx.state.setdefault("task_asks", [])
        if len(asks) >= 5:
            return ToolResult("Enough approvals were asked for in this run; leave the rest for the next run.",
                              is_error=True)
        asks.append({"title": title, "body": body, "action": action})
        return ToolResult("The approval request went to the user. Don't wait for the answer; carry on without doing "
                          "this job.")


class TaskNote(Tool):
    name = "task_note"
    description = ("Background task: write a note to this task's memory; it is shown to you on the next run "
                   "(what was done, what the user liked/disliked, what to watch for). Write short, lasting "
                   "information.")
    parameters = obj({"text": {"type": "string"}}, ["text"])
    risk = READ

    def run(self, ctx: ToolContext, text: str) -> ToolResult:
        ctx.state.setdefault("task_notes", []).append(str(text))
        return ToolResult("Note saved.")


# --------------------------------------------------------------------------- runner
PROMPT = """You are carrying out a standing task you were given, in the background. The user is not at the \
computer. Time: {now}.

# Task: {name}
Responsibility: {responsibility}

# Why now
{reason}
{items}
# The task's memory (notes you left yourself in earlier runs)
{memory}

# Recent events
{recent}

# Rules
- {permission}
- For anything that needs the user's decision, is irreversible or goes outside, ask for approval with `task_ask`
  (title, a detailed body, the action to take if approved). Preparing a draft is fine; sending it needs approval.
- Save what you need to know on the next run with `task_note` (so you don't do the same thing twice).
- If there is nothing to do, do nothing. When done, write the result in 1-3 short sentences in Turkish; if you did
  nothing, write only "sessiz"."""

FOLLOWUP = """The user answered for the background task "{name}". Time: {now}.
Responsibility: {responsibility}

You asked: {title}
{body}
The job you proposed: {action}

The user's answer: {answer}

{todo}
When done, write what you did in 1-3 short sentences in Turkish.

# The task's memory
{memory}"""

PERMISSION_TEXT = {
    "read": "This task can only read: every step that changes files, runs commands or goes outside is refused; "
            "use `task_ask` for those.",
    "act": "This task can edit files and run commands; outgoing or dangerous steps are refused, use `task_ask` "
           "for those.",
    "full": "The user gave this task full permission: you may do what is needed without asking. Still use "
            "`task_ask` for a decision you are unsure of.",
}

# permission level -> (agent permission mode, answer to steps that still ask)
LEVELS = {"read": ("ask", "n"), "act": ("auto", "n"), "full": ("auto", "y")}

AgentFn = Callable[[Task, str, str, str, dict], str]


def make_task_agent(cfg: Any, router_factory: Callable[[], Any] | None = None) -> AgentFn:
    """The unattended agent of a task: clean context, the task's model role and tool groups, the given permission
    mode. Tools write asks/notes into `state` (the runner reads them after the run)."""

    def run(task: Task, prompt: str, mode: str, approve: str, state: dict) -> str:
        from alice.agent import Agent, NullUI
        from alice.decider import Decider
        from alice.permissions import Permissions
        from alice.providers.router import Router
        from alice.tools import all_tools, cleanup, groups

        router = router_factory() if router_factory else Router(cfg)
        decider = Decider.from_config(cfg, router)
        cwd = Path(str(task.trigger.get("path") or "")).expanduser() if task.trigger.get("type") == "folder" else None
        ctx = ToolContext(cwd=cwd if cwd and cwd.is_dir() else Path.home(), config=cfg, router=router,
                          decider=decider)
        allowed = set(task.tools) | {"notify"}
        ctx.state["tool_groups"] = set(allowed)
        ctx.state.update(state)
        tools = [t for t in all_tools(cfg) if t.name in groups.CORE or t.name in allowed
                 or groups.group_of(t.name) in allowed]
        tools = [t for t in tools if t.name not in ("enter_plan_mode", "ask_user")] + [TaskAsk(), TaskNote()]
        agent = Agent(cfg, router, tools, ctx, Permissions(mode), NullUI(approve=approve), decider)
        agent.role = task.role if task.role in ROLES else "brain"
        try:
            text = agent.run_turn(prompt)
        finally:
            state.update({k: ctx.state.get(k) for k in ("task_asks", "task_notes") if ctx.state.get(k)})
            cleanup(ctx)
            decider.close()
            router.close()
        return text or ""

    return run


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


class TaskRunner:
    """Called by the core on every tick: checks triggers, starts due runs and acts on answered approvals."""

    def __init__(self, book: TaskBook, agent: AgentFn | None, notify: Callable[..., Any] | None = None,
                 store: Any = None, mode: Callable[[], str] = lambda: "normal",
                 github_fetch: Callable[..., list[dict[str, Any]]] | None = None, cfg: Any = None,
                 clock: Callable[[], float] = time.time):
        self.book, self.agent, self.store, self.clock = book, agent, store, clock
        self.notify = notify or (lambda *a, **k: None)   # notify(title, body, severity, kind)
        self.triggers = Triggers(book, store, mode, github_fetch, cfg)
        self.running: set[str] = set()
        self.sync = False  # tests: run on the calling thread
        self._lock = threading.Lock()

    # ---- tick
    def tick(self, now: float | None = None) -> list[str]:
        """Returns the ids of the tasks started this tick."""
        now = self.clock() if now is None else now
        started = []
        if self.agent is None:
            return started
        for a in self.book.answered():
            self._start(f"ap:{a.id}", self._followup, a)
        for task in self.book.list():
            if not task.enabled or task.id in self.running:
                continue
            if now - float(self.book.state(task.id).get("last_fire") or 0) < MIN_GAP:
                continue
            try:
                fire = self.triggers.check(task, now)
            except Exception as e:  # noqa: BLE001 - network down, folder gone: log once in a while, keep others
                self._trigger_error(task, e, now)
                continue
            if fire is None:
                continue
            self.book.set_state(task.id, last_fire=now)
            self.book.log(task.id, "trigger", fire.reason + (f" ({len(fire.items)} öğe)" if fire.items else ""))
            if self._start(task.id, self._run, task, fire):
                started.append(task.id)
        return started

    def run_now(self, task_id: str) -> None:
        """'Run now' from the app: the core picks it up on its next tick (the app may be another process)."""
        self.book.set_state(task_id, run_now=True, last_fire=0)

    def _trigger_error(self, task: Task, e: Exception, now: float) -> None:
        st = self.book.state(task.id)
        if now - float(st.get("error_at") or 0) > 3600:
            self.book.set_state(task.id, error_at=now)
            self.book.log(task.id, "error", f"tetik kontrol edilemedi: {str(e)[:300]}")

    def _start(self, key: str, fn: Callable[..., None], *args: Any) -> bool:
        with self._lock:
            if key in self.running:
                return False
            self.running.add(key)

        def work() -> None:
            try:
                fn(*args)
            finally:
                self.running.discard(key)

        if self.sync:
            work()
        else:
            threading.Thread(target=work, daemon=True, name=f"alice-task-{key}").start()
        return True

    # ---- runs
    def _context(self, task: Task) -> tuple[str, str]:
        st = self.book.state(task.id)
        mem = "\n".join(f"- {_fmt(m['ts'])}: {m['text']}" for m in (st.get("memory") or [])[-15:]) or "(empty)"
        recent = "\n".join(f"- {_fmt(e['ts'])} [{e['kind']}] {e['text'][:200]}"
                           for e in reversed(self.book.entries(task.id, 8))) or "(first run)"
        return mem, recent

    def _run(self, task: Task, fire: Fire) -> None:
        mem, recent = self._context(task)
        items = ("\nNew items:\n" + "\n\n".join(f"* {i}" for i in fire.items[:15]) + "\n") if fire.items else ""
        prompt = PROMPT.format(now=datetime.now().strftime("%A %d.%m.%Y %H:%M"), name=task.name,
                               responsibility=task.responsibility, reason=fire.reason, items=items, memory=mem,
                               recent=recent, permission=PERMISSION_TEXT[task.permission])
        mode, approve = LEVELS[task.permission]
        self._execute(task, prompt, mode, approve)

    def _execute(self, task: Task, prompt: str, mode: str, approve: str, approval: Approval | None = None) -> str:
        state: dict[str, Any] = {"standing_task": task.id}
        self.book.log(task.id, "run", "takip: " + approval.title if approval else "çalışıyor")
        try:
            text = " ".join((self.agent(task, prompt, mode, approve, state) or "").split())[:2000]  # type: ignore[misc]
            ok = True
        except Exception as e:  # noqa: BLE001 - one failed run must not stop the task or the core
            text, ok = f"çalıştırma başarısız: {str(e)[:300]}", False
        for note in state.get("task_notes") or []:
            self.book.remember(task.id, note)
        for ask in state.get("task_asks") or []:
            a = self.book.ask(task.id, ask.get("title") or "Onay", ask.get("body") or "", ask.get("action") or "")
            self.book.log(task.id, "approval", a.title)
            self.notify(f"Onay bekliyor · {task.name}", a.title, "warn", "approval")
        silent = text.lower().strip(" .") in SILENT
        self.book.log(task.id, "result" if ok else "error", text or "sessiz")
        self.book.set_state(task.id, last_run=self.clock(), last_result=text, last_ok=ok)
        if approval is not None:
            self.book.finish(approval.id, ok, text)
        if not ok:
            self.notify(f"Görev hata verdi · {task.name}", text, "warn", "task")
        elif task.notify and not silent:
            self.notify(f"Görev · {task.name}", text, "info", "task")
        return text

    def _followup(self, a: Approval) -> None:
        if not self.book.claim(a.id, a.status):
            return
        task = self.book.get(a.task_id)
        if task is None:
            self.book.finish(a.id, False, "görev silinmiş")
            return
        if a.status == "rejected":
            self.book.remember(task.id, f"Kullanıcı reddetti: {a.title}" + (f" ({a.reply})" if a.reply else ""))
            self.book.finish(a.id, True, "reddedildi, yapılmadı")
            return
        mem, _ = self._context(task)
        if a.status == "approved":
            answer = "APPROVED." + (f" Their note: {a.reply}" if a.reply else "")
            todo = "Do the job you proposed now; only that, nothing more. The user gave permission for this job."
            mode, approve = "auto", "y"
        else:
            answer = a.reply
            todo = ("Carry on according to the answer. If a new decision or an irreversible step is needed, ask with "
                    "`task_ask` again.")
            mode, approve = LEVELS[task.permission]
        prompt = FOLLOWUP.format(name=task.name, now=datetime.now().strftime("%A %d.%m.%Y %H:%M"),
                                 responsibility=task.responsibility, title=a.title, body=a.body, action=a.action,
                                 answer=answer, todo=todo, memory=mem)
        self._execute(task, prompt, mode, approve, approval=a)


# --------------------------------------------------------------------------- templates
TEMPLATES: list[dict[str, Any]] = [
    {"template": "github-issues", "name": "GitHub issue'larına cevap taslağı",
     "responsibility": "Depoya açılan her yeni issue'yu oku, gerekiyorsa kodda ilgili yere bak ve kısa, nazik, "
                       "yardımcı bir cevap taslağı hazırla. Taslağı `task_ask` ile bana göster; onaylarsam issue'ya "
                       "yorum olarak yaz. Etiket önerin varsa ekle. Spam ya da kopya issue'ları belirt.",
     "trigger": {"type": "github", "repo": "sahip/depo", "what": "issues", "every_minutes": 10},
     "tools": ["github", "web"], "permission": "read", "role": "brain"},
    {"template": "downloads-cleanup", "name": "Disk dolunca İndirilenler temizliği",
     "responsibility": "Disk dolmaya yaklaştığında İndirilenler klasörüne bak: eski kurulum dosyaları, büyük "
                       "arşivler, kopyalar ve aylardır açılmamış dosyalar. Silinecekleri boyutlarıyla listele, kaç GB "
                       "açılacağını yaz ve `task_ask` ile onay iste; onaylarsam yalnız listedekileri Geri "
                       "Dönüşüm Kutusu'na taşı. Önemli görünen belgeleri asla önerme.",
     "trigger": {"type": "metric", "metric": "pc.disk.*.percent", "op": ">", "threshold": 90, "cooldown": 86400},
     "tools": ["files", "system"], "permission": "read", "role": "fast"},
    {"template": "flight-coach", "name": "Uçuş sonrası koçluk notu",
     "responsibility": "Her DCS / War Thunder uçuşu bittiğinde uçuş değerlendirmesini oku ve bana kısa bir koçluk "
                       "notu yaz: iyi giden bir şey, düzeltilecek en önemli bir şey (somut sayılarla: G, hız, yakıt, "
                       "iniş) ve bir sonraki uçuş için tek bir alıştırma. Önceki notlarına bakıp ilerlemeyi söyle; "
                       "her uçuştan öğrendiğini `task_note` ile kaydet.",
     "trigger": {"type": "event", "kind": "flight", "match": "Uçuş değerlendirmesi"},
     "tools": ["flight", "now"], "permission": "read", "role": "brain"},
]


def template(name: str) -> dict[str, Any] | None:
    return next((dict(t, trigger=dict(t["trigger"])) for t in TEMPLATES if t["template"] == name), None)


# --------------------------------------------------------------------------- views (app / phone)
def task_view(book: TaskBook, t: Task, running: set[str] | None = None) -> dict[str, Any]:
    st = book.state(t.id)
    return {**asdict(t), "when": describe_trigger(t.trigger), "last_run": st.get("last_run"),
            "last_result": st.get("last_result", ""), "last_ok": st.get("last_ok", True),
            "memory": list(reversed(st.get("memory") or [])), "running": bool(running and t.id in running),
            "pending": len(book.approvals("pending", t.id))}


def approval_view(book: TaskBook, a: Approval, names: dict[str, str] | None = None) -> dict[str, Any]:
    if names is None:
        task = book.get(a.task_id)
        name = task.name if task else ""
    else:
        name = names.get(a.task_id, "")
    return {**asdict(a), "task": name}


def inbox(book: TaskBook) -> list[dict[str, Any]]:
    names = {t.id: t.name for t in book.list()}
    return [approval_view(book, a, names) for a in book.approvals("pending")]
