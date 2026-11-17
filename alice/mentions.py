"""@file mentions: adds the contents of files referenced as @yol/dosya.py (path/file) in a message to the model."""

from __future__ import annotations

import os
import re
from pathlib import Path

from alice.tools.files import SKIP_DIRS

MENTION = re.compile(r"(?<![\w@])@((?:\"[^\"]+\")|(?:[^\s,;:!?)]+))")
MAX_FILE = 100_000


def expand_mentions(text: str, cwd: Path) -> str:
    """Append existing, readable files to the end of the message as <file> blocks.
    Folders are added as a content list. Unmatched @ expressions stay as they are (e-mail, username, etc.)."""
    blocks = []
    seen = set()
    for m in MENTION.finditer(text):
        raw = m.group(1).strip('"').rstrip(".")
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = cwd / p
            p = p.resolve()
            if p in seen or not p.exists():
                continue
        except (OSError, ValueError, RuntimeError):  # NUL in path: ValueError; circular link: RuntimeError
            continue
        seen.add(p)
        if p.is_dir():
            try:
                names = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir() if e.name not in SKIP_DIRS)[:200]
            except OSError:  # no permission: the whole turn used to crash before this fix
                continue
            blocks.append(f'<folder path="{p}">\n' + "\n".join(names) + "\n</folder>")
            continue
        try:
            size = p.stat().st_size
            with p.open("rb") as fh:  # so a huge file (ISO, video) is not read into memory in full
                data = fh.read(MAX_FILE)
        except OSError:
            continue
        if b"\0" in data[:4096]:
            blocks.append(f'<file path="{p}">(binary dosya, {size} byte)</file>')
            continue
        body = data.decode("utf-8", errors="replace")
        more = f"\n... ({size - MAX_FILE} byte kesildi)" if size > MAX_FILE else ""
        blocks.append(f'<file path="{p}">\n{body}{more}\n</file>')
    if not blocks:
        return text
    return text + "\n\n" + "\n\n".join(blocks)


def complete_paths(cwd: Path, query: str, limit: int = 30) -> list[str]:
    """For the suggestion list shown when @ is typed: files/folders under cwd whose name contains query."""
    q = query.lower().replace("\\", "/")
    out: list[tuple[int, str]] = []
    scanned = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel_root = os.path.relpath(root, cwd).replace("\\", "/")
        rel_root = "" if rel_root == "." else rel_root + "/"
        for name in dirs + sorted(files):
            rel = rel_root + name + ("/" if name in dirs else "")
            low = rel.lower()
            if not q or q in low:
                # If the file name itself matches, rank it first, then shorter paths.
                score = (0 if name.lower().startswith(q.split("/")[-1]) else 1, len(rel))
                out.append((score, rel))
            scanned += 1
        if scanned > 20000:
            break
    out.sort()
    return [r for _, r in out[:limit]]
