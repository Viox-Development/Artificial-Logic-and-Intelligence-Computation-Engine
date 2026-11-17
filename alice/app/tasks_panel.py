"""Tasks page ("Görevler") and the approvals inbox, for the desktop app and the phone (/api/tasks).

The app only edits the task database (alice/core/tasks.py); the background core runs the tasks. Both processes
share the SQLite file, so a task created or an approval answered here is picked up on the core's next tick.
"""

from __future__ import annotations

from typing import Any

from alice.core.tasks import (PERMISSIONS, ROLES, TEMPLATES, TRIGGERS, Task, TaskBook, approval_view, describe_trigger,
                              inbox, task_view, template)

PARSE_PROMPT = """Turn the user's request into a standing background task for a desktop assistant (ALICE) running on
the user's own PC. Reply ONLY with JSON:
{{"name": "<short Turkish name>",
  "responsibility": "<complete Turkish instruction: what to watch, what to do, when to ask the user first>",
  "trigger": <one of:
     {{"type": "schedule", "cron": "<5-field cron>"}}
     {{"type": "metric", "metric": "<store key or glob, e.g. pc.disk.*.percent, pc.gpu.temp, watch.hr>", "op": ">",
       "threshold": 90, "duration": <seconds it must hold, 0 = now>}}
     {{"type": "event", "kind": "<event kind, e.g. flight>", "match": "<regex on the event title/body>"}}
     {{"type": "github", "repo": "owner/name", "what": "issues|pulls"}}
     {{"type": "folder", "path": "<folder>", "pattern": "*"}}
     {{"type": "manual"}}>,
  "tools": [<tool groups it needs, from: {groups}>],
  "role": "brain|fast|code"}}
User request: {text}"""


class TasksPanel:
    def __init__(self, cfg: Any = None, router: Any = None, book: TaskBook | None = None):
        self.cfg, self.router = cfg, router
        self._book = book

    @property
    def book(self) -> TaskBook:
        if self._book is None:
            self._book = TaskBook()
        return self._book

    def view(self) -> dict[str, Any]:
        from alice.core import service
        from alice.tools import groups

        b = self.book
        tasks = b.list()
        names = {t.id: t.name for t in tasks}
        recent = [approval_view(b, a, names) for a in b.approvals(None, limit=40) if a.status != "pending"][:15]
        return {"tasks": [task_view(b, t) for t in tasks], "approvals": inbox(b), "answered": recent,
                "templates": TEMPLATES, "triggers": list(TRIGGERS), "permissions": list(PERMISSIONS),
                "roles": list(ROLES), "groups": sorted(groups.GROUPS),
                "core": bool(service.read_state().get("running"))}

    def log(self, task_id: str) -> dict[str, Any]:
        t = self.book.get(task_id)
        if t is None:
            return {"ok": False, "error": "görev yok"}
        return {"ok": True, "task": task_view(self.book, t), "log": self.book.entries(task_id, 100),
                "approvals": [approval_view(self.book, a, {t.id: t.name})
                              for a in self.book.approvals(None, task_id, 30)]}

    def approvals(self) -> dict[str, Any]:
        return {"approvals": inbox(self.book)}

    def action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, f"_{action}", None)
        if fn is None or action.startswith("_"):
            return {"ok": False, "error": f"bilinmeyen işlem: {action}"}
        try:
            return fn(d)
        except (ValueError, KeyError, TypeError) as e:
            return {"ok": False, "error": str(e).strip("'\"") or type(e).__name__}

    # ---- tasks
    @staticmethod
    def _fields(d: dict[str, Any]) -> dict[str, Any]:
        keys = ("name", "responsibility", "trigger", "tools", "permission", "role", "notify", "enabled", "template")
        return {k: d[k] for k in keys if k in d}

    def _create(self, d: dict[str, Any]) -> dict[str, Any]:
        base = template(str(d["template"])) if d.get("template") else {}
        if d.get("template") and base is None:
            raise ValueError("böyle bir şablon yok")
        t = self.book.save(Task.from_dict({**(base or {}), **self._fields(d)}))
        self.book.log(t.id, "info", "görev oluşturuldu: " + describe_trigger(t.trigger))
        return {"ok": True, "task": task_view(self.book, t)}

    def _update(self, d: dict[str, Any]) -> dict[str, Any]:
        t = self.book.get(str(d["id"]))
        if t is None:
            raise ValueError("görev yok")
        from dataclasses import asdict

        new = Task.from_dict({**asdict(t), **self._fields(d)})
        if new.trigger != t.trigger:  # a new source starts clean: its first look only records what is there
            self.book.set_state(t.id, seen=None, gh_last=None, event_since=None, armed=None, polled=0)
        self.book.save(new)
        self.book.log(t.id, "info", "görev düzenlendi")
        return {"ok": True, "task": task_view(self.book, new)}

    def _enable(self, d: dict[str, Any]) -> dict[str, Any]:
        on = bool(d.get("enabled", True))
        r = self._update({"id": d["id"], "enabled": on})
        self.book.log(str(d["id"]), "info", "devam ettirildi" if on else "duraklatıldı")
        return r

    def _remove(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"ok": self.book.remove(str(d["id"]))}

    def _run(self, d: dict[str, Any]) -> dict[str, Any]:
        tid = str(d["id"])
        if self.book.get(tid) is None:
            raise ValueError("görev yok")
        self.book.set_state(tid, run_now=True, last_fire=0)
        self.book.log(tid, "info", "şimdi çalıştır istendi")
        return {"ok": True}

    def _forget(self, d: dict[str, Any]) -> dict[str, Any]:
        self.book.forget(str(d["id"]))
        self.book.log(str(d["id"]), "info", "hafıza silindi")
        return {"ok": True}

    def _parse(self, d: dict[str, Any]) -> dict[str, Any]:
        """Plain sentence -> a filled task form (shown to the user before saving)."""
        from alice.decider import extract_json
        from alice.tools import groups

        text = str(d.get("text") or "").strip()
        if not text:
            raise ValueError("ne yapmasını istediğini yaz")
        if self.router is None:
            raise ValueError("model yok")
        msgs = [{"role": "user", "content": PARSE_PROMPT.format(groups=", ".join(sorted(groups.GROUPS)), text=text)}]
        res = self.router.chat("fast", msgs, temperature=0, max_tokens=700)
        data = extract_json(res.text)
        if not isinstance(data, dict):
            raise ValueError("model görev döndürmedi")
        draft = {"name": data.get("name") or text[:40], "responsibility": data.get("responsibility") or text,
                 "trigger": data.get("trigger") if isinstance(data.get("trigger"), dict) else {"type": "manual"},
                 "tools": [g for g in (data.get("tools") or []) if g in groups.GROUPS],
                 "role": data.get("role") if data.get("role") in ROLES else "brain", "permission": "read"}
        Task.from_dict(draft)  # make sure it can be saved; the user still reviews it
        return {"ok": True, "draft": draft, "when": describe_trigger(draft["trigger"])}

    # ---- approvals
    def _answer(self, d: dict[str, Any]) -> dict[str, Any]:
        a = self.book.answer(str(d["id"]), str(d.get("decision", "")), str(d.get("reply") or ""))
        return {"ok": True, "approval": approval_view(self.book, a)}
