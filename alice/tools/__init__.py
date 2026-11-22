from __future__ import annotations

from alice.tools import (agentkit, apps, companions, blender, browser, computer, core, crawl, desktop, diagnostics, extras, files, findfiles, flight, game, github,
                         gmail, groups, home, meta, monitor, nowtool, patch, permshare, hooks_usage, phonecall, preview, review, selftool, sessions, shell, system,
                         tasks, timelinetool, web)
from alice.tools.base import Tool, ToolContext, ToolError, ToolResult


def builtin_tools() -> list[Tool]:
    return [*files.TOOLS, *patch.TOOLS, *shell.TOOLS, *web.TOOLS, *crawl.TOOLS, *system.TOOLS, *computer.TOOLS, *desktop.TOOLS,
            *browser.TOOLS, *blender.TOOLS, *meta.TOOLS, *groups.TOOLS, *core.TOOLS, *flight.TOOLS, *extras.TOOLS,
            *diagnostics.TOOLS, *tasks.TOOLS, *github.TOOLS, *home.TOOLS, *apps.TOOLS, *gmail.TOOLS, *game.TOOLS, *findfiles.TOOLS,
            *selftool.TOOLS, *review.TOOLS, *monitor.TOOLS, *sessions.TOOLS, *permshare.TOOLS, *hooks_usage.TOOLS, *preview.TOOLS, *nowtool.TOOLS, *timelinetool.TOOLS, *phonecall.TOOLS, *agentkit.TOOLS, *companions.TOOLS]


def all_tools(config=None, emit=lambda s: None) -> list[Tool]:
    """Built-in tools + plugin tools (see alice/plugins.py)."""
    from alice import plugins

    from alice.connect import connector_tools

    tools = builtin_tools()
    reserved = {t.name for t in tools} | set(groups.GROUPS) | {"all"}
    # Connections (TOML-defined services) before plugins: reserve their names for plugins too.
    conns = connector_tools(config, emit, reserved=reserved)
    reserved |= {t.name for t in conns}
    plugins.load_plugins(config, emit, reserved=reserved)
    return tools + conns + plugins.plugin_tools()


def attach_mcp(agent, ctx: ToolContext, emit=lambda s: None) -> int:
    """Start MCP servers in config and add their tools to the agent."""
    from alice.mcp import load_mcp

    servers, tools = load_mcp(ctx.config, emit)
    ctx.state["mcp_servers"] = servers
    for t in tools:
        agent.tools[t.name] = t
    return len(tools)


def cleanup(ctx: ToolContext) -> None:
    shell.cleanup(ctx)
    st = ctx.state.pop("core_store", None)
    if st is not None:
        st.close()
    browser.cleanup(ctx)
    from alice import plugins

    plugins.cleanup(ctx)
    from alice.mcp import close_all

    close_all(ctx.state.pop("mcp_servers", []))


__all__ = ["Tool", "ToolContext", "ToolError", "ToolResult", "all_tools", "attach_mcp", "builtin_tools", "cleanup"]
