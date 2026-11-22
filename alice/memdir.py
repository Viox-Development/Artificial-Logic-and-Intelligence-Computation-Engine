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

TYPES = ("user", "feedback", "project", "reference")
TYPE_TR = {"user": "kullanıcı", "feedback": "geri bildirim", "project": "proje", "reference": "kaynak"}
INDEX = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
MAX_NOTES = 200
MAX_BODY = 6000
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
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
    return "bugün" if d == 0 else "dün" if d == 1 else f"{d} gün önce"


def freshness(ts: float, now: float | None = None) -> str:
    """Warning for a note older than a day. Models do not compute date differences well; "47 gün önce" ("47 days ago")
    draws more attention than a date stamp."""
    d = age_days(ts, now)
    if d <= 1:
        return ""
    return (f"Bu not {d} gün önce yazıldı: o anki durumu anlatır, şimdiki durum değişmiş olabilir. İçindeki dosya, "
            "işlev, ayar ya da durum iddialarını kullanmadan önce şimdiki hâlini kontrol et.")


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
            pass
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

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.name}"

    def manifest_line(self) -> str:
        tag = f"[{self.type}] " if self.type else ""
        return f"- {tag}{self.key} ({_iso(self.modified)[:10]}, {age_text(self.modified)}): {self.description}"

    def render(self, now: float | None = None, limit: int = 1800) -> str:
        """What goes into the message when recalled: title, freshness warning and body."""
        body = self.body if len(self.body) <= limit else self.body[:limit - 1] + "…"
        warn = freshness(self.modified, now)
        head = f"### {self.title or self.name} [{self.type or 'not'}, {self.scope}, {age_text(self.modified, now)}]"
        return "\n".join(x for x in (head, f"({warn})" if warn else "", sanitize(body)) if x)

    def dump(self) -> str:
        meta = {"name": self.name, "description": self.description, "type": self.type,
                "created": _iso(self.created or self.modified), "modified": _iso(self.modified)}
        if self.title and self.title != self.name:
            meta["title"] = self.title
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

    def path_for(self, name: str) -> Path:
        # Check subdirectories if not found in root directly
        root_path = self.root / f"{name}.md"
        if root_path.exists():
            return root_path
        for sub in ("people", "areas", "topics"):
            sub_p = self.root / sub / f"{name}.md"
            if sub_p.exists():
                return sub_p
        return root_path

    def stamp(self) -> float:
        """Latest modification time in the folder (a cheap check to know when to refresh the system prompt)."""
        try:
            times = [self.root.stat().st_mtime] + [p.stat().st_mtime for p in self.root.rglob("*.md")]
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
        return Note(name=name, description=desc,
                    type=typ if typ in TYPES else "", body=body, path=path, modified=modified,
                    created=_parse_iso(meta.get("created")) or modified, source=meta.get("source", ""),
                    scope=self.scope, title=meta.get("title", "") or meta.get("name", "") or name)

    def scan(self) -> list[Note]:
        """All notes, newest first (at most MAX_NOTES). Supports subdirectories (people/, areas/, topics/)."""
        if not self.root.is_dir():
            return []
        out = []
        for p in self.root.rglob("*.md"):
            if p.name == INDEX or p.name.startswith(".") or any(part.startswith(".") or part in ("logs", "trash") for part in p.parts):
                continue
            n = self._load(p)
            if n is not None:
                # Category from parent directory if in a subdirectory
                try:
                    rel_parts = p.relative_to(self.root).parts
                    if len(rel_parts) > 1:
                        n.category = rel_parts[0]
                    else:
                        n.category = "you" if n.name in ("preferences", "profile") else "general"
                except ValueError:
                    n.category = "general"
                out.append(n)
        out.sort(key=lambda n: n.modified, reverse=True)
        return out[:MAX_NOTES]

    def get(self, name: str) -> Note | None:
        name = self._resolve(name)
        p = self.path_for(name)
        return self._load(p) if p.is_file() else None

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
             source: str = "", when: float | None = None) -> tuple[Note, bool]:
        """Write the note (overwriting an existing one) and update its line in the index. Returns (note, is_new).
        If the index limit would be exceeded, MemoryFull is raised and nothing is written."""
        name = self._resolve(name or title or description)
        typ = type if type in TYPES else "user"
        description = " ".join(sanitize(description).split())[:300]
        body = sanitize(body).strip()
        if not body and not description:
            raise ValueError("boş not")
        body = body or description
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY - 1] + "…"
        description = description or " ".join(body.split())[:150]
        title = " ".join(sanitize(title).split())[:80] or name
        with self._lock:
            self.ensure()
            old = self.get(name)
            now = when or time.time()
            note = Note(name=name, description=description, type=typ, body=body, path=self.path_for(name),
                        modified=now, created=old.created if old else now,
                        source=source or (old.source if old else ""), scope=self.scope,
                        title=title if title != name or not old else old.title)
            index = self._index_with(note)
            self._check_index(index)
            _atomic_write(note.path, note.dump())
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
        return f"]({name}.md)" in line

    def _index_line(self, note: Note) -> str:
        hook = " ".join(note.description.split())
        if len(hook) > 140:
            hook = hook[:139] + "…"
        title = (note.title or note.name).replace("[", "(").replace("]", ")")
        return f"- [{title}]({note.name}.md) — {hook}"

    def _index_with(self, note: Note) -> str:
        lines = self.index_raw().splitlines()
        line = self._index_line(note)
        for i, old in enumerate(lines):
            if self._points_to(old, note.name):
                lines[i] = line
                break
        else:
            while lines and not lines[-1].strip():
                lines.pop()
            lines.append(line)
        return "\n".join(lines).strip() + "\n"

    def _index_without(self, name: str) -> str:
        return "\n".join(x for x in self.index_raw().splitlines() if not self._points_to(x, name)).strip() + "\n"

    @staticmethod
    def _check_index(text: str) -> None:
        lines = text.strip().splitlines()
        size = len(text.encode("utf-8"))
        if len(lines) > MAX_INDEX_LINES or size > MAX_INDEX_BYTES:
            raise MemoryFull(f"{INDEX} sınırı aşılacaktı ({len(lines)} satır, {size // 1024} KB; sınır "
                             f"{MAX_INDEX_LINES} satır / {MAX_INDEX_BYTES // 1000} KB). Benzer notları tek notta "
                             "birleştir, eskiyenleri sil ya da memory action=dream ile toparla; sonra tekrar kaydet.")

    def write_index(self, text: str) -> None:
        """Rewrite the index from scratch (Dream): keep only lines for existing notes; append missing ones."""
        notes = {n.name: n for n in self.scan()}
        keep, seen = [], set()
        for line in sanitize(text).splitlines():
            m = re.search(r"\]\(([^)]+)\.md\)", line)
            if m is None:
                if line.strip().startswith("#") or not line.strip():
                    keep.append(line)  # section headings and blank lines are allowed
                continue
            if m.group(1) in notes and m.group(1) not in seen:
                seen.add(m.group(1))
                keep.append(line.rstrip())
        keep += [self._index_line(n) for name, n in notes.items() if name not in seen]
        out = "\n".join(keep).strip() + "\n"
        self._check_index(out)
        with self._lock:
            self.ensure()
            _atomic_write(self.index_path, out)

    def repair_index(self) -> bool:
        """Add notes missing from the index and delete lines pointing to missing files. Did it change?"""
        before = self.index_raw()
        if not before.strip() and not self.scan():
            return False
        self.write_index(before)
        return self.index_raw() != before

    def index_for_prompt(self) -> str:
        """The index as it goes into the prompt: cut at the limit with the reason written out; notes that have a file
        but no index line (maybe added by hand) are shown as short lines too."""
        raw = sanitize(self.index_raw()).strip()
        names = {n.name: n for n in self.scan()}
        missing = [n for name, n in names.items() if f"]({name}.md)" not in raw]
        if missing:
            raw = (raw + "\n" if raw else "") + "\n".join(self._index_line(n) for n in missing[:30])
        lines = raw.splitlines()
        size = len(raw.encode("utf-8"))
        if len(lines) <= MAX_INDEX_LINES and size <= MAX_INDEX_BYTES:
            return raw
        cut = "\n".join(lines[:MAX_INDEX_LINES])
        if len(cut.encode("utf-8")) > MAX_INDEX_BYTES:
            cut = cut.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", "ignore")
            cut = cut[:cut.rfind("\n")] if "\n" in cut else cut
        shown = len(cut.splitlines())
        return (cut + f"\n\n> UYARI: {INDEX} {len(lines)} satır / {size // 1024} KB; yalnız ilk {shown} satırı "
                "yüklendi. Dizin satırlarını kısa tut, ayrıntıyı nota taşı; memory action=dream ile toparla.")

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
            raise ValueError("boş kayıt")
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
            pass

    def backup(self) -> Path | None:
        notes = [p for p in self.root.glob("*.md")] if self.root.is_dir() else []
        if not notes:
            return None
        base = self.root / ".backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        dest, i = base, 2
        while dest.exists():  # so a second backup in the same second does not overwrite the previous one
            dest, i = base.with_name(f"{base.name}-{i}"), i + 1
        dest.mkdir(parents=True)
        for p in notes:
            shutil.copy2(p, dest / p.name)
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
        files = {p.name: p.read_bytes() for p in src.glob("*.md")}
        self.backup()  # so the restore itself can be undone too
        for p in self.root.glob("*.md"):
            if p.name not in files and p.name != INDEX:
                self.delete(p.stem)
        for name, data in files.items():
            (self.root / name).write_bytes(data)
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


