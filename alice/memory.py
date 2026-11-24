"""Long-term memory: persistent notes, past conversations, summaries and follow-ups.

There are two layers:
- Notes (alice/memdir.py): lasting information about the user and their work, each in its own markdown file
  (~/.alice/memory/*.md, project-specific ones in ~/.alice/projects/<repo>/memory/). Types: user / feedback /
  project / reference. The MEMORY.md index goes into the system prompt, and all relevant notes are added to every
  message. In the background a model extracts notes from the conversation; Dream (once a day, once enough
  conversation has built up) consolidates the notes.
- Conversation log (~/.alice/memory.db, SQLite): every turn (episodes), summaries of finished conversations
  (summaries), follow-ups to ask about or remind of later (followups). The old one-sentence "facts" table is only
  read to move its contents into notes on first startup.

- episodes: every conversation turn (the user's words + ALICE's reply, channel, time).
- Recall: the facts and past conversations most similar to the user's words are added to the message on every turn.
  Similarity for Turkish is measured with a simple method: letters are folded (ç->c, ı->i...), words are cut to
  their first 4 letters (suffixes drop off: "kahveyi", "kahvemi" -> "kahv"), and rare words weigh more (IDF).
  If a free embedding key is available (Gemini by default), semantic search is added too: "Unity projem" (my
  Unity project) and "oyun motorunda yaptığım şey" (the thing I made in a game engine) find the same memory
  (see alice/memvec.py). Otherwise, keyword search only.
- Learning: explicit requests like "bunu hatırla: X" (remember this: X) are saved directly. When the user talks
  about themselves (seviyorum = I love, benim = mine, her zaman = always...), the conversation is handed to the
  fast model in the background; it extracts lasting facts and updates or deletes outdated ones.
- Summarizing (digest): when a conversation ends (20 min of silence by default), the whole conversation is handed
  to the fast model at once. It extracts a short summary, lasting facts stated without a signal word ("yarın
  sınavım var" = "I have an exam tomorrow", "Unity'de FPS yapıyorum" = "I'm making an FPS in Unity"), and
  follow-ups (e.g. asking how the exam went, unfinished work). Summaries and follow-ups that are due today go into
  the system prompt: ALICE knows what was discussed yesterday and asks about the exam itself.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

# Trace for swallowed errors: silent by default, visible with `logging.basicConfig(level=logging.DEBUG)`.
log = logging.getLogger(__name__)

KINDS = {"preference": "tercih", "fact": "bilgi", "person": "kişi", "routine": "alışkanlık", "note": "not"}
_KIND_ORDER = ["preference", "person", "fact", "routine", "note"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, updated REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact', text TEXT NOT NULL, terms TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', hits INTEGER NOT NULL DEFAULT 0, last_used REAL);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, channel TEXT NOT NULL DEFAULT '',
    user TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', terms TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_episodes_ts ON episodes(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL NOT NULL, end REAL NOT NULL, channel TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL, terms TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_summaries_end ON summaries(end);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, due REAL, kind TEXT NOT NULL DEFAULT 'ask',
    text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS vectors (
    kind TEXT NOT NULL, ref INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (kind, ref));
CREATE TABLE IF NOT EXISTS forgotten (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, text TEXT NOT NULL, terms TEXT NOT NULL DEFAULT '');
"""
KEEP_FORGOTTEN = 200  # trace of deleted notes (so auto-extraction and Dream don't write them back)
FOLLOWUP_KINDS = {"ask": "sor", "remind": "hatırlat", "open": "yarım kaldı"}
# Traces left from the old "sevgili/mommy" persona. While they stayed in memory, the new persona kept coming
# back with "Buradayım bebeğim" (voice.log); Settings > Memory shows them and clears them in one click.
OLD_ROLE_RX = re.compile(
    r"\b(bebeğim|bebegim|aşkım|askim|tatlım|tatlim|sevgilin|sevgilim|sevgili(si)?|kız ?arkadaş\w*|mommy|mami|"
    r"canım benim|iyi çocu\w*|choker|kıskan\w*|cilve\w*|öpücük|sarıl\w*)\b", re.I)

_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})
_STOP = set("""
ve veya ile ama fakat ki de da mi mı mu mü bu şu o bir iki çok daha en gibi için icin kadar sonra önce once ne neden
nasil nasıl nerede hangi her hiç hic var yok evet hayır hayir tamam şey sey ben sen biz siz onlar bana sana beni seni
benim senin bunu şunu onu ise ya hem diye olan olarak oldu olur olsun misin musun mısın müsün lütfen lutfen alice
hey merhaba selam acar acabilir yapar yapabilir eder edebilir şimdi simdi biraz artık neydi nedir hangisi hangisiydi
kadardı miydi mıydı ediyor ettim dedim demiştim kullanıcı kullanıcının alıce the a an to of and or is are i you it in
on for
""".split())
_STOP = {w.translate(_FOLD) for w in _STOP}
_CLOCK = re.compile(r"\s*\[şu an:[^\]]*\]\s*$")
_RECALL = re.compile(r"\s*\[hafızadan.*\]\s*$", re.S)  # note bodies may contain ']': match to the end

# Explicit remember request: saved immediately, without a model.
_EXPLICIT = [
    re.compile(r"^(?:alice[,\s]+)?(?:(?:bunu|şunu|sunu)\s+)?(?:hatırla|hatirla|aklında tut|aklinda tut|unutma)"
               r"\s*[:,-]?\s+(?:ki\s+)?(.{6,})$", re.I),
    re.compile(r"^(?:alice[,\s]+)?(.{6,}?)[,.;\s]+(?:bunu\s+)?(?:hatırla|hatirla|unutma|aklında tut|aklinda tut)"
               r"(?:\s+(?:lütfen|lutfen|tamam mı|tamam mi))?[.!]?$", re.I),
]
# Word stems (folded) showing the user is talking about themselves. The model is asked to extract only when
# one of these is present, so questions like "saat kaç" (what time is it) don't use up free quota.
_SIGNALS = ("benim", "bana ", "beni ", "seviyor", "sevmiyor", "sevmem", "severim", "hoslan", "nefret", "tercih",
            "istemiyorum", "her zaman", "hep ", "asla", "genelde", "genellikle", "adim ", "ismim", "yasim",
            "yasiyorum", "oturuyorum", "calisiyorum", "okuyorum", "dogum", "kardes", "annem", "babam", "arkadasim",
            "sevgilim", "esim", "kizim", "oglum", "favori", "en sevdig", "aliskanl", "hatirla", "unutma",
            "bundan sonra", "artik ", "diye hitap", "diye cagir", "olarak cagir", "aklinda tut",
            "i like", "i love", "i hate", "i prefer", "my ", "remember", "call me")


# Moments when the user corrects or approves how ALICE works (feedback notes come from here). If only
# corrections were saved, ALICE would drift away from approaches that worked; approvals are signals too.
_FEEDBACK = re.compile(
    r"\b(hayır|hayir|öyle değil|oyle degil|yanlış|yanlis|yapma|yapmasana|bırak|birak|olmamış|olmamis|olmadı|olmadi|"
    r"bir daha|asla|her zaman|hep böyle|bundan sonra|aynen|tam olarak|mükemmel|mukemmel|harika|süper|super|"
    r"güzel olmuş|guzel olmus|eline sağlık|eline saglik|böyle devam|boyle devam|doğru yaptın|dogru yaptin|"
    r"beğendim|begendim|istemiyorum|isterim|tercih|don'?t|stop|exactly|perfect|keep doing)\b", re.I)

DIGEST_BAD_TRIES = 3  # skip a conversation after this many unusable summary answers from the model


class _BadDigest(ValueError):
    """The digest model replied, but the reply is not JSON or is empty (not a network or quota error)."""


RECALL_ITEM_CHARS, RECALL_TOTAL_CHARS = 1800, 6000
RECENT_TURNS_HOURS = 3  # "recent turns" in the system prompt are left out if older than this (a new chat isn't taken for a continuation)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def fold(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower().translate(_FOLD)


def terms(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", fold(text)):
        if (len(w) < 3 and not w.isdigit()) or w in _STOP:
            continue
        out.append(w[:4])  # let Turkish suffixes drop: kahveyi/kahvemi -> kahv, filmini/film -> film
    return out


def clean_user_text(text: str) -> str:
    """Strip the time and memory note the voice assistant adds: only the user's own words go into the log and search."""
    return _RECALL.sub("", _CLOCK.sub("", text or "")).strip()


def explicit_note(text: str) -> str | None:
    t = clean_user_text(text).strip().rstrip(".!")
    for rx in _EXPLICIT:
        m = rx.match(t)
        if m:
            note = m.group(1).strip(" ,.:;-")
            if len(note.split()) >= 2:
                return note[0].upper() + note[1:]
    return None


# If the model writes a turn's result as a "bilgi" (fact) despite the rule (most of the old memory was like
# that): file paths, failed or attempted work, approved values, testing ALICE. Applied only to automatically
# extracted items; "bunu hatırla" is always saved.
_EPHEMERAL = re.compile(
    r"[a-z]:\\|(?:^|\s)/[\w.-]+/|\b(?:başarısız|basarisiz|onayladı|onayladi|denedi|deniyor|test (?:et|ed)\w*|"
    r"deneme klasör\w*|indirilenler|kurulum dosyası|şu anda|su anda|şu an\b|bu konuşmada|endişe duy\w*)", re.I)


def ephemeral(text: str) -> bool:
    return bool(_EPHEMERAL.search(text))


# Foreign script that speech recognition invents from noise ("हे या इनिशियलाइज़ेशन"): it got into the chat
# log and polluted search. Greek, Cyrillic, Hebrew/Arabic, Indic, Thai/Tibetan, Japanese, Chinese, Korean.
_FOREIGN_RANGES = ((0x0370, 0x04FF), (0x0590, 0x06FF), (0x0900, 0x0FFF), (0x3040, 0x30FF), (0x3400, 0x9FFF),
                   (0xAC00, 0xD7AF))
_FOREIGN = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _FOREIGN_RANGES) + "]")


# A lasting request from the user to ALICE ("... sormasını ister" = wants it to ask ..., "asla ... yapmasın" = never do ...).
_INSTRUCTION = re.compile(r"\b(ister|istemez|istiyor|istemiyor|asla|her zaman|hitap|çağır|cagir|sorulmasını)\w*",
                          re.I)


