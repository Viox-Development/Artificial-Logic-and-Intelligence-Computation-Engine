"""Project store: on-disk locations, lists and helpers for saved projects and project templates."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from alice.tools.base import ToolError


def projects_dir() -> Path:
    from alice.config import alice_home

    d = alice_home() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_saved(f: Path, tries: int = 10) -> str:
    """Read the saved file. On Windows, trying to open it while another thread is replacing it with a new version
    (replace) raises PermissionError; wait briefly and retry."""
    for i in range(tries):
        try:
            return f.read_text(encoding="utf-8")
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)
    return ""


def _slim(messages: list[dict[str, Any]], limit: int = 6000) -> list[dict[str, Any]]:
    """Conversation to be written to disk: images are dropped and very long tool outputs are truncated."""
    from alice.providers.router import strip_images

    out = []
    for m in strip_images(messages):
        m = dict(m)
        if isinstance(m.get("content"), str) and len(m["content"]) > limit:
            m["content"] = m["content"][:limit] + f"\n... [{len(m['content']) - limit} karakter kaydedilmedi]"
        out.append(m)
    return out


# list_projects cache: path -> ((mtime, size), row). Project files with logs can run to several MB; the Projects
# page used to read and parse all of them from scratch on every open. An unchanged file's row is reused.
_list_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def list_projects() -> list[dict[str, Any]]:
    out = []
    for f in projects_dir().glob("*.json"):
        try:
            st = f.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
        hit = _list_cache.get(str(f))
        if hit is not None and hit[0] == sig:
            out.append(dict(hit[1]))
            continue
        try:
            # If the project was being saved at that moment (Windows replace), a plain read raised PermissionError,
            # and the project dropped out of the list: a finished project looked "yok" (missing).
            data = json.loads(_read_saved(f))
        except (OSError, json.JSONDecodeError):
            continue
        members = data.get("members") or []
        status = data.get("status") or "stopped"
        out.append({"id": data.get("id") or f.stem, "title": data.get("title") or "", "goal": data.get("goal", ""),
                    "created": data.get("created", 0), "updated": data.get("updated", 0),
                    "origin": data.get("origin", "alice"), "cwd": data.get("cwd", ""),
                    "status": status if status in ("done", "stopped") else "stopped",
                    "questions": sum(1 for q in data.get("questions") or [] if not q.get("answer")),
                    "members": [{"name": m.get("name"), "status": m.get("status")} for m in members]})
        _list_cache[str(f)] = (sig, dict(out[-1]))
    return sorted(out, key=lambda x: -float(x.get("updated") or x.get("created") or 0))


def guess_check(cwd: Path) -> str:
    """Guess the project's test command (offered as a suggestion in the new project window)."""
    try:
        if (cwd / "package.json").is_file():
            data = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
            if (data.get("scripts") or {}).get("test"):
                return "npm test"
        if any((cwd / f).is_file() for f in ("pytest.ini", "tox.ini")) or (cwd / "tests").is_dir() or \
                "pytest" in ((cwd / "pyproject.toml").read_text(encoding="utf-8")
                             if (cwd / "pyproject.toml").is_file() else ""):
            return "python -m pytest -q"
        if (cwd / "Cargo.toml").is_file():
            return "cargo test"
        if (cwd / "go.mod").is_file():
            return "go test ./..."
    except (OSError, ValueError):
        pass  # no test command guessed
    return ""


def templates_dir() -> Path:
    from alice.config import alice_home

    d = alice_home() / "project_templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tpl_file(name: str) -> Path:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower().replace("ı", "i").replace("ş", "s").replace("ğ", "g")
                  .replace("ü", "u").replace("ö", "o").replace("ç", "c")).strip("-")[:40]
    if not slug:
        raise ToolError("the template name is empty")
    return templates_dir() / f"{slug}.json"


def list_templates() -> list[dict[str, Any]]:
    out = []
    for f in sorted(templates_dir().glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def delete_template(name: str) -> None:
    _tpl_file(name).unlink(missing_ok=True)