# ---------------------------------------------------------------------------------------------------- recall
def select_relevant(query: str, notes: list[Note], chooser: Callable[[str], str] | None = None,
                    limit: int = 5, recent_tools: Iterable[str] = (), timeout: float = 6.0) -> list[Note]:
    """Notes that will really help with the query. First the descriptions are shown to the fast model, which picks
    (at most limit); if there is no model, it errors, or it times out, it picks by keyword matching."""
    if not notes or not query.strip():
        return []
    by_key = {n.key: n for n in notes}
    if chooser is not None:
        tools = list(recent_tools)
        prompt = SELECT_PROMPT.format(limit=limit, query=query[:2000],
                                      manifest="\n".join(n.manifest_line() for n in notes),
                                      tools=("\nŞu an kullanılan araçlar: " + ", ".join(tools)) if tools else "")
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
    return [n for _, n in scored[:limit]]


SELECT_PROMPT = """You pick which of ALICE's saved memory notes should be shown to her while she answers the \
user's message below. ALICE is a personal assistant running on the user's computer.
Return at most {limit} note keys (the text before the first parenthesis, e.g. "genel:feedback-testler") that will \
CLEARLY help with this message. Be strict: if you are not sure a note helps, leave it out; an empty list is fine and \
common. Notes are judged by their key and description.
If tools that are in use right now are listed, skip notes that are only usage docs for those tools, but keep notes \
that warn about pitfalls or known problems with them.
Answer ONLY with JSON: {{"selected": ["genel:..."]}}

User message:
{query}

Saved notes:
{manifest}{tools}"""


