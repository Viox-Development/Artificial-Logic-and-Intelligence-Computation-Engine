"""Tools for the agent to manage itself: todo list, persistent memory, sub-agent, reflex."""

from __future__ import annotations

from typing import Any

from alice.i18n import _
from alice.tools.base import COMPUTER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class TodoWrite(Tool):
    name = "todo_write"
    description = ("Keeps the plan and progress of a multi-step job in a todo list; the user sees the list live.\n"
                   "- Use it for jobs with 3 or more steps, when the user asks for several things, or when the job "
                   "will take long. Don't use it for single-step jobs or chat.\n"
                   "- Send the WHOLE list on every call. Keep only one in_progress at a time; mark a step done as "
                   "soon as it is finished, don't save several up for the end.\n"
                   "- If a step isn't really finished (a test failed, an error remains), don't mark it done; add the "
                   "blocker as a new step.")
    parameters = obj({"todos": {"type": "array", "description": "The whole current list.", "items": obj({
        "content": {"type": "string", "description": "The step, a short sentence in the imperative, e.g. 'Run the "
                                                     "tests'."},
        "status": {"type": "string", "enum": ["pending", "in_progress", "done"],
                   "description": "pending: not started | in_progress: being done now | done: finished."}},
        ["content", "status"])}}, ["todos"])

    def summary(self, args: dict[str, Any]) -> str:
        todos = args.get("todos") or []
        done = sum(1 for t in todos if t.get("status") == "done")
        return f"{len(todos)} tasks, {done} done"

    def run(self, ctx: ToolContext, todos: list[dict[str, Any]]) -> ToolResult:
        ctx.state["todos"] = todos
        mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        text = "\n".join(f"{mark.get(t.get('status'), '[ ]')} {t.get('content')}" for t in todos)
        return ToolResult(text or "the list is empty")


