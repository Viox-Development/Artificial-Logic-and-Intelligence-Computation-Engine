"""Folder picker: navigation data for selecting the working directory without typing paths manually.

The UI requests subdirectories of a folder, quick places (home, desktop, documents, downloads, GitHub, drives), and
recently used folders; the functions here produce them. Read-only, the only things written are "new folder" and the
recently used list.
"""

from __future__ import annotations

import json
import os
import string
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.i18n import _

RECENT_MAX = 8
LIST_MAX = 400


def _recent_file() -> Path:
    return alice_home() / "recent_dirs.json"


def recent_dirs() -> list[str]:
    """Recently selected folders, newest first; nonexistent ones are skipped."""
    try:
        rows = json.loads(_recent_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, str) and Path(r).is_dir()][:RECENT_MAX]


def remember_dir(path: Path) -> None:
    rows = [str(path)] + [r for r in recent_dirs() if r != str(path)]
    try:
        alice_home().mkdir(parents=True, exist_ok=True)
        _recent_file().write_text(json.dumps(rows[:RECENT_MAX], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # the recent list is a convenience; it is rebuilt


def _first_dir(*names: str) -> Path | None:
    for n in names:
        p = Path.home() / n
        if p.is_dir():
            return p
    return None


def places(clone_root: Path | None = None) -> list[dict[str, str]]:
    """Quick places: home, desktop, documents, downloads, GitHub, and (on Windows) drives if present."""
    out: list[dict[str, str]] = [{"label": _("Home"), "path": str(Path.home()), "icon": "home"}]
    for label, icon, names in ((_("Desktop"), "screen", ("Desktop", "Masaüstü")),
                               (_("Documents"), "file", ("Documents", "Belgeler")),
                               (_("Downloads"), "down", ("Downloads", "İndirilenler"))):
        p = _first_dir(*names)
        if p:
            out.append({"label": label, "path": str(p), "icon": icon})
    if clone_root and clone_root.is_dir():
        out.append({"label": "GitHub", "path": str(clone_root), "icon": "branch"})
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:\\")
            if root.exists():
                out.append({"label": f"{letter}:", "path": str(root), "icon": "disk"})
    else:
        out.append({"label": _("Root"), "path": "/", "icon": "disk"})
    return out


def listing(path: str, show_hidden: bool = False) -> dict[str, Any]:
    """Subdirectories of a folder. Empty path means home folder. Returns an error on inaccessible folders."""
    p = Path(path).expanduser() if path else Path.home()
    try:
        p = p.resolve()
    except OSError:
        pass  # keep the unresolved path
    if not p.is_dir():
        return {"ok": False, "error": _("no such folder: {path}", path=p)}
    dirs: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name.casefold())
    except OSError as e:
        return {"ok": False, "error": _("could not open: {error}", error=e.strerror or e)}
    for e in entries:
        if not show_hidden and (e.name.startswith(".") or e.name.startswith("$")):
            continue
        try:
            if not e.is_dir():
                continue
        except OSError:
            continue
        dirs.append({"name": e.name, "path": os.path.join(str(p), e.name),
                     "git": os.path.isdir(os.path.join(str(p), e.name, ".git"))})
        if len(dirs) >= LIST_MAX:
            break
    parent = str(p.parent) if p.parent != p else ""
    # Breadcrumbs: every segment from root to this folder is clickable.
    crumbs = [{"name": part.name or str(part), "path": str(part)} for part in reversed([p, *p.parents])]
    return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs, "crumbs": crumbs,
            "git": (p / ".git").is_dir(), "truncated": len(dirs) >= LIST_MAX}


def make_dir(parent: str, name: str) -> dict[str, Any]:
    name = name.strip()
    if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|'):
        return {"ok": False, "error": _("invalid folder name")}
    base = Path(parent).expanduser()
    if not base.is_dir():
        return {"ok": False, "error": _("no such folder: {path}", path=base)}
    target = base / name
    if target.exists():
        return {"ok": False, "error": _("something with this name already exists")}
    try:
        target.mkdir()
    except OSError as e:
        return {"ok": False, "error": _("could not create: {error}", error=e.strerror or e)}
    return {"ok": True, "path": str(target)}
