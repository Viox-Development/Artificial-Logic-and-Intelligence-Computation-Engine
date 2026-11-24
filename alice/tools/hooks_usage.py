"""hooks tool (add/remove/test hooks from chat) and usage tool (where tokens went in this session)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import DANGER, EXEC, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class HooksTool(Tool):
    name = "hooks"
    description = (
        "Manages the user's hooks: shell commands that run by themselves at certain moments (like Claude Code hooks). "
        "For requests like 'run ruff after every file edit', 'run the tests when done and continue if they're "
        "broken', 'add the git status to every message'.\n"
        "Events: PostToolUse (after a tool; exit code 2 = stderr fed back to you), PreToolUse (before a tool; 2 = "
        "block), Stop (when the answer ends; 2 = continue), UserPromptSubmit (on every message; stdout is added to "
        "the message), SessionStart (on the first message; stdout is added to the context). matcher: tool name regex "
        "(e.g. 'edit_file|write_file|multi_edit'). The command can use $ALICE_TOOL_PATH (the tool's path argument), "
        "$ALICE_TOOL, $ALICE_CWD; the event JSON comes on stdin.\n"
        "action: list | add (event, command, matcher, timeout, on_failure) | remove (index) | test (index: run the "
        "hook once with a sample event and show the result). A hook is permanent and runs in every conversation: tell "
        "the user what it will do before adding it.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "test"], "description": "The operation to "
                                                                                               "perform."},
        "event": {"type": "string", "enum": ["PostToolUse", "PreToolUse", "Stop", "UserPromptSubmit",
                                              "SessionStart"], "description": "add: the moment the hook runs."},
        "command": {"type": "string", "description": "Shell command to run. If a PostToolUse command writes "
                                                     "{\"updatedToolOutput\": \"...\"} to stdout, the tool output is "
                                                     "replaced with it (e.g. to hide secrets)."},
        "matcher": {"type": "string", "description": "PreToolUse/PostToolUse: tool name regex; empty = every tool."},
        "timeout": {"type": "number", "description": "seconds (default 30)"},
        "on_failure": {"type": "string", "enum": ["ignore", "block"],
                       "description": "add: what happens if the hook itself breaks (times out/doesn't run). For "
                                      "checking PreToolUse hooks use 'block' (if it breaks, the tool doesn't go "
                                      "through)."},
        "index": {"type": "integer", "description": "remove/test: the number from list."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        # An added hook runs commands automatically in every chat: it always asks first.
        return {"add": DANGER, "remove": WRITE, "test": EXEC}.get(str(args.get("action")), READ)

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action", "list")
        if a == "add":
            return f"hook ekle: {args.get('event')} {args.get('matcher') or '*'} → {str(args.get('command'))[:60]}"
        return f"hook {a}" + (f" #{args['index']}" if "index" in args else "")

    def run(self, ctx: ToolContext, action: str = "list", event: str = "", command: str = "", matcher: str = "",
            timeout: float = 30.0, index: int = -1, on_failure: str = "ignore") -> ToolResult:
        from alice import hooks as hk

        try:
            if action == "add":
                h = hk.add_hook(event, command, matcher, timeout, on_failure)
                return ToolResult(f"added ({hk.hooks_path()}): {h.event} [{h.matcher or 'every tool'}] {h.command}. "
                                  "Applies right away, open chats included.")
            if action == "remove":
                h = hk.remove_hook(int(index))
                return ToolResult(f"silindi: {h.event} {h.command}")
            if action == "test":
                rows = hk.file_hooks()
                if not 0 <= int(index) < len(rows):
                    raise ToolError(f"no such hook: {index}")
                h = rows[int(index)]
                r = hk.Hooks([h], ctx.cwd)._run_one(h, {"args": {"path": "ornek.py"}, "result": "", "prompt": "deneme"},
                                                    "edit_file")
                return ToolResult(f"exit code {r.code}" + (" (blocks / gives feedback)" if r.blocked else "")
                                  + (f"\nstdout: {r.stdout}" if r.stdout else "") + (f"\nstderr: {r.stderr}" if r.stderr
                                                                                     else ""))
        except ValueError as e:
            raise ToolError(str(e)) from e
        return ToolResult(describe_hooks(ctx))


def describe_hooks(ctx: ToolContext) -> str:
    from alice import hooks as hk

    rows = hk.file_hooks()
    cfg_rows = hk._parse(dict(((getattr(ctx.config, "data", None) or {}).get("hooks")) or {}))
    out = [f"{i}. {h.event} [{h.matcher or 'every tool'}] {h.command} (timeout {h.timeout:g} s)"
           for i, h in enumerate(rows)]
    out += [f"-. {h.event} [{h.matcher or 'every tool'}] {h.command} (config.toml'da; buradan silinmez)"
            for h in cfg_rows]
    return "\n".join(out) if out else "hook yok"


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


class Usage(Tool):
    name = "usage"
    description = ("Breaks down where tokens went in this session: calls, input (including from the cache), output "
                   "and time per model; output added to the context per tool; how the context is split right now "
                   "(system prompt, tool definitions, messages). Use it for 'where did my tokens go', 'why did it "
                   "spend so much', 'show the usage'.")
    parameters = obj({}, [])
    risk = READ

    def run(self, ctx: ToolContext) -> ToolResult:
        agent = ctx.state.get("agent")
        if agent is None or not hasattr(agent, "usage_breakdown"):
            raise ToolError("no session statistics in this context")
        u = agent.usage_breakdown()
        total_in = u["input"] or 1
        lines = [f"{u['turns']} turns, {u['steps']} model calls · model {u['llm_s']} s, tools {u['tool_s']} s",
                 f"Input {_k(u['input'])} (from cache {_k(u['cached'])}, {100 * u['cached'] // total_in}%), "
                 f"output {_k(u['output'])}", "", "Modeller:"]
        lines += [f"  {m['model']}: {m['calls']} calls, input {_k(m['input'])} (cache {_k(m['cached'])}), "
                  f"output {_k(m['output'])}, {m['seconds']} s" for m in u["models"]] or ["  (no calls yet)"]
        lines += ["", "Tool outputs (~tokens added to the context):"]
        lines += [f"  {t['tool']}: {t['calls']} kez, ~{_k(t['tokens'])}" + (f", {t['errors']} hata" if t["errors"]
                                                                              else "") for t in u["tools"][:12]] \
            or ["  (no tools used)"]
        c = u["ctx"]
        lines += ["", f"Context is now ~{_k(c['total'])}: system prompt {_k(c['system'])}, tool definitions "
                      f"{_k(c['tools'])}, messages {_k(c['messages'])}"]
        return ToolResult("\n".join(lines), card={"kind": "usage", **u})


TOOLS: list[Tool] = [HooksTool(), Usage()]