class Memory(Tool):
    name = "memory"
    description = ("Long-term memory: NOTES about the user and the people, areas and topics in their life (one file "
                   "each; the index is in the system prompt) and past conversations.\n"
                   "- save: write a note or update the one with that name. category: you (profile/preferences), "
                   "people (one note per person, by name), areas (ongoing project/internship/community), topics "
                   "(interests, hardware, tools). Put a person's nicknames in aliases; link related notes with "
                   "[[name]]. Update a similar note instead of adding one. 'Remember' -> save now; 'forget' -> "
                   "delete.\n"
                   "- read: a whole note by name or nickname ('gravity' -> efe); no name: all.\n"
                   "- When the user refers to the past ('last time', 'yesterday'), look first: search_chats (topic), "
                   "recent_chats (time, after/before). Don't make it up.\n"
                   "- log: add to today's journal. dream: tidy up memory now. restore: back to a Dream backup.\n"
                   "- followup: something to ask/remind of later (due); done once asked.")
    parameters = obj({
        "action": {"type": "string", "enum": ["save", "read", "delete", "list", "search", "log", "dream", "restore",
                                              "search_chats", "recent_chats", "followup", "followups", "done",
                                              "add", "update", "forget"],
                   "description": "save | read | delete | list (notes) | search (search notes) | log (add to the "
                                  "journal) | dream (tidy up) | restore (go back to a backup; lists the backups if "
                                  "backup is empty) | search_chats | recent_chats | followup | followups | done. "
                                  "add/update/forget are old names (save/delete)."},
        "name": {"type": "string", "description": "Short name of the note (kebab-case; a person's name: 'efe', an "
                                                  "area: 'staj'); for read/delete/save. read also accepts a nickname. "
                                                  "A note in another scope: 'project:name'."},
        "category": {"type": "string", "description": "save: you | people | areas | topics (or another suitable "
                                                      "folder name). An existing note stays where it is; a different "
                                                      "category moves it there."},
        "aliases": {"type": "array", "items": {"type": "string"}, "description": "save: nicknames, user names, other "
                                                                                 "spellings (important for people: "
                                                                                 "['gravity'])."},
        "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                 "description": "Old field, optional; use category."},
        "description": {"type": "string", "description": "save: one line. For a person: 'Name (\"nickname\") — who "
                                                         "they are to the user, where from, related people; read when "
                                                         "Name or nickname comes up'. Recall looks at this."},
        "content": {"type": "string", "description": "save: the whole note (markdown, short bullets). Link related "
                                                     "notes with [[name]]. Write relative dates as absolute ones. "
                                                     "When updating, merge the old content and write it in full."},
        "title": {"type": "string", "description": "save: short title shown in the index (empty: the name)."},
        "scope": {"type": "string", "enum": ["genel", "proje"], "description": "save: global (default) or project "
                                                                               "(only valid in the current git "
                                                                               "repository)."},
        "note": {"type": "string", "description": "Text for log/followup; one sentence for add."},
        "kind": {"type": "string", "description": "Old kind name for add (preference, fact, person...)."},
        "id": {"type": "integer", "description": "Number of the follow-up, for done."},
        "query": {"type": "string", "description": "Search phrase for search, search_chats or forget."},
        "backup": {"type": "string", "description": "restore: the backup to go back to (YYYYMMDD-HHMMSS)."},
        "due": {"type": "string", "description": "Day for followup, YYYY-MM-DD. Empty if kind=open."},
        "followup_kind": {"type": "string", "enum": ["ask", "remind", "open"],
                          "description": "Follow-up type: ask ask later | remind remind on that day | open unfinished "
                                         "work."},
        "n": {"type": "integer", "description": "How many conversations for recent_chats (default 5, at most 20)."},
        "after": {"type": "string", "description": "search_chats/recent_chats: from this day on (YYYY-MM-DD, "
                                                   "inclusive)."},
        "before": {"type": "string", "description": "search_chats/recent_chats: before this day (YYYY-MM-DD)."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        # Writing a note is memory's normal job (Claude Code doesn't ask either); deleting or restoring a backup
        # can be undone, but the user's information may be lost, so it asks.
        return WRITE if args.get("action") in ("delete", "forget", "restore") else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('name') or args.get('note') or args.get('query') or args.get('id') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, name: str | None = None, type: str | None = None,  # noqa: A002
            description: str | None = None, content: str | None = None, title: str | None = None,
            scope: str | None = None, note: str | None = None, kind: str | None = None, id: int | str | None = None,  # noqa: A002
            query: str | None = None, backup: str | None = None, text: str | None = None, due: str | None = None,
            followup_kind: str | None = None, n: int | None = None, after: str | None = None,
            before: str | None = None, category: str | None = None,
            aliases: list[str] | str | None = None) -> ToolResult:
        from alice.memdir import MemoryFull
        from alice.memory import _day_start, _parse_day, shared_memory

        mem = ctx.state.get("memory") or shared_memory(ctx.config, ctx.router)
        if mem is None:
            raise ToolError("memory is off (config.toml [memory] enabled = false)")
        note = note or text
        cwd = ctx.cwd
        if action in ("save", "add", "update"):
            ctx.state["memory_wrote"] = True
            try:
                if action == "save" or content:
                    if not (content or description):
                        raise ToolError("save needs content (and description)")
                    cat = category or {"user": "", "feedback": "you", "project": "areas",
                                       "reference": "topics"}.get(type or "", "")
                    if (name or "").strip().lower() in ("profile", "preferences"):
                        cat = "you"
                    al = [a.strip() for a in (aliases.split(",") if isinstance(aliases, str) else aliases or [])
                          if str(a).strip()]
                    rec, new = mem.save_note(name or title or description or "", description or "", type or "",
                                             content or description or "", title=title or "", source="araç",
                                             scope=scope or "genel", cwd=cwd, category=cat, aliases=al)
                    where = rec.rel if hasattr(rec, "rel") else rec.path
                    return ToolResult(f"{'saved' if new else 'updated'}: {rec.key} ({where})")
                if action == "update" and (name or id is not None):
                    return ToolResult("updated" if mem.update(str(name or id), note or "", cwd) else "no such note")
                if not note:
                    raise ToolError("add needs note")
                key, new = mem.remember(note, kind or "fact", source="araç", cwd=cwd, scope=scope or "genel")
                return ToolResult(f"saved: {key}" if new else f"similar note updated: {key}")
            except MemoryFull as e:
                raise ToolError(str(e)) from None
            except ValueError as e:
                raise ToolError(str(e)) from None
        if action in ("delete", "forget"):
            ctx.state["memory_wrote"] = True
            if name or (id is not None and not str(id).isdigit()):
                gone = mem.delete_note(str(name or id), cwd)
                return ToolResult(f"deleted (to the trash): {gone.key}" if gone else f"no such note: {name or id}")
            if not (query or note):
                raise ToolError("delete needs name or query")
            gone, hits = mem.forget_matching(query or note or "", cwd)
            if gone:
                return ToolResult(f"deleted (to the trash): {gone[0].key}")
            return ToolResult("not sure which one, deleted none. Candidates (delete by name):\n"
                              + "\n".join(h.manifest_line() for h in hits) if hits else "no matching note")
        if action == "read":
            if name:
                _book, rec = mem.find_note(name, cwd)
                if rec is None:
                    for b in mem.books(cwd):  # a nickname ("gravity") reads the person's note
                        rec = b.resolve_alias(name)
                        if rec is not None:
                            break
                if rec is None:
                    raise ToolError(f"no such note: {name} (look with memory action=list)")
                links = ", ".join(f"[[{x}]]" for x in rec.links)
                return ToolResult(f"{rec.render(limit=8000)}\n\n(file: {rec.path}"
                                  + (f"; linked notes: {links}" if links else "") + ")")
            return ToolResult(mem.export_markdown(cwd))
        if action == "list":
            lines = []
            for b in mem.books(cwd):
                notes = b.scan()
                lines.append(f"## {b.scope} ({b.root}) — {len(notes)} notes")
                lines += [x.manifest_line() for x in notes] or ["(empty)"]
            return ToolResult("\n".join(lines))
        if action == "search":
            if not (query or note):
                raise ToolError("search needs query")
            hits = mem.search_notes(query or note or "", cwd)
            return ToolResult("\n".join(h.manifest_line() for h in hits) or "nothing related in the notes "
                              "(search_chats for past conversations)")
        if action == "log":
            if not note:
                raise ToolError("log needs note")
            p = mem.book(scope or "genel", cwd).append_log(note)
            return ToolResult(f"added to the daily log: {p}")
        if action == "dream":
            done = mem.dream(force=True, cwd=cwd)
            ctx.state["memory_wrote"] = True
            return ToolResult("Dream bitti:\n" + ("\n".join(f"- {x}" for x in done) or "- no changes"))
        if action == "restore":
            b = mem.book(scope or "genel", cwd)
            if not backup:
                return ToolResult("backups (newest first): " + (", ".join(b.backups()) or "none"))
            try:
                count = b.restore(backup)
            except ValueError as e:
                raise ToolError(str(e)) from None
            mem._bump()
            return ToolResult(f"restored backup {backup} ({count} notes); the state before restoring was backed up too")
        if action == "search_chats":
            if not (query or note):
                raise ToolError("search_chats needs query")
            return ToolResult(mem.search_chats(query or note or "", after=_day_start(after), before=_day_start(before)))
        if action == "recent_chats":
            return ToolResult(mem.recent_chats(n or 5, after=_day_start(after), before=_day_start(before)))
        if action == "followup":
            if not note:
                raise ToolError("followup needs note")
            fk = followup_kind or ("open" if not due else "ask")
            day = _parse_day(due)
            if fk != "open" and day is None:
                raise ToolError("followup needs due (YYYY-MM-DD); for unfinished work use followup_kind=open")
            return ToolResult(f"takip eklendi #{mem.add_followup(note, day, fk)}")
        if action == "followups":
            return ToolResult("\n".join(f.line() for f in mem.followups()) or "no open follow-ups")
        if action == "done":
            if id is None:
                raise ToolError("done needs id")
            f = mem.done_followup(int(id))
            return ToolResult(f"closed: {f.text}" if f else f"#{id} not found")
        raise ToolError(f"unknown action: {action}")


class Task(Tool):
    name = "task"
    description = ("Starts a subagent with its own clean context for an independent job (research, a search in a big "
                   "folder, a long subtask); only its result comes back, so the conversation doesn't fill up. It does "
                   "not see this conversation: put everything it needs in prompt. Not for one- or two-call jobs.\n"
                   "- Parallel: for independent jobs call several tasks in the same reply.\n"
                   "- background=true: it runs while you and the user keep chatting; the result arrives as an '[Alt "
                   "ajan aN ...]' message. Use it when you don't need the result right away or the job is long. Don't "
                   "guess its result.\n"
                   "- id: a new message to a subagent that already ran (aN); it remembers what it found. Use it for "
                   "follow-up questions on the same topic instead of a new subagent.\n"
                   "- Status and stopping: the subagents tool (list/stop); the user follows them in the Agents tab.")
    parameters = obj({
        "prompt": {"type": "string", "description": "The complete task for the subagent: what to do, where, what to "
                                                    "return. With id: the new message to send it."},
        "agent": {"type": "string", "description": "Name of a custom subagent the user defined (the 'Custom "
                                                   "subagents' list in the system prompt). Empty: a general subagent."},
        "name": {"type": "string", "description": "Short name shown on screen, e.g. 'test sweep'."},
        "background": {"type": "boolean", "description": "Run in the background; the result arrives as a message when "
                                                         "done."},
        "id": {"type": "string", "description": "Subagent to continue (aN). Empty: a new subagent."},
        "max_steps": {"type": "integer", "description": "Maximum steps. Default 40 (a custom agent has its own "
                                                        "limit)."},
    }, ["prompt"])
    risk = READ  # each of the sub-agent's own tool calls goes through its own permission check

    def parallel_for(self, ctx: ToolContext, args: dict[str, Any]) -> bool:
        return True  # each sub-agent has its own context; task calls in the same reply run at the same time

    def summary(self, args: dict[str, Any]) -> str:
        who = args.get("id") or args.get("name") or args.get("agent") or ""
        bg = " · arka planda" if args.get("background") else ""
        return (f"{who}: " if who else "") + str(args.get("prompt", ""))[:110].replace("\n", " ") + bg

    def run(self, ctx: ToolContext, prompt: str, max_steps: int | None = None, agent: str = "", name: str = "",
            background: bool = False, id: str = "") -> ToolResult:  # noqa: A002 - the name the model sees
        from alice import subagents

        parent = ctx.state.get("agent")
        if parent is None:
            raise ToolError("subagents can't be used in this context")
        if id:
            sub = subagents.resume(ctx, id, prompt, background=bool(background))
        else:
            sub = subagents.start(parent, ctx, prompt, kind=agent, label=name, background=bool(background),
                                  max_steps=max_steps)
        if sub.background:
            return ToolResult(f"subagent {sub.id} ({sub.label}) is running in the background. Its result will come to this chat as a "
                              "message when it finishes; don't guess what it will find until then. The user sees the "
                              "progress in the Agents tab of the side panel.")
        subagents.wait(sub, ctx.cancelled)
        head = f"[subagent {sub.id} · {sub.label}, {sub.steps} steps{'' if sub.status == 'done' else ', ' + sub.status}]"
        return ToolResult(f"{head}\n{sub.result}\n\n(A follow-up question to the same subagent: task id={sub.id})",
                          is_error=sub.status == "failed")


class AgentDefs(Tool):
    name = "agent_defs"
    description = ("Manages custom subagent definitions (called with agent=<name> in the task tool; like "
                   ".claude/agents in Claude Code). For 'define a test-writer agent for me', 'change the reviewer "
                   "agent's instructions', 'which agents do I have'.\n"
                   "- save: name, description (when to use it, one sentence), prompt (the agent's system "
                   "instructions: its role, how it works, what it returns), tools (empty: all; for a read-only agent "
                   "e.g. read_file, grep, glob, list_dir, git), model (brain, fast or provider:model), max_steps, "
                   "where (user: in every project, project: only in this project).\n"
                   "- list | delete (name).")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "save", "delete"], "description": "What to do."},
        "name": {"type": "string", "description": "Agent name: lowercase letters, digits, - and _, e.g. "
                                                  "'test-writer'."},
        "description": {"type": "string", "description": "save: what it does and when to call it (one sentence)."},
        "prompt": {"type": "string", "description": "save: the agent's system instructions."},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "save: allowed tools; empty: all."},
        "model": {"type": "string", "description": "save: brain, fast or provider:model; empty: the main agent's."},
        "max_steps": {"type": "integer", "description": "save: maximum steps (default 40)."},
        "where": {"type": "string", "enum": ["user", "project"], "description": "save: user (default) or project."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        return WRITE if args.get("action") in ("save", "delete") else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('name') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, name: str = "", description: str = "", prompt: str = "",
            tools: list[str] | None = None, model: str = "", max_steps: int = 40, where: str = "user") -> ToolResult:
        from alice import skills

        try:
            if action == "save":
                a = skills.save_agent(name, description, prompt, tools, model, max_steps, where, ctx.cwd)
                return ToolResult(f"saved: {a.path}. It can now be called with task agent={a.name}.")
            if action == "delete":
                return ToolResult(f"deleted: {skills.delete_agent(name, ctx.cwd)}")
        except ValueError as e:
            raise ToolError(str(e)) from e
        defs = skills.load_agents(ctx.cwd)
        return ToolResult("\n".join(
            f"{a.name}: {a.description} [tools: {', '.join(a.tools) or 'all'}; model: {a.model or 'main agent'}; "
            f"{a.max_steps} steps] ({a.path})" for a in defs.values()) or "no custom subagents defined")


