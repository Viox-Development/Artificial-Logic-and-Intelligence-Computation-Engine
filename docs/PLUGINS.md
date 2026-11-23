# ALICE plugins

A plugin adds new tools (abilities) to ALICE without touching the core code. A single Python file is enough.

## Quick start

```
alice plugins new spotify     # creates the ~/.alice/plugins/spotify.py template
alice plugins                 # lists installed plugins and their errors
```

Edit the file, then restart ALICE (core, app or voice).

## Plugin file

```python
from alice.plugins import Plugin
from alice.tools.base import NETWORK, Tool, ToolContext, ToolError, ToolResult, obj


class SpotifyPlay(Tool):
    name = "spotify_play"                       # unique across all tools
    description = ("Searches for and plays a song, album or playlist in the Spotify desktop app. "
                   "Use the media tool for pause/resume and volume.")  # the model reads this to decide when to call it
    parameters = obj({"query": {"type": "string", "description": "Song or artist, e.g. 'Coldplay Yellow'."}},
                     ["query"])
    risk = NETWORK                              # permission system: READ, WRITE, EXEC, COMPUTER, NETWORK, DANGER

    def run(self, ctx: ToolContext, query: str) -> ToolResult:
        if not query:
            raise ToolError("song name is empty")  # returns a meaningful error to the model
        ...
        return ToolResult(f"playing: {query}")


PLUGIN = Plugin(
    name="spotify",                             # also the group name: load_tools(["spotify"])
    tools=[SpotifyPlay()],
    description="Spotify play, pause",          # shown in the load_tools list
    keywords=r"spotify|şarkı çal",              # if a message matches this, the group opens by itself
    live=True,                                  # on from the start in voice (Gemini Live) conversations
    cleanup=None,                               # called when the session closes: cleanup(ctx)
)
```

`Plugin` fields:

| field | meaning |
|---|---|
| `name` | Lowercase letters, digits, `-`, `_`; starts with a letter, at most 40 characters. It is the group name. |
| `tools` | `Tool` objects. |
| `description`, `keywords` | By default tools sit in a group and are sent to the model only when needed; this keeps requests small and the model focused. |
| `always` | `True`: the tools are sent with every request (no group). For a few, frequently used tools. |
| `live` | Open the group from the start in voice conversations. |
| `cleanup` | Closing open connections/processes. |
| `version` | Informational. |

## A good tool definition

The model picks a tool by looking only at its name, description and parameter descriptions. The pattern in the
built-in tools of Claude Code and Codex applies here too (built-in tools are checked by `tests/test_tool_specs.py`):

- The first sentence says what the tool does; the model often reads only that.
- When to use it and, most importantly, when NOT to use it: if there is a similar tool, name it ("use media for
  pause/resume"). This is the part that does the most to prevent the wrong tool from being picked.
- Every parameter has a `description`: its unit, default and a short example (`"Seconds. Default 120."`,
  `"e.g. 'Coldplay Yellow'"`). For enum parameters such as `action`, say there what each value does.
- The description stays under 1000 characters: Gemini Live cuts off the rest.
- The error message is enough for the model to correct itself: it says what was wrong and what to do
  (`"element not found; refresh with browser action=snapshot"`). A missing required parameter or an invalid enum
  value is sent back to the model by ALICE, with an explanation, before the tool runs.

Use `ctx.state` (a dict) for state shared between tools, `ctx.config.data` for settings, and `ctx.emit("...")` for
short messages to the user. Long loops should check `ctx.cancelled()`.

A module defines `PLUGIN` for a single plugin, `PLUGINS = [...]` for several, or a `register()` that returns them.

## Where plugins are loaded from

1. `~/.alice/plugins/*.py` and `~/.alice/plugins/<folder>/__init__.py` (names starting with `_` are skipped),
2. `[plugins] paths = ["C:/path/plugin.py"]` in the config,
3. pip packages: `[project.entry-points."alice.plugins"] spotify = "package.module"` in `pyproject.toml`.

## Turning plugins off, and errors

```toml
[plugins]
enabled = true        # false: no plugin is loaded
disabled = ["spotify"]
```

A broken plugin does not stop ALICE: it is skipped, and its error shows up in the `alice plugins` output and in the
startup messages. A plugin whose name clashes with a built-in tool or group is skipped too.

## Tools ALICE writes itself

If ALICE has no tool for a job, it can write one itself (`make_tool`, details in `alice/selftools.py`):

1. It says what is missing and writes the plugin, its test and its **manifest**. The manifest says what the tool will access: internet addresses, folders, programs. Code that uses an address, folder or program not in the manifest is refused. `eval`, `exec` and `ctypes` are forbidden.
2. The test runs in a sandbox. On Windows this means a low integrity level: the test can only write to a temporary folder and has a time limit.
3. ALICE says in one sentence what the tool does and what it will access, and **always** asks for approval. Without approval, no file gets into that folder.
4. After approval the tool is added as `~/.alice/plugins/<name>.py` and can be used in the same conversation. Its test lives under `_tests/`.

`~/.alice/selftools.json` keeps track of every tool: when and why it was written, how many times it was used, its last error. A tool that fails three times turns itself off. Tools with `asks_each_time` in their manifest (ones that change code, spend money, send messages or delete things) ask for approval again on every use. The answer to "Which tools did you write yourself?" is `make_tool action=list`. A removed tool is not deleted; it is moved to the `_kaldirilan/` folder.

Limitation: the manifest is checked by looking at the code. Network and file access are not restricted at the operating-system level while the tool runs.
