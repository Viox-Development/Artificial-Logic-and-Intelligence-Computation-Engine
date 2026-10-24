"""Proje deposu: kayıtlı projelerin ve proje şablonlarının diskteki yerleri, listeleri ve yardımcıları."""

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
    """Kayıt dosyasını oku. Windows'ta başka bir iş parçacığı dosyayı tam o an yenisiyle değiştirirken (replace)
    açmaya çalışmak PermissionError verir; kısa bekleyip tekrar denenir."""
    for i in range(tries):
        try:
            return f.read_text(encoding="utf-8")
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)
    return ""


def _slim(messages: list[dict[str, Any]], limit: int = 6000) -> list[dict[str, Any]]:
    """Diske yazılacak konuşma: görseller atılır, çok uzun araç çıktıları kısaltılır."""
    from alice.providers.router import strip_images

    out = []
    for m in strip_images(messages):
        m = dict(m)
        if isinstance(m.get("content"), str) and len(m["content"]) > limit:
            m["content"] = m["content"][:limit] + f"\n... [{len(m['content']) - limit} karakter kaydedilmedi]"
        out.append(m)
    return out


def list_projects() -> list[dict[str, Any]]:
    out = []
    for f in projects_dir().glob("*.json"):
        try:
            # Proje o an kaydediliyorsa (Windows'ta replace sırasında) düz okuma PermissionError verip proje
            # listeden düşüyordu: biten proje "yok" görünüyordu.
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
    return sorted(out, key=lambda x: -float(x.get("updated") or x.get("created") or 0))


def guess_check(cwd: Path) -> str:
    """Projenin test komutunu tahmin et (yeni proje penceresinde öneri olarak)."""
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
        pass
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
        raise ToolError("şablon adı boş")
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
