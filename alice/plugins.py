"""Plugins: the way to add new capabilities (tools) to ALICE without touching the core code.

A plugin is a `Plugin` object that describes its tools and when those tools are shown to the model.
It is loaded from three places:

1. `*.py` files under `~/.alice/plugins/` and folders that contain an `__init__.py`,
2. the files/folders in the `[plugins] paths` list in config,
3. the `alice.plugins` entry points of installed Python packages.

The module defines `PLUGIN` (a single plugin), `PLUGINS` (a list), or a `register()` that returns one of these.
A broken plugin does not stop ALICE; its error is visible through `errors()` and written to emit.
It is turned off with `[plugins] disabled = ["ad"]`; `enabled = false` turns off all of them.
Example: docs/PLUGINS.md.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alice.i18n import _

if TYPE_CHECKING:
    from alice.config import Config
    from alice.tools.base import Tool, ToolContext

# Trace for swallowed errors: silent by default, visible with `logging.basicConfig(level=logging.DEBUG)`.
log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


@dataclass
class Plugin:
    name: str
    tools: list["Tool"] = field(default_factory=list)
    # Group description (shown in the load_tools list). Tools stay in this group and are opened when needed.
    description: str = ""
    # If the user's message matches this regex, the group opens automatically (case-insensitive).
    keywords: str = ""
    # True: the tools are sent to the model on every request (no group). For a few frequently used tools.
    always: bool = False
    # True: the group is open from the start in Gemini Live voice conversation.
    live: bool = False
    # Called when the session closes (to close open connections, processes, etc.).
    cleanup: Callable[["ToolContext"], None] | None = None
    version: str = ""
    source: str = ""  # where it was loaded from (informational)

    @property
    def group(self) -> str:
        return self.name

    def tool_names(self) -> set[str]:
        return {t.name for t in self.tools}


_ACTIVE: list[Plugin] = []
_ERRORS: list[str] = []


def plugin_dir() -> Path:
    from alice.config import alice_home

    return alice_home() / "plugins"


def _settings(cfg: "Config | None") -> dict[str, Any]:
    return dict((cfg.data if cfg is not None else {}).get("plugins", {}) or {})


def _from_module(mod: Any, source: str) -> list[Plugin]:
    found: Any = None
    if hasattr(mod, "register") and callable(mod.register):
        found = mod.register()
    elif hasattr(mod, "PLUGINS"):
        found = mod.PLUGINS
    elif hasattr(mod, "PLUGIN"):
        found = mod.PLUGIN
    if found is None:
        raise ValueError("no PLUGIN, PLUGINS or register() found")
    items = found if isinstance(found, (list, tuple)) else [found]
    for p in items:
        if not isinstance(p, Plugin):
            raise TypeError(f"Plugin bekleniyordu, {type(p).__name__} geldi")
        p.source = p.source or source
    return list(items)


def _load_file(path: Path) -> list[Plugin]:
    target = path / "__init__.py" if path.is_dir() else path
    modname = "alice_plugin_" + re.sub(r"\W", "_", path.stem)
    spec = importlib.util.spec_from_file_location(
        modname, target, submodule_search_locations=[str(path)] if path.is_dir() else None)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(modname, None)
        raise
    return _from_module(mod, str(path))


def _candidates(settings: dict[str, Any]) -> list[Path]:
    out: list[Path] = []
    d = plugin_dir()
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.name.startswith(("_", ".")):
                continue
            if (p.is_file() and p.suffix == ".py") or (p.is_dir() and (p / "__init__.py").is_file()):
                out.append(p)
    for raw in settings.get("paths", []) or []:
        out.append(Path(str(raw)).expanduser())
    return out


def _entry_points() -> list[Any]:
    try:
        from importlib.metadata import entry_points

        return list(entry_points(group="alice.plugins"))
    except Exception:  # noqa: BLE001
        return []


def load_plugins(cfg: "Config | None" = None, emit: Callable[[str], None] = lambda s: None,
                 reserved: set[str] | frozenset[str] = frozenset()) -> list[Plugin]:
    """Find plugins, validate them and update the active list. `reserved`: built-in tool/group names."""
    global _ACTIVE, _ERRORS
    settings = _settings(cfg)
    errors: list[str] = []
    if settings.get("enabled", True) is False:
        _ACTIVE, _ERRORS = [], []
        return []
    disabled = {str(n) for n in settings.get("disabled", []) or []}

    loaded: list[Plugin] = []
    for path in _candidates(settings):
        try:
            loaded += _load_file(path)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path.name}: {type(e).__name__}: {e}")
    for ep in _entry_points():
        try:
            obj = ep.load()
            loaded += _from_module(obj, f"entry point {ep.name}") if not isinstance(obj, Plugin) else [obj]
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ep.name}: {type(e).__name__}: {e}")

    active: list[Plugin] = []
    names: set[str] = set()
    tool_names: set[str] = set(reserved)
    for p in loaded:
        if p.name in disabled:
            continue
        if not _NAME.match(p.name):
            errors.append(f"{p.name!r}: invalid name (lowercase letters, digits, - and _; starts with a letter)")
            continue
        if p.name in names or p.name in reserved:
            errors.append(f"{p.name}: this name is already in use, skipped")
            continue
        try:
            re.compile(p.keywords)
        except re.error as e:
            errors.append(f"{p.name}: broken keywords regex ({e}), skipped")
            continue
        clash = p.tool_names() & tool_names
        if clash:
            errors.append(f"{p.name}: tool name clash ({', '.join(sorted(clash))}), skipped")
            continue
        names.add(p.name)
        tool_names |= p.tool_names()
        active.append(p)
    from alice import selftools

    active = selftools.instrument(active)  # ALICE's own plugins: usage/error counting; closed after 3 errors
    _ACTIVE, _ERRORS = active, errors
    for e in errors:
        emit(f"plugin error: {e}")
    if active:
        emit("eklentiler: " + ", ".join(p.name for p in active))
    return list(active)


def active() -> list[Plugin]:
    return list(_ACTIVE)


def errors() -> list[str]:
    return list(_ERRORS)


def plugin_tools() -> list["Tool"]:
    return [t for p in _ACTIVE for t in p.tools]


def groups() -> dict[str, tuple[set[str], str, str]]:
    """Grouped plugins, in the GROUPS format of tools/groups.py: name -> (tools, description, regex)."""
    return {p.group: (p.tool_names(), p.description or p.name, p.keywords) for p in _ACTIVE if not p.always}


def always_tools() -> set[str]:
    return {n for p in _ACTIVE if p.always for n in p.tool_names()}


def live_groups() -> set[str]:
    return {p.group for p in _ACTIVE if p.live and not p.always}


def cleanup(ctx: "ToolContext") -> None:
    for p in _ACTIVE:
        if p.cleanup is not None:
            try:
                p.cleanup(ctx)
            except Exception:  # noqa: BLE001
                log.debug("plugin cleanup failed: %s", p.name, exc_info=True)


TEMPLATE = '''"""ALICE plugin: {name}. Details: docs/PLUGINS.md"""

from alice.plugins import Plugin
from alice.tools.base import READ, Tool, ToolContext, ToolResult, obj


class Hello(Tool):
    name = "{name}_hello"
    description = "Example tool: greets the given name. Does no real work; use it as a template for your own tool."
    parameters = obj({{"who": {{"type": "string", "description": "Name of the person to greet, e.g. 'Ayşe'."}}}}, ["who"])
    risk = READ
    parallel_safe = True

    def run(self, ctx: ToolContext, who: str) -> ToolResult:
        return ToolResult(f"Hello {{who}}!")


PLUGIN = Plugin(
    name="{name}",
    tools=[Hello()],
    description="example plugin",
    keywords=r"hello|selam|merhaba",
)
'''


def plugins_cmd(argv: list[str]) -> int:
    """`alice plugins` (list) | `alice plugins new <ad>` (create a template)."""
    from alice.config import load_config

    if argv[:1] == ["new"]:
        name = argv[1] if len(argv) > 1 else ""
        if not _NAME.match(name):
            print(_("usage: alice plugins new <name>   (lowercase letters, digits, - or _; starts with a letter)"))
            return 2
        d = plugin_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name.replace('-', '_')}.py"
        if path.exists():
            print(f"zaten var: {path}")
            return 1
        path.write_text(TEMPLATE.format(name=name.replace("-", "_")), encoding="utf-8")
        print(_("created: {path}\nEdit it, then restart ALICE; check with `alice plugins`.", path=path))
        return 0

    from alice.tools import all_tools

    all_tools(load_config())
    print(_("plugin folder: {path}", path=plugin_dir()))
    for p in _ACTIVE:
        mode = "her zaman" if p.always else f"grup: {p.group}" + (_(" (live voice)") if p.live else "")
        ver = f" {p.version}" if p.version else ""
        print(f"  {p.name}{ver}  [{mode}]  {', '.join(sorted(p.tool_names())) or '-'}\n    {p.source}")
    if not _ACTIVE:
        print(_("  (no plugins; start with `alice plugins new <name>`)"))
    for e in _ERRORS:
        print(f"  HATA {e}")
    return 1 if _ERRORS else 0
