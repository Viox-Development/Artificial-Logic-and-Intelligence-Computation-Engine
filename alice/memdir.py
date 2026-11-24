"""File-based persistent memory: each note is a separate markdown file, and MEMORY.md is the index of them.

An adaptation of Claude Code's current "auto memory" layout for ALICE:

- One note = one file (`<name>.md`): a short header section at the top (name, description, type, created, modified,
  source), with the note itself below. There are four types: user (who the user is, what they know, what they like),
  feedback (how ALICE is asked to work; rule + **Neden:** + **Nasıl uygulanır:**), project (ongoing work, decisions,
  dates; things that cannot be derived from the code or git), reference (where external sources are: dashboard,
  channel, site).
- MEMORY.md is the INDEX that goes into the system prompt of every conversation, not a note: one line per note,
  `- [Title](name.md) — short hook`. There is a 200 line / 25 KB limit; if it is exceeded, new records are rejected
  (never silently cut).
- Two scopes: general (`~/.alice/memory`, everything about the user) and project (`~/.alice/projects/<repo>/memory`,
  only the notes valid in that git repo; worktrees of the same repo share the same folder).
- Recall: the index is always in the prompt; for each message, the most relevant notes (at most 5), picked by looking
  at their descriptions, are added in full to the message. Notes older than a day get a warning: "this note was
  written N days ago, check its current state."
- Daily log (`logs/YYYY/MM/YYYY-MM-DD.md`): things worth noting in long sessions are only appended (append-only);
  Dream later distills them into permanent notes.
- Dream (alice/memory.py dream()): runs in the background over the memory; merges duplicates, fixes outdated notes,
  makes relative dates absolute, tidies the index. The modification time of the lock file = last Dream time. A backup
  of the notes is taken before every Dream (.backups), and deleted notes go to the trash (.trash): nothing is lost
  irreversibly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from alice.config import alice_home
from alice.i18n import _

TYPES = ("user", "feedback", "project", "reference")
TYPE_TR = {"user": "kullanıcı", "feedback": "geri bildirim", "project": "proje", "reference": "kaynak"}
INDEX = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
MAX_NOTES = 200
MAX_BODY = 6000
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Note folders, in the order the index lists them. A note's folder is its category; notes in the root are "you"
# (profile, preferences) or uncategorized. Any other folder the user makes is a category too.
CATEGORIES = ("you", "people", "areas", "topics")
CATEGORY_TITLES = {"you": "You", "people": "People", "areas": "Areas", "topics": "Topics", "general": "Other"}
SKIP_DIRS = {"logs", "trash"}
YOU_NOTES = {"profile", "preferences"}


# Heading of the generated MEMORY.md (older builds wrote "# Hafıza Dizini").
INDEX_HEAD = "# Memory Index"
OLD_INDEX_HEADS = ("# Hafıza Dizini",)

def category_of(rel_parts: tuple[str, ...], name: str) -> str:
    if len(rel_parts) > 1:
        return rel_parts[0].lower()
    return "you" if name in YOU_NOTES else "general"


LINK_RX = re.compile(r"\[\[([^\[\]|#]{1,64})(?:[|#][^\[\]]*)?\]\]")
_QUOTED_RX = re.compile(r"[\"“”«»]([^\"“”«»]{2,40})[\"“”«»]")
_TRIGGER_RX = re.compile(r"read (?:when|for)\s+(.+?)(?:\s+(?:comes?|come) up|$|;)", re.I)


def aliases_of(note: Any) -> list[str]:
    """Every name a note can be referred to by, lower-case: its name and title, the `aliases` frontmatter, quoted
    nicknames in the description and the subjects of its "read when X or Y comes up" trigger."""
    out: list[str] = []

    def add(s: str) -> None:
        s = " ".join(str(s).replace("[[", "").replace("]]", "").split()).strip(" .,;:'\"").lower()
        if 2 <= len(s) <= 40 and s not in out:
            out.append(s)

    add(note.name.replace("-", " "))
    add(note.name)
    if note.title:
        add(note.title)
    for a in getattr(note, "alias_list", []) or []:
        add(a)
    desc = note.description or ""
    head = re.split(r"\s[—-]\s", desc, maxsplit=1)[0]  # "Efe ("gravity") — friend ..." -> "Efe ("gravity")"
    for q in _QUOTED_RX.findall(head):
        add(q)
    base = re.sub(r"\(.*?\)", "", head).strip()
    if base and len(base.split()) <= 3:
        add(base)
    m = _TRIGGER_RX.search(desc)
    if m:
        for part in re.split(r",|\bor\b|\bveya\b|/", m.group(1)):
            add(part)
    return out
LOCK = ".dream-lock"
LOCK_STALE = 3600.0
KEEP_BACKUPS = 5
KEEP_TRASH = 50

_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})
# Tags that imitate ALICE's own prompt markers: if a note contains "</memory>" or "<system-reminder>", it must not
# break the prompt's structure (text from outside may have been written into a note).
_TAG_RX = re.compile(r"<(/?)(system[\w-]*|memory|hafıza|hafiza|tool[\w-]*|function[\w-]*|assistant|user|human|"
                     r"instructions?|important)\b", re.I)


class MemoryFull(Exception):
    """The index limit would be exceeded: nothing was recorded."""


# ---------------------------------------------------------------------------------------------------- helpers
def sanitize(text: str) -> str:
    """Remove invisible characters (zero-width, bidi overrides) and neutralize prompt tags."""
    out = "".join(ch for ch in str(text or "") if ch in "\n\t" or unicodedata.category(ch) not in ("Cf", "Cc", "Co"))
    return _TAG_RX.sub(lambda m: "‹" + m.group(1) + m.group(2), out)


def slugify(text: str, limit: int = 48) -> str:
    s = str(text or "").replace("İ", "i").replace("I", "ı").lower().translate(_FOLD)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:limit].rstrip("-")
    return s or "not"


def age_days(ts: float, now: float | None = None) -> int:
    return max(0, int(((now or time.time()) - ts) // 86400))


def age_text(ts: float, now: float | None = None) -> str:
    d = age_days(ts, now)
    return "today" if d == 0 else "yesterday" if d == 1 else f"{d} days ago"


def freshness(ts: float, now: float | None = None) -> str:
    """Warning for a note older than a day. Models do not compute date differences well; "47 gün önce" ("47 days ago")
    draws more attention than a date stamp."""
    d = age_days(ts, now)
    if d <= 1:
        return ""
    return (f"This note was written {d} days ago: it describes things as they were then, and they may have changed. Check the "
            "current state before using any claims in it about files, functions, settings or status.")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).timestamp()
    except ValueError:
        return None


def _fm_value(v: str) -> str:
    v = " ".join(str(v).split())
    # If the value contains ':' or '#', or starts with a quote, write it as a JSON string
    # so nothing after "#" is cut off.
    return json.dumps(v, ensure_ascii=False) if (not v or re.search(r"[:#\"'\[\]{}]", v) or v != v.strip()) else v


def parse_note_text(text: str) -> tuple[dict[str, str], str]:
    """The header section between the '---' lines (key: value) and the body."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text.strip()
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text.strip()
    head, body = text[3:end], text[end + 4:]
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith('"'):
            try:
                v = str(json.loads(v))
            except json.JSONDecodeError:
                v = v.strip('"')
        meta[k.strip().lower()] = v
    return meta, body.strip("\n").strip()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # a leftover temp file is harmless; the error is raised below
        raise


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except ImportError:
        if os.name == "nt":
            return True  # We can't be sure: the lock goes stale after an hour anyway
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------------------------------- note
@dataclass
class Note:
    name: str
    description: str
    type: str
    body: str
    path: Path
    modified: float
    created: float = 0.0
    source: str = ""
    scope: str = "genel"
    title: str = ""
    category: str = "general"
    alias_list: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.name}"

    @property
    def links(self) -> list[str]:
        """Names this note points to with [[name]] (in the description or the body), in order, without duplicates."""
        out: list[str] = []
        for m in LINK_RX.finditer(f"{self.description}\n{self.body}"):
            n = m.group(1).strip().lower()
            if n and n != self.name and n not in out:
                out.append(n)
        return out

    @property
    def aliases(self) -> list[str]:
        """Other names the note is known by: the frontmatter `aliases` list, quoted nicknames in the description
        (Efe ("gravity")) and the words of a "read when X or Y comes up" trigger."""
        return aliases_of(self)

    @property
    def rel(self) -> str:
        """Path inside the memory folder, as the index links it (people/efe.md)."""
        return f"{self.category}/{self.name}.md" if self.category not in ("you", "general") else f"{self.name}.md"

    def manifest_line(self) -> str:
        tag = f"[{self.category if self.category != 'general' else self.type or 'not'}] "
        return f"- {tag}{self.key} ({_iso(self.modified)[:10]}, {age_text(self.modified)}): {self.description}"

    def render(self, now: float | None = None, limit: int = 1800) -> str:
        """What goes into the message when recalled: title, freshness warning and body."""
        body = self.body if len(self.body) <= limit else self.body[:limit - 1] + "…"
        warn = freshness(self.modified, now)
        kind = self.category if self.category != "general" else (self.type or "not")
        head = f"### {self.title or self.name} [{kind}, {self.scope}, {age_text(self.modified, now)}]"
        return "\n".join(x for x in (head, f"({warn})" if warn else "", sanitize(body)) if x)

    def dump(self) -> str:
        meta = {"name": self.name, "description": self.description}
        if self.type:
            meta["type"] = self.type
        meta.update({"created": _iso(self.created or self.modified), "modified": _iso(self.modified)})
        if self.title and self.title != self.name:
            meta["title"] = self.title
        if self.alias_list:
            meta["aliases"] = ", ".join(self.alias_list)
        if self.source:
            meta["source"] = self.source
        head = "\n".join(f"{k}: {_fm_value(v)}" for k, v in meta.items())
        return f"---\n{head}\n---\n\n{self.body.strip()}\n"