class SubagentsTool(Tool):
    name = "subagents"
    description = ("Lists or stops this conversation's subagents (the ones started with task). For 'how are the "
                   "background agents doing', 'stop that agent'. If you are waiting for a result, don't ask: its "
                   "message arrives by itself when done.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "stop"], "description": "list: durumlar | stop: id'yi durdur."},
        "id": {"type": "string", "description": "stop: alt ajan (aN)."},
    }, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str, id: str = "") -> ToolResult:  # noqa: A002
        from alice import subagents

        subs = subagents.registry(ctx)
        if action == "stop":
            sub = subs.get(id.strip())
            if sub is None:
                raise ToolError(f"no such subagent: {id}")
            return ToolResult(f"{sub.id} is stopping" if subagents.stop(sub) else f"{sub.id} is not running")
        rows = [f"{s.id} [{s.status}] {s.label} · {s.steps} steps"
                + (f" · son: {s.last}" if s.status == "running" and s.last else "")
                + (f"\n   result: {s.result[:200]}" if s.result else "") for s in subs.values()]
        return ToolResult("\n".join(rows) or "no subagents in this chat")


class Reflex(Tool):
    name = "reflex"
    description = (
        "Does a goal on the screen/in the browser quickly with Jev (~1 s per step): reads the screen and picks an "
        "action each step: click, right/double click, type into a field, type into the window, a key or combo "
        "(ctrl+shift+s), hold a key, drag and drop, scroll. The result includes the text that changed on screen.\n"
        "- Use it for: 'click the song Impossible', 'open Settings > Privacy', 'drag the card to the right column'.\n"
        "- Keyboard work is one step: don't click calculator keys; pass text='1234*5678=', press_first=['escape']; "
        "combos go in keys.\n"
        "- It doesn't open apps: open with the app/windows tool first, then pass title (empty: foreground window).\n"
        "- Best on single-task goals: split long jobs into short goals, one call each; put rules/strategy in notes.\n"
        "- On 'needs_help', read the reason: change the goal, add to notes, or do the rest yourself.\n"
        "- With a TypeSafe key it also finds things not in the list (icons, game pieces) from a screenshot."
    )
    parameters = obj({
        "goal": {"type": "string", "description": "Short, concrete goal using the names as they appear on screen, "
                                                  "e.g. 'click the song Impossible on screen'."},
        "target": {"type": "string", "enum": ["browser", "desktop"],
                   "description": "browser: the front tab in the user's browser | desktop: a desktop window."},
        "url": {"type": "string", "description": "For browser: go to this address first. Empty: stay on the front "
                                                 "tab."},
        "title": {"type": "string", "description": "For desktop: part of the window title. Empty: the foreground "
                                                   "window."},
        "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                   "description": "Fields to fill: part of the field name -> text to type, e.g. {\"search\": "
                                  "\"lofi\"}."},
        "notes": {"type": "string", "description": "Context for the decision engine: rules, strategy, what is what on "
                                                   "screen, things to avoid (short, preferably in English)."},
        "text": {"type": "string", "description": "Text to type into the window with the keyboard (without picking a "
                                                  "field), e.g. '1234*5678='."},
        "keys": {"type": "array", "items": {"type": "string", "description": "Key or combination, e.g. 'shift+w'."},
                 "description": "Keys/combinations to add to the options (pressed together)."},
        "press_first": {"type": "array", "items": {"type": "string", "description": "Key, e.g. 'escape'."},
                        "description": "Keys to press before typing text: to clear previous input ('escape' in a "
                                       "calculator, 'ctrl+a' in a field)."},
        "hold_seconds": {"type": "number", "description": "How many seconds to hold a key down. Default 1."},
        "allow_irreversible": {"type": "boolean", "description": "true only if the user explicitly asked: then "
                                                                 "irreversible buttons such as send, pay, delete, "
                                                                 "buy, confirm/accept may be pressed. Default false: "
                                                                 "reflex stops at such a button (needs_confirmation)."},
        "max_steps": {"type": "integer", "description": "Maximum steps. Default 20."},
    }, ["goal", "target"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, target: str, url: str | None = None, title: str | None = None,
            inputs: dict[str, str] | None = None, notes: str = "", max_steps: int = 20, text: str = "",
            keys: list[str] | None = None, hold_seconds: float | None = None,
            press_first: list[str] | None = None, allow_irreversible: bool = False) -> ToolResult:
        from alice.config import alice_home
        from alice.reflex.agent import (ScreenAgent, make_consultant, make_planner, make_translator,
                                        multi_part_goal)
        from alice.reflex.loop import make_text_writer
        from alice.reflex.observers import BrowserObserver, DesktopObserver

        if ctx.decider is None or ctx.router is None:
            raise ToolError("the decision engine is not configured")
        if target == "browser":
            observer = BrowserObserver(ctx, url)
        else:
            observer = DesktopObserver(ctx, title, goal, echoes=_last_user_text(ctx))
        loop = ScreenAgent(observer, ctx.decider, make_text_writer(ctx.router), make_translator(ctx.router),
                           emit=ctx.emit, trace_path=alice_home() / "reflex-trace.jsonl")
        if hold_seconds:
            observer.hold_seconds = max(0.05, min(float(hold_seconds), 10.0))
        loop.allow_irreversible = bool(allow_irreversible)
        loop.consult = make_consultant(ctx.router)  # when stuck, ask from inside; no full main-model turn
        if ((getattr(ctx.config, "data", None) or {}).get("reflex", {}) or {}).get("plan", True):
            loop.plan = make_planner(ctx.router)  # multi-part goals: split into steps, verify each
        extra = {k: v for k, v in (("text", text), ("keys", keys), ("press_first", press_first)) if v}
        res = loop.run(goal, max_steps=max_steps, inputs=inputs, notes=notes or "", **extra)
        status = getattr(res, "status", "")
        verdict = reflex_verdict(status)
        if status in ("max_steps", "needs_help") and multi_part_goal(goal) and "planın" not in getattr(res, "reason", ""):
            # A long goal was tried as one piece and did not finish: the main model must not resend the same long goal.
            verdict += ("\nThis goal had several jobs in it. Don't send the same one again: look at the steps above, find where "
                        "it stopped and call the rest one by one with short single-job goals (one menu, one field, one "
                        "key per call).")
        return ToolResult(res.report() + ("\n" + verdict if verdict else ""), is_error=status == "error")


def reflex_verdict(status: str) -> str:
    """State the status plainly: models confused "Jev cevap verdi" (Jev replied) with "hedef yapıldı" (goal done)
    and told the user "çalışıyor, başardı" (it's working, it succeeded), even on a needs_help result."""
    return {"done": "RESULT: goal done.",
            "clicked": "RESULT: clicked where the goal seemed to be, but the result was not verified. Check with a screenshot "
                       "that the right thing opened; if so it's done, if not fix it yourself.",
            "likely_done": "RESULT: the last action changed the screen and the decision engine thinks the goal was most likely "
                           "done, but it isn't sure. Verify with a screenshot (or by looking at the file), then tell "
                           "the user.",
            "steps_done": "RESULT: the goal was split into steps and they were done in order; the goal as a whole was not "
                          "verified separately. Check the result yourself (was the file created, what does the window "
                          "show), then tell the user; don't say 'done' without verifying.",
            "needs_help": "RESULT: the goal was NOT done (the decision engine answered but found no way). Don't tell the user "
                          "it worked; do the rest yourself or say why.",
            "acted": "RESULT: an action was taken and the screen changed; whether what was asked happened must be verified separately.",
            "needs_confirmation": "RESULT: STOPPED at a button that may be irreversible (send/pay/delete/confirm). If the user "
                                  "didn't ask for this explicitly, ask them; if they did, call again with allow_irreversible=true.",
            "needs_login": "RESULT: STOPPED, a sign-in/session screen appeared (the goal was NOT done). Don't sign in for the user "
                           "or type numbers/passwords. If the app's desktop version is installed try it (app open/flow "
                           "picks it when installed); otherwise tell the user they need to sign in.",
            "max_steps": "RESULT: the step limit ran out; the goal was NOT done.",
            "error": "RESULT: error; the goal was NOT done."}.get(status, "")


def _last_user_text(ctx: ToolContext) -> tuple[str, ...]:
    """The user's last message: it is visible in the chat window; reflex must not mistake it for a target on screen."""
    for m in reversed(getattr((getattr(ctx, "state", None) or {}).get("agent"), "messages", None) or []):
        if m.get("role") != "user" or m.get("_nudge"):
            continue
        # Memory recalls are added to the content (project names included). When those were also counted as an
        # "echo" (yankı), projects dropped off the sidebar list. The user's own text is in _display.
        text = m.get("_display") or m.get("content")
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        return (text,) if isinstance(text, str) and text else ()
    return ()


class AskUser(Tool):
    name = "ask_user"
    description = ("In the MIDDLE of a job, when you can't continue without an answer, asks the user a question with "
                   "clickable options and WAITS for the answer (a card in the app, a numbered list in the terminal). "
                   "Example: there are two reasonable ways and the choice changes the result, or a needed piece of "
                   "information (which account, which folder) is missing. If the job is DONE and you only want to "
                   "suggest the next step, use next_step, not this. Put the option you recommend first; the user can "
                   "also type an answer that is not in the list.")
    parameters = obj({
        "question": {"type": "string", "description": "Tek, net soru."},
        "options": {"type": "array", "items": {"type": "string", "description": "Option: short label, optionally ' — "
                                                                                "' and a description."}, "description": "2-5 options."},
        "multi": {"type": "boolean", "description": "Can several be chosen? Default false."},
    }, ["question", "options"])
    risk = READ

    def run(self, ctx: ToolContext, question: str, options: list[str], multi: bool = False) -> ToolResult:
        ui = getattr(ctx.state.get("agent"), "ui", None)
        # Must be defined on the class: sub-agent/voice __getattr__ silently returns a function for any name.
        ask = getattr(ui, "ask_choice", None) if callable(getattr(type(ui), "ask_choice", None)) else None
        opts = [str(o).strip() for o in options or [] if str(o).strip()][:6]
        if len(opts) < 2:
            raise ToolError("at least two options are needed")
        if not callable(ask):
            return ToolResult("Choice questions can't be shown in this interface (voice conversation or unattended run). "
                              "Ask the question and the options briefly in your answer, then stop and wait for the user's answer.")
        answer = (ask(question, opts, bool(multi)) or "").strip()
        if not answer:
            return ToolResult("The user didn't answer or closed the question. Don't assume; ask briefly or stop.")
        return ToolResult(f"The user's answer: {answer}")


class SkillTool(Tool):
    name = "skill"
    description = ("Loads and returns the content of a skill the user taught ALICE (step-by-step instructions + "
                   "helper files).\n"
                   "- If a skill in the 'Skills' list of the system prompt has a description that fits the job, call "
                   "this BEFORE starting the job and work by the instructions it returns.\n"
                   "- Use only names in the list; don't make names up. If you already loaded it this turn, don't call "
                   "it again.")
    parameters = obj({"name": {"type": "string", "description": "Skill name (as written in the list)."}}, ["name"])
    risk = READ
    parallel_safe = True

    def run(self, ctx: ToolContext, name: str) -> ToolResult:
        from alice.skills import load_skills

        skills = load_skills(ctx.cwd)
        sk = skills.get(name.strip().lower())
        if sk is None:
            raise ToolError(f"there is no skill called '{name}'. Available: {', '.join(skills) or '(none)'}")
        files = sk.files()
        listing = ("\nFiles in the folder (paths are relative to this folder):\n" + "\n".join(f"- {f}" for f in files)) if files else ""
        return ToolResult(f"# Skill: {sk.name}\nFolder: {sk.path.parent}\n\n{sk.body()}{listing}")


class Projects(Tool):
    name = "projects"
    description = ("Projects on the user's computer (git repositories, Unity, Python, Node, .NET...): list them, find "
                   "them by name/topic, or get a project's details (path, languages, recent commits, folder contents, "
                   "README). For phrases like 'my Unity project', 'my game', 'the ALICE repo', find out here which "
                   "folder it is.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "find", "info", "rescan"],
                   "description": "list: most recently worked on | find: those matching query | info: details of the "
                                  "project in query | rescan: scan the disk again (if a new project was added)."},
        "query": {"type": "string", "description": "Project name or topic for find/info, e.g. 'fps', 'alice', "
                                                   "'unity'."},
    }, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str, query: str = "") -> ToolResult:
        from alice import projects as pj

        if action == "rescan":
            found = pj.refresh(ctx.config)
            return ToolResult(f"{len(found)} proje bulundu:\n" + "\n".join(f"- {p.line()}" for p in found[:15]))
        found = pj.get(ctx.config, background=False)
        if not found:
            return ToolResult("no projects found. Add the project folder to config.toml [projects] paths and rescan.")
        if action == "list":
            return ToolResult("\n".join(f"- {p.line()}" for p in found[:25]))
        if not query:
            raise ToolError(f"{action} needs query")
        hits = pj.find(query, found)
        if not hits:
            return ToolResult(f"no project matches '{query}'. Most recently worked on:\n"
                              + "\n".join(f"- {p.line()}" for p in found[:8]))
        if action == "info":
            return ToolResult(pj.info(hits[0]) + ("\nOther candidates: " + ", ".join(p.name for p in hits[1:5])
                                                 if len(hits) > 1 else ""))
        return ToolResult("\n".join(f"- {p.line()}" for p in hits[:10]))


