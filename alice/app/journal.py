"""Automations → Notes and Logs: `alice notes`, `alice brief`, `alice report`, `alice log`, `alice eval` from the app.

Notes: things ALICE was told to "take note of"; search, add, delete. Morning brief and weekly report shown now or
scheduled as tasks to arrive automatically each day/week. Logs: what ALICE sent to whom (messages, emails), disliked
replies, and the test suite (runs cases in the background, displays progress).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from alice.i18n import _

BRIEF_ID, REPORT_ID = "brief", "weekly-report"


def _task(task_id: str) -> dict[str, Any] | None:
    from alice.core.schedule import Cron, TaskStore

    t = TaskStore().get(task_id)
    if t is None:
        return None
    parts = t.cron.split()
    return {"when": Cron(t.cron).describe(), "at": f"{int(parts[1]):02d}:{int(parts[0]):02d}" if len(parts) > 1
            and parts[0].isdigit() and parts[1].isdigit() else "", "weekdays": parts[4] == "1-5" if len(parts) > 4
            else False, "enabled": t.enabled}


def _hhmm(value: Any, default: str) -> str:
    s = str(value or default).strip()
    try:
        h, m = (int(x) for x in s.split(":"))
    except ValueError as e:
        raise ValueError(_("the time must be HH:MM (given: {value})", value=s)) from e
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(_("invalid time: {value}", value=s))
    return f"{h:02d}:{m:02d}"


class Journal:
    def __init__(self, cfg: Any):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._eval: dict[str, Any] = {"state": "idle", "done": 0, "total": 0, "current": "", "results": []}
        self._eval_stop = threading.Event()
        self._eval_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ Notes tab
    def notes_view(self, q: str = "") -> dict[str, Any]:
        from alice.extras import notes

        found = notes.search(q) if q.strip() else notes.all_notes()
        return {"notes": [{"ts": n.ts, "when": f"{datetime.fromtimestamp(n.ts):%d.%m.%Y %H:%M}", "text": n.text}
                          for n in reversed(found[-200:])],
                "total": len(notes.all_notes()), "brief": _task(BRIEF_ID), "report": _task(REPORT_ID)}

    def notes_action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        from alice.core.schedule import TaskStore
        from alice.extras import brief, notes, report

        try:
            if action == "add":
                text = str(d.get("text", "")).strip()
                if not text:
                    return {"ok": False, "error": _("empty note")}
                notes.add(text)
                return {"ok": True}
            if action == "remove":
                return {"ok": notes.remove(float(d.get("ts", 0))) is not None}
            if action == "brief_show":
                from alice.core.store import Store

                with Store() as st:
                    data = brief.gather(self.cfg.data.get("brief", {}), st, TaskStore())
                return {"ok": True, "text": brief.render(data)}
            if action == "brief_install":
                brief.install(_hhmm(d.get("at"), "08:00"), weekdays_only=bool(d.get("weekdays")))
                return {"ok": True}
            if action == "report_show":
                from alice.core.store import Store

                with Store() as st:
                    r = report.build(st, max(1, min(90, int(d.get("days") or 7))))
                page = report.save_html(r)
                if d.get("open"):
                    from alice.app.automations import open_file

                    open_file(page)
                return {"ok": True, "text": report.text(r), "path": str(page)}
            if action == "report_install":
                report.install(_hhmm(d.get("at"), "20:00"))
                return {"ok": True}
            if action in ("brief_remove", "report_remove"):
                return {"ok": TaskStore().remove(BRIEF_ID if action == "brief_remove" else REPORT_ID)}
        except (ValueError, TypeError, OSError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": _("unknown action: {action}", action=action)}

    # ------------------------------------------------------------------ Logs tab
    def logs_view(self) -> dict[str, Any]:
        from alice import actionlog, evals

        sent = [{"when": time.strftime("%d.%m %H:%M", time.localtime(float(r.get("ts") or 0))), "tool": r.get("tool", ""),
                 "summary": r.get("summary", ""), "outcome": r.get("outcome", ""),
                 "status": actionlog.OUTCOMES.get(str(r.get("outcome")), str(r.get("outcome"))),
                 "detail": r.get("detail", "")} for r in actionlog.recent(60)]
        feedback = [{"when": time.strftime("%d.%m %H:%M", time.localtime(float(r.get("ts") or 0))),
                     "say": r.get("say", ""), "reply": r.get("reply", "")[:400], "note": r.get("note", ""),
                     "source": r.get("source", "")} for r in reversed(evals.recent_feedback(30))]
        try:
            import json

            last = json.loads(evals.results_path().read_text(encoding="utf-8"))
            if not isinstance(last, dict):  # manually corrupted/partial log: treat as never run, let tab open
                raise ValueError(_("the result file is not an object"))
            last = {"when": time.strftime("%d.%m %H:%M", time.localtime(float(last.get("ts") or 0))),
                    "passed": last.get("passed"), "total": last.get("total")}
        except (OSError, ValueError, TypeError, OverflowError):
            last = None
        try:
            cases = len(evals.load_cases())
        except Exception:  # noqa: BLE001 - corrupted evals.toml: let tab open anyway
            cases = 0
        return {"sent": sent, "feedback": feedback, "cases": cases, "cases_path": str(evals.evals_path()),
                "last_run": last, "run": self.eval_view()}

    def eval_view(self) -> dict[str, Any]:
        with self._lock:
            return {**self._eval, "results": list(self._eval["results"])}

    def logs_action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        from alice import evals

        if action == "eval_run":
            if self._eval_thread is not None and self._eval_thread.is_alive():
                return {"ok": False, "error": _("the tests are already running")}
            cases = evals.load_cases()
            if not cases:
                return {"ok": False, "error": _("the test set is empty: give 👎 to an answer you didn't like, or add a case")}
            self._eval_stop.clear()
            with self._lock:
                self._eval = {"state": "running", "done": 0, "total": len(cases), "current": cases[0].label,
                              "results": [], "summary": ""}
            self._eval_thread = threading.Thread(target=self._run_evals, args=(cases,), daemon=True,
                                                 name="alice-eval")
            self._eval_thread.start()
            return {"ok": True}
        if action == "eval_stop":
            self._eval_stop.set()
            return {"ok": True}
        if action == "eval_add":
            text = str(d.get("text", "")).strip()
            if not text:
                return {"ok": False, "error": _("empty")}
            return {"ok": True, "added": evals.add_cases([text], note="uygulamadan") > 0}
        if action == "eval_from_log":
            n = evals.add_cases(evals.sayings_from_log(int(d.get("n") or 20)), "voice.log'dan")
            return {"ok": True, "added": n}
        if action == "eval_open":
            from alice.app.automations import open_file

            p = evals.evals_path()
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# ALICE test seti: bkz. alice/evals.py\n", encoding="utf-8")
            try:
                open_file(p)
            except OSError as e:
                return {"ok": False, "error": _("could not open it: {error} ({path})", error=e, path=p)}
            return {"ok": True}
        return {"ok": False, "error": _("unknown action: {action}", action=action)}

    def _run_evals(self, cases: list[Any]) -> None:
        from alice import evals

        def on_case(i: int, o: Any) -> None:
            with self._lock:
                self._eval["done"] = i
                self._eval["current"] = cases[i].label if i < len(cases) else ""
                self._eval["results"].append({"label": o.case.label, "passed": o.passed, "reasons": o.reasons,
                                              "reply": o.reply[:300], "tools": o.tools, "seconds": round(o.seconds, 1),
                                              "model": o.model})

        try:
            outcomes = evals.run_all(cases, on_case=on_case, stop=self._eval_stop.is_set, cfg=self.cfg)
            cmp = evals.save_results(outcomes) if outcomes else {}
            passed = sum(o.passed for o in outcomes)
            summary = _("{passed}/{total} passed", passed=passed, total=len(outcomes))
            if cmp.get("before") is not None:
                summary += _(" (before: {before}/{total})", before=cmp["before"], total=cmp["before_total"])
            if cmp.get("fixed"):
                summary += _(" · fixed: ") + ", ".join(cmp["fixed"])
            if cmp.get("broken"):
                summary += " · bozulanlar: " + ", ".join(cmp["broken"])
            with self._lock:
                self._eval.update(state="stopped" if self._eval_stop.is_set() else "done", current="", summary=summary)
        except Exception as e:  # noqa: BLE001 - display on screen
            with self._lock:
                self._eval.update(state="error", current="", summary=_("could not run: {error}", error=e))