# ---------------------------------------------------------------------------------------------------- prompt text
MEMORY_RULES = """You have a persistent, file-based memory (your notes on the user and their work). Use the \
`memory` tool to save, update, read and delete notes; it keeps the MEMORY.md index below in sync for you.

Note types:
- user: who the user is (role, school/job, city, people around them, skills, tastes). Helps you tailor how you work \
with them. Nothing judgmental, nothing irrelevant.
- feedback: how the user wants you to work. Save when they correct you ("no, not like that", "stop doing X") AND when \
they confirm a non-obvious choice worked ("yes, exactly", "perfect, keep doing that", accepting an unusual approach \
without pushback); saving only corrections makes you drift into over-caution. Body: the rule, then a "**Neden:**" \
line (the reason they gave or the incident behind it) and a "**Nasıl uygulanır:**" line (when it applies), so you \
can judge edge cases later.
- project: ongoing work, goals, decisions, deadlines, incidents that are NOT visible in the code or git history. \
Same body shape (fact/decision, **Neden:**, **Nasıl uygulanır:**). Always turn relative dates into absolute ones \
("perşembe" -> 2026-03-05). Use scope=project for things that only matter in the current repository.
- reference: where to find things outside (a dashboard, a channel, a site, an account) and what each is for.

Do NOT save (even when asked; then ask what was surprising or non-obvious and save that): things readable from the \
current files, code structure, file paths or git history; fix recipes (the fix is in the code); one-off task state \
or what happened in this conversation (the conversation log is kept separately); anything already in ALICE.md / \
CLAUDE.md; secrets (passwords, keys, tokens).
How: one topic per note, a short kebab-case name ("feedback-testler"), a specific one-line description (it decides \
whether the note gets recalled later). Before saving, check the index: update an existing note instead of writing a \
near-duplicate; delete notes that turned out wrong. If the user says "remember this", save it right away; "forget \
it", delete it.
Using memory: notes relevant to the current message are attached to it automatically ([hafızadan ...]); read others \
with memory action=read when the index suggests they matter, and always when the user asks you to check or recall. \
If the user says to ignore memory, act as if it were empty and do not mention it. A note is what was true when it \
was written: before acting on a file, function, flag or state a note mentions, check that it still holds; if the \
present contradicts a note, trust the present and fix or delete the note. For questions about recent or current \
state prefer reading the files/git over an old note.
Memory is for future conversations: for the current task use todo_write / plans, not memory."""


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
{{"save": [{{"name": "kebab-case-name", "type": "user|feedback|project|reference", "scope": "genel|proje", \
"title": "short title", "description": "one specific line", "content": "markdown body"}}], "delete": ["genel:name"]}}
- To change an existing note, save it under the SAME name with the full new content (merge, don't append blindly).
- scope "proje" only for things that matter solely inside the current repository{repo}.
- Write notes in Turkish (keep technical terms in English). At most 3 saves. Usually nothing qualifies: then \
{{"save": [], "delete": []}}.
- An ALICE reply starting with [doğrulanmadı] is a claim no tool confirmed: never record it as done.

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
4. Prune: delete notes that break the rules (one-off task results, file paths, test runs, passing moods, plans \
whose date has passed and no longer matter). Notes marked (kullanıcı istedi) were explicitly requested by the user: \
you may rewrite or merge them but never drop their meaning. When unsure, keep.
5. Index: if the index needs reordering or shorter lines, return a new one: one line per note, \
"- [Title](name.md) — hook" under ~150 characters, optional "## Section" headings, no note content in it.

Answer ONLY with JSON:
{{"save": [{{"name": "...", "type": "user|feedback|project|reference", "title": "...", "description": "...", \
"content": "..."}}], "delete": ["name"], "index": "full new MEMORY.md or empty string to keep it", \
"summary": "1-3 Turkish sentences: what you merged, fixed, added or pruned (or that nothing needed changing)"}}
Write notes in Turkish (technical terms in English). If the memory is already tight, return empty lists.

Current MEMORY.md:
{index}

Notes:
{notes}

Since the last pass ({since}):
{signal}"""
