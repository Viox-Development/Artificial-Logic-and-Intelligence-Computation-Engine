"""Edit feeds.toml without touching its comments: add, remove, enable/disable feeds.

The user also edits the file manually; rewriting from scratch would wipe out comments and ordering. Hence the file
is split into blocks by `[[feed]]` lines (the part before the first block is the preamble), and operations are only
done on the relevant block. Subtables like `[feed.when]` belong to that block until the next `[[feed]]` per TOML rules.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alice.core.feeds import feeds_path, parse_feed
from alice.i18n import _

HEAD_RX = re.compile(r"(?m)^\[\[feed\]\]\s*$")
NAME_RX = re.compile(r'(?m)^name\s*=\s*"([^"]*)"')
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
SUBTABLES = ("when", "escalate")


def _key(k: str) -> str:
    return k if BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def toml_value(v: Any) -> str:
    """Small TOML writer for feed values (string, number, bool, list, inline table)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) and not v.is_integer() else str(int(v) if isinstance(v, float) else v)
    if isinstance(v, str):
        if "\\" in v and "'" not in v and "\n" not in v:
            return f"'{v}'"  # backslashed text like regex: TOML literal, no escaping
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(str(k))} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise ValueError(_("a value that can't be written as TOML: {value}", value=repr(v)))


def render_block(feed: dict[str, Any]) -> str:
    lines = ["[[feed]]"]
    for k, v in feed.items():
        if k in SUBTABLES or v in (None, "", [], {}):
            continue
        lines.append(f"{_key(k)} = {toml_value(v)}")
    for sub in SUBTABLES:
        tab = feed.get(sub)
        if isinstance(tab, dict) and tab:
            lines.append(f"[feed.{sub}]")
            lines += [f"{_key(str(k))} = {toml_value(v)}" for k, v in tab.items() if v not in (None, "", [], {})]
    return "\n".join(lines) + "\n"


def _split(text: str) -> tuple[str, list[str]]:
    starts = [m.start() for m in HEAD_RX.finditer(text)]
    if not starts:
        return text, []
    pre = text[:starts[0]]
    blocks = [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]
    return pre, blocks


def _name(block: str) -> str:
    m = NAME_RX.search(block)
    return m.group(1) if m else ""


def names(path: Path | None = None) -> list[str]:
    p = path or feeds_path()
    try:
        return [_name(b) for b in _split(p.read_text(encoding="utf-8-sig"))[1]]
    except OSError:
        return []


def add_feed(feed: dict[str, Any], path: Path | None = None) -> str:
    """Validate and append to the end of the file. Invalid feed raises ValueError (clear text returned to model)."""
    parse_feed(json.loads(json.dumps(feed)))  # validate on a copy: parse_feed may mutate subtables
    p = path or feeds_path()
    name = str(feed["name"])
    if name in names(p):
        raise ValueError(_("a feed named '{name}' already exists; pick another name or delete it first", name=name))
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        text = "# ALICE akışları (alice feeds). Sohbetten eklenenler de buraya yazılır.\n"
    sep = "" if text.endswith("\n\n") or not text else ("\n" if text.endswith("\n") else "\n\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text + sep + render_block(feed), encoding="utf-8")
    return name


def remove_feed(name: str, path: Path | None = None) -> bool:
    p = path or feeds_path()
    try:
        pre, blocks = _split(p.read_text(encoding="utf-8-sig"))
    except OSError:
        return False
    keep = [b for b in blocks if _name(b) != name]
    if len(keep) == len(blocks):
        return False
    p.write_text(pre + "".join(keep), encoding="utf-8")
    return True


def set_enabled(name: str, on: bool, path: Path | None = None) -> bool:
    p = path or feeds_path()
    try:
        pre, blocks = _split(p.read_text(encoding="utf-8-sig"))
    except OSError:
        return False
    for i, b in enumerate(blocks):
        if _name(b) != name:
            continue
        head = b.split("\n[feed.", 1)  # enabled line must be in the main table, not in subtables
        main = head[0]
        line = f"enabled = {'true' if on else 'false'}"
        if re.search(r"(?m)^enabled\s*=", main):
            main = re.sub(r"(?m)^enabled\s*=.*$", line, main, count=1)
        else:
            main = main.rstrip("\n") + f"\n{line}\n"
        blocks[i] = main + ("\n[feed." + head[1] if len(head) > 1 else "")
        p.write_text(pre + "".join(blocks), encoding="utf-8")
        return True
    return False
