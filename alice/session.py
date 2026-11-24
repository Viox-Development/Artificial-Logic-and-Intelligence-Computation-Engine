from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.providers.router import strip_images


def sessions_dir() -> Path:
    d = alice_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


_issued: set[str] = set()
_issued_lock = threading.Lock()


def new_session_id() -> str:
    """Timestamp to the second. Chats opened in the same second (one running in the background while a new one
    starts) get -2, -3 appended when needed, so they don't write to the same file."""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    with _issued_lock:
        sid, n = base, 1
        while sid in _issued or (sessions_dir() / f"{sid}.json").exists():
            n += 1
            sid = f"{base}-{n}"
        _issued.add(sid)
    return sid


def save(session_id: str, messages: list[dict[str, Any]], cwd: Path, extra: dict[str, Any] | None = None) -> Path:
    """extra: chat info restored on reopen, besides the messages (statistics, usage breakdown, sub-agents, session
    start). These used to be reset when the app was closed and reopened."""
    path = sessions_dir() / f"{session_id}.json"
    # The agent thread saves the session at every step, and UI requests (stop, switch chat, summarize) save it from
    # their own threads. Writing to a shared .json.tmp at once mixed the file (a corrupted session is skipped on
    # startup, so the chat disappears) or the second replace raised FileNotFoundError/PermissionError. Saves are queued;
    # the snapshot is taken under the lock (so a stale state can't overwrite), and the temp file belongs to the writer.
    with _save_lock:
        # Images aren't written to disk, so session files stay small.
        body = {"cwd": str(cwd), "saved": datetime.now().isoformat(timespec="seconds"),
                "messages": strip_images(messages[1:])}
        if extra:
            body["extra"] = extra
        # Write to a temp file first, so a write interrupted mid-save doesn't corrupt the session.
        _atomic_write(path, json.dumps(body, ensure_ascii=False, indent=1))
    return path


_save_lock = threading.RLock()


def _atomic_write(path: Path, text: str) -> None:
    import os

    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass  # a leftover temp file is harmless; the error is raised below
        raise


ARCHIVE_TOOL_CHARS = 4000  # the archive keeps tool output this many chars (only for showing on screen)


def archive_path(session_id: str) -> Path:
    # .jsonl: not matched by the "*.json" scans that list sessions.
    return sessions_dir() / f"{session_id}.archive.jsonl"


def archive(session_id: str, messages: list[dict[str, Any]]) -> None:
    """Add the messages that drop out of context during summarization to the session archive. The model no longer
    sees them (it sees their summary), but they stay on the chat screen: before, once summarized, all old messages
    were deleted from the screen and the session file."""
    if not messages:
        return
    lines = []
    for m in strip_images(messages):
        m = {k: v for k, v in m.items() if k != "reasoning_content"}
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > ARCHIVE_TOOL_CHARS:
            m["content"] = m["content"][:ARCHIVE_TOOL_CHARS] + "\n…(shortened)"
        lines.append(json.dumps(m, ensure_ascii=False))
    path = archive_path(session_id)
    block = ("\n".join(lines) + "\n").encode("utf-8")
    # If the same messages come a second time (session reloaded after summarizing, before it was saved), don't write
    # them again: the archive already ends with them. On 11 November 164 messages were written twice and the chat
    # repeated on screen. Only the file's end is read: the archive can be megabytes, reading all of it on every
    # summarization is wasteful.
    if _ends_with(path, block):
        return
    with open(path, "ab") as f:
        f.write(block)


def _ends_with(path: Path, block: bytes) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size < len(block):
                return False
            start = size - len(block)
            f.seek(max(0, start - 1))
            tail = f.read()
    except OSError:
        return False
    if start == 0:
        return tail == block
    return tail[0:1] == b"\n" and tail[1:] == block  # must start at a line boundary


def load_archive(session_id: str) -> list[dict[str, Any]]:
    try:
        text = archive_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:  # half-written last line
            continue
        if isinstance(m, dict):
            out.append(m)
    return out


# Marker messages inside a conversation. They are English (the model reads them); chats saved before that hold the
# Turkish wording, so both are recognized everywhere a marker is looked for.
SUMMARY_HEAD = "[Summary of the earlier conversation]"
OLD_SUMMARY_HEAD = "[Önceki konuşmanın özeti]"
NUDGE_PREFIX = "[continuation check] "
OLD_NUDGE_PREFIX = "[devam kontrolü] "
SHOT_REMOVED = "[old screenshot removed to save space]"
OLD_SHOT_REMOVED = "[eski ekran görüntüsü yer kazanmak için kaldırıldı]"
PLAN_NOTE = ("[PLAN MODE: Right now you can only read and research; tools that change files, run commands or touch the "
             "screen will be refused. Study the task, then write a clear step-by-step plan and wait for the user's "
             "approval.]\n\n")