def noise_turn(user: str) -> bool:
    """A turn not worth saving: most of its letters are non-Latin script (speech recognition wrote noise in another language)."""
    letters = [c for c in user if c.isalpha()]
    return bool(letters) and len(_FOREIGN.findall(user)) > len(letters) * 0.3


# A sentence ending in the past tense ("şarkısını açtı", "iletişime geçti", "dinlemeye çalıştı") is an event,
# not a habit ("dinler", "oynar"). This distinction could not be left to the model for facts taken from summaries.
_PAST = re.compile(r"\w+(?:dı|di|du|dü|tı|ti|tu|tü)(?:lar|ler)?[.!]?$", re.I)


def past_event(text: str) -> bool:
    return bool(_PAST.search(text.strip()))


def has_signal(text: str) -> bool:
    t = " " + fold(clean_user_text(text)) + " "
    return any(s in t for s in _SIGNALS)


def has_feedback(text: str) -> bool:
    return bool(_FEEDBACK.search(clean_user_text(text)))


_KIND_TYPE = {"preference": "user", "fact": "user", "person": "user", "routine": "user", "note": "user"}


def kind_to_type(kind: str, text: str) -> str:
    """Convert old kind names (tercih, bilgi, kişi...) to note types; a lasting instruction given to ALICE is feedback."""
    if kind in ("user", "feedback", "project", "reference"):
        return kind
    return "feedback" if _INSTRUCTION.search(text) else _KIND_TYPE.get(kind, "user")


def _ago(ts: float, now: float | None = None) -> str:
    d = ((now or time.time()) - ts) / 86400
    if d < 1:
        return "bugün"
    if d < 2:
        return "dün"
    if d < 30:
        return f"{int(d)} gün önce"
    if d < 365:
        return f"{int(d // 30)} ay önce"
    return f"{int(d // 365)} yıl önce"


@dataclass
class Fact:
    id: int
    kind: str
    text: str
    created: float
    updated: float
    hits: int = 0

    def line(self) -> str:
        return f"#{self.id} ({KINDS.get(self.kind, self.kind)}) {self.text}"


@dataclass
class Episode:
    id: int
    ts: float
    channel: str
    user: str
    reply: str

    def line(self, now: float | None = None, width: int = 220) -> str:
        reply = f" / ALICE: {self.reply}" if self.reply else ""
        body = f"Kullanıcı: {self.user}{reply}"
        if len(body) > width:
            body = body[:width - 1] + "…"
        return f"{datetime.fromtimestamp(self.ts):%d.%m.%Y %H:%M} ({_ago(self.ts, now)}) {body}"


@dataclass
class Summary:
    id: int
    start: float
    end: float
    channel: str
    text: str

    def line(self, now: float | None = None) -> str:
        return f"{datetime.fromtimestamp(self.start):%d.%m.%Y %H:%M} ({_ago(self.start, now)}) özet: {self.text}"


@dataclass
class Followup:
    id: int
    created: float
    due: float | None
    kind: str
    text: str
    done: int = 0

    def line(self, now: float | None = None) -> str:
        when = ""
        if self.due is not None:
            when = f", {datetime.fromtimestamp(self.due):%d.%m}"
        return f"#{self.id} ({FOLLOWUP_KINDS.get(self.kind, self.kind)}{when}) {self.text}"


@dataclass
class Hit:
    score: float
    item: Fact | Episode | Summary

    def line(self) -> str:
        return self.item.line()


HISTORY_OFF = ("past chat search is off (the user turned it off in Settings > Memory); you don't know earlier "
               "conversations, don't make them up: ask the user if needed")