class Feedback(Tool):
    name = "feedback"
    description = ("Call this if the user says they didn't like one of your recent answers ('that answer was bad', "
                   "'you talked nonsense', 'don't say it like that', 'you misunderstood'): the request and answer are "
                   "saved to the test set and tried in later updates. Tell the user briefly that you took it on board "
                   "(no apology formula); if they want you to stop doing something, also add it to memory as a "
                   "preference.")
    parameters = obj({
        "say": {"type": "string", "description": "The user's request before the disliked answer, in their own words."},
        "reply": {"type": "string", "description": "The answer you gave that was disliked."},
        "note": {"type": "string", "description": "What the user said / what was wrong."},
    }, ["note"])
    risk = READ

    def run(self, ctx: ToolContext, note: str, say: str = "", reply: str = "") -> ToolResult:
        from alice.evals import record_feedback

        new = record_feedback(say, reply, note, source="sohbet")
        return ToolResult("saved" + (", added to the test set" if new else ""))


# Files shown with send_file: the "Aç / Klasörde göster" (Open / Show in folder) buttons in the UI can only open these.
SENT_FILES: set[str] = set()
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


class SendFile(Tool):
    """Equivalent of SendUserFile in Claude Code."""

    name = "send_file"
    parallel_safe = True
    description = ("Shows files you produced or found to the user as cards in the chat (name, size, Open and Show in "
                   "folder buttons); images are previewed in the chat.\n"
                   "- Use it when you produce a result the user will want to see: a report, a chart, an exported "
                   "document, a screenshot, a built program.\n"
                   "- Don't send intermediate files, temporary output or every version of a file you are still "
                   "working on; don't send the same file again unless it changed meaningfully.\n"
                   "- The file must already be on disk; this tool doesn't create files (write_file etc. first).")
    parameters = obj({
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths of the files to show, absolute "
                                                                               "or relative to the working folder. At "
                                                                               "most 10."},
        "caption": {"type": "string", "description": "One-line note above the card, e.g. 'The error is on line 42'. "
                                                     "Leave empty if the file speaks for itself."},
    }, ["paths"])
    risk = READ

    def summary(self, args: dict[str, Any]) -> str:
        return ", ".join(str(p) for p in (args.get("paths") or [])[:3])

    def run(self, ctx: ToolContext, paths: list[str], caption: str = "") -> ToolResult:
        files, images, missing = [], [], []
        for raw in (paths or [])[:10]:
            p = ctx.resolve(str(raw))
            if not p.is_file():
                missing.append(str(p))
                continue
            SENT_FILES.add(str(p))
            files.append({"path": str(p), "name": p.name, "size": p.stat().st_size})
            if p.suffix.lower() in IMAGE_EXT and len(images) < 4:
                try:
                    from alice.tools.imaging import file_to_png_b64

                    images.append(file_to_png_b64(p, int(ctx.config.agent.get("screenshot_max_width", 1366))))
                except Exception:  # noqa: BLE001 - the card is shown even without a preview
                    pass
        if not files:
            raise ToolError("no files to show: " + ", ".join(missing))
        text = (f"{caption}\n" if caption else "") + "Shown to the user: " + ", ".join(f["name"] for f in files)
        if missing:
            text += "\nNot found: " + ", ".join(missing)
        return ToolResult(text, images=images, files=files)


