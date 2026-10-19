from __future__ import annotations

from alice.tools import (blender, browser, computer, core, desktop, extras, flight, files, github, groups, home, meta,
                         patch, shell, system, tasks, web)
from alice.tools.base import Tool, ToolContext, ToolError, ToolResult


def builtin_tools() -> list[Tool]:
    return [*files.TOOLS, *patch.TOOLS, *shell.TOOLS, *web.TOOLS, *system.TOOLS, *computer.TOOLS, *desktop.TOOLS,
            *browser.TOOLS, *blender.TOOLS, *meta.TOOLS, *groups.TOOLS, *core.TOOLS, *flight.TOOLS, *extras.TOOLS,
            *tasks.TOOLS, *github.TOOLS, *home.TOOLS]


def all_tools(config=None, emit=lambda s: None) -> list[Tool]:
    """Yerleşik araçlar + eklentilerin araçları (bkz. alice/plugins.py)."""
    from alice import plugins

    from alice.connect import connector_tools

    tools = builtin_tools()
    reserved = {t.name for t in tools} | set(groups.GROUPS) | {"all"}
    # Bağlantılar (TOML tanımlı servisler) eklentilerden önce: adları eklentiler için de ayrılsın.
    conns = connector_tools(config, emit, reserved=reserved)
    reserved |= {t.name for t in conns}
    plugins.load_plugins(config, emit, reserved=reserved)
    return tools + conns + plugins.plugin_tools()


def attach_mcp(agent, ctx: ToolContext, emit=lambda s: None) -> int:
    """config'teki MCP sunucularını başlat ve araçlarını ajana ekle."""
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