class LongMemory:
    def __init__(self, path: Path | str, options: dict[str, Any] | None = None,
                 extractor: Callable[[str], str] | None = None, embedder: Any = None,
                 notes_root: Path | str | None = None):
        o = options or {}
        from alice.memdir import NoteBook

        if notes_root is None:
            # ":memory:" (tests, temporary use): notes also go in a temporary folder, not mixed with anyone else's.
            notes_root = (Path(path).parent / "memory" if str(path) != ":memory:"
                          else Path(tempfile.mkdtemp(prefix="notes-", dir=_scratch_dir())))
        self.notes = NoteBook(notes_root)
        self.recall_model = bool(o.get("recall_model", True))  # pick the relevant notes with the fast model
        self.extract_every = max(1, int(o.get("extract_every", 4)))  # even without a signal, check every N turns
        self.dream_enabled = bool(o.get("dream", o.get("consolidate", True)))
        self.dream_hours = float(o.get("dream_hours", 24))
        self.dream_sessions = int(o.get("dream_sessions", 5))
        # Settings > Memory > "Geçmiş sohbetlerde ara" (like Claude's "Search and reference chats"). Off: turns are not
        # kept at all, search_chats / recent_chats answer that it is off, and no past-chat summary is added to messages.
        self.chat_history = bool(o.get("chat_history", True))
        self._turns_since_extract = 0
        self._turns_lock = threading.Lock()  # the counter is incremented from several channels at once (voice, chat, Discord)
        self._last_dream_scan: float | None = None  # monotonic; None = never scanned (right after startup, monotonic may be < 600)
        self._dream_lock = threading.Lock()
        self._in_profile: dict[str, set[str]] = {}  # cwd -> notes that stay in full in the system prompt (key@time)
        self.embedder = embedder  # alice.memvec.Embedder or None (keyword search only)
        self.embed_min = float(o.get("embed_min", 0.68))  # below this cosine similarity, results count as "unrelated"
        self.digest_enabled = bool(o.get("digest", True))
        self.consolidate_enabled = bool(o.get("consolidate", True))
        self.max_facts = int(o.get("max_facts", 60))  # consolidation target: keep the profile short and to the point
        self.session_gap = float(o.get("session_gap_minutes", 20)) * 60
        self._digest_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._last_digest_check = 0.0
        self.path = Path(path) if str(path) != ":memory:" else path
        self.auto_extract = bool(o.get("auto_extract", True))
        self.recall_limit = int(o.get("recall_limit", 4))
        self.profile_chars = int(o.get("profile_chars", 2500))
        self.keep_days = float(o.get("keep_days", 365))
        self.extractor = extractor  # prompt -> JSON text; if None, only explicit "hatırla" requests are saved
        # A stronger model for the daily consolidation and introduction (two calls a day). The fast model did not
        # follow the rules: it turned "WhatsApp'ta X'e yazdı" (wrote to X on WhatsApp) into a lasting fact and
        # deleted all the summaries. None: the extractor is used.
        self.synthesizer: Callable[[str], str] | None = None
        self._lock = threading.RLock()
        self._learn_lock = threading.Lock()
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        if isinstance(self.path, Path):
            try:
                self.db.execute("PRAGMA journal_mode=WAL")  # the core, voice and the app may use the same file
            except sqlite3.DatabaseError:
                pass  # WAL is an optimization; the default journal still works
        self.db.executescript(_SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(episodes)")}
        if "sid" not in cols:  # which chat a turn belongs to: deleting a chat can also delete its turns from memory
            self.db.execute("ALTER TABLE episodes ADD COLUMN sid TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_episodes_sid ON episodes(sid)")
        self.db.commit()
        self._prune()

    # ------------------------------------------------------------------ setup
    @classmethod
    def open(cls, cfg=None, router=None) -> "LongMemory | None":
        o = dict((cfg.data.get("memory") if cfg is not None else None) or {})
        if not o.get("enabled", True):
            return None
        from alice.memvec import Embedder

        try:
            embedder = Embedder.from_config(cfg, o)
        except Exception:  # noqa: BLE001 - embedding is optional: without it, keyword search is used
            embedder = None
        mem = cls(alice_home() / "memory.db", o, make_extractor(o, router), embedder,
                  notes_root=o.get("dir") or alice_home() / "memory")
        mem.synthesizer = make_extractor(o, router, str(o.get("synthesis_role", "brain")))
        mem.import_markdown(alice_home() / "memory.md")
        try:
            mem.migrate_facts()
        except Exception:  # noqa: BLE001 - the migration is retried on the next start
            log.debug("eski bilgiler notlara taşınamadı", exc_info=True)
        return mem

    # ------------------------------------------------------------------ notes
    def books(self, cwd: Path | str | None = None) -> list[Any]:
        """General notebook + the project's notebook if cwd is in a git repo."""
        from alice.memdir import books_for

        return books_for(self.notes, cwd)

    def all_books(self) -> list[Any]:
        """General notebook + all project notebooks that have notes or a log (for the Dream and Memory tabs)."""
        from alice.memdir import NoteBook

        out = [self.notes]
        base = self.notes.root.parent / "projects"
        if base.is_dir():
            for d in sorted(base.iterdir()):
                m = d / "memory"
                if m.is_dir() and (any(m.glob("*.md")) or (m / "logs").is_dir()):
                    out.append(NoteBook(m, scope="proje"))
        return out

    def book(self, scope: str = "genel", cwd: Path | str | None = None) -> Any:
        if scope in ("proje", "project"):
            books = self.books(cwd)
            if len(books) < 2:
                raise ValueError("proje kapsamı için çalışma klasörü bir git deposu olmalı; genel kapsamı kullan")
            return books[1]
        return self.notes

    def all_notes(self, cwd: Path | str | None = None) -> list[Any]:
        return [n for b in self.books(cwd) for n in b.scan()]

    def notes_stamp(self, cwd: Path | str | None = None) -> tuple[float, ...]:
        return tuple(b.stamp() for b in self.books(cwd))

    def find_note(self, key: str, cwd: Path | str | None = None) -> tuple[Any, Any] | tuple[None, None]:
        """'genel:ad', 'proje:ad' or just 'ad' -> (notebook, note)."""
        key = str(key or "").strip()
        scope = key.split(":", 1)[0] if ":" in key else ""
        books = self.books(cwd) if cwd else []
        roots = {b.root for b in books}
        books += [b for b in self.all_books() if b.root not in roots]
        for b in books:
            if scope and b.scope != scope:
                continue
            n = b.get(key)
            if n is not None:
                return b, n
        return None, None

    def _similar_note(self, text: str, book: Any) -> Any:
        mine = set(terms(text))
        if not mine:
            return None
        best, best_score = None, 0.0
        for n in book.scan():
            other = set(terms(n.description)) | set(terms(n.body[:300]))
            if not other:
                continue
            j = len(mine & other) / len(mine | other)
            if j > best_score:
                best, best_score = n, j
        return best if best_score >= 0.75 else None

    # Memory edits the user makes by hand (deleting/editing notes, forgetting history). The system prompt is only
    # refreshed mid-chat when these change; automatic extraction and Dream's writes wait for the next chat (a changed
    # system prompt rebuilds the prompt cache from scratch, see Agent.refresh_memory).
    user_edits = 0

    def mark_user_edit(self) -> None:
        self.user_edits += 1

    def save_note(self, name: str, description: str, type: str, content: str, title: str = "", source: str = "",
                  scope: str = "genel", cwd: Path | str | None = None, category: str = "",
                  aliases: list[str] | None = None) -> tuple[Any, bool]:
        b = self.book(scope, cwd)
        note, new = b.save(name, description, type, content, title=title, source=source, category=category,
                           aliases=aliases)
        if source in ("kullanıcı", "araç"):  # explicitly saved again in the conversation
            self.unforget(f"{title} {description} {content}")
        if source == "kullanıcı":
            self.mark_user_edit()
        self._bump()
        return note, new

    def delete_note(self, key: str, cwd: Path | str | None = None) -> Any:
        """Delete the note (UI, voice, or ALICE at the user's request). Its trace is kept: conversation summaries
        persist, so end-of-turn extraction and Dream kept re-noting the same facts and the deleted note kept coming
        back. Dream's merge deletions don't go through here (they leave no trace)."""
        b, n = self.find_note(key, cwd)
        if b is None:
            return None
        gone = b.delete(n.name)
        if gone is not None:
            self._tombstone(f"{n.title}. {n.description}. {_clip(n.body, 400)}")
        self.mark_user_edit()
        self._bump()
        return gone

    def _tombstone(self, text: str) -> None:
        text = " ".join(text.split())
        with self._lock:
            self.db.execute("INSERT INTO forgotten(ts, text, terms) VALUES (?, ?, ?)",
                            (time.time(), text[:800], " ".join(sorted(set(terms(text))))))
            self.db.execute("DELETE FROM forgotten WHERE id NOT IN (SELECT id FROM forgotten ORDER BY id DESC "
                            "LIMIT ?)", (KEEP_FORGOTTEN,))
            self.db.commit()

    def forgotten(self, limit: int = 20) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT text FROM forgotten ORDER BY id DESC LIMIT ?", (int(limit),))]

    def _forgotten_like(self, text: str) -> list[int]:
        """IDs of deleted notes that say the same thing as the text (word overlap: at least 60% of the shorter one)."""
        mine = set(terms(text))
        if len(mine) < 2:
            return []
        out = []
        with self._lock:
            rows = self.db.execute("SELECT id, terms FROM forgotten").fetchall()
        for fid, t in rows:
            other = set(t.split())
            if len(other) >= 2 and len(mine & other) / min(len(mine), len(other)) >= 0.6:
                out.append(fid)
        return out

    def unforget(self, text: str) -> int:
        """The user explicitly had the same thing saved again: remove the trace."""
        ids = self._forgotten_like(text)
        if ids:
            with self._lock:
                self.db.execute(f"DELETE FROM forgotten WHERE id IN ({','.join('?' * len(ids))})", ids)
                self.db.commit()
        return len(ids)

    def migrate_facts(self) -> int:
        """Move the old one-sentence facts (facts table) into notes. The table is not deleted (kept as a backup).

        The ID of each moved fact is recorded. If one could not be moved (e.g. the directory is full), the migration
        is not counted as "done"; only unmoved ones are retried on the next start. Previously the first attempt was
        marked done (the rest were never moved); moving everything again would also overwrite notes edited since."""
        if self._meta("notes_migrated"):
            return 0
        try:
            done = {int(i) for i in json.loads(self._meta("notes_migrated_ids") or "[]")}
        except (ValueError, TypeError):
            done = set()
        n, failed = 0, 0
        try:
            for f in reversed(self.facts()):
                if f.id in done:
                    continue
                src = self._source(f.id)
                try:
                    self.remember(f.text, f.kind,
                                  source="kullanıcı" if src in ("kullanıcı", "araç", "memory.md") else src,
                                  when=f.updated)
                except Exception:  # noqa: BLE001 - if the directory is full, the rest is done later
                    failed += 1
                    log.debug("bilgi nota taşınamadı: %s", f.text, exc_info=True)
                    continue
                done.add(f.id)
                n += 1
        finally:
            self._meta("notes_migrated_ids", json.dumps(sorted(done)))
        if not failed:
            self._meta("notes_migrated", datetime.now().isoformat(timespec="seconds"))
        return n

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _prune(self) -> None:
        if self.keep_days > 0:
            with self._lock:
                self.db.execute("DELETE FROM episodes WHERE ts < ?", (time.time() - self.keep_days * 86400,))
                self.db.commit()
        with self._lock:
            # Vectors of deleted turns/summaries were left behind: the table kept growing and stale records
            # took the top ranks of semantic search (real matches fell outside the k limit).
            self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref NOT IN (SELECT id FROM episodes)")
            self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref NOT IN (SELECT id FROM summaries)")
            self.db.execute("DELETE FROM vectors WHERE kind = 'fact' AND ref NOT IN (SELECT id FROM facts)")
            self.db.commit()

    def _meta(self, key: str, value: str | None = None) -> str | None:
        with self._lock:
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
                self.db.commit()
                return value
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    @property
    def version(self) -> int:
        return int(self._meta("version") or 0)

    def _bump(self) -> None:
        # Read and write under one lock: when two threads read the same version and wrote the same number, one
        # change stayed invisible (the side watching the version never noticed the second one).
        with self._lock:
            self._meta("version", str(self.version + 1))

    def import_markdown(self, path: Path) -> int:
        """Import the old plain memory file once (memory.md, "* not  _(tarih)_" lines, tarih = date)."""
        if self._meta("imported_md") or not path.is_file():
            return 0
        n = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith(("* ", "- ")):
                continue
            text = re.sub(r"\s*_\(\d{4}-\d{2}-\d{2}\)_\s*$", "", line[2:]).strip()
            if text:
                self.remember(text, "note", source="memory.md")
                n += 1
        self._meta("imported_md", datetime.now().isoformat(timespec="seconds"))
        return n

    # ------------------------------------------------------------------ facts
    def facts(self, limit: int | None = None) -> list[Fact]:
        with self._lock:
            rows = self.db.execute("SELECT id, kind, text, created, updated, hits FROM facts "
                                   "ORDER BY updated DESC" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        return [Fact(*r) for r in rows]

    def remember(self, text: str, kind: str = "fact", source: str = "", cwd: Path | str | None = None,
                 scope: str = "genel", when: float | None = None) -> tuple[str, bool]:
        """Save the one-sentence fact as a note (old interface: 'remember this', migrating old facts).
        If a note saying almost the same thing exists, it is replaced by that one. Returns (note name, is_new)."""
        from alice.memdir import slugify

        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("boş bilgi")
        typ = kind_to_type(kind, text)
        book = self.book(scope, cwd)
        old = self._similar_note(text, book)
        if old is not None:
            name, title = old.name, old.title
        else:
            words = re.sub(r"^(kullanıcı(nın|ya|yı)?|kullanici)\s+", "", text, flags=re.I)
            name = slugify(f"{typ}-{' '.join(words.split()[:5])}")
            base, i = name, 2
            while book.get(name) is not None:
                name, i = f"{base}-{i}", i + 1
            title = _clip(words.rstrip("."), 60)
        note, new = book.save(name, text, typ, text, title=title,
                              source=source if old is None else (old.source or source), when=when)
        if source in ("kullanıcı", "araç"):  # explicitly saved again in the conversation
            self.unforget(text)
        if source == "kullanıcı":
            self.mark_user_edit()
        self._bump()
        return note.name, new

    def update(self, key: str | int, text: str, cwd: Path | str | None = None) -> bool:
        """Replace the note's description and body with a single sentence (old interface)."""
        text = " ".join(str(text or "").split())
        b, n = self.find_note(str(key), cwd)
        if n is None or not text:
            return False
        b.save(n.name, text, n.type, text, title=n.title, source=n.source)
        self._bump()
        return True

    def forget(self, key: str | int, cwd: Path | str | None = None) -> Any:
        """Delete the note (moved to the trash). Returns the deleted note or None."""
        return self.delete_note(str(key), cwd)

    def old_role(self) -> dict[str, list[Any]]:
        """Traces left from the old role: notes, summaries and that period's replies."""
        notes = [n for b in self.all_books() for n in b.scan()
                 if OLD_ROLE_RX.search(f"{n.description}\n{n.body}")]
        with self._lock:
            sums = [Summary(*r) for r in self.db.execute(
                "SELECT id, start, end, channel, text FROM summaries").fetchall() if OLD_ROLE_RX.search(r[4])]
            eps = [Episode(*r) for r in self.db.execute(
                "SELECT id, ts, channel, user, reply FROM episodes").fetchall() if OLD_ROLE_RX.search(r[4])]
        return {"facts": notes, "summaries": sums, "episodes": eps}

    def purge_old_role(self) -> dict[str, int]:
        """Delete the traces of the old role: notes (to the trash) and summaries go, and in conversations only the old
        reply is deleted (what the user said stays, so searches like "geçen hafta ne konuşmuştuk" (what did we talk
        about last week) don't break)."""
        found = self.old_role()
        for n in found["facts"]:
            self.delete_note(n.key)
        with self._lock:
            for s in found["summaries"]:
                self.db.execute("DELETE FROM summaries WHERE id = ?", (s.id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (s.id,))
            for e in found["episodes"]:
                self.db.execute("UPDATE episodes SET reply = '' WHERE id = ?", (e.id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref = ?", (e.id,))  # so it is re-indexed
            self.db.commit()
            self._bump()
        return {k: len(v) for k, v in found.items()}

    def search_notes(self, query: str, cwd: Path | str | None = None, limit: int = 8) -> list[Any]:
        """Keyword search in notes (title, description and body)."""
        from alice.memdir import keyword_relevant

        notes = self.all_notes(cwd) if cwd else [n for b in self.all_books() for n in b.scan()]
        hits = keyword_relevant(query, notes, limit=limit, min_score=0.25)
        q = fold(query).strip()
        if q and len(q) >= 3:  # exact phrases (name, number, abbreviation) must not be lost to stem matching
            for n in notes:
                if n not in hits and q in fold(f"{n.name} {n.description} {n.body}"):
                    hits.append(n)
        return hits[:limit]

    def forget_matching(self, query: str, cwd: Path | str | None = None) -> tuple[list[Any], list[Any]]:
        """Delete the single note that clearly matches the query. If unsure, delete none and return the candidates."""
        hits = self.search_notes(query, cwd, limit=5)
        if len(hits) == 1:
            gone = self.delete_note(hits[0].key, cwd)
            return ([gone] if gone else []), hits
        return [], hits

    # ------------------------------------------------------------------ conversations
    def record(self, user: str, reply: str, channel: str = "", sid: str = "") -> int | None:
        if not self.chat_history:
            return None
        user = clean_user_text(user)
        reply = (reply or "").strip()
        if not user or noise_turn(user):
            return None
        with self._lock:
            cur = self.db.execute("INSERT INTO episodes(ts, channel, user, reply, terms, sid) VALUES (?, ?, ?, ?, ?, ?)",
                                  (time.time(), channel, user[:2000], reply[:2000], " ".join(terms(user + " " + reply)),
                                   str(sid or "")))
            self.db.commit()
        self.index_soon()
        return int(cur.lastrowid)

    def add_reply(self, eid: int, reply: str) -> bool:
        """Add a reply that arrives after the turn was recorded. In live voice mode, when a tool is called, the model
        stays silent first and speaks the reply in a separate turn after the tool; that reply used to go unsaved
        (half of the records had an empty reply)."""
        reply = (reply or "").strip()
        if not reply:
            return False
        with self._lock:
            row = self.db.execute("SELECT user, reply FROM episodes WHERE id = ?", (int(eid),)).fetchone()
            if row is None:
                return False
            full = (row[1] + " " + reply).strip()[:2000]
            self.db.execute("UPDATE episodes SET reply = ?, terms = ? WHERE id = ?",
                            (full, " ".join(terms(row[0] + " " + full)), int(eid)))
            self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref = ?", (int(eid),))
            self.db.commit()
        self.index_soon()
        return True

    def recent(self, n: int = 5) -> list[Episode]:
        with self._lock:
            rows = self.db.execute("SELECT id, ts, channel, user, reply FROM episodes ORDER BY ts DESC LIMIT ?",
                                   (int(n),)).fetchall()
        return [Episode(*r) for r in reversed(rows)]

    # ------------------------------------------------------------------ vectors
    def index_soon(self) -> None:
        """Turn records lacking a vector into vectors in the background (single thread; no-op without embeddings)."""
        if self.embedder is None or not self.embedder.available or self._index_lock.locked():
            return
        threading.Thread(target=self.index_pending, kwargs={"wait": False}, daemon=True,
                         name="alice-memory-index").start()

    def index_pending(self, limit: int = 60, wait: bool = True) -> int:
        """Turn records without a vector into vectors; returns how many this call added. wait: if an indexing run
        is already running in the background, wait for it (then do the rest). Each batch is saved separately:
        if quota runs out midway, the work done is kept; the rest resumes on its own when quota reopens."""
        if self.embedder is None or not self._index_lock.acquire(timeout=30 if wait else 0):
            return 0
        from alice.memvec import BATCH, pack

        done, left = 0, False
        try:
            with self._lock:
                todo = [("fact", r[0], r[1]) for r in self.db.execute(
                    "SELECT id, text FROM facts WHERE id NOT IN (SELECT ref FROM vectors WHERE kind = 'fact')")]
                todo += [("summary", r[0], r[1]) for r in self.db.execute(
                    "SELECT id, text FROM summaries WHERE id NOT IN (SELECT ref FROM vectors WHERE kind = 'summary')")]
                todo += [("episode", r[0], f"Kullanıcı: {r[1]}\nALICE: {r[2][:400]}") for r in self.db.execute(
                    "SELECT id, user, reply FROM episodes WHERE id NOT IN (SELECT ref FROM vectors WHERE kind = "
                    "'episode') ORDER BY ts DESC LIMIT ?", (limit + 1,))]
            left = len(todo) > limit
            todo = todo[:limit]
            for i in range(0, len(todo), BATCH):
                part = todo[i:i + BATCH]
                try:
                    vecs = self.embedder.embed([t[2][:2000] for t in part])
                except Exception:  # noqa: BLE001 - embedding is optional; keyword search keeps working
                    left = True
                    break
                with self._lock:
                    # Don't save a record's vector with its old text if the record was updated/deleted while embedding
                    # (a deleted turn stayed as an orphan vector; after add_reply the old reply's vector was written).
                    rows = [(k, ref, pack(v)) for (k, ref, text), v in zip(part, vecs)
                            if self._embed_text(k, ref) == text]
                    self.db.executemany("INSERT OR REPLACE INTO vectors(kind, ref, vec) VALUES (?, ?, ?)", rows)
                    self.db.commit()
                done += len(part)
        finally:
            self._index_lock.release()
        if left and isinstance(self.path, Path):
            self._index_later()
        return done

    def _embed_text(self, kind: str, ref: int) -> str | None:
        """The record's current text to embed (same format as in index_pending); None if the record is gone."""
        if kind == "fact":
            row = self.db.execute("SELECT text FROM facts WHERE id = ?", (ref,)).fetchone()
            return row[0] if row else None
        if kind == "summary":
            row = self.db.execute("SELECT text FROM summaries WHERE id = ?", (ref,)).fetchone()
            return row[0] if row else None
        row = self.db.execute("SELECT user, reply FROM episodes WHERE id = ?", (ref,)).fetchone()
        return f"Kullanıcı: {row[0]}\nALICE: {row[1][:400]}" if row else None

    def _index_later(self) -> None:
        """Records have piled up (quota exhausted or too many): resume once the quota reopens."""
        if getattr(self, "_index_timer", None) is not None and self._index_timer.is_alive():
            return
        from alice.memvec import RATE_WAIT

        def run() -> None:
            self._index_timer = None
            self.index_soon()

        self._index_timer = threading.Timer(RATE_WAIT + 5, run)
        self._index_timer.daemon = True
        self._index_timer.start()

    def _semantic(self, query: str, kinds: list[str], limit: int = 12) -> dict[tuple[str, int], float]:
        """(kind, id) -> 0..1 semantic similarity score (below embed_min is ignored). Empty without embeddings."""
        if self.embedder is None or not self.embedder.available or not kinds:
            return {}
        with self._lock:
            rows = self.db.execute(f"SELECT kind, ref, vec FROM vectors WHERE kind IN "
                                   f"({','.join('?' * len(kinds))})", kinds).fetchall()
        if not rows:
            return {}
        try:
            from alice.memvec import top

            qvec = self.embedder.query(query)
        except Exception:  # noqa: BLE001
            return {}
        lo = self.embed_min
        return {ref: 0.3 + 0.7 * (cos - lo) / max(1e-6, 1 - lo)
                for ref, cos in top(qvec, [((k, r), v) for k, r, v in rows], k=limit, min_score=lo)}

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 5, facts: bool = True, episodes: bool = True,
               min_score: float = 0.3, exclude_after: float | None = None, summaries: bool = True) -> list[Hit]:
        clean = clean_user_text(query)
        q = list(dict.fromkeys(terms(clean)))
        kinds = [k for k, on in (("fact", facts), ("summary", summaries and episodes), ("episode", episodes)) if on]
        sem = self._semantic(clean, kinds) if clean.strip() else {}
        if not q and not sem:
            return []
        frows: list[Any] = []  # the old "facts" table moved to notes; searching notes: search_notes
        with self._lock:
            srows = self.db.execute("SELECT id, start, end, channel, text, terms FROM summaries "
                                    "ORDER BY end DESC LIMIT 500").fetchall() if summaries and episodes else []
            erows = self.db.execute("SELECT id, ts, channel, user, reply, terms FROM episodes "
                                    "ORDER BY ts DESC LIMIT 3000").fetchall() if episodes else []
        docs = [set(r[-1].split()) for r in frows + srows + erows]
        n = len(docs) or 1
        df = {t: sum(1 for d in docs if t in d) for t in q}
        idf = {t: math.log(1 + n / (1 + df[t])) + 0.1 for t in q}
        total = sum(idf.values()) or 1.0
        now = time.time()

        def kw(d: set[str], need: int = 1) -> float:
            matched = [t for t in q if t in d]
            return sum(idf[t] for t in matched) / total if len(matched) >= need and matched else 0.0

        def mix(a: float, b: float) -> float:  # keyword/meaning score: the stronger one + a small share of the other
            return max(a, b) + 0.1 * min(a, b)

        hits: list[Hit] = []
        for r, d in zip(frows, docs[:len(frows)]):
            score = mix(kw(d), sem.get(("fact", r[0]), 0.0))
            if score >= min_score:
                hits.append(Hit(score + min(r[5], 10) * 0.005, Fact(*r[:6])))
        off = len(frows)
        for r, d in zip(srows, docs[off:off + len(srows)]):
            if exclude_after is not None and r[1] >= exclude_after:
                continue
            score = mix(kw(d, min(2, len(q))), sem.get(("summary", r[0]), 0.0))
            score *= 0.7 + 0.3 * math.exp(-(now - r[2]) / 86400 / 90)
            if score >= min_score + 0.1:
                hits.append(Hit(score, Summary(*r[:5])))
        off += len(srows)
        for r, d in zip(erows, docs[off:]):
            if exclude_after is not None and r[1] >= exclude_after:
                continue
            # In chats one shared word is often chance: unless the query is short, require two matching words.
            score = mix(kw(d, min(2, len(q))), sem.get(("episode", r[0]), 0.0))
            score *= 0.6 + 0.4 * math.exp(-(now - r[1]) / 86400 / 60)  # older conversations rank a bit lower
            if score >= min_score + 0.15:
                hits.append(Hit(score, Episode(*r[:5])))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    # ------------------------------------------------------------------ past chats (on request)
    # Like Claude's conversation_search / recent_chats tools: old conversations are not added to every message
    # automatically (a wrong match confused the model); when the user mentions the past, the model looks them up.
    def sessions(self, after: float | None = None, before: float | None = None, limit: int = 400) -> list[list[Episode]]:
        """Chats in a time range: groups of turns split by silences longer than session_gap (oldest first)."""
        sql, args = "SELECT id, ts, channel, user, reply FROM episodes WHERE 1=1", []
        if after is not None:
            sql, args = sql + " AND ts >= ?", args + [after]
        if before is not None:
            sql, args = sql + " AND ts < ?", args + [before]
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", args + [int(limit)]).fetchall()
        groups: list[list[Episode]] = []
        for r in reversed(rows):
            e = Episode(*r)
            if groups and e.ts - groups[-1][-1].ts <= self.session_gap:
                groups[-1].append(e)
            else:
                groups.append([e])
        return groups

    def _session_text(self, group: list[Episode], turns: int = 4, now: float | None = None) -> str:
        """Short form of a chat: its summary if it has one, plus the first and last few turns."""
        start, end = group[0].ts, group[-1].ts
        with self._lock:
            row = self.db.execute("SELECT text FROM summaries WHERE start <= ? AND end >= ? ORDER BY id DESC LIMIT 1",
                                  (end, start)).fetchone()
        head = f"## {datetime.fromtimestamp(start):%d.%m.%Y %H:%M} ({_ago(start, now)}, {len(group)} tur)"
        lines = [head] + ([f"Özet: {row[0]}"] if row else [])

        def turn(e: Episode) -> str:
            return f"- Kullanıcı: {_clip(e.user, 240)}" + (f"\n  ALICE: {_clip(e.reply, 240)}" if e.reply else "")

        if turns <= 0:
            return "\n".join(lines)
        if len(group) <= turns:
            return "\n".join(lines + [turn(e) for e in group])
        first, last = group[: turns // 2], group[len(group) - (turns - turns // 2):]
        return "\n".join(lines + [turn(e) for e in first] + [f"  … {len(group) - turns} tur daha …"]
                         + [turn(e) for e in last])

    def recent_chats(self, n: int = 5, after: float | None = None, before: float | None = None) -> str:
        """The last n chats (or those in the given time range), newest first."""
        if not self.chat_history:
            return HISTORY_OFF
        groups = self.sessions(after, before)[-max(1, min(int(n), 20)):]
        now = time.time()
        return "\n\n".join(self._session_text(g, now=now) for g in reversed(groups)) or "no conversations in this range"

    def search_chats(self, query: str, limit: int = 5, after: float | None = None,
                     before: float | None = None) -> str:
        """Search past chats by topic: the chats containing matching turns (summary + context turns)."""
        if not self.chat_history:
            return HISTORY_OFF
        hits = self.search(query, limit=30, facts=False, min_score=0.2)
        picked: list[tuple[float, float]] = []  # (score, turn time)
        for h in hits:
            ts = h.item.ts if isinstance(h.item, Episode) else h.item.start
            if (after is not None and ts < after) or (before is not None and ts >= before):
                continue
            picked.append((h.score, ts))
        if not picked:
            return f"nothing about '{query}' found in past conversations"
        out, seen, now = [], set(), time.time()
        for _, ts in sorted(picked, reverse=True):
            group = next((g for g in self.sessions(ts - 6 * 3600, ts + 6 * 3600)
                          if g[0].ts - 1 <= ts <= g[-1].ts + self.session_gap), None)
            if not group or group[0].id in seen:
                continue
            seen.add(group[0].id)
            # Show the matching turn: if the chat is long, the turns around it.
            i = min(range(len(group)), key=lambda k: abs(group[k].ts - ts))
            window = group[max(0, i - 2): i + 3]
            text = self._session_text(group, turns=0, now=now).splitlines()
            text += [f"- Kullanıcı: {_clip(e.user, 300)}" + (f"\n  ALICE: {_clip(e.reply, 300)}" if e.reply else "")
                     for e in window]
            out.append("\n".join(text))
            if len(out) >= limit:
                break
        return "\n\n".join(out)

    def recall(self, query: str, exclude_after: float | None = None, cwd: Path | str | None = None,
               surfaced: set[str] | None = None, use_model: bool | None = None,
               recent_tools: tuple[str, ...] = (), context: str = "") -> str:
        """Memory note to add to the user's message (may be empty): all notes that will really help this message
        (at most 5; ones already shown in this chat are skipped) and strongly matching chat summaries. Old turns
        are not added one by one; the model finds them itself with search_chats / recent_chats."""
        from alice.memdir import select_relevant

        clean = clean_user_text(query)
        if not clean.strip():
            return ""
        seen = surfaced if surfaced is not None else set()
        stamp = lambda n: f"{n.key}@{int(n.modified)}"  # noqa: E731 - shown again when the note changes
        in_profile = self._in_profile.get(str(cwd or ""), set())
        notes = [n for n in self.all_notes(cwd) if stamp(n) not in seen and stamp(n) not in in_profile]
        use = self.recall_model if use_model is None else use_model
        chooser = self.extractor if use and self.extractor is not None else None
        if chooser is not None and notes and not _worth_asking(clean, notes):
            # Short messages like "selam naber" or "hava nasıl": if no note shares a word with them, the model call
            # (a ~1 s wait at the start of every message) is wasted; no note was being picked anyway.
            chooser = None
        picked = select_relevant(clean, notes, chooser, limit=5, recent_tools=recent_tools,
                                 context=clean_user_text(context) if context else "") if notes else []
        sums = [h for h in self.search(clean, limit=4, facts=False, exclude_after=exclude_after)
                if isinstance(h.item, Summary) and h.score >= 0.6][:2] if self.chat_history else []
        if not picked and not sums:
            return ""
        for n in picked:
            seen.add(stamp(n))
        now = time.time()
        parts = [n.render(now, RECALL_ITEM_CHARS) for n in picked]
        if sums:
            parts.append("From past conversations:\n" + "\n".join(f"- {h.item.line(now)}" for h in sums))
        body = _clip("\n\n".join(parts), RECALL_TOTAL_CHARS)
        return ("[hafızadan (from memory) — your notes that may relate to this message; use them if they help, don't "
                f"say you read them from memory:\n{body}]")

    # ------------------------------------------------------------------ summaries and follow-ups
    def summaries(self, n: int = 5, within_days: float | None = None) -> list[Summary]:
        sql = "SELECT id, start, end, channel, text FROM summaries"
        args: tuple = ()
        if within_days is not None:
            sql += " WHERE end >= ?"
            args = (time.time() - within_days * 86400,)
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY end DESC LIMIT ?", args + (int(n),)).fetchall()
        return [Summary(*r) for r in reversed(rows)]

    def add_summary(self, start: float, end: float, text: str, channel: str = "") -> int:
        text = " ".join(text.split())
        with self._lock:
            cur = self.db.execute("INSERT INTO summaries(start, end, channel, text, terms) VALUES (?, ?, ?, ?, ?)",
                                  (start, end, channel, text[:1200], " ".join(terms(text))))
            self.db.commit()
            self._bump()
        self.index_soon()
        return int(cur.lastrowid)

    def add_followup(self, text: str, due: float | None = None, kind: str = "ask") -> int:
        text = " ".join(text.split())
        if not text:
            raise ValueError("boş takip")
        kind = kind if kind in FOLLOWUP_KINDS else "ask"
        with self._lock:
            for f in self.followups():  # don't add the same follow-up a second time
                if f.kind == kind and set(terms(f.text)) == set(terms(text)):
                    return f.id
            cur = self.db.execute("INSERT INTO followups(created, due, kind, text) VALUES (?, ?, ?, ?)",
                                  (time.time(), due, kind, text[:300]))
            self.db.commit()
            self._bump()
        return int(cur.lastrowid)

    def followups(self, include_done: bool = False) -> list[Followup]:
        sql = "SELECT id, created, due, kind, text, done FROM followups"
        if not include_done:
            sql += " WHERE done = 0"
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY COALESCE(due, created)").fetchall()
        return [Followup(*r) for r in rows]

    def due_followups(self, now: float | None = None, prune: bool = True) -> list[Followup]:
        """Follow-ups due today or earlier, plus unfinished tasks. Very old ones drop off on their own
        ("how did it go?" is not asked 4 days after an exam; a task left unfinished two weeks ago is stale).
        prune=False: old ones are only dropped from the list, not permanently closed (used when looking ahead:
        the nightly report, when looking at "tomorrow", was closing follow-ups a day and a half early)."""
        now = now or time.time()
        end_of_day = datetime.fromtimestamp(now).replace(hour=23, minute=59, second=59).timestamp()
        out, stale = [], []
        for f in self.followups():
            if f.due is not None and f.due > end_of_day:
                continue
            age = now - (f.due if f.due is not None else f.created)
            if age > (14 if f.kind == "open" else 4) * 86400:
                stale.append(f.id)
                continue
            out.append(f)
        if stale and prune:
            with self._lock:
                self.db.executemany("UPDATE followups SET done = 2 WHERE id = ?", [(i,) for i in stale])
                self.db.commit()
        return out

    def done_followup(self, fid: int) -> Followup | None:
        with self._lock:
            row = self.db.execute("SELECT id, created, due, kind, text, done FROM followups WHERE id = ?",
                                  (int(fid),)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE followups SET done = 1 WHERE id = ?", (int(fid),))
            self.db.commit()
            self._bump()
        return Followup(*row)

    # ------------------------------------------------------------------ summarization
    PENDING_PAGE = 400  # most turns read in one pass

    def pending_sessions(self, now: float | None = None, force: bool = False) -> list[list[Episode]]:
        """Finished conversations not yet summarized (turn groups; a silence longer than session_gap splits them)."""
        now = now or time.time()
        raw = self._meta("digested_until")
        since = float(raw) if raw else now - 3 * 86400  # on first run: the last three days, not all history
        with self._lock:
            rows = self.db.execute("SELECT id, ts, channel, user, reply FROM episodes WHERE ts > ? "
                                   "ORDER BY ts LIMIT ?", (since, self.PENDING_PAGE)).fetchall()
        groups: list[list[Episode]] = []
        for r in rows:
            e = Episode(*r)
            if groups and e.ts - groups[-1][-1].ts <= self.session_gap:
                groups[-1].append(e)
            else:
                groups.append([e])
        if len(rows) >= self.PENDING_PAGE:
            # Page is full: the last group may have been cut at the page boundary (the rest is on the next page).
            # If it were summarized, one conversation would be split into two summaries; on the next pass it comes
            # whole. If there is only one group (a single conversation longer than a page) there is no other way:
            # this part is summarized so that we make progress.
            if len(groups) > 1:
                groups.pop()
        elif groups and not force and now - groups[-1][-1].ts < self.session_gap:
            groups.pop()  # conversation still going
        return groups

    def maybe_digest(self) -> None:
        """Cheap check (once a minute): summarize any finished, unsummarized conversation in the background."""
        if not self.digest_enabled or self.extractor is None or self._digest_lock.locked():
            return
        now = time.monotonic()
        if now - self._last_digest_check < 60:
            return
        self._last_digest_check = now
        if float(self._meta("digest_fail_until") or 0) > time.time() or not self.pending_sessions():
            return
        threading.Thread(target=self.digest, daemon=True, name="alice-memory-digest").start()

    def digest(self, force: bool = False, max_sessions: int = 3) -> int:
        """Summarize finished conversations; extract lasting facts and follow-ups. Returns the count summarized."""
        if self.extractor is None or not self._digest_lock.acquire(blocking=False):
            return 0
        done = 0
        try:
            for group in self.pending_sessions(force=force)[:max_sessions]:
                if sum(len(e.user) for e in group) >= 40:  # a single short turn like "saat kaç" is not summarized
                    try:
                        self._digest_one(group)
                    except _BadDigest:
                        # The model answered, but the answer is unusable. If this kept happening in the same
                        # conversation, all later conversations would stay unsummarized forever. After a few
                        # tries, this conversation is skipped.
                        key = repr(group[-1].ts)
                        fails = (int(self._meta("digest_bad_count") or 0) + 1
                                 if self._meta("digest_bad_group") == key else 1)
                        self._meta("digest_bad_group", key)
                        self._meta("digest_bad_count", str(fails))
                        if fails < DIGEST_BAD_TRIES:
                            self._meta("digest_fail_until", repr(time.time() + 900))
                            break
                        log.debug("konuşma özetlenemedi, atlanıyor (%s deneme)", fails)
                    except Exception:  # noqa: BLE001 - no model or quota: retried later
                        self._meta("digest_fail_until", repr(time.time() + 900))
                        break
                    else:
                        done += 1
                self._meta("digested_until", repr(group[-1].ts))
        finally:
            self._digest_lock.release()
        return done

    def _digest_one(self, group: list[Episode]) -> None:
        lines, used = [], 0
        for e in reversed(group):  # in a long conversation the end matters most: go back up to 6000 characters
            line = f"{datetime.fromtimestamp(e.ts):%H:%M} Kullanıcı: {e.user[:600]}\nALICE: {e.reply[:400]}"
            if used + len(line) > 6000:
                break
            lines.append(line)
            used += len(line)
        transcript = "\n".join(reversed(lines))
        open_items = "\n".join(f.line() for f in self.followups()) or "(yok)"
        start = datetime.fromtimestamp(group[0].ts)
        prompt = DIGEST_PROMPT.format(date=f"{start:%Y-%m-%d %A}", open=open_items, transcript=transcript)
        data = parse_json(self.extractor(prompt) if self.extractor else "")
        if not data:
            raise _BadDigest("özet cevabı boş")
        # Durable notes are not written from the summary but by end-of-turn extraction and Dream
        # (Dream reads these summaries and distills them).
        summary = strip_filler(data.get("summary") or "") if isinstance(data.get("summary"), str) else ""
        if summary:
            channels = ", ".join(dict.fromkeys(e.channel for e in group if e.channel))
            self.add_summary(group[0].ts, group[-1].ts, summary, channels)
        for fid in data.get("done") or []:
            try:
                self.done_followup(int(fid))
            except (TypeError, ValueError):
                continue
        for f in (data.get("followups") or [])[:5]:
            if not isinstance(f, dict) or not isinstance(f.get("text"), str):
                continue
            due = _parse_day(f.get("due"))
            kind = str(f.get("kind") or ("open" if due is None else "ask"))
            if kind != "open" and due is None:
                continue
            try:
                self.add_followup(f["text"], due, kind)
            except ValueError:
                continue

    # ------------------------------------------------------------------ prompt
    def profile(self, chars: int | None = None, recent: int = 3, cwd: Path | str | None = None) -> str:
        """Block for the system prompt: memory rules, MEMORY.md index(es), always-valid rules (feedback notes),
        notes introducing the user, a summary of recent days, follow-ups due today, and recent conversations."""
        from alice.memdir import MEMORY_RULES

        chars = chars or self.profile_chars
        books = self.books(cwd)
        parts = ["# Long-term memory", MEMORY_RULES]
        for b in books:
            b.ensure()
            where = "General memory" if b.scope == "genel" else f"This project's memory ({project_label(cwd)})"
            idx = b.index_for_prompt()
            parts.append(f"## {where}: MEMORY.md (`{b.root}`)\n"
                         + (idx or "(empty: the notes you save are listed here)"))
            if b.near_limit():
                parts.append(f"(Note: the {b.scope} index is near its limit; merge similar notes or tidy up with "
                             "memory action=dream.)")
        # Behavior rules and notes that introduce the user are short and needed in every conversation, so they
        # go into the prompt in full. Other notes are read from the index, or added to the message automatically
        # when relevant.
        always, used = [], 0
        shown: set[str] = set()  # notes shown in full in the prompt; recall must not repeat them
        all_notes = [n for b in books for n in b.scan()]
        for typ in ("feedback", "user"):
            # New notes carry a category instead of a type: profile/preferences ("you") count as the user notes.
            pick = (lambda n: n.type == "feedback") if typ == "feedback" else \
                (lambda n: n.type != "feedback" and (n.type == "user" or n.category == "you"))
            for n in sorted((n for n in all_notes if pick(n)), key=lambda n: -n.modified):
                if " ".join(n.body.split()) == " ".join(n.description.split()):
                    continue  # one-sentence note: the full text is already on its index line
                block = n.render(limit=700)
                if used + len(block) > chars:
                    continue
                always.append(block)
                used += len(block)
                shown.add(f"{n.key}@{int(n.modified)}")
        self._in_profile[str(cwd or "")] = shown
        if always:
            parts.append("## Full notes: feedback and user (valid in every conversation)\n"
                         + "\n\n".join(always))
        # Recent turns are included only if they are recent: yesterday's last sentences were slipping into a new
        # chat as "kaldığımız yer" (where we left off), and ALICE took the user's new, unrelated message for a
        # continuation of yesterday's conversation.
        sums = self.summaries(4, within_days=10)
        due = self.due_followups()
        parts.append("This chat is a NEW, separate conversation. The past conversations below are background only: do "
                     "not assume the user is continuing one of them or reacting to something said there, unless they "
                     "say so or it is obvious from their message. Older conversations are not shown here: when the "
                     "user refers to one, look it up with memory action=search_chats (by topic) or recent_chats (by "
                     "time) before answering.")
        if sums:
            parts.append("Earlier conversations (separate chats), summarized; bring one up briefly only when it fits, "
                         "e.g. ask how a thing went, and never treat them as this chat's history:\n"
                         + "\n".join(f"- {s.line()}" for s in sums))
        if due:
            parts.append("Follow-ups due now: bring each up ONCE at a natural moment (a greeting or a pause is fine), "
                         "briefly, in your own words; then call memory action=done id=<n>. Unfinished threads: offer "
                         "to continue.\n" + "\n".join(f"- {f.line()}" for f in due))
        return "\n\n".join(parts)

    def export_markdown(self, cwd: Path | str | None = None) -> str:
        books = self.books(cwd) if cwd else self.all_books()
        lines: list[str] = []
        for b in books:
            notes = b.scan()
            if not notes:
                continue
            lines += [f"# Notlar ({b.scope}: {b.root})"]
            lines += [f"* {n.manifest_line()[2:]}" for n in notes]
        eps = self.recent(10)
        sums = self.summaries(5)
        if sums:
            lines += ["", "# Konuşma özetleri"] + [f"* {s.line()}" for s in sums]
        fups = self.followups()
        if fups:
            lines += ["", "# Takipler"] + [f"* {f.line()}" for f in fups]
        if eps:
            lines += ["", "# Son konuşmalar"] + [f"* {e.line()}" for e in eps]
        return "\n".join(lines).strip() or "(hafıza boş)"

    # ------------------------------------------------------------------ learning
    def learn(self, user: str, reply: str = "", background: bool = True, cwd: Path | str | None = None,
              wrote: bool = False) -> None:
        """Extract durable notes from a conversation turn. An explicit "hatırla" (remember) request is saved
        right away; the others are handled by the model in the background.
        wrote: ALICE wrote to memory itself this turn, so background extraction must not write it again."""
        user = clean_user_text(user)
        self.maybe_digest()  # if the previous finished conversation is not summarized yet, do it now in background
        if wrote:
            with self._turns_lock:
                self._turns_since_extract = 0
            self._advance_cursor()
            return
        note = explicit_note(user)
        if note:
            try:
                self.remember(note, "note", source="kullanıcı", cwd=cwd)
            except Exception:  # noqa: BLE001 - directory full: the model tidies up and saves via the memory tool
                log.debug("açık not kaydedilemedi", exc_info=True)
            self._advance_cursor()
            return
        if self.extractor is None or not self.auto_extract:
            return
        # Read-increment-reset under a single lock: without it, concurrent turns lost increments, or two
        # extractions started at the same threshold at once.
        with self._turns_lock:
            self._turns_since_extract += 1
            if not (has_signal(user) or has_feedback(user) or self._turns_since_extract >= self.extract_every):
                return
            self._turns_since_extract = 0
        if background:
            threading.Thread(target=self._extract, args=(cwd,), daemon=True, name="alice-memory").start()
        else:
            self._extract(cwd)

    def _advance_cursor(self) -> None:
        """Turns up to here were reviewed for notes (or ALICE wrote them itself): don't look at them again."""
        with self._lock:
            row = self.db.execute("SELECT MAX(id) FROM episodes").fetchone()
        self._meta("notes_cursor", str(int(row[0] or 0)))

    def _recent_turns(self, n: int = 8) -> list[Episode]:
        """Turns after the cursor (at most from the last 3 hours), oldest first."""
        after = int(float(self._meta("notes_cursor") or 0))
        with self._lock:
            rows = self.db.execute("SELECT id, ts, channel, user, reply FROM episodes WHERE id > ? AND ts > ? "
                                   "ORDER BY id DESC LIMIT ?", (after, time.time() - 3 * 3600, int(n))).fetchall()
        return [Episode(*r) for r in reversed(rows)]

    def _extract(self, cwd: Path | str | None = None) -> list[str]:
        """Look at recent turns and write, update or delete durable notes (touches only memory notes)."""
        from alice.memdir import EXTRACT_PROMPT, MEMORY_RULES, keyword_relevant, project_root

        if not self._learn_lock.acquire(timeout=60):
            return []
        try:
            turns = self._recent_turns()
            if not turns:
                return []
            transcript = "\n".join(f"Kullanıcı: {_clip(e.user, 1200)}\nALICE: {_clip(e.reply, 700)}" for e in turns)
            notes = self.all_notes(cwd)
            related = keyword_relevant(" ".join(e.user for e in turns), notes, limit=4, min_score=0.2)
            root = project_root(cwd) if cwd else None
            prompt = EXTRACT_PROMPT.format(
                rules=MEMORY_RULES, manifest="\n".join(n.manifest_line() for n in notes) or "(yok)",
                related="\n\n".join(f"--- {n.key}\n{n.body}" for n in related) or "(yok)", explicit="",
                repo=f" ({root.name})" if root else " (şu an bir depoda değilsin: hep genel)", transcript=transcript)
            gone = self.forgotten(15)
            if gone:
                prompt += ("\n\nThe user DELETED these notes; never save them (or anything saying the same) again:\n"
                           + "\n".join(f"- {_clip(g, 200)}" for g in gone))
            data = parse_json(self.extractor(prompt) if self.extractor else "")
            done = self.apply_notes(data, cwd=cwd, source="otomatik", max_save=3, max_delete=2)
            self._meta("notes_cursor", str(turns[-1].id))
            if done:
                log.info("hafıza: %s", ", ".join(done))
            return done
        except Exception:  # noqa: BLE001 - a learning error must not affect the conversation
            log.debug("konuşmadan not çıkarılamadı", exc_info=True)
            return []
        finally:
            self._learn_lock.release()

    def apply_notes(self, data: dict[str, Any], cwd: Path | str | None = None, source: str = "otomatik",
                    max_save: int = 3, max_delete: int = 2, book: Any = None, protect: bool = True) -> list[str]:
        """Apply the model's {"save": [...], "delete": [...]} answer. Notes the user explicitly asked for (source
        "kullanıcı" = user) are not deleted; at most max_delete notes are deleted at once."""
        from alice.memdir import TYPES, MemoryFull

        done: list[str] = []
        for s in (data.get("save") or [])[:max_save]:
            if not isinstance(s, dict):
                continue
            content = str(s.get("content") or s.get("body") or "").strip()
            desc = str(s.get("description") or "").strip()
            typ = str(s.get("type") or "")
            if typ and typ not in TYPES:  # a made-up type ("secret") is a broken answer, not a category save
                continue
            # The prompts now ask for a category (you/people/areas/topics) instead of a type; an answer with
            # neither used to be dropped, which would have silently stopped every automatic save.
            cat = str(s.get("category") or "").strip().lower()
            if not cat and typ:
                cat = {"feedback": "you", "project": "areas", "reference": "topics"}.get(typ, "")
            if not (content or desc) or len(content) > 6000:
                continue
            if typ in ("user", "") and cat in ("", "general") and ephemeral(desc):  # a path, a test, a one-off task
                continue
            raw_al = s.get("aliases") or []
            aliases = [str(a).strip() for a in (raw_al.split(",") if isinstance(raw_al, str) else raw_al)
                       if str(a).strip()] if isinstance(raw_al, (list, str)) else []
            try:
                target = book or self.book(str(s.get("scope") or "genel"), cwd)
            except ValueError:
                target = self.notes
            # Same name as the note being saved: with an empty name, get("") found another note called "not"
            # (the deleted-note check was skipped and the source was taken from that note).
            key = str(s.get("name") or s.get("title") or desc)
            old = target.get(key)
            if old is None and source in ("otomatik", "dream") and \
                    self._forgotten_like(f"{s.get('title') or ''} {desc} {content}"):
                done.append(f"atlandı (kullanıcı silmişti): {desc[:60]}")
                continue
            try:
                note, new = target.save(key, desc, typ, content,
                                        title=str(s.get("title") or ""),
                                        source=(old.source if old is not None and old.source else source),
                                        category=cat, aliases=aliases)
            except (MemoryFull, ValueError) as e:
                done.append(f"kaydedilemedi ({e})"[:160])
                continue
            done.append(("eklendi " if new else "güncellendi ") + note.key)
        removed = 0
        for key in data.get("delete") or []:
            if removed >= max_delete:
                break
            b, n = (book, book.get(str(key))) if book is not None else self.find_note(str(key), cwd)
            if n is None or (protect and n.source == "kullanıcı"):
                continue
            if b.delete(n.name):
                removed += 1
                done.append(f"silindi {n.key}")
        if done:
            self._bump()
        return done

    # ------------------------------------------------------------------ Dream
    def maybe_dream(self) -> bool:
        """Cheap check (the core calls it every minute): if dream_hours passed since the last Dream and at least
        dream_sessions conversations have piled up, consolidate memory. Gate order: time (file timestamp) ->
        conversation count every 10 minutes -> lock."""
        if not self.dream_enabled or (self.synthesizer is None and self.extractor is None):
            return False
        last = self.notes.last_dream()
        if time.time() - last < self.dream_hours * 3600:
            return False
        if self._last_dream_scan is not None and time.monotonic() - self._last_dream_scan < 600:
            return False
        if time.monotonic() < getattr(self, "_dream_retry_at", 0.0):
            return False  # the last Dreams failed: don't pointlessly call the strong model every 10 minutes
        self._last_dream_scan = time.monotonic()
        groups = self.sessions(after=last or time.time() - 7 * 86400)
        if groups and time.time() - groups[-1][-1].ts < self.session_gap:
            groups = groups[:-1]  # an ongoing conversation is not counted
        if len(groups) < self.dream_sessions:
            return False
        report = self.dream()
        if any("toparlanamadı" in r for r in report):
            # A failed Dream rolls the time gate back (so it is retried at the next chance); if the model keeps
            # failing, that would mean a strong-model call every 10 minutes. Waits compound on consecutive failures.
            self._dream_fails = getattr(self, "_dream_fails", 0) + 1
            self._dream_retry_at = time.monotonic() + min(3600.0 * 2 ** (self._dream_fails - 1), 24 * 3600.0)
        else:
            self._dream_fails, self._dream_retry_at = 0, 0.0
        return True

    def dream(self, force: bool = False, cwd: Path | str | None = None) -> list[str]:
        """Go over the memory: merge duplicates, correct outdated entries, make relative dates absolute, add what
        is lasting from recent conversations, tidy the index. Returns what was done. A backup is taken first."""
        from alice.memdir import DREAM_PROMPT, MEMORY_RULES, MemoryFull

        if not self._dream_lock.acquire(blocking=False):
            return ["başka bir Dream sürüyor"]
        try:
            books = self.all_books()
            if cwd:
                books += [b for b in self.books(cwd) if b.root not in {x.root for x in books}]
            report: list[str] = []
            for b in books:
                prior = b.try_lock()
                if prior is None:
                    report.append(f"{b.scope}: başka bir süreç toparlıyor")
                    continue
                ok = False
                try:
                    report += self._dream_book(b, prior, DREAM_PROMPT, MEMORY_RULES, MemoryFull)
                    ok = True
                except Exception as e:  # noqa: BLE001 - no model/quota/bad reply: keep the time gate open
                    log.debug("Dream başarısız", exc_info=True)
                    report.append(f"{b.scope}: toparlanamadı ({str(e)[:120]})")
                finally:
                    b.release(prior, ok=ok)
            summary = "; ".join(report) or "değişiklik yok"
            self._meta("dream_summary", summary[:2000])
            self._meta("dream_at", repr(time.time()))
            return report
        finally:
            self._dream_lock.release()

    def _dream_book(self, b: Any, prior: float, prompt_tpl: str, rules: str, full: type) -> list[str]:
        notes = b.scan()
        since = prior or time.time() - 14 * 86400
        signal: list[str] = []
        logs = b.logs_since(since)
        if logs:
            signal.append("Daily logs:\n" + logs)
        if b.scope == "genel":
            sums = [s for s in self.summaries(60) if s.end > since]
            if sums:
                signal.append("Conversation summaries:\n" + "\n".join(f"- {s.line()}" for s in sums))
            fups = self.followups()
            if fups:
                signal.append("Open follow-ups:\n" + "\n".join(f"- {f.line()}" for f in fups))
        if not notes and not signal:
            return []
        gone = self.forgotten(20)
        if gone and signal:
            signal.append("Notes the user DELETED (do NOT re-create them or anything saying the same):\n"
                          + "\n".join(f"- {_clip(g, 200)}" for g in gone))
        backup = b.backup()
        listing = "\n\n".join(
            f"--- {n.name}.md [{n.type or '?'}] ({age_text_iso(n.modified)})"
            f"{' (kullanıcı istedi)' if n.source == 'kullanıcı' else ''}\ndescription: {n.description}\n"
            f"{_clip(n.body, 2500)}" for n in notes) or "(yok)"
        prompt = prompt_tpl.format(today=f"{datetime.now():%Y-%m-%d %A}", scope=f"{b.scope} ({b.root})", rules=rules,
                                   index=b.index_raw().strip() or "(boş)", notes=listing,
                                   since=datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M"),
                                   signal="\n\n".join(signal) or "(yeni bir şey yok)")
        data = parse_json(self._synth(prompt))
        if not data:
            raise ValueError("Dream cevabı boş")
        done = self.apply_notes(data, source="dream", max_save=15, max_delete=max(2, len(notes) // 4), book=b)
        index = data.get("index")
        if isinstance(index, str) and index.strip():
            try:
                b.write_index(index)
                done.append("dizin yeniden düzenlendi")
            except full as e:
                b.repair_index()
                done.append(f"yeni dizin sığmadı ({e})"[:160])
        elif b.repair_index():
            done.append("dizin onarıldı")
        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            b.append_log(f"Dream: {summary.strip()}")
            done.insert(0, summary.strip())
        if backup is not None and done:
            done.append(f"yedek: {backup.name}")
        return [f"{b.scope}: {x}" for x in done]

    # Old names (the core and the UI called these).
    def maybe_consolidate(self, every_hours: float | None = None) -> bool:
        return self.maybe_dream()

    def consolidate(self) -> list[str]:
        return self.dream(force=True)

    def forget_session(self, sid: str, users: list[str] | None = None, start: float | None = None,
                       end: float | None = None) -> dict[str, int]:
        """Traces of a deleted conversation in memory: its turns (with their vectors) and only the summaries that
        cover nothing but those turns.

        Turns saved with the conversation id are found by that id. For older turns from before ids were added, the
        conversation's time range (start..end) and the user's messages are matched; both must hold, so that another
        conversation's turn is not deleted. Even after the conversation was deleted, ALICE kept recalling it from
        "Son konuşmalar" (Recent conversations)."""
        ids: set[int] = set()
        with self._lock:
            if sid:
                ids |= {r[0] for r in self.db.execute("SELECT id FROM episodes WHERE sid = ?", (str(sid),))}
            wanted = {fold(" ".join(clean_user_text(u).split()))[:2000] for u in users or [] if u and u.strip()}
            if wanted and start is not None and end is not None:
                for eid, user in self.db.execute("SELECT id, user FROM episodes WHERE sid = '' AND ts BETWEEN ? AND ?",
                                                 (start - 120, end + 120)):
                    if fold(" ".join(user.split())) in wanted:
                        ids.add(eid)
            if not ids:
                return {"episodes": 0, "summaries": 0}
            marks = ",".join("?" * len(ids))
            lo, hi = self.db.execute(f"SELECT MIN(ts), MAX(ts) FROM episodes WHERE id IN ({marks})", tuple(ids)).fetchone()
            self.db.execute(f"DELETE FROM episodes WHERE id IN ({marks})", tuple(ids))
            self.db.execute(f"DELETE FROM vectors WHERE kind = 'episode' AND ref IN ({marks})", tuple(ids))
            # A summary covers a whole conversation; if no other turn is left in its range, it goes too.
            gone_sums = []
            for s_id, s_start, s_end in self.db.execute("SELECT id, start, end FROM summaries WHERE end >= ? AND "
                                                        "start <= ?", (lo - 1, hi + 1)).fetchall():
                left = self.db.execute("SELECT COUNT(*) FROM episodes WHERE ts BETWEEN ? AND ?",
                                       (s_start - 1, s_end + 1)).fetchone()[0]
                if not left:
                    gone_sums.append(s_id)
            for s_id in gone_sums:
                self.db.execute("DELETE FROM summaries WHERE id = ?", (s_id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (s_id,))
            self.db.commit()
        self._bump()
        return {"episodes": len(ids), "summaries": len(gone_sums)}

    def history_counts(self) -> dict[str, int]:
        with self._lock:
            return {"episodes": int(self.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]),
                    "summaries": int(self.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0])}

    def forget_history(self) -> dict[str, int]:
        """The whole conversation history (turns, summaries, vectors). Notes and follow-ups stay."""
        with self._lock:
            n_eps = self.db.execute("DELETE FROM episodes").rowcount
            n_sums = self.db.execute("DELETE FROM summaries").rowcount
            self.db.execute("DELETE FROM vectors WHERE kind IN ('episode', 'summary')")
            self.db.commit()
        self._bump()
        return {"episodes": n_eps, "summaries": n_sums}

    def forget_episode(self, eid: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM episodes WHERE id = ?", (int(eid),))
            self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref = ?", (int(eid),))
            self.db.commit()
        if cur.rowcount:
            self._bump()
        return bool(cur.rowcount)

    def forget_summary(self, sid: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM summaries WHERE id = ?", (int(sid),))
            self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (int(sid),))
            self.db.commit()
            if cur.rowcount:
                self._bump()
        return bool(cur.rowcount)

    def _synth(self, prompt: str) -> str:
        """Dream: the strong model; if it is missing or errors, the fast model."""
        if self.synthesizer is not None:
            try:
                out = self.synthesizer(prompt)
                if parse_json(out):
                    return out
            except Exception:  # noqa: BLE001 - if the brain chain fails, try the fast model
                log.debug("güçlü model toparlayamadı", exc_info=True)
        return self.extractor(prompt) if self.extractor else ""

    def _source(self, fid: int) -> str:
        with self._lock:
            row = self.db.execute("SELECT source FROM facts WHERE id = ?", (int(fid),)).fetchone()
        return row[0] if row else ""


def _scratch_dir() -> Path:
    """Notes of the temporary (":memory:") memory: under the ALICE home (tests each get their own temp home)."""
    d = alice_home() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def age_text_iso(ts: float) -> str:
    from alice.memdir import age_text

    return f"{datetime.fromtimestamp(ts):%Y-%m-%d}, {age_text(ts)}"


def project_label(cwd: Path | str | None) -> str:
    from alice.memdir import project_root

    root = project_root(cwd) if cwd else None
    return root.name if root else "?"


# Conversation summary: the conversation log (journal) itself. Lasting notes do not come from here but from the
# end-of-turn extraction and Dream. Dream reads these and distills what recurs in several conversations into notes.
DIGEST_PROMPT = """You keep the conversation log of ALICE, a personal assistant (like Jarvis) for one user. Below is \
one finished conversation (it started {date}). Produce:
1. "summary": 1-3 short Turkish sentences: what was talked about, what got done or decided, what is left. Concrete \
(names, projects, numbers, dates). Write only what happened: never sentences like "Hiçbir işlem yapılmadı", "karar \
alınmadı", "devam eden iş kalmadı". Empty string if it was only trivial commands, small talk, testing ALICE's \
features, or misheard noise (fragments that make no sense, other languages). ALICE replies starting with \
[doğrulanmadı] are claims no tool confirmed: do not report them as done or as confirmed by the user.
2. "followups": things a good assistant should bring up later. kind "ask": an upcoming event in the user's life to \
ask about afterwards (exam, match, trip, interview) with "due" = the day after it happens; kind "remind": something \
the user asked to be reminded of on a day; kind "open": only when the user said they will continue something later \
(no due). A command that failed or a task ALICE was doing is NOT a follow-up. Turkish, written as an instruction to \
ALICE ("Matematik sınavının nasıl geçtiğini sor."). Usually zero.
3. "done": ids of the open follow-ups below that this conversation already resolved.
Dates are YYYY-MM-DD; resolve "yarın", "cuma" etc. relative to the conversation date.
Open follow-ups:
{open}
Answer ONLY with JSON: {{"summary": "...", "followups": [{{"text": "...", "kind": "ask", "due": "YYYY-MM-DD"}}], \
"done": []}}

Conversation:
{transcript}"""


# Filler sentences the summary model adds to every summary ("Hiçbir işlem tamamlanmadı veya karar alınmadı."):
# they padded search and the profile but said nothing.
_FILLER_SENTENCE = re.compile(
    r"[^.!?;]*\b(?:hiçbir|herhangi bir) (?:işlem|karar|iş|eylem|şey)\w*[^.!?;]*?(?:yapılmadı|alınmadı|gerçekleş\w*|"
    r"tamamlanmadı|kalmadı|yok)\w*[^.!?;]*[.!?;]?|[^.!?;]*\bdevam eden (?:bir )?(?:iş|konu) (?:kalmadı|yok)[^.!?;]*[.!?;]?|"
    r"[^.!?;]*\b(?:henüz )?(?:yanıt|cevap) (?:vermedi|alınmadı|verilmedi)[^.!?;]*[.!?;]?", re.I)


def strip_filler(text: str) -> str:
    out = re.sub(r"\s{2,}", " ", _FILLER_SENTENCE.sub("", text)).strip(" ;,")
    return out + "." if out and out[-1] not in ".!?" else out


def _parse_day(value: Any) -> float | None:
    """'2026-10-23' -> 09:00 (local time) on that day; None if invalid."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").replace(hour=9).timestamp()
    except ValueError:
        return None


def _day_start(value: Any) -> float | None:
    """'2026-10-23' -> start of that day (00:00, local time); None if empty or invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").timestamp()
    except ValueError:
        return None


def parse_json(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _worth_asking(text: str, notes: list[Any]) -> bool:
    """Worth asking the model about note selection? 3+ meaningful words, or a word shared with a note."""
    q = set(terms(text))
    if len(q) >= 3:
        return True
    if not q:
        return False
    for n in notes:
        if q & set(terms(f"{n.title} {n.name.replace('-', ' ')} {n.description} {n.body[:600]}")):
            return True
    return False


def make_extractor(options: dict[str, Any], router, role: str | None = None) -> Callable[[str], str] | None:
    """Function using the existing free model chain (default "fast" role) for information extraction."""
    if router is None or not options.get("auto_extract", True):
        return None
    role = role or str(options.get("extract_role", "fast"))

    def extract(prompt: str) -> str:
        msgs = [{"role": "user", "content": prompt}]
        try:
            res = router.chat(role, msgs, temperature=0, max_tokens=1600, response_format={"type": "json_object"},
                              cache={"mode": "none"})
        except Exception:  # noqa: BLE001 - some models reject response_format
            res = router.chat(role, msgs, temperature=0, max_tokens=1600, cache={"mode": "none"})
        return res.message.get("content") or ""

    return extract


_shared: dict[str, LongMemory] = {}
_shared_lock = threading.Lock()


def shared_memory(cfg=None, router=None) -> LongMemory | None:
    """One memory object per process (the tool, the agent and Live share the same connection)."""
    key = str(alice_home())
    with _shared_lock:
        mem = _shared.get(key)
        if mem is None:
            mem = LongMemory.open(cfg, router)
            if mem is None:
                return None
            _shared[key] = mem
        elif mem.extractor is None and router is not None and mem.auto_extract:
            o = dict((cfg.data.get("memory") if cfg is not None else None) or {})
            mem.extractor = make_extractor(o, router)
            mem.synthesizer = make_extractor(o, router, str(o.get("synthesis_role", "brain")))
        return mem


def attach_memory(agent, channel: str) -> Any:
    """Attach memory to an interactive agent: profile enters the system prompt, every turn is recorded and learned."""
    try:
        mem = shared_memory(agent.config, agent.router)
    except Exception:  # noqa: BLE001 - a corrupted memory file must not stop ALICE from starting
        mem = None
    agent.memory, agent.memory_channel = mem, channel
    if mem is not None:
        agent.ctx.state["memory"] = mem
        agent.refresh_memory(force=True)
        mem.maybe_digest()  # if yesterday's conversation is still not summarized, do it in the background
        mem.index_soon()
    return mem


def profile_text(home: Path | None = None, cwd: Path | str | None = None) -> str:
    """Profile for the system prompt (empty if there is no memory file; does not create one)."""
    home = home or alice_home()
    if not any((home / x).exists() for x in ("memory.db", "memory.md", "memory")):
        return ""
    mem = shared_memory()
    return mem.profile(cwd=cwd) if mem is not None else ""
