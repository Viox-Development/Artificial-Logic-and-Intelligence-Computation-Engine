from __future__ import annotations

import fnmatch
import json
import os
import re
import difflib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from alice.tools.shell import NO_WINDOW
from alice.tools.base import WRITE, Tool, ToolContext, ToolError, ToolResult, make_diff, obj

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".idea", ".vs", "bin", "obj", "Library", "Temp", ".gradle", "AppData",
             "site-packages"}
# File search limits: keep the agent from hanging for minutes when searching a large place like the home folder.
SEARCH_SECONDS = 20.0
SEARCH_MAX_FILES = 20000
SEARCH_MAX_BYTES = 2_000_000
# Paginated grep collects at most this many matching lines in memory (a cap independent of the page size).
MAX_SCAN_LINES = 5000


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def glob_regex(pattern: str) -> "re.Pattern[str]":
    """Convert a glob pattern (e.g. '**/*.py', 'src/**/*.cs', '*.txt') into a regex matching relative paths.
    '**/' matches zero or more folders, '*' and '?' match within a single folder name. Case-insensitive on Windows."""
    pat = pattern.replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        elif pat[i] == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out += re.escape(pat[i])
                i += 1
            else:
                body = pat[i + 1:j]
                out += "[" + ("^" + body[1:] if body.startswith("!") else body) + "]"
                i = j + 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile(out + r"\Z", re.IGNORECASE if os.name == "nt" else 0)


def _limit_note(reason: str) -> str:
    return (f"[SEARCH CUT SHORT: {reason}. The result is incomplete; 'no matches' does NOT mean what you searched for isn't there. "
            "Search again with a narrower folder (path) or a file filter (glob).]\n")


def _with_note(body: str, note: str) -> ToolResult:
    # Put the note first in a cut search, so weak models don't take "eşleşme yok" for a definitive result.
    return ToolResult(note + body, is_error=bool(note))


_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return True
    # Files written by PowerShell's `>` are UTF-16: full of zero bytes but still text. They used to count as binary and
    # could not be read (a 740-byte .txt in the 2026-11-13 chat).
    if head.startswith(_UTF16_BOMS):
        return False
    return b"\0" in head


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(_UTF16_BOMS):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1254", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def file_encoding(path: Path) -> str:
    """The encoding _read_text uses to decode the file (writes reuse the same one). Falls back to utf-8 if unreadable."""
    try:
        raw = path.read_bytes()
    except OSError:
        return "utf-8"
    if raw.startswith(_UTF16_BOMS):
        return "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ("utf-8", "cp1254"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def write_encoded(path: Path, text: str, encoding: str) -> None:
    """Write text in the given encoding; use UTF-8 if it has characters the old single-byte encoding can't hold."""
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError:
        data = text.encode("utf-8")
    path.write_bytes(data)


def _write_like(path: Path, text: str) -> None:
    """Write an edited file back in the same encoding, so a UTF-16 .ps1/.reg does not silently become UTF-8 and
    a UTF-8 BOM'd .ps1 keeps its BOM (Windows PowerShell 5.1 reads a BOM-less file as ANSI, so Turkish characters
    get garbled). A cp1254 .bat must not be converted to UTF-8."""
    write_encoded(path, text, file_encoding(path))


def snapshot(ctx: ToolContext, p: Path) -> None:
    """For undo: save the file's state from before it was first touched in this turn."""
    cp = ctx.state.get("checkpoint")
    if cp is None:
        return
    key = str(p)
    if key not in cp:
        try:
            cp[key] = p.read_bytes() if p.exists() else None
        except OSError:
            pass  # unreadable: no checkpoint for this file


def _page_range(pages: str | None, total: int) -> list[int]:
    if not pages:
        return list(range(total))
    out: list[int] = []
    for part in str(pages).replace(" ", "").split(","):
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError as e:
            raise ToolError(f"could not understand pages: {pages!r} (e.g. '3' or '1-5')") from e
        out += [i - 1 for i in range(max(lo, 1), min(hi, total) + 1)]
    if not out:
        raise ToolError(f"pages={pages!r} is out of range; the PDF has {total} pages")
    return out