OLD_PLAN_NOTE = ("[PLAN MODU: Şu an sadece okuyup araştırabilirsin; dosya değiştiren, komut çalıştıran ya da ekrana "
                 "dokunan araçlar reddedilecek. İşi incele, sonra adım adım net bir uygulama planı yaz ve kullanıcının "
                 "onayını bekle.]\n\n")
STOPPED = "stopped (the user interrupted)"
OLD_STOPPED = "durduruldu (kullanıcı kesti)"
ACK_SUMMARY = "Got the summary, carrying on from where I was."
OLD_ACK_SUMMARY = "Özeti aldım, kaldığım yerden devam ediyorum."


def strip_plan_note(text: str) -> str:
    """The user's own text without the plan-mode note put in front of it (new or old wording)."""
    for note in (PLAN_NOTE, OLD_PLAN_NOTE):
        if text.startswith(note):
            return text[len(note):]
    return text


def is_stopped(text: Any) -> bool:
    """A tool result that says the user interrupted it (new or old wording)."""
    return isinstance(text, str) and (STOPPED in text or OLD_STOPPED in text)
_NOT_A_PROMPT = (SUMMARY_HEAD, OLD_SUMMARY_HEAD, NUDGE_PREFIX.strip(), OLD_NUDGE_PREFIX.strip(), "[old screenshot",
                 "[eski ekran görüntüsü")


def is_summary(text: Any) -> bool:
    """A summary message that replaced the older part of a conversation (new or old wording)."""
    return isinstance(text, str) and text.startswith((SUMMARY_HEAD, OLD_SUMMARY_HEAD))


def summary_body(text: str) -> str:
    """The summary text without its heading."""
    for head in (SUMMARY_HEAD, OLD_SUMMARY_HEAD):
        if text.startswith(head):
            return text[len(head):].strip()
    return text


def is_removed_screenshot(text: Any) -> bool:
    return isinstance(text, str) and text.startswith(("[old screenshot", "[eski ekran görüntüsü"))


