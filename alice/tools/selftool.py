"""make_tool: ALICE writes a tool itself when no existing tool can do the job (details: alice/selftools.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import DANGER, EXEC, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

PLUGIN_SKELETON = (
    "from alice.plugins import Plugin\n"
    "from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj\n\n"
    "class <Ad>(Tool):\n    name = '<ad>_<eylem>'\n    description = '...'\n    parameters = obj({...}, [...])\n"
    "    risk = READ\n    def run(self, ctx: ToolContext, ...) -> ToolResult: ...\n\n"
    "PLUGIN = Plugin(name='<ad>', tools=[<Ad>()], description='...', keywords=r'...')"
)


class MakeTool(Tool):
    name = "make_tool"
    description = (
        "Writes a new tool (an ALICE plugin) for a job you can't do, tries its test in a sandbox, adds it with the "
        "user's approval and makes it usable right away in the same conversation.\n"
        "- Use it when no tool (connections and load_tools groups included) can do the job. First tell the user what "
        "is missing (\"I don't have a tool that lists recent Steam games, shall I write one?\").\n"
        "- Don't use it if an existing tool or run_command does the job in one go.\n"
        "Flow: action=test (code + test + manifest) -> if it passes, ask the user about its access in one sentence -> "
        "action=install. action=list: the tools you wrote; action=remove: remove one.\n"
        "Code: a single file using alice.plugins.Plugin and alice.tools.base.Tool; tool names start with '<name>_'. "
        "Test: pytest, with fake data instead of the network (monkeypatch). Code that uses an internet address, "
        "folder or app not in the manifest is refused; eval/exec/ctypes are forbidden."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["test", "install", "list", "remove"],
                   "description": "test: check it and try it in the sandbox. install: add a tested tool with the "
                                  "user's approval. list: tools you wrote. remove: remove one."},
        "name": {"type": "string", "description": "Plugin name: lowercase letters and _, e.g. 'steam_recent'."},
        "need": {"type": "string", "description": "test: the user's need, in their own words."},
        "description": {"type": "string",
                        "description": "test: what the tool does, one sentence (shown to the user for approval)."},
        "manifest": {"type": "object", "description": (
            "test: what it will access. network: internet addresses (e.g. ['api.steampowered.com']), folders: folders "
            "it will read/write, apps: programs it will run, asks_each_time: true if it changes code, spends money, "
            "sends messages or deletes something (asks for approval on every use)."),
            "properties": {"network": {"type": "array", "items": {"type": "string"},
                                       "description": "Domains it will access, e.g. ['api.steampowered.com']."},
                           "folders": {"type": "array", "items": {"type": "string"},
                                       "description": "Folders it will read/write (absolute paths)."},
                           "apps": {"type": "array", "items": {"type": "string"},
                                    "description": "Programs it will run, e.g. ['git']."},
                           "asks_each_time": {"type": "boolean", "description": "true: asks for approval on every use "
                                                                                "(tools that change code, spend "
                                                                                "money, send messages or delete)."}}},
        "code": {"type": "string", "description": "test: eklentinin Python kodu. İskelet:\n" + PLUGIN_SKELETON},
        "test": {"type": "string",
                 "description": "test: a pytest test. The plugin module is imported by its name: `import <name>`."},
    }, ["action"])
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        return {"list": READ, "install": DANGER, "remove": WRITE}.get(str(args.get("action")), EXEC)

    def summary(self, args: dict[str, Any]) -> str:
        from alice import selftools

        act, name = args.get("action", "?"), args.get("name", "")
        if act == "install":
            return selftools.approval_text(str(name))[:300]
        return f"make_tool {act} {name}".strip()

    def run(self, ctx: ToolContext, action: str, name: str = "", need: str = "", description: str = "",
            manifest: dict[str, Any] | None = None, code: str = "", test: str = "") -> ToolResult:
        from alice import selftools

        if action == "list":
            return ToolResult(selftools.listing())
        if action == "remove":
            return ToolResult(selftools.remove(name.strip().lower()))
        if action == "test":
            agent = ctx.state.get("agent")
            existing = set(getattr(agent, "tools", {}) or {})
            ok, msg = selftools.prepare(name, need, description, manifest or {}, code, test, ctx, existing=existing)
            return ToolResult(msg, is_error=not ok)
        if action == "install":
            try:
                path, tools = selftools.install(name.strip().lower())
            except Exception as e:  # noqa: BLE001 - make the install error meaningful to the model
                raise ToolError(f"eklenmedi: {type(e).__name__}: {e}") from e
            agent = ctx.state.get("agent")
            if agent is not None:  # usable right away in this conversation (visible without waiting for the group)
                for t in tools:
                    agent.tools[t.name] = t
            names = ", ".join(t.name for t in tools)
            return ToolResult(f"Eklendi: {path.name}. Araçlar: {names}. Şimdi kullanabilirsin.")
        raise ToolError("action test, install, list ya da remove olmalı")


TOOLS = [MakeTool()]