# ---------------------------------------------------------------------------------------------------- notebook
_root_locks: dict[str, threading.RLock] = {}
_root_locks_guard = threading.Lock()


def _root_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(root)))
    with _root_locks_guard:
        lock = _root_locks.get(key)
        if lock is None:
            lock = _root_locks[key] = threading.RLock()
        return lock


class NoteBook:
    """A memory folder (general, or of one project)."""

    def __init__(self, root: Path | str, scope: str = "genel"):
        self.root = Path(root)
        self.scope = scope
        # The notebook for the same folder is rebuilt on every call (project notebook, all_books): the lock must be tied
        # to the folder, otherwise MEMORY.md lines were lost when background extraction and the tool wrote at once.
        self._lock = _root_lock(self.root)

    # -------------------------------------------------------------- files
    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    @property
    def index_path(self) -> Path:
        return self.root / INDEX

    def _note_files(self):
        """Note files inside the folder, with their path relative to it. Hidden folders (.trash, .backups), the
        daily logs and the index itself are not notes."""
        if not self.root.is_dir():
            return
        for p in self.root.rglob("*.md"):
            rel = p.relative_to(self.root).parts
            if p.name == INDEX or any(part.startswith(".") for part in rel) or rel[0].lower() in SKIP_DIRS:
                continue
            yield p, rel

    def path_for(self, name: str, category: str = "") -> Path:
        """Where note `name` lives: its existing file in any category folder, else a new file in `category`'s folder
        (the root for "you"/"general"). Names are unique across folders."""
        for p, _rel in self._note_files():
            if p.stem == name:
                return p
        cat = (category or "").strip().lower()
        if cat and cat not in ("you", "general") and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", cat):
            return self.root / cat / f"{name}.md"
        return self.root / f"{name}.md"

    def stamp(self) -> float:
        """Latest modification time in the folder (a cheap check to know when to refresh the system prompt)."""
        try:
            times = [self.root.stat().st_mtime] + [p.stat().st_mtime for p, _cat in self._note_files()]
            if self.index_path.is_file():
                times.append(self.index_path.stat().st_mtime)
        except OSError:
            return 0.0
        return max(times) if times else 0.0

    def _load(self, path: Path) -> Note | None:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            st = path.stat()
        except OSError:
            return None
        meta, body = parse_note_text(raw)
        name = path.stem
        modified = _parse_iso(meta.get("modified")) or st.st_mtime
        # A file edited by hand (or with write_file) may keep an old modified value:
        # if the file's own time is newer, use that.
        modified = max(modified, st.st_mtime) if abs(st.st_mtime - modified) > 120 else modified
        typ = meta.get("type", "")
        desc = meta.get("description", "") or (body.splitlines()[0][:150] if body else "")
        try:
            rel = path.relative_to(self.root).parts
        except ValueError:
            rel = (path.name,)
        aliases = tuple(a.strip() for a in re.split(r"[,;]", meta.get("aliases", "")) if a.strip())
        return Note(name=name, description=desc,
                    type=typ if typ in TYPES else "", body=body, path=path, modified=modified,
                    created=_parse_iso(meta.get("created")) or modified, source=meta.get("source", ""),
                    scope=self.scope, title=meta.get("title", "") or meta.get("name", "") or name,
                    category=category_of(rel, name), alias_list=aliases)

    def scan(self) -> list[Note]:
        """All notes, newest first (at most MAX_NOTES), from the root and the category folders (people/, areas/...).
        Only the path inside the memory folder is checked for hidden parts: the folder itself sits under ~/.alice,
        and judging the absolute path skipped every note."""
        out = [n for p, _rel in self._note_files() if (n := self._load(p)) is not None]
        out.sort(key=lambda n: n.modified, reverse=True)
        return out[:MAX_NOTES]

    def get(self, name: str) -> Note | None:
        name = self._resolve(name)
        p = self.path_for(name)
        return self._load(p) if p.is_file() else None

    def resolve_alias(self, text: str) -> Note | None:
        """The note a name or nickname points to ("gravity" -> people/efe), exact match only."""
        want = " ".join(str(text or "").split()).strip().lower()
        if not want:
            return None
        n = self.get(want)
        if n is not None:
            return n
        for note in self.scan():
            if want in note.aliases:
                return note
        return None

    def _resolve(self, name: str) -> str:
        name = str(name or "").strip()
        if ":" in name and name.split(":", 1)[0] in ("genel", "proje", self.scope):
            name = name.split(":", 1)[1]
        if name.endswith(".md"):
            name = name[:-3]
        if NAME_RX.match(name):
            out = name
        elif name not in ("", ".", "..") and not re.search(r"[\\/:*?\"<>|]", name) and self.path_for(name).is_file():
            # A hand-added file such as "Sunucu Notları.md": it shows up in the list under this name, but once
            # the name is simplified ("sunucu-notlari") it could not be found, read or deleted.
            # A file that sits in the folder under exactly this name is valid.
            out = name
        else:
            out = slugify(name)
        # "memory.md" is the very same file as MEMORY.md on Windows (case-insensitive): a note with
        # that name was written over the index, and then the index write deleted the note.
        return "memory-not" if out.lower() == INDEX[:-3].lower() else out

    # -------------------------------------------------------------- writing
    def save(self, name: str, description: str, type: str, body: str, title: str = "",
             source: str = "", when: float | None = None, category: str = "",
             aliases: Iterable[str] | None = None) -> tuple[Note, bool]:
        """Write the note (overwriting an existing one) and update its line in the index. Returns (note, is_new).

        category puts a NEW note in that folder (people, areas, topics, or any folder name; empty/"you"/"general" =
        the root). An existing note stays where it is unless a different category is given, then it moves.
        If the index limit would be exceeded, MemoryFull is raised and nothing is written."""
        name = self._resolve(name or title or description)
        typ = type if type in TYPES else ""
        description = " ".join(sanitize(description).split())[:300]
        body = sanitize(body).strip()
        if not body and not description:
            raise ValueError(_("empty note"))
        body = body or description
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY - 1] + "…"
        description = description or " ".join(body.split())[:150]
        title = " ".join(sanitize(title).split())[:80] or name
        cat = (category or "").strip().lower()
        with self._lock:
            self.ensure()
            old = self.get(name)
            now = when or time.time()
            path = self.path_for(name, cat)
            if old is not None and cat and cat != old.category:
                # Moving between categories (a person note that was saved in the root): new folder, old file goes.
                path = (self.root / cat / f"{name}.md") if cat not in ("you", "general") else self.root / f"{name}.md"
            alias_list = tuple(dict.fromkeys(a.strip() for a in (aliases or ()) if str(a).strip())) or \
                (old.alias_list if old else ())
            note = Note(name=name, description=description, type=typ or (old.type if old else ""), body=body,
                        path=path, modified=now, created=old.created if old else now,
                        source=source or (old.source if old else ""), scope=self.scope,
                        title=title if title != name or not old else old.title,
                        category=category_of(path.relative_to(self.root).parts, name), alias_list=alias_list)
            index = self._index_with(note)
            self._check_index(index)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(note.path, note.dump())
            if old is not None and old.path != note.path:
                old.path.unlink(missing_ok=True)
            if when:
                os.utime(note.path, (when, when))  # an old note being moved: keep its age looking right
            _atomic_write(self.index_path, index)
        return note, old is None

    def delete(self, name: str) -> Note | None:
        """Move the note to the trash (.trash, the last 50 are kept) and remove it from the index."""
        with self._lock:
            note = self.get(name)
            if note is None:
                return None
            trash = self.root / ".trash"
            trash.mkdir(parents=True, exist_ok=True)
            # Microseconds + a counter if needed: when the same note was deleted twice in the same second
            # (write-delete-write-delete), the second-resolution name overwrote the earlier trash copy.
            # The name stays sorted by time.
            stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"
            dest, i = trash / f"{stamp}-{note.name}.md", 1
            while dest.exists():
                dest, i = trash / f"{stamp}.{i}-{note.name}.md", i + 1
            shutil.move(str(note.path), str(dest))
            for old in sorted(trash.glob("*.md"))[:-KEEP_TRASH]:
                old.unlink(missing_ok=True)
            if self.index_path.is_file():
                _atomic_write(self.index_path, self._index_without(note.name))
        return note

    # -------------------------------------------------------------- index
    def index_raw(self) -> str:
        try:
            return self.index_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _points_to(line: str, name: str) -> bool:
        return f"]({name}.md)" in line or f"/{name}.md)" in line

    def _index_line(self, note: Note) -> str:
        hook = " ".join(note.description.split())
        if len(hook) > 200:
            hook = hook[:199] + "…"
        title = (note.title or note.name).replace("[", "(").replace("]", ")")
        return f"- [{title}]({note.rel}) — {hook}"

    def build_index(self, notes: Iterable[Note] | None = None) -> str:
        """MEMORY.md generated from the notes, grouped like Claude's memory screen: You, Topics, Areas, People, then
        any other folder, then notes without a category. Alphabetical inside a group, so the index (the start of
        every prompt) only changes when a note is added, removed or its description changes."""
        groups: dict[str, list[Note]] = {}
        for n in notes if notes is not None else self.scan():
            groups.setdefault(n.category, []).append(n)
        order = ["you", "topics", "areas", "people"] + sorted(c for c in groups if c not in (*CATEGORIES, "general"))
        order += ["general"]
        out = [INDEX_HEAD]
        for cat in order:
            items = sorted(groups.get(cat, []), key=lambda n: n.name)
            if not items:
                continue
            out += ["", f"## {CATEGORY_TITLES.get(cat, cat.replace('-', ' ').title())}"]
            out += [self._index_line(n) for n in items]
        return "\n".join(out).strip() + "\n"

    def _index_with(self, note: Note) -> str:
        notes = {n.name: n for n in self.scan()}
        notes[note.name] = note
        return self.build_index(notes.values())

    def _index_without(self, name: str) -> str:
        return self.build_index(n for n in self.scan() if n.name != name)

    @staticmethod
    def _check_index(text: str) -> None:
        lines = text.strip().splitlines()
        size = len(text.encode("utf-8"))
        if len(lines) > MAX_INDEX_LINES or size > MAX_INDEX_BYTES:
            raise MemoryFull(_("{index} would go over its limit ({lines} lines, {kb} KB; the limit is {max_lines} lines / {max_kb} KB). "
                               "Merge similar notes into one, delete outdated ones or tidy up with memory action=dream; then save again.",
                               index=INDEX, lines=len(lines), kb=size // 1024, max_lines=MAX_INDEX_LINES,
                               max_kb=MAX_INDEX_BYTES // 1000))

    def write_index(self, text: str = "") -> None:
        """Rewrite the index from the notes. The text argument (Dream's proposed index) is ignored on purpose: the
        index is derived from the files, so it can never point to a missing note or miss one."""
        out = self.build_index()
        self._check_index(out)
        with self._lock:
            self.ensure()
            _atomic_write(self.index_path, out)

    def repair_index(self) -> bool:
        """Regenerate the index if it does not match the notes (hand edits, moved files). Did it change?"""
        before = self.index_raw()
        if not before.strip() and not self.scan():
            return False
        self.write_index()
        return self.index_raw() != before

    def index_for_prompt(self) -> str:
        """The index as it goes into the prompt, generated from the notes (a stale MEMORY.md written by an older
        version or by hand can't hide or misplace a note), cut at the limit with the reason written out."""
        raw = sanitize(self.build_index()).strip() if self.root.is_dir() else ""
        if raw == INDEX_HEAD or raw in OLD_INDEX_HEADS:
            return ""
        lines = raw.splitlines()
        size = len(raw.encode("utf-8"))
        if len(lines) <= MAX_INDEX_LINES and size <= MAX_INDEX_BYTES:
            return raw
        cut = "\n".join(lines[:MAX_INDEX_LINES])
        if len(cut.encode("utf-8")) > MAX_INDEX_BYTES:
            cut = cut.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", "ignore")
            cut = cut[:cut.rfind("\n")] if "\n" in cut else cut
        shown = len(cut.splitlines())
        return (cut + f"\n\n> WARNING: {INDEX} is {len(lines)} lines / {size // 1024} KB; only its first {shown} lines were "
                "loaded. Keep index lines short and move the details into the note; tidy up with memory action=dream.")

    def near_limit(self) -> bool:
        raw = self.index_raw()
        return len(raw.splitlines()) > MAX_INDEX_LINES * 0.85 or len(raw.encode("utf-8")) > MAX_INDEX_BYTES * 0.85

    # -------------------------------------------------------------- daily log
    def log_path(self, when: float | None = None) -> Path:
        d = datetime.fromtimestamp(when or time.time())
        return self.root / "logs" / f"{d:%Y}" / f"{d:%m}" / f"{d:%Y-%m-%d}.md"

    def append_log(self, text: str, when: float | None = None) -> Path:
        text = " ".join(sanitize(text).split())
        if not text:
            raise ValueError(_("empty record"))
        p = self.log_path(when)
        with self._lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"- {datetime.fromtimestamp(when or time.time()):%H:%M} {text[:1000]}\n")
        return p

    def logs_since(self, since: float, limit_chars: int = 8000) -> str:
        root = self.root / "logs"
        if not root.is_dir():
            return ""
        day0 = datetime.fromtimestamp(since).strftime("%Y-%m-%d") if since else ""
        files = sorted(p for p in root.rglob("*.md") if p.stem >= day0)
        out, used = [], 0
        for p in reversed(files):  # newest to oldest: if the limit fills up, the oldest are dropped
            body = p.read_text(encoding="utf-8", errors="replace").strip()
            block = f"## {p.stem}\n{body}"
            if used + len(block) > limit_chars:
                break
            out.append(block)
            used += len(block)
        return "\n\n".join(reversed(out))

    # -------------------------------------------------------------- Dream lock and backups
    @property
    def lock_path(self) -> Path:
        return self.root / LOCK

    def last_dream(self) -> float:
        try:
            return self.lock_path.stat().st_mtime
        except OSError:
            return 0.0

    def try_lock(self) -> float | None:
        """Take the Dream lock: returns the previous time (to restore it), or None if someone else holds it."""
        p = self.lock_path
        prior = self.last_dream()
        if p.is_file() and time.time() - prior < LOCK_STALE:
            try:
                pid = int((p.read_text(encoding="utf-8").strip() or "0"))
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                return None
            if pid == os.getpid() and getattr(self, "_dreaming", False):
                return None
        self.ensure()
        p.write_text(str(os.getpid()), encoding="utf-8")
        try:
            if int(p.read_text(encoding="utf-8").strip()) != os.getpid():
                return None
        except (OSError, ValueError):
            return None
        self._dreaming = True
        return prior

    def release(self, prior: float | None = None, ok: bool = True) -> None:
        """Release the lock. ok=False: move the time back to the old value (so the time gate opens again)."""
        self._dreaming = False
        p = self.lock_path
        try:
            p.write_text("", encoding="utf-8")
            if not ok:
                if not prior:
                    p.unlink(missing_ok=True)
                else:
                    os.utime(p, (prior, prior))
        except OSError:
            pass  # the note's time is only a hint for sorting

    def backup(self) -> Path | None:
        # Category folders too (people/, areas/, ...): a root-only glob left every categorized note out of the
        # backup, so "Yedeğe dön" could not bring a deleted person or project note back.
        notes = [p for p in self.root.glob("*.md")] + [p for p, _cat in self._note_files() if p.parent != self.root]             if self.root.is_dir() else []
        if not notes:
            return None
        base = self.root / ".backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        dest, i = base, 2
        while dest.exists():  # so a second backup in the same second does not overwrite the previous one
            dest, i = base.with_name(f"{base.name}-{i}"), i + 1
        dest.mkdir(parents=True)
        for p in notes:
            out = dest / p.relative_to(self.root)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
        for old in sorted((self.root / ".backups").iterdir())[:-KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
        return dest

    def backups(self) -> list[str]:
        d = self.root / ".backups"
        return sorted((p.name for p in d.iterdir() if p.is_dir()), reverse=True) if d.is_dir() else []

    def restore(self, stamp: str) -> int:
        """Go back to a Dream backup: the notes from that moment come back; notes added later go to the trash."""
        src = self.root / ".backups" / stamp
        if not re.fullmatch(r"\d{8}-\d{6}(-\d+)?", stamp or "") or not src.is_dir():
            raise ValueError(f"yedek yok: {stamp}")
        # Read into memory first: pruning the oldest backups below could also delete the one we are restoring.
        files = {p.relative_to(src).as_posix(): p.read_bytes() for p in src.rglob("*.md")}
        self.backup()  # so the restore itself can be undone too
        current = [(p, p.relative_to(self.root).as_posix()) for p, _cat in self._note_files()]
        for p, rel in current:
            if rel not in files:
                self.delete(p.stem)
        for rel, data in files.items():
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return len([n for n in files if n != INDEX])


# ---------------------------------------------------------------------------------------------------- scopes
_roots: dict[str, Path | None] = {}


def project_root(cwd: Path | str | None) -> Path | None:
    """The main folder of the git repo cwd belongs to (in a worktree too, the main repo, so memory is shared)."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _roots:
        return _roots[key]
    root: Path | None = None
    try:
        from alice.gitsafe import harden

        r = subprocess.run(["git", *harden(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)], cwd=str(cwd),
                           capture_output=True, text=True, timeout=5)
        out = r.stdout.strip()
        if r.returncode == 0 and out:
            p = Path(out)
            root = p.parent if p.name == ".git" else p
    except (OSError, subprocess.SubprocessError):
        root = None
    if root is not None and root.resolve() == Path.home().resolve():
        root = None  # even if the home folder is a repo, it does not count as a "project"
    _roots[key] = root
    return root


def project_slug(root: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(root.resolve())).strip("-")[:120] or "proje"


def project_book(cwd: Path | str | None, home: Path | None = None) -> NoteBook | None:
    root = project_root(cwd)
    if root is None:
        return None
    return NoteBook((home or alice_home()) / "projects" / project_slug(root) / "memory", scope="proje")


def books_for(general: NoteBook, cwd: Path | str | None) -> list[NoteBook]:
    proj = project_book(cwd, general.root.parent) if cwd else None
    return [general] + ([proj] if proj is not None else [])


# ---------------------------------------------------------------------------------------------------- entities
_WORD_RX = re.compile(r"[0-9a-zçğıöşüâîû]+", re.I)
_TR_SUFFIX_RX = re.compile(r"(?:'?(?:nın|nin|nun|nün|ın|in|un|ün|ya|ye|a|e|yı|yi|yu|yü|ı|i|u|ü|da|de|ta|te|dan|den|"
                           r"tan|ten|la|le|yla|yle|cığım|ciğim|cım|cim|lar|ler|ları|leri))$")


def _fold(text: str) -> str:
    return str(text or "").replace("İ", "i").replace("I", "ı").lower().translate(_FOLD)


def _mentions(text_folded: str, words: set[str], alias: str) -> bool:
    """Does the (folded) message mention the alias? Multi-word aliases as a phrase, single words as a word or the
    word with a Turkish suffix ("Efe'ye", "Nehirle", "kemalin"), so names are found the way people write them."""
    a = _fold(alias)
    if not a:
        return False
    if " " in a:
        return a in text_folded
    if len(a) < 3:
        return a in words
    if a in words:
        return True
    return any(w.startswith(a) and _TR_SUFFIX_RX.fullmatch(w[len(a):]) for w in words if len(w) > len(a))


def linked_notes(text: str, notes: list[Note], context: str = "", limit: int = 6) -> list[Note]:
    """Entity linking: the notes this message is about, found WITHOUT a model call, in priority order.

    1. Direct mention: a note's name, title, nickname or trigger word appears in the message ("gravity" -> efe,
       "işletme" -> staj), with Turkish suffixes ("Nehir'le").
    2. Context: the same, in the recent turns of this chat (`context`): a person named two messages ago is still the
       subject when the user now says "o" or "bizimki".
    3. Graph: notes linked with [[name]] from a directly mentioned note (talking about self-improvement brings
       [[nehir]], the reason behind it).
    The model-based selector still runs after this for indirect references ("Discord'daki kız"); linking just makes
    sure the obvious ones are never missed or left to chance."""
    if not notes:
        return []
    by_name = {n.name: n for n in notes}
    msg = _fold(text)
    msg_words = set(_WORD_RX.findall(msg))
    ctx = _fold(context)
    ctx_words = set(_WORD_RX.findall(ctx))
    direct, contextual = [], []
    for n in notes:
        al = n.aliases
        if any(_mentions(msg, msg_words, a) for a in al):
            direct.append(n)
        elif ctx and any(_mentions(ctx, ctx_words, a) for a in al):
            contextual.append(n)
    linked = []
    for n in direct:
        for name in n.links:
            t = by_name.get(name)
            if t is not None and t not in direct and t not in linked:
                linked.append(t)
    out: list[Note] = []
    for n in direct + contextual + linked:
        if n not in out:
            out.append(n)
    return out[:limit]


# ---------------------------------------------------------------------------------------------------- recall
def select_relevant(query: str, notes: list[Note], chooser: Callable[[str], str] | None = None,
                    limit: int = 5, recent_tools: Iterable[str] = (), timeout: float = 6.0,
                    context: str = "") -> list[Note]:
    """Notes that will really help with the query. Entity linking first (names, nicknames, triggers, recent
    context, [[links]]: no model needed); then the fast model sees the descriptions plus the recent turns and adds
    indirect references; without a model, keyword matching fills in."""
    if not notes or not query.strip():
        return []
    sure = linked_notes(query, notes, context=context, limit=limit)
    rest = [n for n in notes if n not in sure]
    room = max(0, limit - len(sure))
    if room == 0 or not rest:
        return sure[:limit]
    return (sure + _select_more(query, rest, chooser, room, recent_tools, timeout, context))[:limit]


def _select_more(query: str, notes: list[Note], chooser: Callable[[str], str] | None, limit: int,
                 recent_tools: Iterable[str], timeout: float, context: str) -> list[Note]:
    by_key = {n.key: n for n in notes}
    if chooser is not None:
        tools = list(recent_tools)
        q = query[:2000] if not context else f"{query[:1500]}\n\n(Recent turns of this chat, for context:\n" \
            f"{context[-1500:]})"
        prompt = SELECT_PROMPT.format(limit=limit, query=q,
                                      manifest="\n".join(n.manifest_line() for n in notes),
                                      tools=("\nTools in use right now: " + ", ".join(tools)) if tools else "")
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["raw"] = chooser(prompt)
            except Exception as e:  # noqa: BLE001 - no model: fall back to keyword matching
                box["err"] = e

        t = threading.Thread(target=run, daemon=True, name="alice-memory-select")
        t.start()
        t.join(timeout)
        if "raw" in box:
            from alice.memory import parse_json

            data = parse_json(box["raw"])
            picked = data.get("selected") if isinstance(data.get("selected"), list) else None
            if picked is not None:
                out = []
                for k in picked:
                    k = str(k).strip()
                    n = by_key.get(k) or next((x for x in notes if x.name == k.split(":")[-1].removesuffix(".md")),
                                              None)
                    if n is not None and n not in out:
                        out.append(n)
                return out[:limit]
    return keyword_relevant(query, notes, limit=min(limit, 3))


def keyword_relevant(query: str, notes: list[Note], limit: int = 3, min_score: float = 0.34) -> list[Note]:
    from alice.memory import terms

    q = set(terms(query))
    if not q:
        return []
    scored = []
    for n in notes:
        head = set(terms(f"{n.title} {n.name.replace('-', ' ')} {n.description}"))
        body = set(terms(n.body[:600]))
        hit_head = len(q & head) / len(q)
        hit_body = len(q & body) / len(q)
        score = max(hit_head, 0.6 * hit_body) + 0.1 * min(hit_head, hit_body)
        if score >= min_score and (len(q & (head | body)) >= min(2, len(q))):
            scored.append((score, n))
    scored.sort(key=lambda x: -x[0])
    return [n for _score, n in scored[:limit]]


SELECT_PROMPT = """You pick which of ALICE's saved memory notes should be shown to her while she answers the \
user's message below. ALICE is a personal assistant running on the user's computer.
Return at most {limit} note keys (the text before the first parenthesis, e.g. "genel:efe") that will CLEARLY help \
with this message. Be strict: if you are not sure a note helps, leave it out; an empty list is fine and common. \
Notes are judged by their key and description.
People are often referred to WITHOUT their name: by role ("iş yerindeki abi", "mentorum"), by relation ("Nehir'in \
arkadaşı", "bizim gruptaki çocuk"), by place ("Discord'daki kız"), by a pronoun after being named in the recent turns, \
or by a nickname. Match those against the people notes' descriptions too.
If tools that are in use right now are listed, skip notes that are only usage docs for those tools, but keep notes \
that warn about pitfalls or known problems with them.
Answer ONLY with JSON: {{"selected": ["genel:..."]}}

User message:
{query}

Saved notes:
{manifest}{tools}"""


# ---------------------------------------------------------------------------------------------------- prompt text
MEMORY_RULES = """You have a persistent memory of the user: one markdown note per subject, grouped like a personal \
wiki. The index below lists every note with a one-line description; notes relevant to the current message are \
attached to it automatically ([hafızadan ...]). Use the `memory` tool to save, update, read and delete notes; it \
keeps the index in sync.

Where a note goes (category):
- you: `profile` (who the user is: age, school/job, city, skills, goals) and `preferences` (how they want you to \
answer and work). Update these two instead of making new notes about the user in general.
- people: one note per real person in the user's life, named after them (`efe`, `nehir`). Description format: \
`Name ("nickname") — who they are to the user, where they know each other, linked people; read when Name or nickname \
comes up`. Put every nickname, username and spelling in `aliases`. Body: short facts (relation, shared groups, what \
they know, notable events with dates).
- areas: ongoing parts of the user's life that keep coming back (a long project, internship, school, a community, \
a self-improvement plan): goals, people involved, decisions, dates.
- topics: stable interests and things they own or use (hardware, games, tools, subscriptions).
- any other folder name is fine when none of these fit (e.g. `places`, `health`), but prefer the four above.

Linking: write other notes' names as [[name]] wherever they relate (`efe` body: "same friend group as [[nehir]]"). \
Links are followed when a note is recalled, so a person mentioned without their name still comes with context.
Descriptions decide recall: name the subject, its nicknames and the words that should bring it up \
("read when staj or işletme comes up"). Keep them specific and one line.

Save: lasting facts about the user and their people, projects and interests; corrections to how you work and \
approaches they confirmed (into `preferences`); dates of future events (exam, trip, deadline) as absolute dates.
Do NOT save: what happened in this conversation, task progress, errors or failures you ran into, bugs and fixes \
(the fix lives in the code), quota or model problems, file paths or code structure, one-off questions (one question \
about a topic is not an interest), anything already in ALICE.md or the user instructions, secrets.
How: update the existing note of a subject (same name, merged full content) instead of making a near-duplicate; one \
subject per note; kebab-case names; text in the user's language, technical terms in English. "Remember this" -> save now; \
"forget it" -> delete. A note is what was true when it was written: if the present contradicts it, trust the present \
and fix the note. If the user says to ignore memory, act as if it were empty and don't mention it."""


# ---------------------------------------------------------------------------------------------------- extraction
EXTRACT_PROMPT = """You maintain the long-term memory notes of ALICE, a personal assistant on the user's computer. \
Below are the newest turns of a conversation. Decide whether any of it deserves to be kept as a note for FUTURE \
conversations, following these rules:
{rules}

Existing notes (key [type] (date): description):
{manifest}

Full text of the existing notes that look related:
{related}
{explicit}
Answer ONLY with JSON:
{{"save": [{{"name": "kebab-case-name", "category": "you|people|areas|topics", "scope": "genel|proje", \
"title": "short title", "description": "one specific line", "aliases": ["nickname"], "content": "markdown body"}}], \
"delete": ["genel:name"]}}
- To change an existing note, save it under the SAME name with the full new content (merge, don't append blindly).
- A new person goes to people/ under their name; link related notes with [[name]].
- scope "proje" only for things that matter solely inside the current repository{repo}.
- Write notes in the user's language (keep technical terms in English). At most 3 saves. Usually nothing qualifies: then \
{{"save": [], "delete": []}}.
- An ALICE reply starting with [unverified] (older: [doğrulanmadı]) is a claim no tool confirmed: never record it as done.

Conversation (oldest first):
{transcript}"""


DREAM_PROMPT = """You are ALICE "dreaming": a quiet, reflective pass over her memory notes so future conversations \
start well-oriented. Today is {today}. Memory scope: {scope}.
Rules for what a note may hold:
{rules}

Do this, in order:
1. Orient: read the index and every note below; you will improve them, not create near-duplicates.
2. Gather: look at what happened since the last pass (conversation summaries, daily logs, open follow-ups). Pick out \
only what is durable and matches the rules: new facts about the user, corrections or confirmed approaches, \
project decisions and dates, pointers to external places. Something seen in two or more conversations (games they \
play, ongoing projects, recurring interests) is durable even if never stated as "remember this".
3. Consolidate: merge overlapping notes into one (save the merged note under the best name, delete the others); \
fix notes the newer information contradicts; turn relative dates into absolute ones; give feedback/project notes the \
rule + **Neden:** + **Nasıl uygulanır:** shape; tighten vague descriptions (they decide recall).
4. Prune: delete notes that break the rules (one-off task results, errors, bugs and fixes, file paths, test runs, \
passing moods, plans whose date has passed and no longer matter). Notes marked (the user asked for it) were explicitly \
requested by the user: you may rewrite or merge them but never drop their meaning. When unsure, keep.
5. Link and file: put each note in the right category (a person -> people), add missing [[name]] links between \
related notes, and make sure every people note lists its nicknames in aliases. The index is rebuilt from the notes.

Answer ONLY with JSON:
{{"save": [{{"name": "...", "category": "you|people|areas|topics", "title": "...", "description": "...", \
"aliases": ["..."], "content": "..."}}], "delete": ["name"], \
"summary": "1-3 sentences in the user's language: what you merged, fixed, added or pruned (or that nothing needed changing)"}}
Write notes in the user's language (technical terms in English). If the memory is already tight, return empty lists.

Current MEMORY.md:
{index}

Notes:
{notes}

Since the last pass ({since}):
{signal}"""