def first_prompt(messages: list[dict[str, Any]]) -> str:
    """The first message the user actually wrote (summaries, reminders and screenshot notes are skipped)."""
    for m in messages:
        if m.get("role") != "user" or m.get("_nudge") or m.get("_image"):
            continue
        c = m.get("_display") or m.get("content")
        if isinstance(c, list):
            c = next((p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"), "")
        if isinstance(c, str) and c.strip() and not c.lstrip().startswith(_NOT_A_PROMPT):
            return c.strip()
    return ""


def _first_archived(session_id: str) -> str:
    """The archive's first user message. Looks at the first lines only, not the whole file (it can be megabytes)."""
    try:
        with open(archive_path(session_id), encoding="utf-8") as f:
            head = []
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                try:
                    head.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return ""
    return first_prompt([m for m in head if isinstance(m, dict)])


def session_prompt(session_id: str, messages: list[dict[str, Any]]) -> str:
    """The chat's first request. In a summarized chat the session file's first message is the summary; the real first
    message is in the archive. Titles used to be cut from the summary ("[Summary of the earlier conversation] <tool calls>…")."""
    return _first_archived(session_id) or first_prompt(messages)


# list_sessions cache: path -> ((mtime/size of the file and its archive), (cwd, first request)). The sidebar re-read
# and parsed the 60 chat files on every open (~600 ms on a cold start); an unchanged file is no longer re-read.
_list_cache: dict[str, tuple[tuple[int, ...], tuple[str, str]]] = {}
_list_lock = threading.Lock()


def _file_sig(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def list_sessions(limit: int = 15) -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(sessions_dir().glob("*.json"), reverse=True)[:limit]:
        sig = _file_sig(p) + _file_sig(archive_path(p.stem))
        with _list_lock:
            hit = _list_cache.get(str(p))
        if hit is not None and hit[0] == sig:
            cwd, first = hit[1]
        else:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("messages", []), list):
                continue
            cwd, first = str(data.get("cwd", "")), session_prompt(p.stem, data.get("messages", []))
            with _list_lock:
                _list_cache[str(p)] = (sig, (cwd, first))
        out.append((p.stem, cwd, first[:70].replace("\n", " ")))
    return out


def load(session_id: str | None = None) -> tuple[str, list[dict[str, Any]]] | None:
    files = sorted(sessions_dir().glob("*.json"), reverse=True)
    if session_id:
        files = [f for f in files if f.stem == session_id]
    # A corrupt or half-written file (also skipped by list_sessions) must not break resuming: go to the next one.
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            continue  # valid JSON but not in session format (e.g. a list or number): skip it like a corrupt file
        return f.stem, [m for m in messages if isinstance(m, dict)]
    return None


def load_extra(session_id: str) -> dict[str, Any]:
    try:
        data = json.loads((sessions_dir() / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    extra = data.get("extra")
    return extra if isinstance(extra, dict) else {}


# Original versions of files for the Undo and Changes tabs. Kept apart from the session file: they can be large, and
# are written only when they change. The suffix isn't .json, so the "*.json" session scan skips it.
CHECKPOINT_FILE_MAX = 2_000_000  # larger files aren't kept in original form (can't be restored on reopen)
CHECKPOINT_TOTAL_MAX = 40_000_000


def checkpoints_path(session_id: str) -> Path:
    return sessions_dir() / f"{session_id}.checkpoints"


def save_checkpoints(session_id: str, checkpoints: dict[int, dict[str, bytes | None]]) -> None:
    import base64

    out: dict[str, dict[str, str | None]] = {}
    total = 0
    for idx in sorted(list(checkpoints)):  # sub-agents may write at the same time: iterate over copies
        row = {}
        for path, data in list(checkpoints[idx].items()):
            if data is not None and (len(data) > CHECKPOINT_FILE_MAX or total + len(data) > CHECKPOINT_TOTAL_MAX):
                continue
            total += len(data or b"")
            row[path] = None if data is None else base64.b64encode(data).decode("ascii")
        if row:
            out[str(idx)] = row
    with _save_lock:
        _atomic_write(checkpoints_path(session_id), json.dumps(out))


def load_checkpoints(session_id: str) -> dict[int, dict[str, bytes | None]]:
    import base64

    try:
        raw = json.loads(checkpoints_path(session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[int, dict[str, bytes | None]] = {}
    for k, row in raw.items():
        if not isinstance(row, dict):
            continue
        try:
            out[int(k)] = {p: (None if v is None else base64.b64decode(v)) for p, v in row.items()}
        except (ValueError, TypeError):
            continue
    return out


def saved_cwd(session_id: str) -> Path | None:
    """The working folder the session was saved in (if it still exists). A chat reopened from disk in the background
    (e.g. the PR watch's fix job) should run in its own repo, not in the app's current folder."""
    try:
        data = json.loads((sessions_dir() / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    cwd = Path(data.get("cwd") or "")
    return cwd if data.get("cwd") and cwd.is_dir() else None


# The user's name and pin for a chat. Kept separate because the session file is rewritten from scratch at every step:
# {"<id>": {"title": "...", "pinned": true}}
def _meta_path() -> Path:
    return alice_home() / "session_meta.json"


def load_meta() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_meta_lock = threading.Lock()


def set_meta(session_id: str, **fields: Any) -> dict[str, Any]:
    """Update the fields; an empty title or a False pin field removes it."""
    with _meta_lock:  # so the background title job and renaming from the UI don't overwrite each other
        return _set_meta(session_id, **fields)


TITLE_PROMPT = (
    "The message below is the first request of a chat. Write a short title to show in the chat list: 3-6 words "
    "about what the request is, in the language of the message, starting with a capital letter. No quotes, full "
    "stop, emoji or explanation; only the title.")


def make_title(router: Any, prompt: str) -> str:
    """Short title from the first request (fast model). On failure return "": the list shows the request's start."""
    import re

    text = " ".join(prompt.split())[:1500]
    if len(text) < 4:
        return ""
    try:
        res = router.chat("fast", [{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": text}],
                          max_tokens=200, temperature=0.2)
    except Exception:  # noqa: BLE001 - no model: stays untitled
        return ""
    lines = [ln.strip() for ln in (res.text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    title = re.sub(r"^(başlık|title)\s*[:：-]\s*", "", lines[-1], flags=re.I).strip(" \"'`*#.«»“”")
    if not 2 <= len(title) <= 70 or title.startswith(("<", "{", "[")) or len(title.split()) > 10:
        return ""
    return title


def ensure_title(router: Any, session_id: str, messages: list[dict[str, Any]]) -> str:
    """If the chat has no automatic title, generate one from the first request and store it (session_meta: auto_title).
    Made once: later summaries or growth don't change it. The user's own name (title) is kept separately."""
    row = load_meta().get(session_id) or {}
    if row.get("auto_title") or row.get("title"):
        return str(row.get("auto_title") or row.get("title"))
    prompt = session_prompt(session_id, messages)
    title = make_title(router, prompt) if prompt else ""
    if title:
        set_meta(session_id, auto_title=title)
    return title


def _set_meta(session_id: str, **fields: Any) -> dict[str, Any]:
    meta = load_meta()
    row = dict(meta.get(session_id) or {})
    for k, v in fields.items():
        if v in ("", None, False):
            row.pop(k, None)
        else:
            row[k] = v
    if row:
        meta[session_id] = row
    else:
        meta.pop(session_id, None)
    _meta_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _meta_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_meta_path())
    return row