def read_pdf(p: Path, pages: str | None, max_chars: int) -> str:
    """Extract text from a PDF: with pypdf if available, otherwise with pdftotext (poppler)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        try:
            reader = PdfReader(str(p))
            total = len(reader.pages)
            parts = [f"--- sayfa {i + 1}/{total} ---\n{(reader.pages[i].extract_text() or '').strip()}"
                     for i in _page_range(pages, total)]
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - corrupt/encrypted PDF
            raise ToolError(f"could not read the PDF: {type(e).__name__}: {e}") from e
        text = "\n\n".join(parts)
    elif shutil.which("pdftotext"):
        cmd = ["pdftotext", "-layout"]
        if pages:
            a, _, b = str(pages).partition("-")
            cmd += ["-f", a.strip(), "-l", (b or a).strip()]
        r = subprocess.run(cmd + [str(p), "-"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise ToolError(f"pdftotext error: {r.stderr.strip()[:300]}")
        text = r.stdout
    else:
        raise ToolError("reading PDFs needs pypdf: run 'pip install pypdf' with run_command, then try again")
    if not text.strip() or not re.sub(r"--- sayfa \d+/\d+ ---", "", text).strip():
        return f"{p}: the PDF has no text (it may be a scanned image; use the screen/vision tools to see the pages)"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [cut; ask for a smaller range with pages]"
    return text


def render_notebook(p: Path) -> str:
    """Convert a Jupyter notebook into readable text cell by cell (source + text outputs)."""
    import json

    try:
        nb = json.loads(_read_text(p))
    except ValueError as e:
        raise ToolError(f"could not read the ipynb: {e}") from e
    out: list[str] = []
    for i, cell in enumerate(nb.get("cells") or [], 1):
        src = cell.get("source") or ""
        src = "".join(src) if isinstance(src, list) else str(src)
        out.append(f"# %% cell {i} [{cell.get('cell_type', '?')}]")
        out += src.splitlines()
        for o in cell.get("outputs") or []:
            txt = o.get("text") or (o.get("data") or {}).get("text/plain") or o.get("evalue") or ""
            txt = "".join(txt) if isinstance(txt, list) else str(txt)
            if txt.strip():
                out.append("# output:")
                out += [f"# {ln}" for ln in txt.splitlines()[:30]]
    return "\n".join(out)


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


def _stamp(p: Path) -> tuple[int, int]:
    """The file's state: (modification time, size). Time alone is not enough on Windows: two writes in a row can
    get the same timestamp, and a file changed in between was counted as "unchanged". Size is compared too."""
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, -1


def mark_read(ctx: ToolContext, p: Path) -> None:
    """Mark as read in this session: so editing tools know which state of the file they are writing against."""
    ctx.state.setdefault("read_files", {})[str(p)] = _stamp(p)


_READS = ("read_file", "write_file", "edit_file", "multi_edit", "notebook_edit")


def restore_reads(ctx: ToolContext, messages: list[dict[str, Any]], saved_at: float) -> int:
    """Rebuild the "read" record of a chat loaded from disk, from its history. The record was only kept in memory, so
    after ALICE restarted (update, crash) a model that wanted to edit a file it had just read got a "not read in this
    session" error and had to read it again. Only files unchanged on disk since the chat's last save (saved_at) count
    as read; if one changed, editing against the old content is still rejected."""
    ok = {m.get("tool_call_id") for m in messages
          if m.get("role") == "tool" and not str(m.get("content") or "").startswith(("ERROR:", "HATA:"))}
    n = 0
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if (fn.get("name") not in _READS and fn.get("name") != "apply_patch") or tc.get("id") not in ok:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if fn.get("name") == "apply_patch":  # files the patch wrote (added, edited, moved)
                    from alice.tools.patch import parse_patch

                    paths = [o.move_to or o.path for o in parse_patch(str(args.get("patch") or ""))
                             if o.kind != "delete"]
                else:
                    paths = [args.get("path")]
                targets = [ctx.resolve(str(x)) for x in paths if x]
            except Exception:  # noqa: BLE001 - corrupt history record: skip
                continue
            for p in targets:
                if not p.is_file():
                    continue
                if _mtime(p) / 1e9 <= saved_at:  # changed after the save (even in the same second): not read
                    mark_read(ctx, p)
                    n += 1
    return n


def require_read(ctx: ToolContext, p: Path) -> None:
    """No editing without reading first (the Edit/Write rule from Claude Code). The model must not guess the file's
    content or overwrite it from memory of an old version; if the file changed on disk after it was read (by the user
    or another tool), it must be read again. Disabled with [agent] require_read_before_edit = false."""
    if not ctx.config.agent.get("require_read_before_edit", True):
        return
    seen = ctx.state.get("read_files", {})
    key = str(p)
    if key not in seen:
        raise ToolError(f"'{p.name}' ({p}) has not been read in this session. Read it with read_file first, then edit "
                        f"(don't guess the content).") 
    if seen[key] != _stamp(p):
        raise ToolError(f"'{p.name}' ({p}) changed on disk after it was read: it was written to since read_file, so it can't be "
                        f"edited against the old content. Read its current state with read_file, then edit.")


def _find_symbol_range(lines: list[str], symbol: str) -> tuple[int, int]:
    """Finds the start and end line indices (0-based) of a symbol/function/class definition in the text.
    Supports Python, JS/TS, Rust and C-style languages."""
    import re
    symbol_pat = re.escape(symbol)
    patterns = [
        re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+" + symbol_pat + r"\b"),  # Python/Ruby
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+" + symbol_pat + r"\b"),  # JS/TS
        re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl|type)\s+" + symbol_pat + r"\b"),  # Rust
        re.compile(r"^\s*(?:public|private|protected|static|final|abstract|class|struct|interface|enum|void|int|string|bool)\s+.*\b" + symbol_pat + r"\s*(?:\(|{)"), # C++/Java/C#/Go
    ]

    matches: list[int] = []
    for idx, line in enumerate(lines):
        for pat in patterns:
            if pat.search(line):
                matches.append(idx)
                break

    if not matches:
        # Fallback: any line that matches as a whole word
        fallback_pat = re.compile(r"\b" + symbol_pat + r"\b")
        for idx, line in enumerate(lines):
            if fallback_pat.search(line):
                matches.append(idx)
                break

    if not matches:
        raise ToolError(f"symbol '{symbol}' was not found in the file.")

    start_idx = matches[0]
    start_line = lines[start_idx]
    start_indent = len(start_line) - len(start_line.lstrip())

    brace_count = 0
    has_braces = False
    end_idx = start_idx

    for i in range(start_idx, len(lines)):
        l = lines[i]
        brace_count += l.count("{") - l.count("}")
        if "{" in l:
            has_braces = True
        end_idx = i
        if has_braces:
            if brace_count <= 0 and i > start_idx:
                break
        else:
            if i > start_idx:
                stripped = l.strip()
                if stripped and not stripped.startswith(("#", "//", "/*", "*")):
                    current_indent = len(l) - len(l.lstrip())
                    if current_indent <= start_indent:
                        end_idx = i - 1
                        break
    return start_idx, max(start_idx, end_idx)


class ReadFile(Tool):
    name = "read_file"
    parallel_safe = True
    description = ("Reads a file's contents with line numbers; images come back as images, PDFs as text, .ipynb as "
                   "cells.\n"
                   "- Read a file before editing it; copy from here into edit_file exactly (without the line "
                   "numbers).\n"
                   "- In a big file, find the place with grep, then read with offset/limit or symbol.\n"
                   "- With symbol (e.g. 'my_func'), the block of that definition in the file is read.")
    parameters = obj({
        "path": {"type": "string", "description": "File path, absolute or relative to the working folder, e.g. "
                                                  "'src/main.py'."},
        "offset": {"type": "integer", "description": "Line to start reading at (starts at 1). Default 1."},
        "limit": {"type": "integer", "description": "Maximum lines to read. Default 2000."},
        "symbol": {"type": "string", "description": "Name of the symbol/function/class to read. If given, that block "
                                                    "is read."},
        "pages": {"type": "string", "description": "PDF only: pages to read, e.g. '3' or '1-5'. Default: all (give "
                                                   "pages for a big PDF)."},
    }, ["path"])

    def summary(self, args: dict[str, Any]) -> str:
        s = args.get("symbol")
        extra = f" [{s}]" if s else (f" (line {args['offset']}+)" if args.get("offset") else "")
        return f"{args.get('path', '')}{extra}"[:120]

    def run(self, ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000, pages: str | None = None,
            symbol: str | None = None) -> ToolResult:
        p = ctx.resolve(path)
        if not p.exists():
            raise ToolError(f"file not found: {p}")
        if p.is_dir():
            raise ToolError(f"{p} is a folder; use list_dir")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
            from alice.tools.imaging import file_to_png_b64
            mark_read(ctx, p)
            return ToolResult(f"image: {p}", images=[file_to_png_b64(p, ctx.config.agent.get("screenshot_max_width", 1366))])
        if p.suffix.lower() == ".pdf":
            content = read_pdf(p, pages, int(ctx.config.agent.get("tool_output_chars", 12000)) * 4)
            mark_read(ctx, p)
            return ToolResult(content)
        if _is_binary(p):
            raise ToolError(f"{p} is a binary file ({p.stat().st_size} bytes)")
        text = render_notebook(p) if p.suffix.lower() == ".ipynb" else _read_text(p)
        lines = text.splitlines()
        header = ""
        if symbol:
            s_start, s_end = _find_symbol_range(lines, symbol)
            offset, limit = s_start + 1, (s_end - s_start) + 1
            header = f"[Read symbol '{symbol}' (lines {offset}-{s_end + 1})]\n"

        start = max(int(offset or 1), 1)
        limit = max(int(limit or 2000), 1)
        chunk = lines[start - 1:start - 1 + limit]
        if not chunk and lines:
            # It used to say "(boş dosya)" (empty file): the model took a full file for an empty one.
            raise ToolError(f"offset={start} is past the end of the file: {p.name} has {len(lines)} lines")
        body = "\n".join(f"{i:>6}\t{line[:2000]}" for i, line in enumerate(chunk, start))
        more = len(lines) - (start - 1 + len(chunk))
        if more > 0:
            body += f"\n... ({more} more lines; continue with offset={start + len(chunk)})"
        # Count the file as "read" only once its content is actually delivered: if the symbol is not found, it must not be
        # counted (otherwise edit_file would let an agent edit content it never saw).
        mark_read(ctx, p)
        return ToolResult(header + (body or "(empty file)"))


class WriteFile(Tool):
    name = "write_file"
    description = ("Creates a new file or replaces the WHOLE of an existing file with the given content; missing "
                   "folders are created. Returns created/updated and the line count.\n"
                   "- For a small change to an existing file use edit_file/multi_edit, not this; rewriting the whole "
                   "file is slow and risky.\n"
                   "- Read an existing file with read_file before overwriting it; overwriting a file not read in this "
                   "session, or changed after it was read, is refused.\n"
                   "- Don't create new files such as docs/READMEs unless the user asks; prefer editing an existing "
                   "one.")
    parameters = obj({
        "path": {"type": "string", "description": "Path of the file to write, absolute or relative to the working "
                                                  "folder."},
        "content": {"type": "string", "description": "The whole new content of the file."},
    }, ["path", "content"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} ({len(args.get('content', ''))} karakter)"

    def run(self, ctx: ToolContext, path: str, content: str) -> ToolResult:
        p = ctx.resolve(path)
        if p.is_file():
            require_read(ctx, p)
        snapshot(ctx, p)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        before = _read_text(p) if existed and not _is_binary(p) else ""
        p.write_text(content, encoding="utf-8", newline="")
        mark_read(ctx, p)  # it knows the state it just wrote, so it can edit right after
        return ToolResult(f"{'updated' if existed else 'created'}: {p} ({len(content.splitlines())} lines)",
                          diff=make_diff(path, before, content))


def _apply_edit(text: str, old: str, new: str, replace_all: bool, where: str = "") -> tuple[str, int]:
    """Apply one change to the text in memory. Raises ToolError if it is not found or ambiguous."""
    if old == new:
        raise ToolError(f"{where}old_string and new_string are the same")
    if not old:
        raise ToolError(f"{where}old_string can't be empty")
    count = text.count(old)
    if count == 0 and "\r\n" in text:
        # Models often send LF for CRLF files.
        old_crlf = old.replace("\r\n", "\n").replace("\n", "\r\n")
        if old_crlf in text:
            old, new = old_crlf, new.replace("\r\n", "\n").replace("\n", "\r\n")
            count = text.count(old)
    if count == 0:
        # Diagnose the error: what kind of mismatch is it? Is the content nearly the same, or really absent?
        norm_old = "".join(old.split())
        norm_text = "".join(text.split())
        if norm_old in norm_text:
            hint = "\n[HINT: The text itself is in the file, but with different spaces/indentation/tabs. " \
                   "Copy the lines from the read_file output exactly (without the line numbers).]"
        else:
            hint = _similar_hint(text, old)
        raise ToolError(f"{where}old_string was not found in the file. Read the file again with read_file and copy it exactly.{hint}")
    if "\r\n" in text and text.count("\r\n") == text.count("\n") and "\n" in new and "\r\n" not in old:
        # In a CRLF file, when the old text has no line ending, the new text's LFs were not converted and the file
        # kept mixed line endings.
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    if count > 1 and not replace_all:
        lines = text.splitlines()
        old_lines = [l.strip() for l in old.splitlines() if l.strip()]
        probe = old_lines[0] if old_lines else old.strip()
        where_at = [i + 1 for i, l in enumerate(lines)
                    if probe in l or (probe and difflib.SequenceMatcher(None, probe, l.strip()).ratio() > 0.8)]
        loc = ""
        if where_at:
            shown = ", ".join(map(str, where_at[:5]))
            loc = f" (lines {shown}{'...' if len(where_at) > 5 else ''})"
        raise ToolError(f"{where}old_string occurs {count} times{loc}; add context or use replace_all=true")
    if replace_all:
        return text.replace(old, new), count
    return text.replace(old, new, 1), 1


def _similar_hint(text: str, old: str) -> str:
    """For an old_string that was not found, find the most similar lines and produce a guiding hint."""
    lines = text.splitlines()
    old_lines = [l.strip() for l in old.splitlines() if l.strip()]
    if not old_lines:
        return ""
    probe = old_lines[0]
    probe_words = set(re.findall(r"\w\w+", probe))
    scored: list[tuple[float, int, str]] = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = difflib.SequenceMatcher(None, probe, stripped).ratio()
        overlap = len(probe_words & set(re.findall(r"\w\w+", stripped))) / max(len(probe_words), 1)
        score = max(ratio, overlap)
        if score >= 0.55:
            scored.append((score, i, stripped))
    scored.sort(reverse=True)
    top = scored[:3]
    if not top:
        return "\n[HINT: No similar line was found either; the file changed a lot. Read the whole file with read_file.]"
    shown = "\n  ".join(f"line {i}: {line[:100]}" for _, i, line in top)
    return f"\n[HINT: Similar lines found:\n  {shown}\n Look at the current lines with read_file and copy them exactly.]"


class EditFile(Tool):
    name = "edit_file"
    description = ("Replaces exact text in an existing file and shows the diff of the change.\n"
                   "- Read it with read_file first: editing a file not read in this session, or changed on disk after "
                   "it was read, is refused.\n"
                   "- old_string must appear in the file exactly (indentation and spaces included) and only once; "
                   "copy it from the read_file output without the line number and tab. If it appears more than once, "
                   "add more context or pass replace_all=true.\n"
                   "- For several changes in the same file use multi_edit; for a new file use write_file.")
    parameters = obj({
        "path": {"type": "string", "description": "Path of the file to edit."},
        "old_string": {"type": "string", "description": "Text to replace, exactly as in the read_file output, without "
                                                        "line numbers. Add the lines before/after if needed to make "
                                                        "it unique."},
        "new_string": {"type": "string", "description": "Text to put in its place; must differ from old_string."},
        "replace_all": {"type": "boolean", "description": "true: replace every occurrence of old_string (e.g. a "
                                                          "rename). Default false."},
    }, ["path", "old_string", "new_string"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path"))

    def run(self, ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolResult:
        p = ctx.resolve(path)
        if not p.is_file():
            raise ToolError(f"file not found: {p}")
        require_read(ctx, p)
        before = _read_text(p)
        text, n = _apply_edit(before, old_string, new_string, replace_all)
        snapshot(ctx, p)
        _write_like(p, text)
        mark_read(ctx, p)
        return ToolResult(f"edited: {p} ({n} changes)",
                          diff=make_diff(path, before.replace("\r\n", "\n"), text.replace("\r\n", "\n")))


class MultiEdit(Tool):
    name = "multi_edit"
    description = ("Makes several changes in one file at once and shows the combined diff.\n"
                   "- edits are applied in order (each on the result of the previous one); if even one fails, NOTHING "
                   "is written to the file.\n"
                   "- Each edit follows the edit_file rules: old_string must be exact and unique, or "
                   "replace_all=true. Don't search a later edit for text an earlier edit changed.\n"
                   "- Read it with read_file first (an unread or since-changed file is refused). For a new file use "
                   "write_file.")
    parameters = obj({
        "path": {"type": "string", "description": "Path of the file to edit."},
        "edits": {"type": "array", "minItems": 1, "description": "Changes to apply in order.", "items": obj({
            "old_string": {"type": "string", "description": "Text to replace (exact)."},
            "new_string": {"type": "string", "description": "Text to put in its place."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
        }, ["old_string", "new_string"])},
    }, ["path", "edits"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} ({len(args.get('edits') or [])} changes)"

    def run(self, ctx: ToolContext, path: str, edits: list[dict[str, Any]]) -> ToolResult:
        p = ctx.resolve(path)
        if not p.is_file():
            raise ToolError(f"file not found: {p}")
        if not edits:
            raise ToolError("edits is empty")
        require_read(ctx, p)
        before = _read_text(p)
        text = before
        total = 0
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict) or "old_string" not in e or "new_string" not in e:
                raise ToolError(f"edit {i}: old_string and new_string are required")
            text, n = _apply_edit(text, str(e["old_string"]), str(e["new_string"]), bool(e.get("replace_all")),
                                  where=f"edit {i}/{len(edits)}: ")
            total += n
        snapshot(ctx, p)
        _write_like(p, text)
        mark_read(ctx, p)
        return ToolResult(f"edited: {p} ({len(edits)} edits, {total} changes)",
                          diff=make_diff(path, before.replace("\r\n", "\n"), text.replace("\r\n", "\n")))


# Folders the user calls "İndirilenler" (Downloads), "Masaüstü" (Desktop). The model used to try dozens of PowerShell
# commands to find them (voice.log: 40+ run_command calls in a minute); the name now maps straight to the folder.
KNOWN_FOLDERS = {
    "downloads": ["Downloads"], "indirilenler": ["Downloads"], "i̇ndirilenler": ["Downloads"],
    "desktop": ["OneDrive/Desktop", "OneDrive/Masaüstü", "Desktop"], "masaüstü": ["OneDrive/Desktop",
                                                                                "OneDrive/Masaüstü", "Desktop"],
    "documents": ["OneDrive/Documents", "OneDrive/Belgeler", "Documents"],
    "belgeler": ["OneDrive/Documents", "OneDrive/Belgeler", "Documents"],
    "pictures": ["OneDrive/Pictures", "Pictures"], "resimler": ["OneDrive/Pictures", "Pictures"],
    "videos": ["Videos"], "videolar": ["Videos"], "music": ["Music"], "müzik": ["Music"],
}


def known_folder(path: str) -> Path | None:
    """'İndirilenler', 'downloads', 'Masaüstü' -> the user's folder for that (the OneDrive one if it was moved there)."""
    key = path.strip().strip("/\\").lower()
    for cand in KNOWN_FOLDERS.get(key, []):
        p = Path.home() / cand
        if p.is_dir():
            return p
    if KNOWN_FOLDERS.get(key) == ["Downloads"]:
        # A moved Downloads folder (other drive, Properties > Location) is not in ~/Downloads: use Windows' location.
        # It used to return None and report "klasör yok: <çalışma klasörü>/İndirilenler" (folder not found).
        from alice.tools.fetch_file import downloads_dir

        p = downloads_dir()
        if p.is_dir():
            return p
    return None


