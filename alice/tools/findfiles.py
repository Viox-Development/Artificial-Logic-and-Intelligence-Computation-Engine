"""Personal file finder: "geçen hafta indirdiğim staj PDF'i" (last week's internship PDF), "dün açtığım Excel"
(yesterday's Excel).

Searching project code is glob/grep's job; this tool is for the user's own files:
  * recent: Windows' "son kullanılanlar" (recently used) list (files actually opened, with when they were opened)
  * downloads: Downloads folder (İndirilenler), by download time
  * search: files with the query in their name, in Desktop, Documents, Downloads, Pictures, OneDrive
    (depth-limited, fast)
  * open: open the file with its default program
  * tidy: sort Downloads into subfolders by type; shows the plan first, moves with apply=true
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from alice.tools.base import COMPUTER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

KINDS = {
    "pdf": {".pdf"}, "excel": {".xlsx", ".xls", ".csv", ".ods"}, "word": {".docx", ".doc", ".odt", ".rtf"},
    "sunum": {".pptx", ".ppt", ".odp", ".key"}, "resim": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp"},
    "video": {".mp4", ".mkv", ".mov", ".avi", ".webm"}, "ses": {".mp3", ".wav", ".m4a", ".flac", ".ogg"},
    "arşiv": {".zip", ".rar", ".7z", ".tar", ".gz"}, "kurulum": {".exe", ".msi"},
    "metin": {".txt", ".md"}, "kod": {".py", ".js", ".ts", ".cs", ".cpp", ".json", ".lua"},
}
# Folder names used by tidy
TIDY_DIRS = {"pdf": "Belgeler", "word": "Belgeler", "excel": "Belgeler", "sunum": "Belgeler", "metin": "Belgeler",
             "resim": "Resimler", "video": "Videolar", "ses": "Sesler", "arşiv": "Arşivler", "kurulum": "Kurulumlar"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "AppData", ".venv", "venv", "site-packages"}


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def downloads_dir() -> Path:
    """The user's real Downloads folder (İndirilenler): if it was moved to another disk/folder (Properties > Location),
    that folder is used instead of ~/Downloads; a moved folder used to be reported as "bulunamadı" (not found)."""
    from alice.tools.fetch_file import downloads_dir as known

    p = home() / "Downloads"
    return p if p.is_dir() else known()


def search_roots() -> list[Path]:
    h = home()
    roots = [h / "Desktop", h / "Documents", downloads_dir(), h / "Pictures", h / "Videos"]
    roots += [p for p in h.glob("OneDrive*") if p.is_dir()]
    return [r for r in roots if r.is_dir()]


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    return next((k for k, exts in KINDS.items() if ext in exts), "")


def _match(path: Path, query: str, kind: str) -> bool:
    if kind and kind_of(path) != kind:
        return False
    if not query:
        return True
    name = path.name.lower()
    return all(w in name for w in query.lower().split())


def _since(days: float | None) -> float:
    return time.time() - days * 86400 if days else 0.0


def _fmt(p: Path, ts: float) -> str:
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    s = f"{size / 2**20:.1f} MB" if size >= 2**20 else f"{size // 1024} KB"
    return f"{datetime.fromtimestamp(ts):%d.%m %H:%M} | {s} | {p}"


def recent(query: str = "", kind: str = "", days: float | None = None, limit: int = 15,
           folder: Path | None = None) -> list[tuple[Path, float]]:
    """Recently opened files: the targets of the shortcuts in %APPDATA%\\Microsoft\\Windows\\Recent
    (a shortcut's time = its last open). If a shortcut's target can't be read, it is guessed from the name
    and still included in the search."""
    folder = folder or Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Recent"
    if not folder.is_dir():
        return []
    cutoff = _since(days)
    out = []
    for lnk in folder.glob("*.lnk"):
        try:
            ts = lnk.stat().st_mtime
        except OSError:
            continue
        if ts < cutoff:
            continue
        target = _lnk_target(lnk) or Path(lnk.stem)
        if "?" in target.name and (target.parent / lnk.stem).exists():
            target = target.parent / lnk.stem  # shortcut name is Unicode: keep names with Cyrillic/emoji intact
        if target.suffix == "" or not _match(target, query, kind):
            continue
        out.append((target, ts))
    return sorted(out, key=lambda x: -x[1])[:limit]


def _lnk_target(lnk: Path) -> Path | None:
    """The .lnk's target path (via the Windows shell; otherwise from the local path string in the file)."""
    if sys.platform == "win32":
        try:  # the shell returns the Unicode path; in the file's ANSI string, Turkish/emoji characters became "?"
            import win32com.client

            target = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(lnk)).TargetPath
            if target:
                return Path(target)
        except Exception:  # noqa: BLE001 - if pywin32 is missing or the shortcut is broken, read the file
            pass
    try:
        data = lnk.read_bytes()
    except OSError:
        return None
    # Local path in the shortcut file: an ANSI string starting with "C:\..." and ending in NUL
    import re

    m = re.search(rb"([A-Za-z]:\\[^\x00]{2,259})\x00", data)
    if m:
        try:
            return Path(m.group(1).decode("mbcs" if sys.platform == "win32" else "latin-1", errors="replace"))
        except LookupError:
            return Path(m.group(1).decode("latin-1", errors="replace"))
    return None