class NextStep(Tool):
    """Claude Code's end-of-turn box: short summary, optional question with buttons. Does not block the turn."""

    name = "next_step"
    parallel_safe = True
    description = ("Called when the job is DONE, right before your last answer: puts a short recap box under the "
                   "chat; optionally adds one question about the next step and 1-3 buttons. It does NOT wait; the "
                   "turn ends; if the user presses a button, that text arrives as their new message. If you need an "
                   "answer to continue, use ask_user, not this.\n"
                   "- Use it at the end of multi-step jobs; not needed for short questions and chat.\n"
                   "- recap: where things stand now, what was not done/verified (2-3 short sentences).\n"
                   "- question: the one decision left to the user; the option you recommend is the first button.")
    parameters = obj({
        "recap": {"type": "string", "description": "2-3 short sentences: what changed, where things stand, what is "
                                                   "missing/unverified."},
        "question": {"type": "string", "description": "The one decision left to the user (at most 100 characters). "
                                                      "Empty if none."},
        "options": {"type": "array", "items": {"type": "string", "description": "Button text (1-4 words)."},
                    "description": "Answers to question; the recommended one first. At most 3."},
    }, ["recap"])
    risk = READ

    def run(self, ctx: ToolContext, recap: str, question: str = "", options: list[str] | None = None) -> ToolResult:
        recap = " ".join(str(recap).split())[:400]
        question = " ".join(str(question or "").split())[:140]
        opts = [" ".join(str(o).split())[:40] for o in (options or []) if str(o).strip()][:3] if question else []
        return ToolResult("The summary box was shown. Don't repeat the summary in your last answer; don't ask the question again.",
                          card={"kind": "next_step", "recap": recap, "question": question, "options": opts})