def _when(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


class ListDir(Tool):
    name = "list_dir"
    parallel_safe = True
    description = ("Lists the files and subfolders of a folder with their sizes and types (does not go into "
                   "subfolders). Pass names like 'Downloads', 'Desktop', 'Documents' (or 'İndirilenler', 'Masaüstü', "
                   "'Belgeler') directly as path. For questions like 'the file I downloaded last' use sort=newest "
                   "(newest first, with dates). Don't use run_command for folder contents. To find files by pattern "
                   "use glob; to search contents use grep.")
    parameters = obj({
        "path": {"type": "string", "description": "Folder path, or 'Downloads', 'Desktop', 'Documents'. Default: the "
                                                  "working folder."},
        "sort": {"type": "string", "enum": ["name", "newest"], "description": "name (default) or newest: most "
                                                                              "recently changed first, with dates."},
        "limit": {"type": "integer", "description": "Maximum items (default 500; 30 for newest)."},
    })

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path") or ".")

    def run(self, ctx: ToolContext, path: str = ".", sort: str = "name", limit: int | None = None) -> ToolResult:
        p = known_folder(path) or ctx.resolve(path)
        if not p.is_dir():
            raise ToolError(f"folder not found: {p}")
        newest = sort == "newest"
        limit = max(1, min(int(limit or (30 if newest else 500)), 500))

        def mtime(x: Path) -> float:
            try:
                return x.stat().st_mtime
            except OSError:
                return 0.0

        entries = list(p.iterdir())
        entries.sort(key=(lambda x: -mtime(x)) if newest else (lambda x: (not x.is_dir(), x.name.lower())))
        rows = []
        for e in entries[:limit]:
            try:
                when = f", {_when(e.stat().st_mtime)}" if newest else ""
                rows.append(f"{e.name}/{'  (' + when[2:] + ')' if when else ''}" if e.is_dir()
                            else f"{e.name}  ({e.stat().st_size} B{when})")
            except OSError:
                rows.append(f"{e.name}  (not accessible)")
        more = f"\n... and {len(entries) - limit} more items" if len(entries) > limit else ""
        return ToolResult(f"{p}\n" + "\n".join(rows) + more if rows else f"{p} is empty")


class Glob(Tool):
    name = "glob"
    parallel_safe = True
    description = ("Finds files and folders whose names match a pattern; most recently changed first, at most 300 "
                   "results. To search for text INSIDE files use grep. To find an installed program use windows "
                   "action=find, not this. '**' searches in big places such as the user folder or a drive root are "
                   "slow; give a narrow path.")
    parameters = obj({
        "pattern": {"type": "string", "description": "Glob pattern, relative to path: '**/*.py' (any depth), '*.txt' "
                                                     "(that folder only), 'src/**/test_*.py'."},
        "path": {"type": "string", "description": "Folder to start the search in. Default: the working folder."},
    }, ["pattern"])

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("pattern", ""))

    def run(self, ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
        import time

        base = ctx.resolve(path)
        rx = glob_regex(pattern)
        # Without "**" the pattern's depth is bounded: no need to go any deeper.
        max_depth = None if "**" in pattern else pattern.replace("\\", "/").strip("/").count("/")
        hits: list[Path] = []
        note = ""
        deadline = time.monotonic() + SEARCH_SECONDS
        # Path.glob only yields matches: with few matches, the time/stop checks never ran and it still descended
        # into folders to skip (AppData, node_modules). We walk ourselves and check per folder.
        for root, dirs, files in os.walk(base):
            if ctx.cancelled():
                note = _limit_note("stopped by the user")
                break
            if time.monotonic() > deadline:
                note = _limit_note(f"{SEARCH_SECONDS:.0f} sn doldu")
                break
            rel_root = os.path.relpath(root, base).replace("\\", "/")
            rel_root = "" if rel_root == "." else rel_root + "/"
            depth = rel_root.count("/")
            dirs[:] = [d for d in dirs if not _skip_dir(d)] if max_depth is None or depth < max_depth else []
            for name in [*files, *dirs]:
                if rx.match(rel_root + name):
                    hits.append(Path(root) / name)
            if len(hits) >= 2000:
                break
        hits.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
        out = [str(h.relative_to(base)) for h in hits[:300]]
        return _with_note("\n".join(out) if out else "no matches", note)


class Grep(Tool):
    name = "grep"
    parallel_safe = True
    description = ("Searches inside files with a regular expression (uses ripgrep if available). Use this instead of "
                   "grep/findstr in run_command to find where something appears in code/text; for file NAMES use "
                   "glob.\n"
                   "- output_mode: content gives 'file:line:text' lines (default), files_with_matches only file names "
                   "(the cheapest way to see which files first), count the number of matches per file.\n"
                   "- Use context to see lines around each match; multiline for patterns across lines.\n"
                   "- With many results: narrow the files with files_with_matches first, then look at one file with "
                   "path; or read the next page with offset. max_results is the number of lines shown per page.\n"
                   "- A file filter such as glob='*.py' speeds up both code and text searches a lot.")
    parameters = obj({
        "pattern": {"type": "string", "description": "Regex, e.g. 'def \\w+_test' or 'TODO'. Escape special "
                                                     "characters (. ( [ {) to search for them literally."},
        "path": {"type": "string", "description": "Folder or file to search. Default: the working folder."},
        "glob": {"type": "string", "description": "Only files matching this pattern, e.g. '*.py' or '*.{ts,tsx}'."},
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                        "description": "content (lines, default), files_with_matches (file names), count."},
        "context": {"type": "integer", "description": "In content mode, lines to show before and after each match. "
                                                      "Default 0."},
        "ignore_case": {"type": "boolean", "description": "Case-insensitive search. Default false."},
        "multiline": {"type": "boolean", "description": "true: the pattern can span line ends ('.' matches line ends "
                                                        "too). Default false."},
        "max_results": {"type": "integer", "description": "Maximum result lines (page size). Default 200; lower it "
                                                          "for few results, page through many results with offset."},
        "offset": {"type": "integer", "description": "Skip this many result lines first (paging). Default 0."},
    }, ["pattern"])

    def summary(self, args: dict[str, Any]) -> str:
        where = f" ({args['glob']})" if args.get("glob") else ""
        mode = f" [{args['output_mode']}]" if args.get("output_mode") not in (None, "content") else ""
        return f"{args.get('pattern', '')}{where}{mode}"

    def run(self, ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None,
            output_mode: str = "content", context: int = 0, ignore_case: bool = False, multiline: bool = False,
            max_results: int = 200, offset: int = 0) -> ToolResult:
        base = ctx.resolve(path)
        if not base.exists():
            raise ToolError(f"path not found: {base}")
        context = max(0, min(int(context or 0), 20))
        max_results = max(1, int(max_results or 200))
        offset = max(0, int(offset or 0))
        multiline = multiline or "\\n" in pattern  # a pattern that looks for a line end never matches a single line
        rg = shutil.which("rg")
        exact_total = True  # rg content mode stops at 100 matches per file: then the total is an "at least" count
        if rg:
            lines, note = self._rg(rg, ctx, base, pattern, glob, output_mode, context, ignore_case, multiline)
            if output_mode == "content" and len(lines) > offset + max_results:
                exact_total = False
        else:
            # Python path: collect all matches (cap MAX_SCAN_LINES); if cut early, the count is an "at least" number.
            lines, note, cut = self._py(ctx, base, pattern, glob, output_mode, context, ignore_case, multiline,
                                        MAX_SCAN_LINES)
            if cut:
                exact_total = False
                note = note or _limit_note(f"{MAX_SCAN_LINES} matching lines scanned")
        total = len(lines)
        page = lines[offset:offset + max_results]
        scanned = str(base) if base.is_dir() else str(base.parent)
        shown_total = f"{total}" if exact_total else f"at least {total}"
        if not page:
            if offset and total:
                # offset is past the last page: "eşleşme yok" would mislead; there are matches, just not on this page.
                body = f"no results on this page ({shown_total} matching lines, offset={offset} is past the last page)"
            else:
                body = f"no matches (pattern={pattern!r}"
                if glob:
                    body += f", glob={glob!r}"
                body += f", taranan alan: {scanned})"
                if glob:
                    body += ("\n[HINT: the glob filter may be too narrow; first see the files with files_with_matches, "
                             "then look at a single file with path.]")
        else:
            body = "\n".join(page)
            if total > offset + max_results:
                body += (f"\n... (showing {offset + max_results} of {shown_total} matching lines in total; "
                         f"continue with offset={offset + max_results}"
                         + (", or narrow the search with path/glob" if base.is_dir() else "")
                         + ")")
        # Grep: reading matches and their surrounding context lines counts as reading the file,
        # so the model can edit the matched lines without an extra read_file step if the file is unchanged.
        for f in {ctx.resolve(ln.split(":", 1)[0]) for ln in page if ":" in ln}:
            if f.is_file():
                mark_read(ctx, f)
        return _with_note(body, note)

    @staticmethod
    def _rg(rg: str, ctx: ToolContext, base: Path, pattern: str, glob: str | None, mode: str, context: int,
            ignore_case: bool, multiline: bool) -> tuple[list[str], str]:
        cmd = [rg, "--no-heading", "--color", "never", "--max-columns", "300"]
        if mode == "files_with_matches":
            cmd.append("-l")
        elif mode == "count":
            cmd.append("-c")
        else:
            cmd += ["-n", "-m", "100"]
            if context:
                cmd += ["-C", str(context)]
        if ignore_case:
            cmd.append("-i")
        if multiline:
            cmd += ["-U", "--multiline-dotall"]
            # rg reads files raw: so a '\n' pattern doesn't fail on '\r' in CRLF files, turn it into '\r?\n'.
            pattern = re.sub(r"(?<!\\)\\n", r"\\r?\\n", pattern)
        if glob:
            cmd += ["-g", glob]
        # Relative paths: more readable and fewer tokens for the model.
        cmd += ["-e", pattern, "." if base.is_dir() else str(base)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                               cwd=base if base.is_dir() else None, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return [], _limit_note("60 sn doldu")
        if r.returncode == 2 and not r.stdout.strip():
            # rg error (bad regex, missing path): swallowing it as "eşleşme yok" misleads the model.
            raise ToolError(f"rg error: {(r.stderr or '').strip()[:300]}")
        lines = [ln[2:] if ln.startswith("./") or ln.startswith(".\\") else ln for ln in r.stdout.splitlines()]
        return lines, ""

    def _py(self, ctx: ToolContext, base: Path, pattern: str, glob: str | None, mode: str, context: int,
            ignore_case: bool, multiline: bool, enough: int) -> tuple[list[str], str, bool]:
        import time

        flags = (re.I if ignore_case else 0) | ((re.S | re.M) if multiline else 0)
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}") from e
        out: list[str] = []
        note = ""
        truncated = False  # if the scan ended early, the match count is an "at least" number
        deadline = time.monotonic() + SEARCH_SECONDS
        files = [base] if base.is_file() else self._walk(base)
        for n, f in enumerate(files):
            if n % 200 == 0:
                if ctx.cancelled():
                    note = _limit_note("stopped by the user")
                    truncated = True
                    break
                if time.monotonic() > deadline:
                    note = _limit_note(f"{SEARCH_SECONDS:.0f} sn doldu")
                    truncated = True
                    break
            if n >= SEARCH_MAX_FILES:
                note = _limit_note(f"looked at {SEARCH_MAX_FILES} files")
                truncated = True
                break
            if glob and not _glob_match(f.name, glob, f.relative_to(base).as_posix() if base.is_dir() else f.name):
                continue
            try:
                if f.stat().st_size > SEARCH_MAX_BYTES or _is_binary(f):
                    continue
                text = _read_text(f).replace("\r\n", "\n")  # CRLF: multiline "\\n" patterns match Windows files too
            except OSError:
                continue
            try:
                shown = str(f.relative_to(ctx.cwd))
            except ValueError:
                shown = str(f.relative_to(base) if base.is_dir() else f)
            lines = text.splitlines()
            if multiline:
                starts = sorted({text.count("\n", 0, m.start()) for m in rx.finditer(text)})
            else:
                starts = [i for i, line in enumerate(lines) if rx.search(line)]
            if not starts:
                continue
            if mode == "files_with_matches":
                out.append(shown)
            elif mode == "count":
                out.append(f"{shown}:{len(starts)}")
            else:
                shown_lines: set[int] = set()
                prev = -2
                for i in starts:
                    for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                        if j in shown_lines:
                            continue
                        if context and prev >= 0 and j > prev + 1:
                            out.append("--")
                        sep = ":" if j == i or (multiline and j in starts) else "-"
                        out.append(f"{shown}{sep}{j + 1}{sep}{lines[j][:300]}")
                        shown_lines.add(j)
                        prev = j
            if len(out) >= enough:
                truncated = True  # more matches may exist: the exact count is unknown
                break
        return out, note, truncated

    @staticmethod
    def _walk(base: Path):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not _skip_dir(d)]
            for name in files:
                yield Path(root) / name


