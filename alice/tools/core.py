"""Tools communicating with the core: historical metrics, rules, scheduled tasks, mode.

This allows the brain model to directly handle requests like "what was the GPU temperature yesterday while playing DCS?",
"warn me if my heart rate stays high for a long time", "backup my projects every Sunday night".
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from alice.tools.base import DANGER, NETWORK, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


def _store(ctx: ToolContext):
    from alice.core.store import Store

    st = ctx.state.get("core_store")
    if st is None:
        st = ctx.state["core_store"] = Store()
    return st


class Metrics(Tool):
    name = "metrics"
    parallel_safe = False  # sqlite connection is shared in ctx.state
    description = ("PAST measurements and events recorded by the core: computer (pc.*), flight (dcs.*), watch/heart "
                   "rate (watch.hr) and so on. For questions like 'how hot was the GPU in DCS yesterday', 'CPU over "
                   "the last 2 hours'. For the current state use system_info. If you don't know the key, use "
                   "action=keys first.\n"
                   "E.g. GPU temperature during yesterday's flight: action=stats key=pc.gpu.temp since=yesterday "
                   "until=today mode=flight.")
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "health", "keys", "stats", "series", "events", "modes"],
                   "description": "status: is the core running, which mode | health: is anything in ALICE's "
                                  "components (calendar, Gmail, models, disk, recurring errors) broken right now "
                                  "('how are you doing', 'is something wrong') | keys: keys matching pattern | stats: "
                                  "min/avg/max for key | series: key's values over time | events: the event log | "
                                  "modes: time ranges when modes were on."},
        "key": {"type": "string", "description": "Key for stats/series, e.g. 'pc.gpu.temp'; stats also takes a glob "
                                                 "('pc.disk.*.percent')."},
        "pattern": {"type": "string", "description": "Glob for keys, e.g. 'pc.*'. Default '*'."},
        "since": {"type": "string", "description": "Start: '15m', '2h', '3d' (back from now), 'yesterday', 'today' or "
                                                   "an ISO date."},
        "until": {"type": "string", "description": "End, same format as since. Empty: now."},
        "mode": {"type": "string", "enum": ["normal", "dev", "flight", "quiet"],
                 "description": "Only measurements taken while this mode was on (flight = DCS/War Thunder)."},
    }, ["action"])

    def run(self, ctx: ToolContext, action: str, key: str | None = None, pattern: str = "*",
            since: str | None = None, until: str | None = None, mode: str | None = None) -> ToolResult:
        from alice.core.service import read_state
        from alice.core.store import parse_time

        try:
            t0, t1 = parse_time(since), parse_time(until)
        except ValueError as e:
            raise ToolError(str(e)) from e
        st = _store(ctx)
        if action == "status":
            s = read_state()
            if not s.get("running"):
                return ToolResult("the core is not running (start it: alice core run; permanently: alice core install)")
            p = s.get("policy", {})
            return ToolResult(f"the core is running, mode: {s.get('mode')}{' (pinned by hand)' if s.get('forced') else ''}, "
                              f"lowest notification: {p.get('min_severity')}, voice: {p.get('voice')}, "
                              f"local GPU: {p.get('local_gpu')}")
        if action == "health":
            from alice import health_report

            return ToolResult(health_report.summary_now(ctx.config, st))
        if action == "keys":
            ks = st.keys(pattern or "*", since=t0)
            return ToolResult("\n".join(ks) or "no data")
        if action == "events":
            evs = [e for e in st.events(since=t0, limit=100) if t1 is None or e.ts <= t1]
            return ToolResult("\n".join(e.line() for e in evs) or "no events")
        if action == "modes":
            out = []
            for m in [mode] if mode else ["flight", "dev", "quiet"]:
                for a, b in st.mode_spans(m, t0, t1):
                    out.append(f"{m}: {datetime.fromtimestamp(a):%d.%m %H:%M} - {datetime.fromtimestamp(b):%H:%M} "
                               f"({(b - a) / 60:.0f} dk)")
            return ToolResult("\n".join(out) or "no mode records in this range")
        if not key:
            raise ToolError("key is required (action=keys to see the keys)")
        if action == "stats":
            keys = st.keys(key, since=t0) if any(c in key for c in "*?[") else [key]
            return ToolResult("\n".join(st.stats(k, t0, t1, mode).line() for k in keys[:30]) or "no data")
        pts = st.series(key, t0, t1, mode)
        if not pts:
            return ToolResult("no data")
        step = max(1, len(pts) // 60)
        rows = [f"{datetime.fromtimestamp(t):%d.%m %H:%M:%S} {v:.1f}" for t, v in pts[::step]]
        return ToolResult(f"{key}, {len(pts)} measurements" + (f" (1 in every {step} shown)" if step > 1 else "") + "\n"
                          + "\n".join(rows))


class Rules(Tool):
    name = "rules"
    description = ("Alert rules tied to measurements: 'warn me if the GPU goes over 85 degrees', 'if my heart rate "
                   "stays above 120 for 10 minutes'. The core watches the measurements and notifies when the "
                   "condition holds. For a reminder or job at a set TIME use schedule. If you don't know the metric "
                   "key, use metrics action=keys first.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "enable", "disable"],
                   "description": "list | add: yeni kural (metric, op, threshold, message gerekli) | "
                   "remove/enable/disable: id'li kural."},
        "id": {"type": "string", "description": "Rule number for remove/enable/disable (from the list output)."},
        "metric": {"type": "string", "description": "Key or glob to watch, e.g. 'pc.gpu.temp', 'pc.disk.*.percent', "
                                                    "'watch.hr'."},
        "op": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "!="], "description": "Comparison."},
        "threshold": {"type": "number", "description": "Threshold value, e.g. 85."},
        "duration": {"type": "number", "description": "Seconds the condition must hold without a break. Default 0."},
        "cooldown": {"type": "number", "description": "Seconds to wait before warning again. Default 1800."},
        "message": {"type": "string", "description": "Notification text; the placeholders {value}, {key}, {match}, "
                                                     "{threshold} can be used, e.g. 'GPU {value} degrees!'."},
        "severity": {"type": "string", "enum": ["info", "warn", "critical"], "description": "Importance. Default "
                                                                                            "warn."},
        "topic": {"type": "string", "description": "Topic label, e.g. 'pc', 'flight', 'health'. Default pc."},
        "modes": {"type": "array", "items": {"type": "string"},
                  "description": "Only active in these modes, e.g. [\"flight\"]. Empty: every mode."},
        "confirm": {"type": "string", "description": "If false alarms are likely, a yes/no question to ask the fast "
                                                     "model before warning."},
        "rule_action": {"type": "string", "enum": ["notify", "agent"],
                        "description": "notify: only notify (default) | agent: ALICE does the job in prompt."},
        "prompt": {"type": "string", "description": "For rule_action=agent, a description of the job ALICE should do."},
        "description": {"type": "string", "description": "The user's own sentence, e.g. 'tell me if the GPU gets "
                                                         "hot'."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("action") == "list":
            return READ
        # Rules that run an autonomous agent when triggered require confirmation.
        return DANGER if args.get("rule_action") == "agent" else WRITE

    def run(self, ctx: ToolContext, action: str, id: str | None = None, rule_action: str = "notify",
            **fields: Any) -> ToolResult:
        from alice.core.rules import Rule, RuleSet

        rs = RuleSet()
        if action == "list":
            return ToolResult("\n".join(r.summary() for r in rs.rules) or "no rules")
        if action == "add":
            for req in ("metric", "op", "threshold", "message"):
                if fields.get(req) in (None, ""):
                    raise ToolError(f"add needs {req}")
            try:
                rule = Rule.from_dict({**{k: v for k, v in fields.items() if v is not None}, "action": rule_action,
                                       **({"id": id} if id else {})})
            except (TypeError, ValueError) as e:
                raise ToolError(str(e)) from e
            rs.add(rule)
            return ToolResult(f"kural kuruldu: {rule.summary()}")
        if not id:
            raise ToolError("id is required")
        ok = rs.remove(id) if action == "remove" else rs.set_enabled(id, action == "enable")
        return ToolResult("done" if ok else f"no rule: {id}", is_error=not ok)


class Schedule(Tool):
    name = "schedule"
    description = ("Jobs that run at set times or repeat: 'tell me the weather every morning at 8', 'back up my "
                   "projects every Sunday night', 'remind me of the meeting tomorrow at 17:00'. At that time ALICE "
                   "does the job in prompt unattended. Tell the user when and what will happen before setting it up. "
                   "For measurement-based alerts (temperature, heart rate) use rules.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "run_now"],
                   "description": "list | add: a new job (cron and prompt required) | remove: delete the job with id "
                                  "| run_now: run the job with id right now."},
        "id": {"type": "string", "description": "Job number for remove/run_now (from the list output)."},
        "cron": {"type": "string", "description": "5-field cron: minute hour day month day-of-week (0 = Sunday). E.g. "
                                                  "'0 8 * * *' every day at 08:00, '30 22 * * 0' Sunday 22:30, '0 17 "
                                                  "11 10 *' 11 October 17:00."},
        "prompt": {"type": "string", "description": "A complete description of the job to do at that time; write it "
                                                    "so someone who hasn't seen the conversation understands it."},
        "description": {"type": "string", "description": "Short name, e.g. 'morning weather'."},
        "catch_up": {"type": "boolean", "description": "If missed while the computer was off, do it at startup. "
                                                       "Default true."},
        "once": {"type": "boolean", "description": "One-off job ('call in 5 minutes', 'remind me tomorrow at 17:00'): "
                                                   "pass true and put the exact minute/hour/day/month in cron. It "
                                                   "deletes itself after running; false (default) for repeating jobs."},
        "notify": {"type": "boolean", "description": "Report the result when done. Default true."},
        "project": {"type": "string", "description": "add: at that time, start this project template as an autonomous "
                                                     "project instead of prompt ('run the dependency update project "
                                                     "every Monday'). Templates are saved on the Projects page. If "
                                                     "prompt is given, it is added to the goal as a note."},
        "cwd": {"type": "string", "description": "project: working folder; empty: the template's."},
    }, ["action"])
    risk = DANGER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "list" else (WRITE if args.get("action") == "remove" else DANGER)

    def summary(self, args: dict[str, Any]) -> str:
        if args.get("action") == "add" and args.get("cron"):
            from alice.core.schedule import Cron

            try:
                when = Cron(args["cron"]).describe()
            except ValueError:
                when = args["cron"]
            return f"{when}: {args.get('description') or args.get('prompt', '')[:120]}"
        return super().summary(args)

    def run(self, ctx: ToolContext, action: str, id: str | None = None, cron: str | None = None,
            prompt: str | None = None, description: str = "", catch_up: bool = True, notify: bool = True,
            once: bool = False, project: str = "", cwd: str = "") -> ToolResult:
        from alice.core.schedule import ScheduledTask, TaskStore

        ts = TaskStore()
        if action == "list":
            return ToolResult("\n".join(t.summary() for t in ts.tasks) or "no scheduled tasks")
        if action == "add":
            if not cron or not (prompt or project):
                raise ToolError("add needs cron and prompt (or project)")
            if project:
                from alice.team_store import list_templates

                names = [str(t.get("name", "")) for t in list_templates()]
                if project.lower() not in [n.lower() for n in names]:
                    raise ToolError(f"there is no project template called '{project}'. Available: {', '.join(names) or '(none)'}"
                                    ". A template is saved from a project's menu on the Projects page.")
            try:
                task = ScheduledTask(cron=cron, prompt=prompt or "", description=description, catch_up=catch_up,
                                     notify=notify, once=bool(once), created=time.time(), project=project or "",
                                     cwd=str(ctx.resolve(cwd)) if cwd else "")
            except ValueError as e:
                raise ToolError(str(e)) from e
            ts.add(task)
            from alice.core.service import read_state

            warn = "" if read_state().get("running") else \
                "\nWarning: the core is not running; for the task to run: `alice core install` or `alice core run`."
            return ToolResult(f"task set up: {task.summary()}{warn}")
        if not id:
            raise ToolError("id is required")
        if action == "remove":
            if not ts.remove(id):  # treat unknown id as an error so the model doesn't think it was deleted
                raise ToolError(f"no task: {id}. action=list for the ids")
            return ToolResult("deleted")
        task = ts.get(id)
        if task is None:
            raise ToolError(f"no task: {id}")
        task.pending = True
        ts.save()
        return ToolResult("it will run on the next core tick")


def _obj_arg(name: str, v: Any) -> dict[str, Any]:
    """Object parameters arrive as JSON text (some providers don't accept free-form objects)."""
    import json

    if v in (None, ""):
        return {}
    if isinstance(v, dict):
        return v
    try:
        out = json.loads(str(v))
    except ValueError as e:
        raise ToolError(f"{name} is not valid JSON: {e}") from e
    if not isinstance(out, dict):
        raise ToolError(f"{name} must be a JSON object, e.g. {{\"any\": [\"alice\"]}}")
    return out


class Feeds(Tool):
    name = "feeds"
    description = (
        "Feeds that watch connected services in the background and tell you when something important happens: 'tell "
        "me if I get a mention in the Discord general channel', 'let me know when Ahmet starts a game on Steam', "
        "'tell me when a video comes to the DCS channel', 'don't disturb me at night, give me a summary in the "
        "morning'. For a measurement threshold (GPU temperature) use rules; for a set time, schedule.\n"
        "- First connectors: which connection is ready, its actions and fields.\n"
        "- Before add, see sample items with test_action; take field names for the title/body templates and the when "
        "condition from there (nested as {author.username}).\n"
        "- For new items mode=new; for a field changing (started a game, came online) mode=change + watch.\n"
        "- Avoid needless notifications: filter with when, silence the night with quiet, mark urgent ones with "
        "escalate."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "connectors", "test_action", "add", "test", "remove",
                                              "enable", "disable"],
                   "description": "list: feeds | connectors: connections and their actions | test_action: call a "
                                  "connection action now and show sample items | add: a new feed | test: poll a "
                                  "defined feed, whether and why each item passed | remove/enable/disable: the feed "
                                  "with name."},
        "name": {"type": "string", "description": "Feed name (add, test, remove, enable, disable), e.g. 'ahmet_game'."},
        "connector": {"type": "string", "description": "Connection name, e.g. 'discord', 'steam', 'youtube_api'."},
        "connector_action": {"type": "string", "description": "The connection's action, e.g. 'messages', 'profile'."},
        "args": {"type": "string", "description": "Action parameters as JSON text, e.g. '{\"channel_id\": \"123\"}'."},
        "every_minutes": {"type": "number", "description": "Polling interval in minutes. Default 10, at least 1."},
        "mode": {"type": "string", "enum": ["new", "change"],
                 "description": "new: an item newly in the list | change: when a field in watch changes."},
        "watch": {"type": "array", "items": {"type": "string", "description": "Field name."},
                  "description": "Fields to watch for mode=change, e.g. [\"gameextrainfo\"]."},
        "id_field": {"type": "string", "description": "The field that identifies an item. Default 'id' ('steamid' on "
                                                      "Steam)."},
        "title": {"type": "string", "description": "Notification title template, e.g. '{personaname} started a game'."},
        "body": {"type": "string", "description": "Notification body template, e.g. '{content}'. In change mode "
                                                  "{prev.field} is the old value, {changed} the changed fields."},
        "when": {"type": "string", "description": "Which item is worth a notification, JSON: any/all/none (word "
                                                  "lists), regex, fields (field -> value or list, \"*\" not empty), "
                                                  "min/max (numeric), ask (a yes/no question by meaning). E.g. "
                                                  "'{\"any\": [\"alice\"], \"none\": [\"bot\"]}'."},
        "escalate": {"type": "string", "description": "If this condition holds, importance is critical (said even in "
                                                      "quiet hours); same format as when."},
        "quiet": {"type": "string", "description": "Hours to hold notifications, e.g. '23:00-08:00'; a summary comes "
                                                   "when they end."},
        "digest_minutes": {"type": "number", "description": "A summary at this interval instead of one by one. 0: "
                                                            "off."},
        "cooldown_minutes": {"type": "number", "description": "After a notification, collect what comes in for this "
                                                              "long."},
        "severity": {"type": "string", "enum": ["info", "warn", "critical"], "description": "Importance. Default "
                                                                                            "info."},
        "agent": {"type": "string", "description": "If set, ALICE wakes up when the notification goes out and does "
                                                   "this job (summarize, check). It asks for approval because it runs "
                                                   "unattended."},
        "description": {"type": "string", "description": "The user's own sentence (kept as a note in the record)."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("list", "connectors"):
            return READ
        if a in ("test", "test_action"):
            return NETWORK
        return DANGER if a == "add" and args.get("agent") else WRITE

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action", "")
        if a in ("add", "test", "remove", "enable", "disable"):
            return f"{a} {args.get('name', '')}".strip()
        if a == "test_action":
            return f"{args.get('connector', '')}.{args.get('connector_action', '')}"
        return a

    def run(self, ctx: ToolContext, action: str, name: str = "", connector: str = "", connector_action: str = "",
            args: Any = None, **fields: Any) -> ToolResult:
        from alice.core import feedfile
        from alice.core.feeds import FeedMonitor, load_feeds

        if action == "list":
            feeds, errors = load_feeds()
            lines = [f"{f.name}{'' if f.enabled else ' (off)'}: {f.describe()}" for f in feeds]
            return ToolResult("\n".join(lines + [f"error: {e}" for e in errors]) or "no feeds")
        if action == "connectors":
            return ToolResult(_connectors_text(ctx))
        if action == "test_action":
            return ToolResult(_test_action(ctx, connector, connector_action, _obj_arg("args", args)))
        if action in ("remove", "enable", "disable", "test") and not name:
            raise ToolError(f"{action} needs name")
        if action == "remove":
            return ToolResult("deleted" if feedfile.remove_feed(name) else f"no feed: {name}")
        if action in ("enable", "disable"):
            ok = feedfile.set_enabled(name, action == "enable")
            return ToolResult(("turned on" if action == "enable" else "turned off") if ok else f"no feed: {name}")
        if action == "test":
            feed = next((f for f in load_feeds()[0] if f.name == name), None)
            if feed is None:
                raise ToolError(f"no feed: {name}")
            try:
                verdicts = FeedMonitor(cfg=ctx.config, decider=ctx.decider).explain(feed)
            except Exception as e:  # noqa: BLE001 - return connection error clearly to the model
                raise ToolError(f"could not poll: {e}") from e
            lines = [f"{'✓' if v.ok else '✗'} {v.title}" + (f" — {v.body}" if v.body else "")
                     + f"  ({v.reason}{', ' + v.severity if v.ok and v.severity != feed.severity else ''})"
                     for v in verdicts[:15]]
            return ToolResult(f"{sum(v.ok for v in verdicts)}/{len(verdicts)} items pass\n" + "\n".join(lines))
        if action == "add":
            return ToolResult(_add(ctx, name, connector, connector_action, _obj_arg("args", args), fields))
        raise ToolError(f"unknown action: {action}")


def _connectors_text(ctx: ToolContext) -> str:
    from alice.connect.manifest import discover
    from alice.connect.runner import ready

    out = []
    for m in sorted(discover(ctx.config)[0].values(), key=lambda x: x.name):
        ok, note = ready(m)
        out.append(f"{m.name} ({'ready' if ok else note})")
        for a in m.actions.values():
            if a.writes or a.sends:
                continue  # feed checks read-only actions only
            params = ", ".join(f"{p.name}{'*' if p.required else ''}" for p in a.params.values())
            fields = f" alanlar: {', '.join(a.fields)}" if a.fields else ""
            out.append(f"  - {a.name}({params}): {a.summary}{fields}")
    return "\n".join(out) or "no connections"


def _test_action(ctx: ToolContext, connector: str, action: str, args: dict[str, Any]) -> str:
    import json

    from alice.connect.manifest import discover
    from alice.connect.runner import ConnectError, call, pick, ready

    m = discover(ctx.config)[0].get(connector)
    if m is None:
        raise ToolError(f"no such connection: {connector}")
    act = m.actions.get(action)
    if act is None:
        raise ToolError(f"{connector}: no action {action}. Available: {', '.join(sorted(m.actions))}")
    if act.writes or act.sends:
        raise ToolError(f"{connector}.{action} changes/sends something; a feed only polls read-only actions")
    ok, note = ready(m)
    if not ok:
        raise ToolError(f"{m.label} is not ready: {note}")
    try:
        data = call(m, act, args, cache=False)
    except ConnectError as e:
        raise ToolError(str(e)) from e
    rows = pick(data, act.pick) if act.pick else data
    rows = rows if isinstance(rows, list) else [rows]
    sample = json.dumps(rows[:3], ensure_ascii=False, indent=1)
    return f"{len(rows)} items. The first {min(3, len(rows))} (take the field names from here):\n{sample[:6000]}"


def _add(ctx: ToolContext, name: str, connector: str, action: str, args: dict[str, Any],
         fields: dict[str, Any]) -> str:
    from alice.connect.manifest import discover
    from alice.connect.runner import ready
    from alice.core import feedfile

    if not (name and connector and action):
        raise ToolError("add needs name, connector and connector_action")
    m = discover(ctx.config)[0].get(connector)
    if m is None:
        raise ToolError(f"no such connection: {connector}. Look with feeds action=connectors")
    act = m.actions.get(action)
    if act is None:
        raise ToolError(f"{connector}: no action {action}. Available: {', '.join(sorted(m.actions))}")
    if act.writes or act.sends:
        raise ToolError(f"{connector}.{action} changes/sends something; give the feed a read-only action")
    missing = [p.name for p in act.params.values() if p.required and not str(args.get(p.name, "")).strip()]
    if missing:
        raise ToolError(f"missing in args for {connector}.{action}: {', '.join(missing)}")
    feed: dict[str, Any] = {"name": name, "connector": connector, "action": action, "args": args}
    for k in ("every_minutes", "mode", "watch", "id_field", "title", "body", "quiet", "digest_minutes",
              "cooldown_minutes", "severity", "agent", "description"):
        if fields.get(k) not in (None, "", []):
            feed[k] = fields[k]
    for sub in ("when", "escalate"):
        tab = _obj_arg(sub, fields.get(sub))
        if tab:
            feed[sub] = tab
    try:
        feedfile.add_feed(feed)
    except ValueError as e:
        raise ToolError(str(e)) from e
    hazır = "" if ready(m)[0] else f" Note: {m.label} is not ready yet; it works once the key is added."
    return (f"feed added: {name}. While the core runs it is polled every {feed.get('every_minutes', 10)} minutes; "
            f"the first round is silent (current items count as seen).{hazır} To try it: feeds action=test name={name}")


class ActionLog(Tool):
    name = "action_log"
    parallel_safe = True
    description = ("Returns the log of what ALICE sent to others on the user's behalf (Discord/WhatsApp messages, "
                   "DMs, emails): sent, refused by the user, blocked in plan mode, and failed.\n"
                   "- Use it for 'who did you write to today and what?', 'what did you send on my behalf?', 'did the "
                   "message go out?'.\n"
                   "- It only reads the log; it doesn't send or undo anything.")
    parameters = obj({
        "limit": {"type": "integer", "description": "Maximum entries. Default 20."},
        "hours": {"type": "number", "description": "Only the last this many hours. Empty: all."},
    })
    risk = READ

    def run(self, ctx: ToolContext, limit: int = 20, hours: float | None = None) -> ToolResult:
        from alice import actionlog

        since = time.time() - float(hours) * 3600 if hours else 0.0
        rows = actionlog.recent(max(1, min(int(limit), 200)), since)
        return ToolResult("\n".join(actionlog.format_row(r) for r in rows) or "nothing recorded: nothing was sent to anyone")


TOOLS = [Metrics(), Rules(), Schedule(), Feeds(), ActionLog()]