def downloads(query: str = "", kind: str = "", days: float | None = None, limit: int = 15,
              folder: Path | None = None) -> list[tuple[Path, float]]:
    folder = folder or downloads_dir()
    cutoff = _since(days)
    out = []
    for p in folder.rglob("*") if folder.is_dir() else []:
        try:
            if not p.is_file():
                continue
            ts = p.stat().st_mtime
        except OSError:
            continue
        if ts >= cutoff and _match(p, query, kind):
            out.append((p, ts))
    return sorted(out, key=lambda x: -x[1])[:limit]


def search(query: str = "", kind: str = "", days: float | None = None, limit: int = 20,
           roots: list[Path] | None = None, max_depth: int = 5, budget: float = 6.0) -> list[tuple[Path, float]]:
    cutoff = _since(days)
    end = time.monotonic() + budget
    out = []
    for root in roots if roots is not None else search_roots():
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > end:
                break
            depth = len(Path(dirpath).parts) - base
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and depth < max_depth]
            for f in filenames:
                p = Path(dirpath) / f
                if not _match(p, query, kind):
                    continue
                try:
                    ts = p.stat().st_mtime
                except OSError:
                    continue
                if ts >= cutoff:
                    out.append((p, ts))
    return sorted(out, key=lambda x: -x[1])[:limit]


def tidy_plan(folder: Path | None = None, older_than_days: float = 1.0) -> list[tuple[Path, Path]]:
    """Top-level files in Downloads -> type folder. Skips anything from the last day (it may still be downloading)."""
    folder = folder or downloads_dir()
    cutoff = time.time() - older_than_days * 86400
    plan = []
    taken: set[Path] = set()  # paths already targeted by this plan: stops two files overwriting each other
    for p in sorted(folder.iterdir()) if folder.is_dir() else []:
        try:
            if not p.is_file() or p.stat().st_mtime > cutoff or p.suffix.lower() in (".crdownload", ".part", ".tmp"):
                continue
        except OSError:
            continue
        sub = TIDY_DIRS.get(kind_of(p))
        if sub:
            dest = folder / sub / p.name
            n = 1
            while dest.exists() or dest in taken:
                dest = folder / sub / f"{p.stem} ({n}){p.suffix}"
                n += 1
            taken.add(dest)
            plan.append((p, dest))
    return plan


class FindFiles(Tool):
    name = "find_files"
    description = ("Kullanıcının KENDİ dosyalarını bulur ve açar (kod araması için glob/grep): 'geçen hafta indirdiğim "
                   "PDF', 'dün açtığım Excel', 'staj belgesi nerede'. Bulunca yolu söyle; açmasını isterse open.\n"
                   "- 'açtığım/çalıştığım' -> recent, 'indirdiğim' -> downloads, ikisi de değilse search.\n"
                   "- tidy önce apply olmadan çağrılıp plan gösterilir; kullanıcı onaylayınca apply=true.")
    parameters = obj({
        "action": {"type": "string", "enum": ["recent", "downloads", "search", "open", "tidy"],
                   "description": "recent: son açılanlar | downloads: İndirilenler | search: kişisel klasörlerde ad "
                   "araması | open: path'i aç | tidy: İndirilenler'i tür klasörlerine ayır."},
        "query": {"type": "string", "description": "Dosya adında geçen kelimeler (hepsi geçmeli), ör. 'staj'. Boş: hepsi."},
        # No empty option (""): Gemini Live rejected the whole tool list on an empty enum value
        "kind": {"type": "string", "enum": list(KINDS), "description": "Dosya türü (boş bırakılabilir)."},
        "days": {"type": "number", "description": "Son kaç gün ('dün' 2, 'geçen hafta' 8)."},
        "path": {"type": "string", "description": "open için dosya yolu."},
        "apply": {"type": "boolean", "description": "tidy için: true ise gerçekten taşı."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("action") == "tidy" and args.get("apply"):
            return WRITE
        return COMPUTER if args.get("action") == "open" else READ

    def run(self, ctx: ToolContext, action: str, query: str = "", kind: str = "", days: float | None = None,
            path: str = "", apply: bool = False) -> ToolResult:
        if action == "open":
            if not path:
                raise ToolError("open için path gerekli")
            # A relative path is resolved against the chat's working folder (it used to be relative to the process cwd).
            p = ctx.resolve(path)
            if not p.exists():
                raise ToolError(f"dosya yok: {p}")
            if sys.platform == "win32":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(p)])
            return ToolResult(f"açıldı: {p.name}")
        if action == "tidy":
            plan = tidy_plan()
            if not plan:
                return ToolResult("İndirilenler zaten düzenli (taşınacak dosya yok)")
            counts: dict[str, int] = {}
            for _, dest in plan:
                counts[dest.parent.name] = counts.get(dest.parent.name, 0) + 1
            summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
            if not apply:
                return ToolResult(f"{len(plan)} dosya taşınacak: {summary}. Onaylarsa apply=true ile tekrar çağır.")
            moved = 0
            for src, dest in plan:
                try:
                    if dest.exists():  # created between planning and moving: don't overwrite (POSIX would replace it)
                        continue
                    dest.parent.mkdir(exist_ok=True)
                    shutil.move(str(src), str(dest))
                    moved += 1
                except OSError:
                    continue
            return ToolResult(f"{moved} dosya taşındı ({summary}); İndirilenler'in içinde alt klasörlerde.")
        fn = {"recent": recent, "downloads": downloads, "search": search}.get(action)
        if fn is None:
            raise ToolError(f"bilinmeyen action: {action}")
        hits = fn(query, kind, days)
        if not hits and action == "recent":  # not among recent files: search the disk
            hits = search(query, kind, days)
        return ToolResult("\n".join(_fmt(p, ts) for p, ts in hits) or "bulunamadı")


TOOLS = [FindFiles()]
