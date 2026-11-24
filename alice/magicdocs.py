"""MagicDocs: documents that stay up to date on their own.

A Markdown file whose first line is ``# MAGIC DOC: <title>`` is registered for that chat when ALICE reads it. After
each turn ends (at most once every 10 minutes per document), a model in the background writes the lasting information
from the conversation (decisions, newly learned facts, changed steps) into the document. Only files marked with this
title are touched; the title line is kept, and if the model says "no change" the file is not written. If the first line
under the title is italic (``_..._``), it is treated as the document's purpose and given to the model as guidance.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

MAGIC_RX = re.compile(r"^#\s*MAGIC DOC:\s*(.+?)\s*$", re.I)
MIN_GAP = 600  # sec: the same document is updated at most at this interval
MAX_DOC = 40_000
NO_CHANGE = "DEĞİŞİKLİK YOK"
_lock = threading.Lock()

PROMPT = """You keep a "magic doc" up to date. Document title: {title}
{purpose}
Below are the document as it is now and the latest conversation. If the conversation has LASTING new information on
this document's subject (a decision, a fact learned, a changed step, a solved problem), update the document: write it
in place, correct what is outdated, don't repeat, keep the document's structure, write short and clear. Don't add
passing details, chat or guesses. Keep the document's language.
If no update is needed, write only this: {no_change}
If one is needed, write the document's FULL new version (the first line must be exactly "# MAGIC DOC: {title}") and
nothing else.

# The document now
{doc}

# Latest conversation
{talk}"""


def title_of(path: Path) -> str | None:
    """Returns the title if the file is a magic doc, otherwise None (only the first line is checked)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline(400)
    except OSError:
        return None
    m = MAGIC_RX.match(first.strip())
    return m.group(1) if m else None


def note_read(state: dict[str, Any], path: Path) -> bool:
    """read_file has read a file: if it is a magic doc, register it for the chat."""
    if path.suffix.lower() not in (".md", ".markdown", ".txt") or not path.is_file():
        return False
    title = title_of(path)
    if not title:
        return False
    docs: dict[str, dict[str, Any]] = state.setdefault("magic_docs", {})
    docs.setdefault(str(path), {"title": title, "at": 0.0})
    return True


def _read(path: Path) -> str | None:
    """The document's text; None if it cannot be read or is not UTF-8 (title_of swallows bad bytes, and here a
    UnicodeDecodeError was crashing the update at the end of the turn)."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _talk(messages: list[dict[str, Any]], n: int = 24) -> str:
    rows = []
    for m in messages[1:]:
        c = m.get("content")
        if m.get("role") in ("user", "assistant") and isinstance(c, str) and c.strip() and not m.get("_nudge"):
            rows.append(f"{'Kullanıcı' if m['role'] == 'user' else 'ALICE'}: {' '.join(c.split())[:1200]}")
    return "\n".join(rows[-n:])


def update(state: dict[str, Any], router: Any, messages: list[dict[str, Any]], now: float | None = None,
           role: str = "fast") -> list[str]:
    """Update the magic docs whose time has come. Paths of the updated files."""
    now = time.time() if now is None else now
    done: list[str] = []
    talk = _talk(messages)
    if not talk:
        return done
    for path_s, info in list((state.get("magic_docs") or {}).items()):
        if now - float(info.get("at") or 0) < MIN_GAP:
            continue
        path = Path(path_s)
        doc = _read(path)
        if doc is None:
            continue
        title = title_of(path)
        if not title or len(doc) > MAX_DOC:
            continue  # title was removed: it is no longer a magic doc
        info["at"] = now
        lines = doc.splitlines()
        purpose = ""
        if len(lines) > 1 and re.fullmatch(r"\s*[_*].+[_*]\s*", lines[1] or ""):
            purpose = "The document's purpose: " + lines[1].strip(" _*")
        prompt = PROMPT.format(title=title, purpose=purpose, no_change=NO_CHANGE, doc=doc, talk=talk)
        try:
            res = router.chat(role, [{"role": "user", "content": prompt}], max_tokens=6000)
            new = (res.text or "").strip()
        except Exception:  # noqa: BLE001 - if it cannot be updated, the document stays as it is
            continue
        new = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", new)
        if not new or NO_CHANGE in new[:60] or not MAGIC_RX.match(new.splitlines()[0].strip()):
            continue
        with _lock:
            try:
                if _read(path) != doc:
                    continue  # the user (or ALICE) changed it in the meantime: don't overwrite
                # The file's line endings are kept: on Windows, text mode was turning an LF file into CRLF throughout.
                eol = "\r\n" if b"\r\n" in path.read_bytes() else "\n"
                with open(path, "w", encoding="utf-8", newline=eol) as fh:
                    fh.write(new.replace("\r\n", "\n").rstrip() + "\n")
            except OSError:
                continue
        done.append(path_s)
    return done