class ExitPlanMode(Tool):
    """Equivalent of ExitPlanMode in Claude Code: presents the plan prepared in plan mode for approval."""

    name = "exit_plan_mode"
    description = ("Presents the implementation plan you prepared in plan mode to the user for approval. If approved, "
                   "plan mode ends and writing files and running commands are allowed; if rejected, fix the plan with "
                   "the user's note.\n"
                   "- Use it only in plan mode, when the plan for a job that needs code/file changes is ready. Don't "
                   "use it for research, answering questions or read-only work.\n"
                   "- In the plan, say what will change, in which files, in what order, and how it will be verified. "
                   "Don't also ask \"is the plan OK?\" with ask_user; this tool already asks for approval.\n"
                   "- If there is an unclear choice, settle it with ask_user first, then present the plan.")
    parameters = obj({
        "plan": {"type": "string", "description": "The implementation plan (Markdown): steps, files that will change, "
                                                  "verification."},
    }, ["plan"])
    risk = READ

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("plan", "")).strip().splitlines()[0][:80] if args.get("plan") else ""

    def run(self, ctx: ToolContext, plan: str) -> ToolResult:
        agent = ctx.state.get("agent")
        perms = getattr(agent, "permissions", None)
        if perms is None or getattr(perms, "mode", "") != "plan":
            raise ToolError("you are not in plan mode; continue the work directly")
        ui = getattr(agent, "ui", None)
        ask = getattr(ui, "ask_choice", None) if callable(getattr(type(ui), "ask_choice", None)) else None
        if not callable(ask):
            return ToolResult("An approval card can't be shown in this interface. Write the plan in your answer and wait for the "
                              "user's approval; don't change files before it comes.")
        approve, change = _("Approve and apply"), _("Change")
        answer = (ask(_("Approve the plan?\n\n") + plan.strip()[:6000], [approve, change], False) or "").strip()
        if answer == approve:
            back = getattr(perms, "before_plan", None) or str(ctx.config.agent.get("permission_mode", "auto"))
            perms.mode = back if back != "plan" else "auto"
            return ToolResult(f"The user approved the plan; plan mode is off ({perms.mode}). Carry out the plan.")
        if not answer:
            return ToolResult("The user didn't answer. Stay in plan mode; don't change files.")
        return ToolResult(f"The user didn't approve the plan: {answer}. Stay in plan mode, fix the plan according to their note and "
                          "present it again.")


TOOLS = [TodoWrite(), Memory(), Task(), SubagentsTool(), AgentDefs(), Reflex(), Projects(), SkillTool(), AskUser(),
         Feedback(), SendFile(), NextStep(), ExitPlanMode()]
