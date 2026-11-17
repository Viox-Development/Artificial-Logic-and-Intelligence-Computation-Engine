"""How many tools and tokens of tool definitions go out per request: `python -m alice.tools.measure`.

Sizes use the router's own estimate (`estimate_tokens`), the same number ALICE uses to decide whether a request fits a
model; the exact count depends on each provider's tokenizer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alice.providers.router import estimate_tokens
from alice.tools import builtin_tools
from alice.tools.groups import GROUPS, select_groups, visible_tools

# (label, message, context): "project" = inside a code project, "home" = the home folder.
CASES = [
    ("coding: fix a test", "fix the failing test in parser.py and run pytest", "project"),
    ("coding: fix (Turkish)", "parser.py'deki hatayı düzelt ve testleri çalıştır", "project"),
    ("coding: add a feature", "add a --verbose flag to the CLI", "project"),
    ("coding: refactor + commit", "refactor parser.py, then commit and push", "project"),
    ("coding: open a PR", "open a pull request and wait for CI", "project"),
    ("chat", "merhaba", "home"),
    ("assistant: YouTube", "youtube'da lofi aç", "home"),
    ("assistant: fix a doc (home)", "şu belgedeki yazım hatalarını düzelt", "home"),
]


def size(tools) -> dict[str, int]:
    specs = [t.spec() for t in tools]
    return {"tools": len(specs), "tokens": estimate_tokens([], specs),
            "chars": len(json.dumps(specs, ensure_ascii=False))}


def report(project: Path | None = None) -> dict[str, Any]:
    tools = {t.name: t for t in builtin_tools()}
    project = project or Path(__file__).resolve().parents[2]
    out: dict[str, Any] = {"all": size(tools.values()), "core": size(visible_tools(tools, set())), "cases": [],
                           "groups": {g: size(tools[n] for n in names if n in tools) for g, (names, _, _) in GROUPS.items()}}
    for label, text, where in CASES:
        cwd = project if where == "project" else Path.home()
        opened = select_groups(text, cwd=cwd, tool_names=tools)
        out["cases"].append({"label": label, "text": text, "opened": sorted(opened),
                             **size(visible_tools(tools, opened))})
    return out


def main() -> None:  # pragma: no cover - developer report
    r = report()
    print(f"all built-in tools: {r['all']['tools']} tools, ~{r['all']['tokens']} tokens")
    print(f"core (every request): {r['core']['tools']} tools, ~{r['core']['tokens']} tokens\n")
    for c in r["cases"]:
        print(f"{c['label']:30} {c['tools']:3} tools ~{c['tokens']:6} tokens  opened: {', '.join(c['opened']) or '-'}")
    print("\ngroups:")
    for g, s in sorted(r["groups"].items(), key=lambda kv: -kv[1]["tokens"]):
        print(f"  {g:12} {s['tools']:3} tools ~{s['tokens']:6} tokens")


if __name__ == "__main__":  # pragma: no cover
    main()
