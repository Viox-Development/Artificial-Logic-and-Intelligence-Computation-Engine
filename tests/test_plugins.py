from __future__ import annotations

from alice import plugins
from alice.config import Config, default_config_dict
from alice.tools import ToolContext, all_tools, cleanup
from alice.tools.groups import LoadTools, group_of, groups_for_text, visible_tools

PLUGIN_SRC = '''
from alice.plugins import Plugin
from alice.tools.base import Tool, ToolResult

CLOSED = []

class Ping(Tool):
    name = "ping_pong"
    description = "ping"
    def run(self, ctx, **a):
        return ToolResult("pong")

PLUGIN = Plugin(name="ping", tools=[Ping()], description="ping aracı", keywords=r"\\bping\\b", live=True,
                cleanup=lambda ctx: CLOSED.append(1))
'''


def cfg(**plugins_section) -> Config:
    data = default_config_dict()
    data["plugins"].update(plugins_section)
    return Config(data)


def write(isolated_home, name, src):
    d = isolated_home / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(src, encoding="utf-8")


def test_no_plugins_keeps_builtin_tools(isolated_home):
    names = [t.name for t in all_tools(cfg())]
    assert "read_file" in names and len(names) == len(set(names))
    assert plugins.active() == [] and plugins.errors() == []


def test_plugin_tools_are_grouped_and_open_by_keyword(isolated_home, tmp_path):
    write(isolated_home, "ping.py", PLUGIN_SRC)
    tools = {t.name: t for t in all_tools(cfg())}
    assert "ping_pong" in tools
    assert group_of("ping_pong") == "ping"
    assert "ping_pong" not in {t.name for t in visible_tools(tools, set())}
    assert "ping" in groups_for_text("bir ping at", tools)
    assert "ping_pong" in {t.name for t in visible_tools(tools, {"ping"})}
    assert "ping" in plugins.live_groups()
    from alice.tools.groups import catalog

    assert "- ping (ping_pong)" in catalog(tools)  # in the system prompt's list of loadable tools
    ctx = ToolContext(cwd=tmp_path, config=cfg())
    assert "ping_pong" in LoadTools().run(ctx, groups=["ping"]).text
    assert "ping_pong" in LoadTools().run(ctx, groups=["ping_pong"]).text  # also opened by tool name
    cleanup(ctx)
    import sys
    assert sys.modules["alice_plugin_ping"].CLOSED == [1]


def test_always_plugin_is_always_visible(isolated_home):
    write(isolated_home, "a.py", PLUGIN_SRC.replace('description="ping aracı"', "always=True"))
    tools = {t.name: t for t in all_tools(cfg())}
    assert "ping_pong" in {t.name for t in visible_tools(tools, set())}
    assert "ping" not in plugins.groups()


def test_broken_and_clashing_plugins_are_skipped(isolated_home):
    write(isolated_home, "bad.py", "raise RuntimeError('boom')")
    write(isolated_home, "empty.py", "x = 1")
    write(isolated_home, "clash.py", PLUGIN_SRC.replace('name = "ping_pong"', 'name = "read_file"'))
    write(isolated_home, "_private.py", "raise SystemExit")
    write(isolated_home, "rx.py", PLUGIN_SRC.replace('name="ping"', 'name="rx"').replace(
        'name = "ping_pong"', 'name = "rx_t"').replace(r"\bping\b", "("))
    names = [t.name for t in all_tools(cfg())]
    assert names.count("read_file") == 1
    errs = " | ".join(plugins.errors())
    assert "bad.py" in errs and "boom" in errs and "empty.py" in errs and "çakışıyor" in errs and "regex" in errs
    assert plugins.active() == []


def test_disabled_and_paths(isolated_home, tmp_path):
    extra = tmp_path / "ext.py"
    extra.write_text(PLUGIN_SRC, encoding="utf-8")
    assert "ping_pong" in [t.name for t in all_tools(cfg(paths=[str(extra)]))]
    assert "ping_pong" not in [t.name for t in all_tools(cfg(paths=[str(extra)], disabled=["ping"]))]
    assert "ping_pong" not in [t.name for t in all_tools(cfg(paths=[str(extra)], enabled=False))]


def test_plugins_new_scaffold_loads(isolated_home, capsys):
    assert plugins.plugins_cmd(["new", "hava_durumu"]) == 0
    assert plugins.plugins_cmd(["new", "hava_durumu"]) == 1
    assert plugins.plugins_cmd([]) == 0
    assert "hava_durumu_hello" in capsys.readouterr().out
    t = next(t for t in all_tools(cfg()) if t.name == "hava_durumu_hello")
    assert t.run(None, who="Hayat").text == "Merhaba Hayat!"