def _glob_match(name: str, pattern: str, rel: str | None = None) -> bool:
    """fnmatch, plus brace alternatives such as '*.{ts,tsx}' (same behavior as rg -g).
    Like rg: a '/' in the pattern (e.g. 'src/**/*.py') matches the path relative to the root (rel), else the file name."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if m:
        return any(_glob_match(name, pattern[:m.start()] + alt + pattern[m.end():], rel)
                   for alt in m.group(1).split(","))
    pat = pattern.replace("\\", "/")
    if "/" in pat.rstrip("/"):
        target = (rel if rel is not None else name).replace("\\", "/")
        return bool(glob_regex(pat.lstrip("/")).match(target))
    return fnmatch.fnmatch(name, pat.rstrip("/"))


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


class FileOps(Tool):
    name = "file_ops"
    description = ("File and folder management: move/rename (move), copy, delete and create a folder (mkdir). Use "
                   "this instead of move/copy/del/rm in run_command: it works the same on Windows and Linux, and "
                   "deleted items are not removed for good but moved to ALICE's trash folder (can be undone).\n"
                   "- For several files give a paths list; destination must then be a folder.\n"
                   "- Does not overwrite an existing target; pass overwrite=true.")
    parameters = obj({
        "action": {"type": "string", "enum": ["move", "copy", "delete", "mkdir"], "description": "The operation to "
                                                                                                 "perform."},
        "path": {"type": "string", "description": "Source file/folder (for mkdir, the folder to create)."},
        "paths": {"type": "array", "items": {"type": "string", "description": "Kaynak yol."},
                  "description": "Birden fazla kaynak (path yerine)."},
        "destination": {"type": "string", "description": "move/copy target: a new path, or a folder to put it in."},
        "overwrite": {"type": "boolean", "description": "Overwrite the target if it exists. Default false."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        from alice.permissions import is_system_path
        from alice.tools.base import DANGER

        n = len(args.get("paths") or []) + (1 if args.get("path") else 0)
        if args.get("action") == "delete" and n > 20:
            return DANGER
        for key in ("destination", *(["paths"] if args.get("paths") else [])):
            vals = args.get(key) or []
            for v in vals if isinstance(vals, list) else [vals]:
                try:
                    if is_system_path(Path(str(v)).expanduser().resolve()):
                        return DANGER
                except (OSError, ValueError):
                    return DANGER
        return WRITE

    def summary(self, args: dict[str, Any]) -> str:
        src = args.get("path") or ", ".join(map(str, args.get("paths") or []))
        dest = f" -> {args['destination']}" if args.get("destination") else ""
        return f"{args.get('action')} {src}{dest}"[:160]

    def run(self, ctx: ToolContext, action: str, path: str | None = None, paths: list[str] | None = None,
            destination: str | None = None, overwrite: bool = False) -> ToolResult:
        sources = [ctx.resolve(x) for x in ([path] if path else []) + list(paths or [])]
        if not sources:
            raise ToolError("path or paths is required")
        if action == "mkdir":
            for s in sources:
                s.mkdir(parents=True, exist_ok=True)
            return ToolResult("folder ready: " + ", ".join(map(str, sources)))
        missing = [str(s) for s in sources if not s.exists()]
        if missing:
            raise ToolError(f"not found: {', '.join(missing)}")
        if action == "delete":
            return ToolResult("moved to the recycle bin (move it back from there to undo):\n" +
                              "\n".join(f"{s} -> {self._trash(ctx, s)}" for s in sources))
        if not destination:
            raise ToolError(f"{action} needs a destination")
        dest = ctx.resolve(destination)
        given = Path(destination.rstrip("/\\")).name
        if given and given != dest.name and given.lower() == dest.name.lower():
            # On Windows, resolve() returns the on-disk letter case of an existing file: a "Rapor.txt" target became
            # "rapor.txt". The name the user typed is kept.
            dest = dest.with_name(given)
        into_dir = dest.is_dir() or len(sources) > 1 or destination.endswith(("/", "\\"))
        if into_dir and _same_file(sources[0], dest) and len(sources) == 1:
            into_dir = False  # renaming a folder to its own name (case only), not moving it inside itself
        if len(sources) > 1 and dest.exists() and not dest.is_dir():
            raise ToolError(f"with more than one source, the destination must be a folder: {dest}")
        plan = [(s, dest / s.name if into_dir else dest) for s in sources]
        for s, d in plan:
            case_rename = action == "move" and d.name != s.name and d.parent == s.parent and _same_file(s, d)
            if not case_rename and (d == s or s in d.parents):
                raise ToolError(f"{s} can't be {'moved' if action == 'move' else 'copied'} into itself")
            if d.exists() and not overwrite and not case_rename:
                raise ToolError(f"the destination already exists: {d} (overwrite=true to replace it)")
        done = []
        for s, d in plan:
            d.parent.mkdir(parents=True, exist_ok=True)
            if action == "move" and d.name != s.name and d.parent == s.parent and _same_file(s, d):
                # On Windows, a name that changes only in letter case ("rapor.txt" -> "Rapor.txt"): the target looks
                # "existing" but is the source itself. Before, overwrite=true trashed the source and the move failed.
                snapshot(ctx, s)
                os.rename(s, d)
                done.append(f"{s} -> {d}")
                continue
            if d.exists():
                self._trash(ctx, d)
            if action == "move":
                if s.is_file():
                    snapshot(ctx, s)
                shutil.move(str(s), str(d))
            elif action == "copy":
                snapshot(ctx, d)
                if s.is_dir():
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
            done.append(f"{s} -> {d}")
        return ToolResult(f"{'moved' if action == 'move' else 'copied'}:\n" + "\n".join(done))

    @staticmethod
    def _trash(ctx: ToolContext, p: Path) -> Path:
        import time

        from alice.config import alice_home

        if p.is_file():
            snapshot(ctx, p)
        box = alice_home() / "trash" / time.strftime("%Y%m%d-%H%M%S")
        box.mkdir(parents=True, exist_ok=True)
        target = box / p.name
        k = 1
        while target.exists():
            target = box / f"{p.stem}-{k}{p.suffix}"
            k += 1
        shutil.move(str(p), str(target))
        return target


class NotebookEdit(Tool):
    name = "notebook_edit"
    description = ("Changes one cell of a Jupyter notebook (.ipynb), inserts a new cell or deletes one; other cells "
                   "and their outputs are left alone.\n"
                   "- Read it with read_file first: the cell numbers (starting at 0) are there.\n"
                   "- A changed code cell loses its old output; the cell is not run.\n"
                   "- Don't edit an .ipynb with edit_file or write_file: that breaks the notebook's JSON structure.")
    parameters = obj({
        "path": {"type": "string", "description": "Path of the .ipynb file."},
        "cell": {"type": "integer", "description": "Cell number (starts at 0). For insert the new cell goes at this "
                                                   "number (cell count = append at the end)."},
        "mode": {"type": "string", "enum": ["replace", "insert", "delete"], "description": "replace: change the "
                                                                                           "cell's content | insert: "
                                                                                           "add a new cell | delete: "
                                                                                           "delete the cell. Default "
                                                                                           "replace."},
        "source": {"type": "string", "description": "The cell's new content (replace/insert)."},
        "cell_type": {"type": "string", "enum": ["code", "markdown"], "description": "Cell type for insert; if given "
                                                                                     "with replace, the type changes "
                                                                                     "too. Default code."},
    }, ["path", "cell"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} cell {args.get('cell')} ({args.get('mode') or 'replace'})"

    def run(self, ctx: ToolContext, path: str, cell: int, mode: str = "replace", source: str = "",
            cell_type: str = "") -> ToolResult:
        import json as _json

        p = ctx.resolve(path)
        if p.suffix.lower() != ".ipynb" or not p.is_file():
            raise ToolError(f"Jupyter notebook not found: {p}")
        require_read(ctx, p)
        before = _read_text(p)
        try:
            nb = _json.loads(before)
        except ValueError as e:
            raise ToolError(f"could not read the notebook (broken JSON): {e}") from e
        cells = nb.get("cells")
        if not isinstance(cells, list):
            raise ToolError("the notebook has no cell list")
        n = len(cells)
        lines = source.splitlines(keepends=True)
        if mode == "insert":
            if not 0 <= cell <= n:
                raise ToolError(f"for insert, the cell must be between 0..{n}")
            kind = cell_type or "code"
            new_cell: dict[str, Any] = {"cell_type": kind, "metadata": {}, "source": lines}
            if kind == "code":
                new_cell.update(execution_count=None, outputs=[])
            cells.insert(cell, new_cell)
            done = f"cell {cell} added ({kind})"
        elif not 0 <= cell < n:
            raise ToolError(f"there is no cell {cell} (the notebook has {n} cells, 0..{n - 1})")
        elif mode == "delete":
            cells.pop(cell)
            done = f"cell {cell} deleted"
        elif mode == "replace":
            c = cells[cell]
            c["source"] = lines
            if cell_type and cell_type != c.get("cell_type"):
                c["cell_type"] = cell_type
                if cell_type == "markdown":
                    c.pop("outputs", None)
                    c.pop("execution_count", None)
            if c.get("cell_type") == "code":
                c["outputs"], c["execution_count"] = [], None
            done = f"cell {cell} replaced"
        else:
            raise ToolError(f"bilinmeyen mode: {mode}")
        snapshot(ctx, p)
        after = _json.dumps(nb, ensure_ascii=False, indent=1) + "\n"
        p.write_text(after, encoding="utf-8", newline="")
        mark_read(ctx, p)
        return ToolResult(f"{done}: {p} ({len(cells)} cells in total)", diff=make_diff(path, before, after))


TOOLS = [ReadFile(), WriteFile(), EditFile(), MultiEdit(), ListDir(), Glob(), Grep(), FileOps(), NotebookEdit()]
