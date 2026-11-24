"""Don't lose big tool output: output over the limit is written to disk in full; the model gets head, tail and path.

Previously the middle of output over 12 thousand characters was thrown away (only run_command kept all of it): the
part cut from a long CI log, web page, grep or MCP output could never be seen again. The model now reads the file with
read_file (offset/limit) or grep if needed. Files are in ``~/.alice/tool-results/GGAAYY/``; ones older than 7 days are
deleted.
"""
from __future__ import annotations

import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

KEEP_DAYS = 7
NO_PERSIST = {"read_file"}  # has its own limit; writing it to a file and reading it back would be a loop
_MARK = "[tam çıktı dosyada:"
_last_sweep = 0.0


def root() -> Path:
    from alice.config import alice_home

    return alice_home() / "tool-results"


def _sweep(now: float) -> None:
    global _last_sweep
    if now - _last_sweep < 3600:
        return
    _last_sweep = now
    try:
        for d in root().iterdir():
            if d.is_dir() and now - d.stat().st_mtime > KEEP_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass  # cleanup is retried on the next sweep


def save(text: str, call_id: str, tool: str) -> Path | None:
    now = time.time()
    folder = root() / datetime.fromtimestamp(now).strftime("%Y%m%d")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{tool}-{call_id}")[:120] or "cikti"
    # The call ID is not unique: some providers send an empty one or a sequence number ("call_0"). When the name was
    # only from the ID, a later output overwrote the earlier file; the old result's path pointed at another output.
    path = folder / f"{safe}-{uuid.uuid4().hex[:8]}.txt"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", errors="replace")
    except OSError:
        return None
    _sweep(now)
    return path


def shape(text: str, limit: int, call_id: str, tool: str) -> str:
    """If over the limit, save the whole text and return head, tail and file path; otherwise return the text itself."""
    from alice.tools.base import truncate

    if len(text) <= limit or tool in NO_PERSIST or _MARK in text:
        return truncate(text, limit)
    path = save(text, call_id, tool)
    if path is None:
        return truncate(text, limit)
    note = (f"\n{_MARK} {path} ({len(text)} karakter) - kesilen kısmı read_file (offset/limit) ya da grep ile "
            "oku; aracı tekrar çalıştırma]")
    return truncate(text, max(1000, limit - len(note))) + note
