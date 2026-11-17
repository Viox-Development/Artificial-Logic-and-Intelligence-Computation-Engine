"""Local server for the ALICE desktop application.

The agent runs in a thread, events flow to the UI via Server-Sent Events, and UI
commands arrive via POST. It binds only to 127.0.0.1 and every request requires
a session-specific random token: because this server is connected to an agent with
full computer control, another site in the browser sending requests to it is unacceptable.
Because the token travels in a custom header, cross-site requests are blocked by
CORS preflight; Host header checks also prevent DNS rebinding.
"""

from __future__ import annotations

import base64
import json
import os
import re
import queue
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from alice import __version__
from alice import session as sess
from alice.agent import Agent, parse_args
from alice.config import Config, alice_home
from alice.decider import Decider
from alice.permissions import MODES, Permissions
from alice.providers.router import Router, parse_ref
from alice.tools import ToolContext, all_tools, attach_mcp, cleanup
from alice.tools.base import DesktopLock, ToolResult

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "text/javascript", "projects.js": "text/javascript", "ui.js": "text/javascript", "wizard.js": "text/javascript", "buddy.js": "text/javascript",
                "main.js": "text/javascript", "app.css": "text/css", "marked.min.js": "text/javascript",
                "purify.min.js": "text/javascript", "highlight.js": "text/javascript", "icon.svg": "image/svg+xml", "alice.png": "image/png", "quick.js": "text/javascript",
                "quick.css": "text/css", "m.css": "text/css", "m.js": "text/javascript",
                "Geist.woff2": "font/woff2", "GeistMono.woff2": "font/woff2"}
# media-src: "Listen" sample in System → Voice is played from a data: URL. Missing; default-src 'none' caused
# the browser was silently blocking audio.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "media-src 'self' data: blob:; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "frame-src 'self' blob:")
# frame-src blob: In file preview, a PDF opens in an iframe from a blob: URL; 'self' does not cover blob:, so the PDF
# preview stayed blank. Only the page itself can generate blob: URLs.
# The previewed site runs in a sandboxed frame (separate origin): it cannot touch ALICE's page or API.
PREVIEW_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals"
# Request body limit: sized to fit 6 screenshots (1600 px PNG, base64) or a long audio recording.
MAX_BODY = 32_000_000
# Unresponsive connection should not hold a thread (prevents clogging server with idle connections from LAN).
REQUEST_TIMEOUT = 60


def _same(given: str, expected: str) -> bool:
    """Constant-time key comparison. compare_digest raises TypeError on non-ASCII str; compare with bytes."""
    return secrets.compare_digest(given.encode("utf-8", "surrogateescape"), expected.encode("utf-8"))


class BadRequest(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def int_field(d: dict[str, Any], key: str, default: int = 0) -> int:
    """Integer field in request body; returns 400 (BadRequest) instead of 500 if not a number."""
    v = d.get(key)
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise BadRequest(400, f"{key} tamsayı olmalı")
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        raise BadRequest(400, f"{key} tamsayı olmalı") from None


# ----------------------------------------------------------------------- events
class EventBus:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


class AppUI:
    """Translates the Agent UI contract into event publishing. Permission prompts wait until an answer arrives.
    Events carry the chat ID (sid): prevents background chat streams from interfering with the foreground chat."""

    sid = ""

    def __init__(self, bus: EventBus, sid: str = ""):
        self.bus = bus
        self.sid = sid
        self.pending: dict[str, dict[str, Any]] = {}
        self._answers: dict[str, str] = {}
        self._events: dict[str, threading.Event] = {}
        self.agent: Agent | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.bus.publish({**event, "sid": self.sid} if self.sid else event)

    def stream_start(self) -> None:
        self.publish({"type": "stream_start"})

    def stream_delta(self, text: str) -> None:
        self.publish({"type": "delta", "text": text})

    def thinking_delta(self, text: str) -> None:
        self.publish({"type": "thinking", "text": text})

    def stream_reset(self) -> None:
        self.publish({"type": "stream_reset"})

    def stream_end(self, text: str, source: str) -> None:
        self.publish({"type": "stream_end", "text": text, "source": source})

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.publish({"type": "tool_start", "id": call_id, "name": name, "summary": summary})

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        self.publish({"type": "tool_end", "id": call_id, "name": name, "text": result.text[:20000],
                          "is_error": result.is_error, "diff": result.diff, "images": result.images[:4],
                      "files": getattr(result, "files", [])[:10], "card": getattr(result, "card", {}) or {}})

    def ask_permission(self, name: str, summary: str, risk: str, cancelled: Any = None) -> str:
        """cancelled: stop callable that dismisses the prompt (sub-agent's own Stop); defaults to this chat's agent."""
        pid = secrets.token_hex(6)
        ev = threading.Event()
        self._events[pid] = ev
        self.pending[pid] = {"type": "permission", "id": pid, "name": name, "summary": summary, "risk": risk}
        self.publish(self.pending[pid])
        stop = cancelled or (lambda: self.agent is not None and self.agent.cancel.is_set())
        while not ev.wait(0.25):
            if stop():
                self._answers[pid] = "n"
                break
        self.pending.pop(pid, None)
        self._events.pop(pid, None)
        ans = self._answers.pop(pid, "n")
        self.publish({"type": "permission_done", "id": pid, "answer": ans})
        return ans

    def answer(self, pid: str, ans: str) -> bool:
        ev = self._events.get(pid)
        if ev is None:
            return False
        self._answers[pid] = ans if ans in ("y", "n", "a", "p") else "n"
        ev.set()
        return True

    def ask_choice(self, question: str, options: list[str], multi: bool = False) -> str:
        """Multiple choice question card; answer arrives via /api/choice (empty = cancelled)."""
        pid = secrets.token_hex(6)
        ev = threading.Event()
        self._events[pid] = ev
        self.pending[pid] = {"type": "ask_choice", "id": pid, "question": question, "options": options,
                             "multi": multi}
        self.publish(self.pending[pid])
        while not ev.wait(0.25):
            if self.agent is not None and self.agent.cancel.is_set():
                self._answers[pid] = ""
                break
        self.pending.pop(pid, None)
        self._events.pop(pid, None)
        ans = self._answers.pop(pid, "")
        self.publish({"type": "ask_choice_done", "id": pid, "answer": ans})
        return ans

    def answer_choice(self, pid: str, ans: str) -> bool:
        ev = self._events.get(pid)
        if ev is None:
            return False
        self._answers[pid] = str(ans)[:2000]
        ev.set()
        return True

    def info(self, msg: str) -> None:
        self.publish({"type": "info", "text": msg})


def _summary(tools: dict[str, Any], name: str, raw: str) -> str:
    tool = tools.get(name)
    if tool is not None:
        try:
            return tool.summary(parse_args(raw))
        except Exception:  # noqa: BLE001 - invalid argument: show raw text
            pass
    return raw if len(raw) < 160 else raw[:157] + "..."


def _user_text(m: dict[str, Any]) -> str:
    if m.get("_display"):
        return m["_display"]
    c = m.get("content")
    if isinstance(c, list):
        return next((p.get("text", "") for p in c if p.get("type") == "text"), "")
    return c or ""


def display_history(messages: list[dict[str, Any]], tools: dict[str, Any] | None = None,
                    archive: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Convert message history into items that the UI can render. User messages carry their indices
    in the agent history (mi) for rewinding.

    archive: old messages evicted from context by summarization (see session.archive). Prepended to the front;
    they have no index because they are not in agent history (cannot be rewound)."""
    from alice.agent import PLAN_NOTE

    items: list[dict[str, Any]] = []
    if archive:
        items = [{k: v for k, v in it.items() if k != "mi"} for it in display_history([{"role": "system"}, *archive], tools)]
    results = {m.get("tool_call_id"): m.get("content", "") for m in messages if m.get("role") == "tool"}
    extras = {m.get("tool_call_id"): m for m in messages if m.get("role") == "tool" and (m.get("_files") or m.get("_card"))}
    if extras:  # allow files displayed in a reopened chat to still be opened / previewed
        from alice.tools.meta import SENT_FILES

        for m in extras.values():
            SENT_FILES.update(str(f.get("path")) for f in m.get("_files") or [] if f.get("path"))
    for i, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            if m.get("_image") or m.get("_nudge"):
                continue
            text = _user_text(m)
            if text.startswith("[Önceki konuşmanın özeti]"):
                items.append({"type": "info", "text": "buradan öncesi özetlendi: ALICE yukarıdakileri özet olarak "
                              "hatırlıyor" if items else "önceki konuşma özetlendi"})
            elif not text.startswith("[eski ekran görüntüsü"):
                if text.startswith(PLAN_NOTE):
                    text = text[len(PLAN_NOTE):]
                # A message inserted mid-turn while running has no rewind point: no index is assigned.
                items.append({"type": "user", "text": text, "mi": None if m.get("_steer") else i,
                              "n_images": m.get("_user_images", 0)})
        elif role == "assistant":
            if content and content != "Özeti aldım, kaldığım yerden devam ediyorum.":
                items.append({"type": "assistant", "text": content})
            for tc in m.get("tool_calls") or []:
                res = results.get(tc["id"], "")
                args = tc["function"].get("arguments", "")
                items.append({"type": "tool", "id": tc["id"], "name": tc["function"]["name"],
                              "summary": _summary(tools or {}, tc["function"]["name"], args),
                              "text": res, "is_error": res.startswith("HATA:"),
                              "files": (extras.get(tc["id"]) or {}).get("_files") or [],
                              "card": (extras.get(tc["id"]) or {}).get("_card") or {}})
    return items


PREVIEW_TEXT = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".log": "text", ".csv": "csv",
                ".tsv": "csv", ".json": "code", ".py": "code", ".js": "code", ".ts": "code", ".html": "html",
                ".htm": "html", ".css": "code", ".toml": "code", ".yaml": "code", ".yml": "code", ".xml": "code",
                ".sh": "code", ".ps1": "code", ".sql": "code", ".ini": "code", ".cfg": "code"}
PREVIEW_IMAGE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                 ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp"}
PREVIEW_MAX = 400_000  # bytes: the head of larger text files is displayed


def file_preview(path: str, allowed: set[str]) -> dict[str, Any]:
    """Preview of a file displayed to the user: text (markdown/code/csv/html) or image/PDF (base64)."""
    import base64

    p = Path(path)
    if path not in allowed or not p.is_file():
        return {"ok": False, "error": "dosya bulunamadı"}
    ext = p.suffix.lower()
    size = p.stat().st_size
    if ext in PREVIEW_IMAGE or ext == ".pdf":
        if size > 15_000_000:
            return {"ok": False, "error": "dosya önizleme için çok büyük"}
        return {"ok": True, "kind": "pdf" if ext == ".pdf" else "image", "name": p.name,
                "mime": "application/pdf" if ext == ".pdf" else PREVIEW_IMAGE[ext],
                "b64": base64.b64encode(p.read_bytes()).decode()}
    kind = PREVIEW_TEXT.get(ext)
    if kind is None:
        with open(p, "rb") as f:
            head = f.read(4096)
        if b"\0" in head:
            return {"ok": False, "error": "bu dosya türü önizlenemiyor; Aç ile aç"}
        kind = "text"
    with open(p, "rb") as f:
        data = f.read(PREVIEW_MAX)
    return {"ok": True, "kind": kind, "name": p.name, "lang": ext.lstrip("."), "truncated": size > PREVIEW_MAX,
            "text": data.decode("utf-8", errors="replace")}


def git_branch(cwd: Path) -> str:
    """Working directory's git branch (for the status bar). Reads .git/HEAD without invoking git."""
    for d in (cwd, *cwd.parents):
        g = d / ".git"
        try:
            if g.is_file():  # worktree / submodule: "gitdir: ..."
                line = g.read_text(encoding="utf-8").strip()
                if not line.startswith("gitdir:"):
                    return ""
                g = (d / line[7:].strip()).resolve()
            if g.is_dir():
                head = (g / "HEAD").read_text(encoding="utf-8").strip()
                return head[16:] if head.startswith("ref: refs/heads/") else head[:7]
        except OSError:
            return ""
    return ""


def _text_of(data: bytes | None) -> str | None:
    if data is None:
        return None
    if b"\0" in data[:4096]:
        return None
    for enc in ("utf-8", "cp1254"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def session_changes(checkpoints: dict[int, dict[str, bytes | None]], cwd: Path) -> list[dict[str, Any]]:
    """Files modified by file tools in this session: diff between their initial state and current state.
    Shows all changes in one place, like the review panel in Codex."""
    import difflib

    original: dict[str, bytes | None] = {}
    for idx in sorted(checkpoints):
        for path, data in checkpoints[idx].items():
            original.setdefault(path, data)
    out: list[dict[str, Any]] = []
    for path, old in original.items():
        p = Path(path)
        try:
            new = p.read_bytes() if p.is_file() else None
        except OSError:
            continue
        if new == old:
            continue
        status = "eklendi" if old is None else "silindi" if new is None else "değişti"
        try:
            rel = str(p.relative_to(cwd))
        except ValueError:
            rel = str(p)
        rel = rel.replace("\\", "/")
        a, b = _text_of(old), _text_of(new)
        if (old is not None and a is None) or (new is not None and b is None):
            out.append({"path": rel, "abs": path, "status": status, "diff": "", "added": 0, "removed": 0, "binary": True})
            continue
        lines = list(difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(),
                                          f"a/{rel}", f"b/{rel}", lineterm="", n=3))
        added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        out.append({"path": rel, "abs": path, "status": status, "diff": "\n".join(lines[:4000]),
                    "added": added, "removed": removed, "binary": False})
    out.sort(key=lambda c: c["path"])
    return out


def revert_change(checkpoints: dict[int, dict[str, bytes | None]], path: str) -> bool:
    """Revert a single file to its initial state in the session (per-file "undo" like in Codex)."""
    for idx in sorted(checkpoints):
        if path in checkpoints[idx]:
            data = checkpoints[idx][path]
            p = Path(path)
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            return True
    return False


# ------------------------------------------------------------------------ state
class Chat:
    """A chat: its own agent, tool context, and worker thread. Enables switching to another chat and assigning work
    there while one chat is running: the remaining chat continues in the background, is saved and closed when done.

    Thread-bound objects (Playwright browser, UI Automation) remain in the chat's context and its own thread; spawning
    a new thread for every message would break them. Tool objects are stateless (state is in context) and shared across chats.
    Tools that drive the screen/browser are used sequentially across chats (see DesktopLock)."""

    def __init__(self, app: "AppState", cwd: Path, mode: str, sid: str = "",
                 messages: list[dict[str, Any]] | None = None):
        from alice.memory import attach_memory

        self.app = app
        self.sid = sid or sess.new_session_id()
        self.ui = AppUI(app.bus, self.sid)
        self.ctx = ToolContext(cwd=cwd, config=app.cfg, router=app.router, decider=app.decider, emit=self.ui.info)
        self.agent = Agent(app.cfg, app.router, list(app.tools), self.ctx, Permissions(mode), self.ui, app.decider)
        self.ui.agent = self.agent
        self.ctx.state["set_cwd"] = lambda path: app.set_cwd(path, self)  # github clone tool changes the folder
        self.ctx.state["publish"] = app.bus.publish  # live update for team panel
        self.ctx.state["teams"] = app.teams  # projects belong to all chats
        self.ctx.state["desktop_lock"] = app.desktop_lock
        self.ctx.state["session_id"] = self.sid  # allow PR watch events to return to the chat that wrote the code
        # Messages from background to this chat (monitor events, detached results) and starting detached sessions.
        self.ctx.state["deliver"] = lambda text: app.deliver(self.sid, text)
        self.ctx.state["deliver_to"] = app.deliver  # (chat, text): route subagent result to the initiating chat
        self.ctx.state["sessions"] = SessionsAPI(app, self)
        self.ctx.state["builtin_browser"] = app.builtin_browser  # used by browser tool and preview
        attach_memory(self.agent, "uygulama")
        self.started = time.time()  # session duration (status bar); first launch time in saved chat
        self._ckpt_sig = (0, 0)
        if messages is not None:
            self.agent.messages = self.agent.messages[:1] + messages
            self._restore_extra()
            try:  # in a reopened chat, consider recently read files as "read" again
                from alice.tools.files import restore_reads

                saved = (sess.sessions_dir() / f"{self.sid}.json").stat().st_mtime
                restore_reads(self.ctx, messages, saved)
            except Exception:  # noqa: BLE001 - if record cannot be restored, fallback to old behavior: must read first
                pass
        self.agent.on_step = self.save
        self.agent.on_compact = lambda old: sess.archive(self.sid, old)  # keep summarized messages on screen
        self.queued = False
        self.abandoned = False  # unstoppable job: thread abandoned, no longer saves or writes to the UI
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._work, daemon=True, name=f"alice-chat-{self.sid[-6:]}")
        self._thread.start()

    @property
    def busy(self) -> bool:
        return bool(self.agent.busy or self.queued)

    def set_sid(self, sid: str) -> None:
        self.sid = self.ui.sid = sid
        self.ctx.state["session_id"] = sid

    def save(self) -> None:
        if not self.abandoned and self.sid not in getattr(self.app, "deleted_sids", ()):
            from alice import subagents

            extra = {**self.agent.export_stats(), "started": self.started,
                     "subagents": subagents.export(self.ctx)}
            sess.save(self.sid, self.agent.messages, self.ctx.cwd, extra)
            cps = self.agent.checkpoints
            sig = (len(cps), sum(len(v) for v in cps.values()))
            if sig != self._ckpt_sig:  # initial file states are only written when changed
                try:
                    sess.save_checkpoints(self.sid, cps)
                    self._ckpt_sig = sig
                except OSError:
                    pass
            self._title_once()

    def switch_to(self, sid: str, messages: list[dict[str, Any]] | None = None) -> None:
        """Switch to another chat with the same agent (new or saved): do not carry over previous chat's statistics,
        usage breakdown, rewind checkpoints, or subagents to the new one."""
        self.agent.reset()
        self.agent.stats = {k: 0 for k in self.agent.stats}
        self.agent.usage_models, self.agent.usage_tools = {}, {}
        self.agent.checkpoints = {}
        for k in ("subagents", "subagent_ids"):
            self.ctx.state.pop(k, None)
        self.set_sid(sid)
        getattr(self.app, "deleted_sids", set()).discard(sid)
        self.started = time.time()
        self._ckpt_sig = (0, 0)
        if messages is not None:
            self.agent.messages = self.agent.messages[:1] + messages
            self._restore_extra()

    def _restore_extra(self) -> None:
        """When opening a saved chat: statistics, usage breakdown, subagents, rewind checkpoints, and start time."""
        from alice import subagents

        try:
            extra = sess.load_extra(self.sid)
            self.agent.import_stats(extra)
            self.started = float(extra.get("started") or self.started)
            subagents.restore(self.ctx, extra.get("subagents") or [])
            cps = sess.load_checkpoints(self.sid)
            if cps:
                self.agent.checkpoints.update(cps)
                self._ckpt_sig = (len(cps), sum(len(v) for v in cps.values()))
        except Exception:  # noqa: BLE001 - missing information should not prevent opening the chat
            pass

    def _title_once(self) -> None:
        """Generate a title for the chat from the first request on initial save (in the background, once)."""
        if getattr(self, "_titled", False) or not getattr(self.app.router, "_real_clients", False):
            return
        self._titled = True
        sid, msgs = self.sid, list(self.agent.messages[1:6])

        def work() -> None:
            try:
                sess.ensure_title(self.app.router, sid, msgs)  # list refreshes after turn finishes, title appears there
            except Exception:  # noqa: BLE001 - failed to generate title: list displays the beginning of the prompt
                pass

        threading.Thread(target=work, daemon=True, name="alice-title").start()

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, done = job
            try:
                fn()
            except Exception:  # noqa: BLE001
                log_error()
            finally:
                if done is not None:
                    done.set()

    def run(self, fn, wait: float | None = None) -> None:
        done = threading.Event()
        self._jobs.put((fn, done))
        if wait is not None:
            done.wait(wait)

    def close(self, wait: float | None = None) -> None:
        """Clean up tool resources (browser connections, background commands) in its own thread, terminate thread."""
        self.run(lambda: cleanup(self.ctx), wait=wait)
        self._jobs.put(None)


def fork_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """History to pass to a detached session: excluding the system message, up to the last unhandled tool call
    if present (the call that initiated the session). Unanswered tool_calls cause provider requests to fail with 400."""
    rows = [dict(m) for m in messages[1:]]
    answered = {m.get("tool_call_id") for m in rows if m.get("role") == "tool"}
    for i in range(len(rows) - 1, -1, -1):
        m = rows[i]
        if m.get("role") == "assistant" and m.get("tool_calls") and \
                any(tc.get("id") not in answered for tc in m["tool_calls"]):
            rows = rows[:i]
            break
    return rows


class SessionsAPI:
    """Detached sessions initiated by a chat (session tool)."""

    def __init__(self, app: "AppState", chat: "Chat"):
        self.app, self.chat = app, chat

    def start(self, prompt: str, title: str, fork: bool) -> tuple[str, str]:
        app, parent = self.app, self.chat
        with app._chats_lock:
            if len(app.running()) >= app.MAX_BACKGROUND:
                return "", f"arkada zaten {app.MAX_BACKGROUND} sohbet çalışıyor; biri bitince tekrar dene"
            sid = sess.new_session_id()
            msgs = fork_messages(parent.agent.messages) if fork else None
            chat = Chat(app, parent.ctx.cwd, parent.agent.permissions.mode, sid=sid, messages=msgs)
            chat.ctx.state["report_to"] = parent.sid
            chat.ctx.state["report_title"] = title
            app.background[sid] = chat
        try:
            sess.set_meta(sid, title=title)
        except OSError:
            pass
        parent.ctx.state.setdefault("linked_sessions", []).append(sid)
        ok, err = app.send(prompt, sid=sid)
        if not ok:
            return "", err or "oturum başlatılamadı"
        app.bus.publish({"type": "sessions"})
        return sid, ""

    def linked(self) -> list[dict[str, str]]:
        meta = sess.load_meta()
        rows = []
        for sid in self.chat.ctx.state.get("linked_sessions", []):
            live = self.app.find_chat(sid)
            state = "çalışıyor" if live is not None and live.busy else "bitti/boşta"
            rows.append({"id": sid, "title": str((meta.get(sid) or {}).get("title") or ""), "state": state})
        return rows

    def message(self, sid: str, text: str) -> str:
        if sid not in self.chat.ctx.state.get("linked_sessions", []):
            return "bu sohbetten başlatılmış böyle bir oturum yok"
        ok, err = self.app.send(text, sid=sid)
        return "" if ok else (err or "gönderilemedi")


class AppState:
    MAX_BACKGROUND = 3  # number of chats that can run concurrently in the background
    STOP_GRACE = 10.0  # seconds to wait for job abandonment after Stop

    def __init__(self, cfg: Config, cwd: Path, mode: str, provider_factory=None):
        self.cfg = cfg
        self.bus = EventBus()
        self.router = Router(cfg, provider_factory=provider_factory,
                             on_event=lambda s: self.bus.publish({"type": "route", "text": s}))
        # Main model selected from menu persists across restarts: previously reverted to Automatic on every update.
        pin = str(cfg.agent.get("pinned_brain") or "")
        if pin.partition(":")[0] in self.router.providers_cfg and pin.partition(":")[2]:
            self.router.role_overrides["brain"] = [pin]
        self.decider = Decider.from_config(cfg, self.router)
        self.tools = all_tools(cfg, lambda s: self.bus.publish({"type": "info", "text": s}))
        self.teams: dict[str, Any] = {}
        self.desktop_lock = DesktopLock()
        self._mcp_servers: list[Any] = []
        self._chats_lock = threading.RLock()
        self.background: dict[str, Chat] = {}  # chats that continue running after navigating away from foreground
        # Deleted chats: prevent a Chat still in memory from saving them back to disk (a deleted chat used to reappear
        # on startup).
        self.deleted_sids: set[str] = set()
        self.chat = Chat(self, cwd, mode)
        self.token = secrets.token_urlsafe(24)
        # Preview URL key is separate: even if site code inspects it, it can only read project files.
        self.preview_token = secrets.token_urlsafe(18)
        self.allowed_hosts = {"127.0.0.1", "localhost"}  # LAN address is added in phone server
        self.watch_store = None  # phone server: watch data is written here
        self.quick_pending: dict[str, Any] | None = None  # text/image to fill when opening quick bar
        self.update: Any = None  # alice.updater.UpdateInfo: new version found at startup/in background
        self.restart_hook: Any = None  # when running inside host (alice/app/host.py), this is the host restart hook
        self._update_lock = threading.Lock()
        self._update_stop = threading.Event()  # stop background update loop on shutdown
        self._update_waits = (10.0, 60.0)  # initial check, subsequent rounds (s)
        self._stt = None
        # MCP servers (npx launched ones can be slow) should not block the UI. Servers belong to the app, not chat:
        # a closing chat should not terminate them; their tools should pass to subsequent chats as well.
        self.run_in_worker(self._attach_mcp)
        self._start_cliproxy()

    def _watch_quota(self) -> None:
        """Notify once if subscription quota is about to run out (alice/quota_watch.py). [cliproxy] quota_warn = false
        disables this."""
        try:
            if not bool((self.cfg.data.get("cliproxy", {}) or {}).get("quota_warn", True)):
                return
            if getattr(self, "_quota_watch", None) is None:
                from alice.quota_watch import QuotaWatch

                self._quota_watch = QuotaWatch(lambda s: self.bus.publish({"type": "info", "text": s}))
            self._quota_watch.maybe_check()
        except Exception:  # noqa: BLE001 - warning should not break turn completion
            pass

    def _start_cliproxy(self) -> None:
        """If local Claude Code proxy (CLIProxyAPI) is installed, automatically start it on launch."""
        try:
            cl = self.cfg.data.get("cliproxy", {}) or {}
            if not bool(cl.get("auto_start", True)):
                return
            from alice.app import cliproxy

            cliproxy.start_in_background(install=bool(cl.get("auto_install", True)),
                                         version=str(cl.get("version") or ""))
        except Exception:  # noqa: BLE001 - if proxy is missing, ALICE startup should not be affected
            pass

    def _attach_mcp(self) -> None:
        chat = self.chat
        before = set(chat.agent.tools)
        attach_mcp(chat.agent, chat.ctx, emit=lambda s: self.bus.publish({"type": "info", "text": s}))
        self._mcp_servers = chat.ctx.state.pop("mcp_servers", [])
        added = [t for n, t in chat.agent.tools.items() if n not in before]
        self.tools = self.tools + added
        with self._chats_lock:
            for c in [self.chat, *self.background.values()]:
                for t in added:
                    c.agent.tools.setdefault(t.name, t)

    # Foreground chat: endpoints and other modules (quick bar, phone) operate through these.
    @property
    def agent(self) -> Agent:
        return self.chat.agent

    @property
    def ctx(self) -> ToolContext:
        return self.chat.ctx

    @property
    def ui(self) -> AppUI:
        return self.chat.ui

    @property
    def sid(self) -> str:
        return self.chat.sid

    @property
    def _queued(self) -> Any:
        return self.chat.queued

    @_queued.setter
    def _queued(self, v: Any) -> None:
        self.chat.queued = v

    def run_in_worker(self, fn, wait: float | None = None) -> None:
        self.chat.run(fn, wait)

    def running(self) -> list[str]:
        """Chats still running in the background (marked in the sidebar)."""
        with self._chats_lock:
            return [sid for sid, c in self.background.items() if c.busy]

    # ------------------------------------------------------------------ chat by device
    # Desktop, quick bar, and phone share the same app state, but each can display its own chat. Previously
    # there was only a single "foreground chat": opening a new chat on phone switched the PC screen as well, and
    # switching chats on PC routed phone messages to it (Nov 7). If request carries a sid, work is done in that chat;
    # if different from foreground, it runs in the background without changing the PC screen.
    _SID_RX = re.compile(r"[\w-]{6,64}")

    def find_chat(self, sid: str) -> "Chat | None":
        with self._chats_lock:
            if sid == self.chat.sid:
                return self.chat
            return self.background.get(sid)

    def chat_for(self, sid: str) -> "Chat | None":
        """Live chat for sid; otherwise opens background chat from disk (or creates new). Returns None if no room."""
        if not self._SID_RX.fullmatch(sid or ""):
            return None
        with self._chats_lock:
            live = self.find_chat(sid)
            if live is not None:
                return live
            if len(self.running()) >= self.MAX_BACKGROUND:
                return None
            loaded = sess.load(sid)
            # A saved chat opens in its own directory: even if the user switched to another project, PR watch tasks
            # are executed in that chat's repository.
            cwd = (sess.saved_cwd(sid) if loaded else None) or self.ctx.cwd
            chat = Chat(self, cwd, self.agent.permissions.mode, sid=sid,
                        messages=loaded[1] if loaded else [])
            self.background[sid] = chat
            return chat

    def history_for(self, sid: str = "") -> list[dict[str, Any]]:
        """History to display: sid's live chat or record from disk; foreground chat if sid is not provided."""
        if not sid or sid == self.chat.sid:
            return display_history(self.agent.messages, self.agent.tools, sess.load_archive(self.chat.sid))
        live = self.find_chat(sid)
        if live is not None:
            return display_history(live.agent.messages, live.agent.tools, sess.load_archive(live.sid))
        loaded = sess.load(sid) if self._SID_RX.fullmatch(sid) else None
        if not loaded:
            return []
        return display_history([{"role": "system", "content": ""}, *loaded[1]], self.agent.tools,
                               sess.load_archive(loaded[0]))

    # ------------------------------------------------------------------ chat switching
    def _make_room(self) -> str | None:
        """Move foreground chat to background if running. Returns error string if no room."""
        if not self.chat.busy:
            return None
        if len(self.running()) >= self.MAX_BACKGROUND:
            return f"arkada zaten {self.MAX_BACKGROUND} sohbet çalışıyor; birini bitmeden yenisine geçilemez"
        self.background[self.chat.sid] = self.chat
        try:
            self.chat.save()  # show in sidebar even if its first step has not finished
        except OSError:
            pass
        return ""

    def _swap_in(self, chat: Chat, old_busy: bool) -> None:
        """Bring new chat to front. If old front is idle, close it; if running, _make_room moved it to background."""
        old = self.chat
        chat.agent.permissions.mode = old.agent.permissions.mode  # permission mode belongs to the user, not the chat
        self.chat = chat
        if not old_busy and old is not chat:
            old.close()

    def delete_session(self, sid: str) -> dict[str, Any]:
        """Delete chat from history: file, archive, rewind checkpoints, title, and traces in memory (turns,
        summaries). If memory is not cleared, ALICE continued to remember the deleted chat from "Recent chats"."""
        f = sess.sessions_dir() / f"{sid}.json"
        if not sid or f.parent != sess.sessions_dir() or not f.exists():
            return {"ok": True}
        users: list[str] = []
        start = end = None
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            msgs = sess.load_archive(sid) + list(raw.get("messages") or [])
            for m in msgs:
                if m.get("role") != "user" or m.get("_image") or m.get("_steer"):
                    continue
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                if isinstance(c, str) and c.strip():
                    users.append(c)
            start = float((raw.get("extra") or {}).get("started") or 0) or None
            end = f.stat().st_mtime
            if start is None:
                start = end - 7 * 86400
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.deleted_sids.add(sid)
        f.unlink(missing_ok=True)
        sess.archive_path(sid).unlink(missing_ok=True)
        sess.checkpoints_path(sid).unlink(missing_ok=True)
        sess.set_meta(sid, title="", pinned=False, auto_title="")
        forgot = {"episodes": 0, "summaries": 0}
        mem = getattr(self.agent, "memory", None)
        if mem is not None:
            try:
                forgot = mem.forget_session(sid, users, start, end)
            except Exception:  # noqa: BLE001 - chat deleted even if memory could not be deleted
                log_error()
        return {"ok": True, "forgot": forgot}

    def new_session(self) -> str | None:
        """New chat. Returns error text if any."""
        with self._chats_lock:
            err = self._make_room()
            if err:
                return err
            if err is None:  # foreground is idle: continue using the same agent (and browser connection)
                self.ctx.state.pop("team", None)  # team continues running as a project (Projects page)
                self.chat.switch_to(sess.new_session_id())
                return None
            self._swap_in(Chat(self, self.ctx.cwd, self.agent.permissions.mode), old_busy=True)
        return None

    def resume(self, sid: str) -> bool:
        if not sid:  # sess.load("") opens the latest chat: empty ID is not "that chat"
            return False
        with self._chats_lock:
            if sid == self.chat.sid:
                return True
            if sid in self.background:  # return to chat running in background: from live instance, not disk
                target = self.background.pop(sid)
                busy = self.chat.busy
                if busy and self._make_room():
                    self.background[sid] = target
                    return False
                self._swap_in(target, old_busy=busy)
                return True
            loaded = sess.load(sid)
            if not loaded:
                return False
            sid, msgs = loaded
            err = self._make_room()
            if err:
                return False
            if err is None:
                self.chat.switch_to(sid, msgs)
                return True
            self._swap_in(Chat(self, self.ctx.cwd, self.agent.permissions.mode, sid=sid, messages=msgs), old_busy=True)
            return True

    def _finished(self, chat: Chat) -> None:
        """Chat's turn finished (in its own thread). If not foreground, it has no more work: closed."""
        self._watch_quota()
        with self._chats_lock:
            chat.queued = False
            if chat is self.chat:
                return
            self.background.pop(chat.sid, None)
        chat.close()

    # ------------------------------------------------------------------ durdurma
    def stop(self, sid: str = "") -> None:
        """Stop button (foreground chat; if sid is provided, that chat). If a tool fails to terminate despite everything
        (unresponsive window/COM call), chat's thread would remain busy forever: in that case, the chat detaches from that thread
        (see _abandon).

        If sid is provided and that chat is not live (phone chat finished or never started), nothing stops: previously
        it fell back to the foreground chat, and clicking Stop on phone cancelled running work on PC."""
        chat = self.find_chat(sid) if sid else self.chat
        if chat is None:
            return
        chat.agent.stop()
        if chat.busy:
            threading.Thread(target=self._unstick, args=(chat,), daemon=True, name="alice-unstick").start()

    def _unstick(self, chat: Chat) -> None:
        deadline = time.monotonic() + self.STOP_GRACE
        while time.monotonic() < deadline:
            if not chat.busy:
                return
            time.sleep(0.2)
        self._abandon(chat)

    def _abandon(self, chat: Chat) -> None:
        """Abandon the stuck thread: chat is saved and if in foreground, the same chat is opened with a new thread.
        If the old thread ever returns, it does not save, write to the UI, or hold the screen."""
        try:
            chat.agent._repair_after_interrupt()  # prevent unanswered tool call from staying in history
            chat.save()
        except Exception:  # noqa: BLE001 - better than hanging even if save fails
            log_error()
        msgs = chat.agent.messages[1:]
        with self._chats_lock:
            chat.abandoned = True
            chat.ui.publish = lambda event: None
            self.desktop_lock.release(chat.ctx)
            b = chat.ctx.state.get("browser")
            if b is not None and hasattr(b, "abort"):
                b.abort("stop")
            self.background.pop(chat.sid, None)
            if chat is self.chat:
                self.chat = Chat(self, chat.ctx.cwd, chat.agent.permissions.mode, sid=chat.sid, messages=msgs)
        self.bus.publish({"type": "info", "text": "bir araç durdurulamadı; sohbet kaydedildi ve kaldığı yerden açıldı",
                          "sid": chat.sid})
        self.bus.publish({"type": "busy", "value": False, "sid": chat.sid})
        self.bus.publish({"type": "status", **self.status()})

    _builtin: Any = None
    _builtin_lock = threading.Lock()

    def builtin_browser(self) -> Any:
        """Built-in browser (singleton, opened on first use): both agent and panel connect to the same instance."""
        with self._builtin_lock:
            if self._builtin is None:
                from alice.app.builtin_browser import BuiltinBrowser

                self._builtin = BuiltinBrowser()
            return self._builtin

    def shutdown(self) -> None:
        if self._builtin is not None:
            try:
                self._builtin.stop()
            except Exception:  # noqa: BLE001 - errors on shutdown can be ignored
                pass
        self._update_stop.set()
        with self._chats_lock:
            chats = [self.chat, *self.background.values()]
        for c in chats:
            c.agent.stop()
        for c in chats:
            c.close(wait=10 if c is self.chat else 2)
        from alice.mcp import close_all

        close_all(self._mcp_servers)

    def status(self) -> dict[str, Any]:
        a = self.agent
        return {
            "version": __version__,
            # Selected model button (pinned model immediately); bottom bar shows model that actually served response.
            # Previously both were the serving model: newly selected model was not visible until the next response.
            "model": ((self.router.role_overrides.get("brain") or [None])[0] or self.router.last_ref.get("brain")
                      or self.router.first_available("brain") or ""),
            "served_model": self.router.last_ref.get("brain") or "",
            "mode": a.permissions.mode,
            "ctx_tokens": a.context_tokens(),
            "ctx_limit": a.compact_at,
            "ctx_window": self._ctx_window(),
            "today_requests": sum(r["requests"] for r in self.router.usage.today().values()),
            "decider": self.decider.names[0] if self.decider.names else "",
            "cwd": str(self.ctx.cwd),
            "branch": git_branch(self.ctx.cwd),
            "busy": a.busy or self._queued,
            "session": self.sid,
            "started": self.chat.started,
            "running": self.running(),
            # Includes chat ID of prompt: UI shouldn't draw background card in foreground. list(): iterating
            # over the dictionary while prompts were added/removed in agent thread caused RuntimeError.
            "pending": [{**p, "sid": ui.sid} for ui in self.all_uis() for p in list(ui.pending.values())],
            "brain_chain": self.router.chain("brain"),
            "brain_pinned": "brain" in self.router.role_overrides,
            "thinking": self.router.thinking_level("brain"),
            **self._thinking_options(),
            "stats": self.agent.stats_view(),
            "user": _user_name(),
            "checkpoints": sorted(self.agent.checkpoints),
            "team": self.team_snapshot(),
            "update": self.update.as_dict() if self._update_shown() else None,
            "update_mode": self._update_mode(),
        }

    def subagents_view(self) -> dict[str, Any]:
        """Agents panel: foreground chat's subagents and custom subagent definitions."""
        from alice import subagents
        from alice.skills import load_agents

        home = str(alice_home() / "agents")
        defs = [{"name": a.name, "description": a.description, "prompt": a.prompt, "tools": a.tools, "model": a.model,
                 "max_steps": a.max_steps, "path": str(a.path or ""),
                 "where": "user" if a.path is not None and str(a.path).startswith(home) else "project"}
                for a in load_agents(self.ctx.cwd).values()]
        return {"subs": subagents.snapshots(self.ctx), "defs": defs, "cwd": str(self.ctx.cwd)}

    def subagents_action(self, path: str, d: dict[str, Any]) -> tuple[dict[str, Any], int]:
        from alice import skills, subagents
        from alice.tools.base import ToolError

        try:
            if path == "/api/subagents/stop":
                sub = subagents.registry(self.ctx).get(str(d.get("id", "")))
                if sub is None:
                    return {"ok": False, "error": "böyle bir alt ajan yok"}, 404
                return {"ok": subagents.stop(sub)}, 200
            if path == "/api/subagents/send":
                text = str(d.get("text", "")).strip()
                if not text:
                    return {"ok": False, "error": "mesaj boş"}, 400
                sub = subagents.resume(self.ctx, str(d.get("id", "")), text, background=True)
                return {"ok": True, "note": f"{sub.id} devam ediyor; bitince sonucu sohbete gelecek"}, 200
            if path == "/api/agents/save":
                a = skills.save_agent(str(d.get("name", "")), str(d.get("description", "")), str(d.get("prompt", "")),
                                      [str(t) for t in d.get("tools") or []], str(d.get("model", "")),
                                      int(d.get("max_steps") or 40), str(d.get("where") or "user"), self.ctx.cwd)
                return {"ok": True, "note": f"{a.name} kaydedildi"}, 200
            if path == "/api/agents/delete":
                skills.delete_agent(str(d.get("name", "")), self.ctx.cwd)
                return {"ok": True, "note": "silindi"}, 200
        except (ToolError, ValueError, OSError) as e:
            return {"ok": False, "error": str(e)[:300]}, 400
        return {"ok": False, "error": "bilinmeyen işlem"}, 404

    def pr_panel(self, force: bool = False) -> dict[str, Any]:
        """PR panel: watched PRs (with live status) and unwatched open PR of the current branch (to start watching)."""
        from alice import prwatch
        from alice.tools.base import ToolError
        from alice.tools.github import GitHubAPI, current_branch, github_token, origin_repo

        watches = prwatch.load()
        out: dict[str, Any] = {"ok": True, "watching": [], "suggest": None, "error": ""}
        try:
            api = GitHubAPI(github_token(self.cfg))
        except ToolError as e:
            out.update(ok=False, error=str(e)[:200])
            api = None
        for w in watches:
            row = {"repo": w.repo, "number": w.number, "status": w.status, "history": w.history[-5:],
                   "session": w.session, **{f: getattr(w, f) for f in prwatch.FLAGS}}
            if api is not None:
                try:
                    row.update(prwatch.live_status(api, w.repo, w.number, cache=not force))
                except Exception as e:  # noqa: BLE001 - a single PR error should not take down the panel
                    row["error"] = str(e)[:160]
            out["watching"].append(row)
        if api is not None:
            try:
                repo, branch = origin_repo(self.ctx.cwd), current_branch(self.ctx.cwd)
                if repo and branch:
                    owner = repo.split("/")[0]
                    prs = api.get(f"/repos/{repo}/pulls", head=f"{owner}:{branch}", state="open")
                    if prs and not any(w.repo == repo and w.number == prs[0]["number"] for w in watches):
                        out["suggest"] = {"repo": repo, "number": prs[0]["number"], "title": prs[0]["title"],
                                          "url": prs[0]["html_url"], "head": branch}
            except Exception:  # noqa: BLE001 - not a git repository, no network
                pass
        return out

    def _thinking_options(self) -> dict[str, Any]:
        """Thinking levels supported by the selected (or last responding) brain model: menu only displays these."""
        from alice.providers import thinking

        ref = ((self.router.role_overrides.get("brain") or [None])[0] or self.router.last_ref.get("brain")
               or self.router.first_available("brain") or "")
        provider, _, model = ref.partition(":")
        levels, kind = thinking.levels_for(provider, model) if ref else (list(thinking.LEVELS), "graded")
        if ref in getattr(self.router, "no_thinking", set()):
            levels, kind = ["default"], "none"  # model rejected the setting (learned at runtime)
        return {"thinking_levels": levels, "thinking_kind": kind,
                "thinking_effective": thinking.fit(self.router.thinking_level("brain"), levels)}

    # ----------------------------------------------------------------- connections
    def connections(self) -> dict[str, Any]:
        """Connections: service statuses and monitoring feeds."""
        from alice.connect.manifest import discover
        from alice.connect.runner import ready
        from alice.core.feeds import load_feeds, state_path

        manifests, errors = discover(self.cfg)
        rows = []
        for m in sorted(manifests.values(), key=lambda x: x.name):
            ok, note = ready(m)
            rows.append({"name": m.name, "title": m.label, "kind": m.auth.kind, "ready": ok, "status": note,
                         "env": m.auth.env or m.auth.client_id_env, "help": m.auth.help, "docs": m.docs,
                         "actions": sorted(m.actions), "description": m.description})
        feeds, feed_errors = load_feeds()
        try:
            fstate = json.loads(state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fstate = {}
        frows = [{"name": f.name, "connector": f.connector, "action": f.action, "every_minutes": f.every_minutes,
                  "enabled": f.enabled, "agent": bool(f.agent), "describe": f.describe(),
                  "held": len((fstate.get(f.name) or {}).get("held") or []),
                  "last": float((fstate.get(f.name) or {}).get("last") or 0),
                  "error": str((fstate.get(f.name) or {}).get("error") or "")} for f in feeds]
        from alice import actionlog

        sent = [{"text": actionlog.format_row(r), "outcome": r.get("outcome", "")} for r in actionlog.recent(10)]
        return {"connectors": rows, "feeds": frows, "errors": errors + feed_errors, "actions": sent}

    def connect_auth(self, name: str) -> dict[str, Any]:
        """Start OAuth flow in the background: opens consent page in browser, reports completion as an event."""
        import os

        from alice.connect import oauth
        from alice.connect.manifest import discover

        m = discover(self.cfg)[0].get(name)
        if m is None:
            return {"ok": False, "error": f"bağlantı yok: {name}"}
        if m.auth.kind != "oauth2":
            return {"ok": False, "error": f"{m.label} anahtarla çalışıyor: Bağlantılar → Anahtarlar'dan {m.auth.env} gir"}
        cid = os.environ.get(m.auth.client_id_env, "").strip()
        if not cid:
            return {"ok": False, "error": f"{m.auth.client_id_env} yok; Bağlantılar → Anahtarlar'dan gir"}
        secret = os.environ.get(m.auth.client_secret_env, "").strip() if m.auth.client_secret_env else ""

        def run() -> None:
            say = lambda s: self.bus.publish({"type": "info", "text": f"{m.label}: {s}"})  # noqa: E731
            try:
                oauth.authorize(m.auth, m.name, cid, secret, emit=say)
            except oauth.OAuthError as e:
                say(str(e))
                return
            say("bağlandı")
            self.bus.publish({"type": "connect", "name": m.name, "ready": True})

        threading.Thread(target=run, daemon=True, name=f"alice-oauth-{name}").start()
        return {"ok": True, "note": f"{m.label} için tarayıcıda izin sayfası açılıyor"}

    def connect_logout(self, name: str) -> dict[str, Any]:
        from alice.connect import store

        return {"ok": store.forget(name)}

    # ------------------------------------------------------------------ updater
    def _update_mode(self) -> str:
        from alice import updater

        return str(updater.settings(self.cfg).get("mode", "auto"))

    # ------------------------------------------------------------------ Assistant tab
    ASSISTANT_SETTINGS = {  # (section, key) -> allowed values; only these can be changed from the UI
        ("agent", "sandbox"): ("off", "auto", "on"),
        ("agent", "taint_guard"): (True, False),
        ("agent", "search_grounding"): (True, False),
        ("agent", "turn_token_budget"): (0, 200_000, 500_000, 1_000_000, 2_000_000),
        ("greeting", "enabled"): (True, False),
        ("greeting", "away_minutes"): (60, 90, 120, 180),
        ("memory", "digest"): (True, False),
        ("gmail", "notify"): (True, False),
        ("buddy", "enabled"): (True, False),
        ("buddy", "chatter"): (True, False),
    }

    def assistant(self) -> dict[str, Any]:
        from alice.connect import store
        from alice.doctor import run_all
        from alice.permissions import project_rules, project_rules_trusted
        from alice.skills import load_agents, load_skills
        from alice.tools import gmail

        cfg_rules = self.cfg.data.get("permissions") or {}
        proj = project_rules(self.ctx.cwd, include_untrusted=True)
        trusted = project_rules_trusted(self.ctx.cwd)
        rules = [{"kind": k, "rule": r, "source": "config"} for k in ("allow", "ask", "deny")
                 for r in cfg_rules.get(k, []) or []]
        rules += [{"kind": k, "rule": r, "source": "project", "untrusted": k == "allow" and not trusted}
                  for k in ("allow", "ask", "deny") for r in proj.get(k, [])]
        values = {f"{sec}.{key}": (self.cfg.data.get(sec) or {}).get(key) for sec, key in self.ASSISTANT_SETTINGS}
        return {
            "checks": [c.__dict__ for c in run_all(self.cfg, self.ctx.cwd, network=False)],
            "settings": values,
            "rules": rules,
            "project": str(self.ctx.cwd),
            "gmail": [{"account": a, "email": store.get(gmail.PREFIX + a).get("email", "")} for a in gmail.accounts()],
            "google_ready": bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip()),
            "skills": [{"name": k.name, "description": k.description, "path": str(k.path.parent)}
                       for k in load_skills(self.ctx.cwd).values()],
            "agents": [{"name": a.name, "description": a.description, "path": str(a.path)}
                       for a in load_agents(self.ctx.cwd).values()],
        }

    # ------------------------------------------------------------------ BUDDY and KAIROS
    def buddy_view(self) -> dict[str, Any]:
        from alice import buddy

        try:
            router = self.agent.router
        except AttributeError:
            router = None
        return buddy.view(self.cfg, router=router)

    def buddy_action(self, action: str, value: str = "") -> dict[str, Any]:
        from alice import buddy

        if action not in ("pet", "feed", "rename"):
            return {"ok": False, "error": "bilinmeyen işlem"}
        return {"ok": True, "text": buddy.act(action, value), "buddy": buddy.view(self.cfg)}

    def kairos_view(self) -> dict[str, Any]:
        from alice import kairos

        return {**kairos.view(), "enabled": kairos.is_enabled(self.cfg)}

    def kairos_action(self, action: str) -> dict[str, Any]:
        from alice import kairos

        if action == "wake":
            kairos.request_wake()
        elif action in ("on", "off"):
            kairos.set_enabled(self.cfg, action == "on")
            self.cfg.data.setdefault("kairos", {})["enabled"] = action == "on"
        else:
            return {"ok": False, "error": "bilinmeyen işlem"}
        return {"ok": True, **self.kairos_view()}

    def assistant_set(self, section: str, key: str, value: Any) -> bool:
        from alice.config import set_config_value

        allowed = self.ASSISTANT_SETTINGS.get((section, key))
        if allowed is None or value not in allowed or isinstance(value, bool) != isinstance(allowed[0], bool):
            return False
        self.cfg.data.setdefault(section, {})[key] = value
        set_config_value(section, key, value)
        return True

    def assistant_action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        from alice.connect import store
        from alice.tools import gmail

        if action == "rules_trust":
            from alice.permissions import trust_project_rules

            return {"ok": trust_project_rules(self.ctx.cwd)}
        if action == "rule_delete":
            from alice.permissions import remove_project_rule

            return {"ok": remove_project_rule(self.ctx.cwd, str(d.get("kind")), str(d.get("rule")))}
        if action == "gmail_logout":
            return {"ok": store.forget(gmail.PREFIX + str(d.get("account", "")))}
        if action == "gmail_connect":
            acc = str(d.get("account", "")).strip().lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,30}", acc):
                return {"ok": False, "error": "ad sadece harf, rakam, - ve _ olsun (ör. okul)"}
            if not os.environ.get("GOOGLE_CLIENT_ID", "").strip():
                return {"ok": False, "error": "önce Google kurulumu: Sistem → Kurulum → Google"}

            def run() -> None:
                try:
                    email = gmail.connect(acc, emit=lambda s: None)
                    self.ui.info(f"Gmail bağlandı: {acc}" + (f" ({email})" if email else ""))
                except Exception as e:  # noqa: BLE001
                    self.ui.info(f"Gmail bağlanamadı ({acc}): {e}")

            threading.Thread(target=run, daemon=True, name="alice-gmail-connect").start()
            return {"ok": True, "started": True}
        return {"ok": False, "error": f"bilinmeyen işlem: {action}"}

    # ------------------------------------------------------------------ Voice tab
    PREVIEW_TEXT = "Selam, ben Alice. Bugün ne yapıyoruz? İstersen önce şu indirmeleri bir toparlayayım."

    def voice_view(self) -> dict[str, Any]:
        from alice.voice.tts import ALL_GEMINI_VOICES

        v = self.cfg.data.get("voice", {})
        key_env = self.cfg.data.get("providers", {}).get("gemini", {}).get("api_key_env", "GEMINI_API_KEY")
        return {"live": str(v.get("live_voice", "Kore")), "tts": str(v.get("tts_cloud", "gemini:Aoede")),
                "can_preview": bool(os.environ.get(key_env, "").strip()),
                "voices": [{"name": n, "desc": d} for n, d in ALL_GEMINI_VOICES.items()]}

    def voice_preview(self, name: str) -> dict[str, Any]:
        """Short Turkish sample with this voice (wav, base64). Generated once, stored in ~/.alice/voice/previews."""
        import base64

        from alice.voice.tts import ALL_GEMINI_VOICES, TTSError, make_cloud

        if name not in ALL_GEMINI_VOICES:
            return {"ok": False, "error": f"bilinmeyen ses: {name}"}
        path = alice_home() / "voice" / "previews" / f"{name}.wav"
        if not path.is_file():
            tts = make_cloud(self.cfg, f"gemini:{name}")
            if tts is None:
                return {"ok": False, "error": "örnek için Gemini anahtarı gerekli (Bağlantılar → Anahtarlar)"}
            try:
                audio = tts.synth(self.PREVIEW_TEXT, "happy")
            except TTSError as e:
                return {"ok": False, "error": f"örnek üretilemedi: {e}"}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(audio.to_wav())
        return {"ok": True, "audio": base64.b64encode(path.read_bytes()).decode()}

    def set_voice(self, name: str) -> dict[str, Any]:
        """Make Live and standard spoken responses use the same voice."""
        from alice.config import set_config_value
        from alice.voice.tts import ALL_GEMINI_VOICES

        if name not in ALL_GEMINI_VOICES:
            return {"ok": False, "error": f"bilinmeyen ses: {name}"}
        v = self.cfg.data.setdefault("voice", {})
        v["live_voice"], v["tts_cloud"] = name, f"gemini:{name}"
        set_config_value("voice", "live_voice", name)
        set_config_value("voice", "tts_cloud", f"gemini:{name}")
        return {"ok": True}

    _setup = None

    @property
    def setup(self):
        """System → Setup: doctor, benchmark models, Google, phone, DCS, plugins, shortcut."""
        if self._setup is None:
            from alice.app.setup_panel import SetupPanel

            self._setup = SetupPanel(self.cfg, self.router, self.system)
        return self._setup

    _features = None

    @property
    def features(self):
        """First-run wizard and Settings → Features: optional features installed from the app."""
        if self._features is None:
            from alice.app.features_panel import FeaturesPanel

            self._features = FeaturesPanel(self)
        return self._features

    _journal = None

    @property
    def journal(self):
        """Automations → Notes and Logs."""
        if self._journal is None:
            from alice.app.journal import Journal

            self._journal = Journal(self.cfg)
        return self._journal

    _automations = None

    @property
    def automations(self):
        """Automations → Automation: rules, scheduled tasks, feeds."""
        if self._automations is None:
            from alice.app.automations import Automations

            self._automations = Automations(self.router, self.cfg)
        return self._automations

    _system = None

    @property
    def system(self):
        """System → Core: background ALICE and voice assistant."""
        if self._system is None:
            from alice.app.system import SystemPanel

            self._system = SystemPanel(self.cfg, restart_core=self._restart_core)
        return self._system

    def _restart_core(self) -> bool:
        """System panel: restart core if app is running inside core (path used during updates)."""
        if self.restart_hook is None:
            return False
        threading.Thread(target=self._restart_soon, daemon=True, name="alice-restart").start()
        return True

    _enroll = None

    @property
    def enroll(self):
        """System → Voice → Enroll voice (GUI version of alice voice enroll)."""
        if self._enroll is None:
            from alice.voice.enroll import EnrollSession

            self._enroll = EnrollSession()
        return self._enroll

    def enroll_action(self, action: str) -> dict[str, Any]:
        fn = {"start": self.enroll.start, "test": self.enroll.test, "install": self.enroll.install,
              "cancel": self.enroll.cancel, "remove": self.enroll.remove, "done": self.enroll.done,
              "skip": self.enroll.skip}.get(action)
        return fn() if fn else {"ok": False, "error": f"bilinmeyen işlem: {action}"}

    # ------------------------------------------------------------------ Memory tab
    def memory_view(self) -> dict[str, Any]:
        """Notes in memory (by scope; can be opened, edited, deleted), open follow-ups, last Dream, and traces
        remaining from former role."""
        from alice.memdir import TYPE_TR, age_text
        from alice.memory import OLD_ROLE_RX

        mem = self.agent.memory
        if mem is None:
            return {"enabled": False, "books": [], "followups": [], "old_role": {}, "text": ""}
        old = mem.old_role()
        cwd = getattr(self.agent.ctx, "cwd", None)
        roots = {b.root for b in mem.books(cwd)}
        books = mem.all_books() + [b for b in mem.books(cwd) if b.root not in {x.root for x in mem.all_books()}]
        out = []
        for b in books:
            notes = b.scan()
            out.append({
                "scope": b.scope, "root": str(b.root), "current": b.root in roots,
                "label": "Genel" if b.scope == "genel" else b.root.parent.name,
                "notes": [{"key": n.key, "name": n.name, "title": n.title, "type": n.type,
                           "type_tr": TYPE_TR.get(n.type, n.type or "?"), "description": n.description,
                           "body": n.body, "age": age_text(n.modified), "source": n.source,
                           "old": bool(OLD_ROLE_RX.search(f"{n.description}\n{n.body}"))} for n in notes],
                "backups": b.backups()[:5], "last_dream": b.last_dream(),
            })
        return {
            "enabled": True,
            "books": out,
            "followups": [{"id": f.id, "text": f.line()} for f in mem.followups()],
            # Remembered past chats: summaries and recent turns. Can be deleted (previously only notes could be deleted;
            # conversations ALICE "remembered" could not be removed from the UI).
            "summaries": [{"id": x.id, "text": x.line()} for x in reversed(mem.summaries(30))],
            "episodes": [{"id": e.id, "text": e.line(width=200), "channel": e.channel} for e in reversed(mem.recent(30))],
            "dream": {"summary": mem._meta("dream_summary") or "", "at": float(mem._meta("dream_at") or 0),
                      "enabled": mem.dream_enabled, "hours": mem.dream_hours, "sessions": mem.dream_sessions},
            "old_role": {"facts": len(old["facts"]), "summaries": len(old["summaries"]),
                         "episodes": len(old["episodes"]),
                         "examples": [e.reply[:140] for e in old["episodes"][-3:]] + [s.text[:140] for s in old["summaries"][-2:]]},
            "text": mem.export_markdown(),
        }

    def memory_action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        mem = self.agent.memory
        if mem is None:
            return {"ok": False, "error": "hafıza kapalı"}
        cwd = getattr(self.agent.ctx, "cwd", None)
        if action in ("tidy", "dream", "forget", "save", "restore", "forget_summary", "forget_episode",
                      "forget_history", "purge_old_role"):
            mem.mark_user_edit()  # refresh system prompts of open chats on the next turn
        if action in ("tidy", "dream"):  # Settings → Memory → Dream: run consolidation now
            try:
                done = mem.dream(force=True, cwd=cwd)
            except Exception as e:  # noqa: BLE001 - no model / quota error
                return {"ok": False, "error": f"toparlanamadı: {e}"}
            return {"ok": True, "done": done}
        if action == "forget":
            gone = mem.delete_note(str(d.get("key") or d.get("id") or ""), cwd)
            return {"ok": gone is not None}
        if action == "save":
            from alice.memdir import MemoryFull

            key = str(d.get("key") or "")
            b, n = mem.find_note(key, cwd) if key else (None, None)
            if n is None:
                return {"ok": False, "error": "not bulunamadı"}
            # Only sent fields are modified: body-less save (e.g. description only) should not erase note body.
            body = str(d.get("body") or "") if "body" in d else n.body
            try:
                b.save(n.name, str(d.get("description") or n.description), str(d.get("type") or n.type),
                       body, title=n.title, source="kullanıcı")
            except (MemoryFull, ValueError) as e:
                return {"ok": False, "error": str(e)}
            mem._bump()
            return {"ok": True}
        if action == "restore":
            b, _ = (mem.notes, None)
            for x in mem.all_books():
                if str(x.root) == str(d.get("root") or ""):
                    b = x
            try:
                return {"ok": True, "count": b.restore(str(d.get("backup") or ""))}
            except ValueError as e:
                return {"ok": False, "error": str(e)}
        if action == "done":
            try:
                return {"ok": mem.done_followup(int(d.get("id", -1))) is not None}
            except (TypeError, ValueError):
                return {"ok": False}
        if action == "forget_summary":
            try:
                return {"ok": mem.forget_summary(int(d.get("id", -1)))}
            except (TypeError, ValueError):
                return {"ok": False}
        if action == "forget_episode":
            try:
                return {"ok": mem.forget_episode(int(d.get("id", -1)))}
            except (TypeError, ValueError):
                return {"ok": False}
        if action == "forget_history":
            return {"ok": True, **mem.forget_history()}
        if action == "purge_old_role":
            return {"ok": True, "removed": mem.purge_old_role()}
        return {"ok": False, "error": f"bilinmeyen işlem: {action}"}

    @property
    def calls(self) -> Any:
        """Voice call with phone (alice/app/call.py); initialized on first use."""
        if getattr(self, "_calls", None) is None:
            from alice.app.call import CallManager

            self._calls = CallManager(self)
        return self._calls

    def call_action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        mgr = self.calls
        cid = str(d.get("id", ""))
        try:
            if action == "start":
                return mgr.start()
            if action == "ring":  # ALICE calls the user (desktop test, automation)
                return mgr.ring(str(d.get("reason", "")), str(d.get("message", "")) or "Selam, ben ALICE.")
            if action == "answer":
                return mgr.answer(cid)
            if action == "decline":
                return mgr.decline(cid)
            if action == "say":
                return mgr.say(cid, str(d.get("text", "")))
            if action == "hangup":
                return mgr.hangup(cid)
            if action == "mic":
                try:
                    pcm = base64.b64decode(str(d.get("pcm", "")), validate=True)
                except ValueError:
                    return {"ok": False, "error": "bozuk ses"}
                return mgr.mic(cid, pcm)
            if action == "interrupt":
                return mgr.interrupt(cid)
        except KeyError as e:
            return {"ok": False, "error": str(e.args[0]) if e.args else "görüşme yok"}
        return {"ok": False, "error": f"bilinmeyen işlem: {action}"}

    def all_uis(self) -> list[Any]:
        """UIs of all live chats: foreground, background workers, and phone call. Permission and choice responses
        route to the asking chat (previously routed only to foreground chat, leaving background prompts unanswered)."""
        with self._chats_lock:
            chats = [self.chat, *self.background.values()]
        calls = getattr(self, "_calls", None)
        c = calls.active() if calls is not None else None
        if c is not None and c.chat is not None:
            chats.append(c.chat)
        return [ch.ui for ch in chats]

    # ------------------------------------------------------------------ Keys tab
    def keys(self) -> dict[str, Any]:
        """API keys: which are populated, where to obtain them. Full values are never sent to browser.
        Also includes additional keys (GEMINI_API_KEY_2...) and pool status for model providers."""
        from alice.config import env_catalog, env_path, mask_secret, read_env

        in_file = read_env()
        cfgs = self.router.providers_cfg
        providers = {pc.api_key_env: name for name, pc in cfgs.items() if pc.api_key_env}
        catalog = env_catalog()
        known = {e["key"] for e in catalog}
        for name, pc in cfgs.items():  # custom providers added via UI
            if pc.custom and pc.api_key_env and pc.api_key_env not in known:
                catalog.append({"key": pc.api_key_env, "label": name, "url": "", "help": pc.base_url, "group": "model",
                                "part_of": pc.api_key_env, "field": "Anahtar"})
        pools = self.router.key_status()
        rows = []
        for e in catalog:
            key = e["key"]
            val = in_file.get(key) or ""
            env_val = os.environ.get(key, "").strip()
            source = "file" if val else ("env" if env_val else "")
            row = {**e, "set": bool(source), "source": source, "preview": mask_secret(val or env_val),
                   "provider": providers.get(key, "")}
            if row["provider"]:
                pc = cfgs[row["provider"]]
                row["custom"] = pc.custom
                row["extra"] = [{"key": k, "preview": mask_secret(os.environ.get(k, "")),
                                 "source": "file" if k in in_file else "env"} for k in self._extra_keys(key)]
                row["pool"] = pools.get(row["provider"], [])
            rows.append(row)
        return {"path": str(env_path()), "keys": rows,
                "strategy": str(self.cfg.agent.get("key_strategy") or "round-robin")}

    @staticmethod
    def _extra_keys(base: str) -> list[str]:
        """BASE_2, BASE_3...: names of populated additional keys in environment (in order)."""
        rx = re.compile(rf"{re.escape(base)}_(\d+)")
        found = [(int(m.group(1)), k) for k, v in os.environ.items() if (m := rx.fullmatch(k)) and v.strip()]
        return [k for _, k in sorted(found)]

    def _key_owner(self, key: str) -> tuple[str, str] | None:
        """Is it a writable key name: (base name, provider). Catalog or UI-added provider key
        and their secondary keys (GEMINI_API_KEY_2 ... _50)."""
        from alice.config import env_catalog

        known = {e["key"] for e in env_catalog()}
        bases = {pc.api_key_env: name for name, pc in self.router.providers_cfg.items() if pc.api_key_env}
        known |= {k for k, n in bases.items() if self.router.providers_cfg[n].custom}
        if key in known:
            return key, bases.get(key, "")
        m = re.fullmatch(r"(.+)_(\d+)", key)
        if m and m.group(1) in bases and 2 <= int(m.group(2)) <= 50:
            return m.group(1), bases[m.group(1)]
        return None

    def set_key(self, key: str, value: str) -> dict[str, Any]:
        """Write to .env and use right away: provider initialized with new key in session, model ranks re-measured."""
        from alice.config import set_env_value

        owner = self._key_owner(key)
        if owner is None:
            return {"ok": False, "error": f"bilinmeyen anahtar: {key}"}
        try:
            set_env_value(key, value)
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        for name, pc in self.router.providers_cfg.items():
            if pc.api_key_env == owner[0] or (key in pc.base_url):
                # Previous invalid key may have shut down provider with "auth"; recreate pool with the new key.
                self.router.reload_providers(name)
        if value.strip():
            from alice.providers import ranking

            # Put new provider into "auto" chain: benchmark runs in background, ranking updates on completion.
            ranking.refresh_in_background(self.router, emit=self.router.on_event, max_age=0)
        return {"ok": True}

    def add_key(self, base: str, value: str) -> dict[str, Any]:
        """Add another key to provider (pool): into the first vacant slot. Duplicate key is not added."""
        owner = self._key_owner(base)
        if owner is None or owner[0] != base or not owner[1]:
            return {"ok": False, "error": f"bu anahtara ek anahtar eklenemez: {base}"}
        value = value.strip()
        if not value:
            return {"ok": False, "error": "anahtar boş"}
        pc = self.router.providers_cfg[owner[1]]
        if value in pc.api_keys:
            return {"ok": False, "error": "bu anahtar zaten ekli"}
        if not os.environ.get(base, "").strip():
            return self.set_key(base, value)
        n = 2
        while os.environ.get(f"{base}_{n}", "").strip():
            n += 1
        return {**self.set_key(f"{base}_{n}", value), "key": f"{base}_{n}"}

    def set_key_strategy(self, strategy: str) -> dict[str, Any]:
        from alice.config import set_config_value

        if strategy not in ("round-robin", "fill-first"):
            return {"ok": False, "error": "round-robin ya da fill-first"}
        self.cfg.data.setdefault("agent", {})["key_strategy"] = strategy
        set_config_value("agent", "key_strategy", strategy)
        for pool in [c for c in self.router._clients.values() if hasattr(c, "strategy")]:
            pool.strategy = strategy
        return {"ok": True}

    def add_provider(self, d: dict[str, Any]) -> dict[str, Any]:
        """Add an OpenAI-compatible server from the UI (LM Studio, vLLM, LiteLLM, custom proxy, another cloud).
        Attempts /models first; if unresponsive, it is not saved (force saves anyway)."""
        from alice.config import ProviderConfig, set_config_value, set_env_value
        from alice.providers.openai_compat import OpenAICompatProvider, ProviderError

        name = str(d.get("name", "")).strip().lower()
        base_url = str(d.get("base_url", "")).strip().rstrip("/")
        key = str(d.get("key", "")).strip()
        models = [m.strip() for m in str(d.get("models", "")).replace("\n", ",").split(",") if m.strip()]
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,23}", name):
            return {"ok": False, "error": "ad küçük harf, rakam, - ya da _ olmalı (ör. lmstudio)"}
        if name in self.router.providers_cfg and not self.router.providers_cfg[name].custom:
            return {"ok": False, "error": f"{name} zaten hazır bir sağlayıcı"}
        if not re.fullmatch(r"https?://[^\s\"']+", base_url):
            return {"ok": False, "error": "adres http:// ya da https:// ile başlamalı (ör. http://localhost:1234/v1)"}
        env = re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_API_KEY"
        found: list[str] = []
        try:
            client = OpenAICompatProvider(ProviderConfig(name=name, base_url=base_url, key_override=key), timeout=15)
            try:
                found = [str(m.get("id")) for m in client.list_models() if m.get("id")]
            finally:
                client.close()
        except ProviderError as e:
            if not d.get("force"):
                return {"ok": False, "error": f"sunucu cevap vermedi: {e}", "can_force": True}
        sec = f"providers.{name}"
        set_config_value(sec, "base_url", base_url)
        set_config_value(sec, "api_key_env", env if key else "")
        set_config_value(sec, "custom", True)
        set_config_value(sec, "max_input_tokens", int(d.get("max_input_tokens") or 32000))
        set_config_value(sec, "auto", bool(d.get("auto", True)))
        if models:
            set_config_value(sec, "models", models)
        if key:
            set_env_value(env, key)
        self.cfg.data.setdefault("providers", {})[name] = {
            "base_url": base_url, "api_key_env": env if key else "", "custom": True,
            "max_input_tokens": int(d.get("max_input_tokens") or 32000), "auto": bool(d.get("auto", True)),
            **({"models": models} if models else {})}
        self.router.reload_providers(name)
        from alice.providers import ranking

        ranking.refresh_in_background(self.router, emit=self.router.on_event, max_age=0)
        return {"ok": True, "name": name, "models": models or found[:50]}

    def remove_provider(self, name: str) -> dict[str, Any]:
        from alice.config import remove_config_section, set_env_value

        pc = self.router.providers_cfg.get(name)
        if pc is None or not pc.custom:
            return {"ok": False, "error": "sadece arayüzden eklenen sunucular silinebilir"}
        remove_config_section(f"providers.{name}")
        for k in [pc.api_key_env, *self._extra_keys(pc.api_key_env)] if pc.api_key_env else []:
            set_env_value(k, "")
        self.cfg.data.get("providers", {}).pop(name, None)
        self.router.reload_providers(name)
        return {"ok": True}

    def set_update_mode(self, mode: str) -> bool:
        """Persist update behavior (config.toml [update] mode)."""
        from alice.config import set_config_value

        if mode not in ("auto", "notify", "off"):
            return False
        self.cfg.data.setdefault("update", {})["mode"] = mode
        set_config_value("update", "mode", mode)
        return True

    def _update_shown(self) -> bool:
        u = self.update
        return u is not None and (u.available or u.stale)

    def check_update(self, details: bool = True) -> dict[str, Any]:
        """Is there a new commit on the GitHub branch? Keeps result in state and publishes to the UI."""
        from alice import updater

        with self._update_lock:
            info = updater.check(self.cfg, details=details)
        self.update = info
        data = info.as_dict()
        if info.available or info.stale:
            self.bus.publish({"type": "update", **data})
        return data

    def apply_update(self, force: bool = False) -> dict[str, Any]:
        """Pull the update. On success, application restarts (a new window opens)."""
        from alice import updater

        if self.agent.busy or self._queued:
            return {"ok": False, "error": "önce mevcut işi durdur"}
        with self._update_lock:
            info = self.update if self._update_shown() else updater.check(self.cfg)
            if info.stale and not info.available:
                # Code is already downloaded (core or manual git pull): nothing to pull, restarting is sufficient.
                ok, msg = True, f"yeni sürüm zaten indirilmiş ({info.local[:7]})"
            else:
                ok, msg = updater.apply_update(info, self.cfg, force=force,
                                               emit=lambda s: self.bus.publish({"type": "info", "text": "güncelleme: " + s}))
        if ok:
            self.update = None
            self.bus.publish({"type": "info", "text": msg + " — ALICE yeniden başlatılıyor"})
            threading.Thread(target=self._restart_soon, daemon=True, name="alice-restart").start()
        return {"ok": ok, "note": msg, "restarting": ok}

    def _restart_soon(self) -> None:
        """Wait briefly for response to reach the UI, then restart with the same command."""
        import sys

        from alice import updater

        time.sleep(1.2)
        try:
            self.shutdown()
        except Exception:  # noqa: BLE001 - shutdown error should not block restarting
            pass
        if self.restart_hook is not None:
            self.restart_hook()  # core: releases lock and restarts, window moves to new address
            return
        # Allow new process to check again at launch: do not forward the flag.
        updater.restart([sys.executable, "-m", "alice", *sys.argv[1:]],
                        env={"ALICE_RESTARTED": ""})

    def watch_updates(self, first: Any = None) -> None:
        """Periodic background update check; notifies UI when an update is found."""
        from alice import updater

        if updater.settings(self.cfg).get("mode") == "off":
            return
        self.update = first
        def loop() -> None:
            if first is not None and (getattr(first, "available", False) or getattr(first, "stale", False)):
                time.sleep(2.0)  # wait until UI connects
                self.bus.publish({"type": "update", **first.as_dict()})
            wait = self._update_waits[0]  # first check right after launch, then minute intervals
            first_round = first is None  # if not checked at launch (notify mode, disabled check) check immediately
            last = 0.0
            while not self._update_stop.wait(wait):
                wait = self._update_waits[1]
                try:
                    u = self.update
                    # If new version is waiting for CI, check every few minutes instead of hourly: ALICE launched
                    # after merge kept saying "CI running" and hid the new version for an hour.
                    waiting_ci = u is not None and getattr(u, "ci", "") == "pending" and \
                        time.monotonic() - last >= updater.CI_RECHECK
                    if (first_round or waiting_ci or updater.due(self.cfg)) and not (self.agent.busy or self._queued):
                        first_round = False
                        last = time.monotonic()
                        self.check_update()
                except Exception:  # noqa: BLE001 - update check should not crash the app
                    pass

        threading.Thread(target=loop, daemon=True, name="alice-update").start()

    def team_snapshot(self) -> dict[str, Any] | None:
        """Mini card in chat: ALICE's last project; if finished, the newest active project."""
        team = self.ctx.state.get("team")
        if team is None or not team.active():
            live = sorted((t for t in self._teams().values() if t.active()), key=lambda t: -t.created)
            team = live[0] if live else team
        return team.snapshot() if team is not None else None

    # -------------------------------------------------------------- projects
    def _teams(self) -> dict[str, Any]:
        return self.ctx.state.setdefault("teams", {})

    def get_team(self, pid: str):
        """Return the live project or project from disk (loading it)."""
        from alice.team import Team
        from alice.tools.base import ToolError

        teams = self._teams()
        if pid in teams:
            return teams[pid]
        try:
            team = Team.load(pid, self.ctx, int(self.cfg.agent.get("team_max_parallel", 5)), self.bus.publish)
        except (OSError, ValueError, KeyError) as e:
            raise ToolError(f"proje bulunamadı: {pid}") from e
        teams[team.id] = team
        return team

    def projects(self) -> list[dict[str, Any]]:
        from alice.team import list_projects

        live = {t.id: t for t in self._teams().values()}
        rows = []
        for row in list_projects():
            t = live.pop(row["id"], None)
            rows.append(self._project_row(t) if t is not None else {**row, "active": False})
        rows = [self._project_row(t) for t in live.values()] + rows
        return rows

    @staticmethod
    def _project_row(t) -> dict[str, Any]:
        return {"id": t.id, "title": t.title, "goal": t.goal, "created": t.created, "updated": time.time(),
                "origin": t.origin, "cwd": str(t.cwd), "active": t.active(), "status": t.status(),
                "questions": sum(1 for q in t.questions if not q.get("answer")),
                "members": [{"name": m.name, "status": m.status} for m in t.members.values()]}

    def project_detail(self, pid: str, member: str = "") -> dict[str, Any]:
        team = self.get_team(pid)
        out = team.snapshot()
        out["log"] = [{"from": x.sender, "to": x.to, "text": x.text, "at": x.at} for x in team.log[-300:]]
        from alice.site import find_entry, last_change

        entry = find_entry(team.cwd)
        if entry:
            out["preview"] = {"entry": entry, "base": f"/preview/{self.preview_token}/{team.id}/"}
            if member == "__preview":
                out["preview"]["changed"] = last_change(team.cwd)
        if member == "__preview":
            return out  # if site is not found, UI closes the preview
        if member == "__council":  # project-level councils: plan, final, ask council
            out["council_list"] = team.councils
            return out
        if member == "__timeline":
            out["timeline"] = team.timeline_view()
            return out
        if member:
            from alice.team import LEAD, LeadAssign, LeadDiff, LeadReview, TeamSend, TeamWait

            view = team.thread_view(member)
            agent = team.lead_agent if member == LEAD else team.members[member].agent
            tools = {**self.agent.tools, **({"team_send": TeamSend(team, member), "team_wait": TeamWait(team, member),
                                             "message": TeamSend(team, LEAD), "assign": LeadAssign(team),
                                             "review": LeadReview(team), "diff": LeadDiff(team)}
                                            if agent is None else agent.tools)}
            out["member"] = {**{k: v for k, v in view.items() if k != "messages"},
                             "history": display_history(view["messages"], tools)}
        return out

    def create_project(self, d: dict[str, Any]) -> str:
        from alice.team import THINKING, Team
        from alice.tools.base import ToolError

        goal = str(d.get("goal") or "").strip()
        specs = [a for a in d.get("agents") or [] if isinstance(a, dict)]
        if not goal:
            raise ToolError("hedef boş")
        if len(specs) > 8:
            raise ToolError("en çok 8 ajan")
        cwd = self.ctx.resolve(str(d["cwd"])) if d.get("cwd") else self.ctx.cwd
        if not cwd.is_dir():
            raise ToolError(f"klasör yok: {cwd}")
        lead_model = str(d.get("lead_model") or "auto").strip()
        lead_thinking = str(d.get("lead_thinking") or "default")
        team = Team(self.ctx, goal, int(self.cfg.agent.get("team_max_parallel", 5)), self.bus.publish, origin="user",
                    review=d.get("review") is not False, council=d.get("council") is not False, lead_model=lead_model,
                    check=str(d.get("check") or ""), pr=bool(d.get("pr")),
                    lead_thinking=lead_thinking if lead_thinking in THINKING else "default", cwd=cwd)
        team._check_model(lead_model, "yönetici")
        team.plan_council = d.get("plan_council") is not False
        for spec in specs:
            team.add(spec)
        self._teams()[team.id] = team
        team.start(plan=not specs)
        return team.id

    def project_action(self, pid: str, action: str, name: str = "", text: str = "",
                       agent: dict[str, Any] | None = None) -> str:
        from alice.team import USER
        from alice.tools.base import ToolError

        team = self.get_team(pid)
        if action == "stop":
            if name and name not in team.members:
                raise ToolError(f"projede '{name}' yok")
            team.stop(name or None)
            return "durduruldu"
        if action == "resume":
            team.resume()
            return "sürdürülüyor"
        if action == "send":
            return team.send(USER, name or "all", text)
        if action == "answer":
            team.answer(name, text)  # name: question ID
            return "cevap iletildi"
        if action in ("approve", "revise"):
            return team.review_member(name, action == "approve", text, by=USER)
        if action == "save_template":
            tpl = team.save_template(text or team.title)
            return f"şablon kaydedildi: {tpl['name']}"
        if action == "vote":
            dec = team.user_vote(name, text, str((agent or {}).get("note") or ""))
            return f"oyun sayıldı; kurul kararı: {dec}"
        if action == "council":
            team.ask_council(name)
            return "kurul toplanıyor"
        if action == "restart":
            team.restart(name)
            return f"{name} baştan başladı"
        if action == "revert":
            files = team.revert(name)
            return f"{len(files)} dosya geri alındı"
        if action == "add":
            m = team.add_member(agent or {})
            return f"{m.name} eklendi"
        if action == "delete":
            if team.active():
                raise ToolError("önce projeyi durdur")
            # Flag and deletion under save lock: an agent thread in the process of shutting down might be inside save()
            # (having passed the flag check); deleting without lock rewrote the file, reopening the deleted project
            # (observed in Windows CI).
            with team._save_lock:
                team.deleted = True
                team.path().unlink(missing_ok=True)
            team.cleanup()
            self._teams().pop(team.id, None)
            if self.ctx.state.get("team") is team:
                self.ctx.state.pop("team", None)
            return "silindi"
        raise ToolError(f"bilinmeyen işlem: {action}")

    def _ctx_window(self) -> int:
        """Context window of the current brain model (limit displayed in the UI)."""
        try:
            ref = self.router.last_ref.get("brain") or self.router.first_available("brain")
        except KeyError:
            ref = None
        return self.router.context_limit(ref) if ref else 0

    def send(self, text: str, images: list[str] | None = None, sid: str = "") -> tuple[bool, str | None]:
        chat = self.chat if not sid or sid == self.chat.sid else self.chat_for(sid)
        if chat is None:
            return False, f"arkada zaten {self.MAX_BACKGROUND} sohbet çalışıyor; biri bitince tekrar dene"
        if text.split(" ", 1)[0].lower() == "/btw":
            question = text[4:].strip()
            if not question:
                self._drop_if_idle(chat)
                return False, "kullanım: /btw <soru>"
            threading.Thread(target=self._side_question, args=(chat, question), daemon=True, name="alice-btw").start()
            return True, None
        if chat.busy:
            # If ALICE is waiting for an answer on a question card, the sent message is the answer to that question.
            # Previously message went to inbox; since agent waited in question tool, inbox unread, message was "lost".
            choice = next((p for p in list(chat.ui.pending.values()) if p.get("type") == "ask_choice"), None)
            if choice and not text.startswith("/") and chat.ui.answer_choice(choice["id"], text):
                chat.ui.publish({"type": "user", "text": text, "mi": None, "images": [], "steer": True})
                return True, None
            if not text.startswith("/") and chat.agent.steer(text, images):
                # Delivered to the running turn: ALICE sees this on the next step without stopping work. Previously
                # the message waited in the UI queue, never reaching ALICE until work finished.
                chat.ui.publish({"type": "user", "text": text, "mi": None, "images": (images or [])[:6],
                                 "steer": True})
                return True, None
            return False, "ALICE şu an çalışıyor"
        prompt = text
        if text.startswith("/"):
            from alice.commands import builtin_prompt, expand_custom

            prompt = expand_custom(text, chat.ctx.cwd)  # user's command first (built-ins are already excluded)
            if prompt is None:
                prompt = builtin_prompt(text)
                if prompt is None:
                    self._drop_if_idle(chat)
                    return False, f"bilinmeyen komut: {text.split()[0]}"
        self._start_turn(chat, text, prompt, images or [])
        return True, None

    def _drop_if_idle(self, chat: Chat) -> None:
        """Close background chat opened by chat_for if it stalled without starting a turn (unknown command, /btw).
        Background chats only closed on turn finish: if turn never started, thread and agent stayed forever."""
        with self._chats_lock:
            if chat is self.chat or chat.busy or self.background.get(chat.sid) is not chat:
                return
            self.background.pop(chat.sid, None)
        chat.close()

    def _suggest(self, chat: Any) -> list[str]:
        """User's likely next message after turn ends (up to 2 short suggestions). Displayed as buttons in UI; clicking
        populates the input box for the user to edit and send. No suggestions if not confident."""
        msgs = [m for m in chat.agent.messages[1:] if m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str) and m["content"].strip() and not m.get("_nudge")]
        if len(msgs) < 2 or msgs[-1].get("role") != "assistant":
            return []
        lines = [f"{'Kullanıcı' if m['role'] == 'user' else 'ALICE'}: {' '.join(m['content'].split())[:600]}"
                 for m in msgs[-8:]]
        prompt = ("Aşağıdaki konuşmada kullanıcının büyük ihtimalle yazacağı bir sonraki mesajı tahmin et. En fazla 2 "
                  "öneri; her biri kullanıcının ağzından, en çok 8 kelime, Türkçe. ALICE bir soru sorduysa olası cevaplar; "
                  "iş bittiyse mantıklı sonraki adım. Emin değilsen boş liste. Yalnız JSON: {\"items\": [\"...\"]}\n\n"
                  + "\n".join(lines))
        try:
            res = chat.agent.router.chat("fast", [{"role": "user", "content": prompt}], max_tokens=120,
                                         response_format={"type": "json_object"})
            raw = res.text or ""
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1] or "{}")
            items = [" ".join(str(x).split())[:80] for x in data.get("items") or [] if str(x).strip()][:2]
        except Exception:  # noqa: BLE001 - no suggestions means no buttons
            items = []
        if items and not chat.busy:
            chat.ui.publish({"type": "suggest", "items": items})
        return items

    def recap(self) -> dict[str, Any]:
        """"While you were away": 1-3 sentence summary of where foreground chat left off after returning from absence.
        Only inspects last ~30 messages; not written to chat. Generated once for the same conversation state."""
        chat = self.chat
        msgs = [m for m in chat.agent.messages[1:] if m.get("role") in ("user", "assistant", "tool")
                and isinstance(m.get("content"), str) and m["content"].strip()]
        if len(msgs) < 4:
            return {"text": ""}
        key = (chat.sid, len(chat.agent.messages))
        cached = getattr(self, "_recap_cache", None)
        if cached and cached[0] == key:
            return {"text": cached[1]}
        lines = []
        for m in msgs[-30:]:
            who = {"user": "Kullanıcı", "assistant": "ALICE", "tool": "araç"}[m["role"]]
            lines.append(f"{who}: {' '.join(m['content'].split())[:400]}")
        prompt = ("Kullanıcı bir süredir uzaktaydı ve sohbete dönüyor. Aşağıdaki konuşmanın nerede kaldığını Türkçe, "
                  "en fazla 3 kısa cümleyle özetle: ne yapılıyordu, ne bitti, ne bekliyor. Selamlama yok.\n\n"
                  + "\n".join(lines))
        try:
            res = chat.agent.router.chat("fast", [{"role": "user", "content": prompt}], max_tokens=220)
            text = " ".join((res.text or "").split())
        except Exception:  # noqa: BLE001 - if there is no recap, there is no card
            text = ""
        self._recap_cache = (key, text)
        return {"text": text}

    def _side_question(self, chat: Any, question: str) -> None:
        """/btw: side question while work is ongoing. Answered with current conversation state (no tools);
        not saved to chat or memory, doesn't interrupt turn. Mostly read from cache due to shared prefix."""
        pub = chat.ui.publish
        pub({"type": "btw", "q": question, "a": "", "pending": True})
        try:
            msgs = [m for m in list(chat.agent.messages) if not m.get("_image")]
            msgs.append({"role": "user", "content": (
                "[Yan soru: kullanıcı süren işi bölmeden bunu soruyor. Araç kullanmadan, bildiklerinle kısa cevap ver; "
                "işe devam etme, plan değiştirme.]\n" + question)})
            res = chat.agent.router.chat(chat.agent.role, msgs, max_tokens=800)
            answer = (res.text or "").strip() or "(cevap yok)"
        except Exception as e:  # noqa: BLE001 - failure of side question should not affect ongoing work
            answer = f"cevaplanamadı: {str(e)[:300]}"
        pub({"type": "btw", "q": question, "a": answer, "pending": False})
        self._drop_if_idle(chat)

    def deliver(self, sid: str, text: str, tries: int = 30) -> str:
        """Message to chat from background (watch event, detached session result): steered into work if chat is running,
        starts a new turn if idle. Retries a few times if turn finishes at that exact moment. Empty: delivered."""
        err = ""
        for _ in range(max(1, tries)):
            ok, err = self.send(text, sid=sid)
            if ok:
                return ""
            if "çalışıyor" not in (err or ""):
                break
            time.sleep(1.0)
        return err or "iletilemedi"

    def _report_to_parent(self, chat: Chat) -> None:
        """If detached session finished its turn, report result to parent chat (do not report if user stopped)."""
        parent = chat.ctx.state.get("report_to")
        if not parent or chat.agent.cancel.is_set() or chat.abandoned:
            return
        final = next((m.get("content") for m in reversed(chat.agent.messages)
                      if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()), "")
        title = chat.ctx.state.get("report_title") or chat.sid
        text = f"[Oturum bitti] '{title}' ({chat.sid}):\n{(final or '(cevap yok)')[:3000]}"
        threading.Thread(target=self.deliver, args=(parent, text), daemon=True, name="alice-report").start()

    def _start_turn(self, chat: Chat, text: str, prompt: str, images: list[str]) -> None:
        agent, pub = chat.agent, chat.ui.publish
        chat.queued = True

        def run() -> None:
            try:
                pub({"type": "user", "text": text, "mi": len(agent.messages), "images": images[:6]})
                pub({"type": "busy", "value": True})
                start = len(agent.messages)
                agent.run_turn(prompt, images=images or None)
                if prompt != text:  # for custom commands, display what the user actually typed (not injected text)
                    for m in agent.messages[start:]:
                        if m.get("role") == "user" and not m.get("_image") and not m.get("_steer"):
                            m["_display"] = text
                            break
            except Exception as e:  # noqa: BLE001 - errors within thread should not crash the app
                chat.ui.publish({"type": "info", "text": f"beklenmeyen hata: {e}"})
                log_error()
            finally:
                try:
                    chat.save()
                except OSError:
                    pass
                # If turn ended before taking an incoming message (error, step limit), do not lose message: start
                # a new turn in the same chat. Must decide before chat closes: when background chat closed in _finished,
                # jobs queued to it never ran. Pending messages are discarded if user clicked Stop.
                left = agent.take_inbox()
                if left and not agent.cancel.is_set() and not chat.abandoned:
                    joined = "\n\n".join(t for t, _ in left)
                    self._start_turn(chat, joined, joined, [img for _, imgs in left for img in imgs][:6])
                else:
                    self._report_to_parent(chat)
                    self._finished(chat)
                    chat.ui.publish({"type": "busy", "value": False})
                    self.bus.publish({"type": "status", **self.status()})
                    if not agent.cancel.is_set() and self.cfg.agent.get("suggestions", True):
                        threading.Thread(target=self._suggest, args=(chat,), daemon=True,
                                         name="alice-suggest").start()

        chat.run(run)

    def transcribe(self, data: bytes, filename: str) -> str:
        """Transcribe UI microphone recording to text (Whisper)."""
        if self._stt is None:
            from alice.voice.stt import make_stt

            self._stt = make_stt(self.cfg)
        return self._stt.transcribe_file(data, filename)

    def set_cwd(self, path: Path, chat: Chat | None = None) -> None:
        """Change working directory, update directory in system prompt, and remember for next launch.
        chat: chat whose folder changed (prevents background chat's github tool from modifying foreground folder)."""
        from alice.prompts import build_system_prompt

        chat = chat or self.chat
        chat.ctx.cwd = path
        msgs = chat.agent.messages
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = build_system_prompt(path)
        try:
            alice_home().mkdir(parents=True, exist_ok=True)
            (alice_home() / "last_cwd").write_text(str(path), encoding="utf-8")
        except OSError:
            pass
        from alice.app.folders import remember_dir

        remember_dir(path)
        self.bus.publish({"type": "status", **self.status()})

    def github_repos(self, query: str = "") -> list[dict[str, Any]]:
        from alice.tools.github import GitHubAPI, clone_root, github_token

        root = clone_root(self.cfg)
        rows = GitHubAPI(github_token(self.cfg)).user_repos(query)
        for r in rows:
            local = root / r["full_name"]
            r["local"] = str(local) if (local / ".git").exists() else ""
        return rows

    def open_github(self, repo: str, branch: str = "") -> str:
        """Clone repository (update if exists) and set as working directory."""
        from alice.tools.github import clone_or_update

        path, note = clone_or_update(self.cfg, repo, branch)
        self.set_cwd(path)
        return note

    def rewind(self, mi: int) -> dict[str, Any]:
        text, restored = self.agent.rewind(mi)
        try:  # statistics, subagents, and rewind checkpoints as well (saving without extra wiped them)
            self.chat.save()
        except OSError:
            pass
        return {"text": text, "restored": restored}


def _user_name() -> str:
    import getpass

    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""
    return name[:1].upper() + name[1:]


def log_error() -> None:
    try:
        with open(alice_home() / "app.log", "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
    except OSError:
        pass


# ----------------------------------------------------------------------- HTTP
def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ALICE"
        timeout = REQUEST_TIMEOUT

        def log_message(self, *args: Any) -> None:  # silent
            pass

        # ------------------------------------------------------------ helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in state.allowed_hosts

        def _origin_ok(self) -> bool:
            """If the browser sends an Origin, it must come from an allowed address (companion to Host check; prevents
            site page from using API even if token leaks). Origin-less clients (watch automations) unaffected."""
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return (urlparse(origin).hostname or "") in state.allowed_hosts

        def _token_ok(self, query: dict[str, list[str]]) -> bool:
            tok = self.headers.get("X-Alice-Token") or (query.get("t") or [""])[0]
            return _same(tok, state.token)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")  # URL contains key, prevent leaking to external links
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest(400, "geçersiz Content-Length") from None
            if n <= 0:
                return {}
            if n > MAX_BODY:  # silently proceeding with empty body caused attached images to be lost
                raise BadRequest(413, "istek çok büyük")
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:  # JSONDecodeError and non-UTF-8 body (UnicodeDecodeError)
                raise BadRequest(400, "geçersiz JSON") from None
            return data if isinstance(data, dict) else {}

        def _deny(self) -> None:
            self._send(403, b"forbidden", "text/plain")

        def _preview(self, path: str) -> None:
            """/preview/<preview key>/<project>/<path>: serve the site in the project folder in sandbox."""
            from urllib.parse import unquote

            from alice.site import content_type, safe_path
            from alice.tools.base import ToolError

            parts = path.split("/", 4)  # "", "preview", key, project, path
            if len(parts) < 4 or not _same(parts[2], state.preview_token):
                return self._deny()
            try:
                team = state.get_team(parts[3])
            except ToolError:
                return self._send(404, b"not found", "text/plain")
            f = safe_path(team.cwd, unquote(parts[4] if len(parts) > 4 else ""))
            if f is None:
                return self._send(404, b"not found", "text/plain")
            try:
                body = f.read_bytes()
            except OSError:
                return self._send(404, b"not found", "text/plain")
            self._send(200, body, content_type(f),
                       {"Content-Security-Policy": PREVIEW_CSP, "Access-Control-Allow-Origin": "*"})

        def _fail(self) -> None:
            """Unexpected error: details logged to app.log, client receives a short message instead of traceback."""
            log_error()
            try:
                self._json({"ok": False, "error": "sunucu hatası (ayrıntı app.log'da)"}, 500)
            except OSError:
                pass

        # --------------------------------------------------------------- GET
        def do_GET(self) -> None:  # noqa: N802
            try:
                self._get()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001 - connection should not drop without a response
                self._fail()

        def _get(self) -> None:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._host_ok():
                return self._deny()
            if not u.path.startswith(("/preview/", "/static/")) and not self._origin_ok():
                return self._deny()  # preview requests resources from sandboxed iframe (Origin: null)
            if u.path == "/":
                if not self._token_ok(q):
                    return self._deny()
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", state.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
            if u.path == "/quick":
                if not self._token_ok(q):
                    return self._deny()
                html = (STATIC / "quick.html").read_text(encoding="utf-8").replace("{{TOKEN}}", state.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": CSP.replace("img-src 'self' data:",
                                                                          "img-src 'self' data:; media-src 'self' blob:")})
            if u.path == "/m":  # mobile web app
                if not self._token_ok(q):
                    return self._deny()
                html = (STATIC / "m.html").read_text(encoding="utf-8").replace("{{TOKEN}}", state.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": CSP.replace("img-src 'self' data:",
                                                                          "img-src 'self' data: blob:")})
            if u.path == "/m.webmanifest":  # "Add to Home Screen": launch URL carries the token
                if not self._token_ok(q):
                    return self._deny()
                body = json.dumps({"name": "ALICE", "short_name": "ALICE", "start_url": f"/m?t={state.token}",
                                   "display": "standalone", "background_color": "#000000", "theme_color": "#000000",
                                   "icons": [{"src": "/static/alice.png", "sizes": "512x512", "type": "image/png"}]})
                return self._send(200, body.encode("utf-8"), "application/manifest+json")
            if u.path.startswith("/preview/"):
                return self._preview(u.path)
            if u.path.startswith("/static/"):
                name = u.path[len("/static/"):]
                if name not in STATIC_FILES:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name])
            if not self._token_ok(q):
                return self._deny()
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/phone/app":  # latest version of mobile app on PC
                from alice.app import phone_apk

                phone_apk.refresh_async()
                return self._json(phone_apk.view())
            if u.path == "/api/phone/app.apk":
                from alice.app import phone_apk

                try:
                    body = phone_apk.apk_path().read_bytes()
                except OSError:
                    return self._send(404, b"telefon uygulamasi henuz indirilmedi", "text/plain")
                return self._send(200, body, "application/vnd.android.package-archive",
                                  {"Content-Disposition": 'attachment; filename="ALICE.apk"'})
            if u.path == "/api/quick/pending":
                pending, state.quick_pending = state.quick_pending, None
                return self._json(pending or {})
            if u.path == "/api/state":
                return self._json(state.status())
            if u.path == "/api/remote/status":
                from alice.app import remote

                return self._json(remote.status())
            if u.path == "/api/remote/screen":
                from alice.app import remote

                try:
                    return self._send(200, remote.screen_jpeg(int((q.get("m") or ["1"])[0])), "image/jpeg")
                except Exception:  # noqa: BLE001 - failed to capture screen (e.g. lock screen)
                    return self._send(503, b"", "image/jpeg")
            if u.path == "/api/remote/apps":
                from alice.app import remote

                return self._json({"apps": remote.apps()})
            if u.path == "/api/call":  # ongoing call (empty if none)
                return self._json(state.calls.view())
            if u.path == "/api/call/audio":
                try:
                    n = int((q.get("n") or ["0"])[0])
                except ValueError:
                    n = 0
                wav = state.calls.audio((q.get("id") or [""])[0], n)
                if wav is None:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, wav, "audio/wav")
            if u.path == "/api/cliproxy":  # Claude Code proxy accounts (CLIProxyAPI)
                from alice.app import cliproxy

                return self._json(cliproxy.status())
            if u.path == "/api/remote/windows":
                from alice.app import remote

                return self._json({"windows": remote.windows()})
            if u.path == "/api/history":  # sid: active chat of that device (phone), otherwise foreground
                return self._json(state.history_for((q.get("sid") or [""])[0]))
            if u.path == "/api/sessions":
                rows = []
                for i, c, t in sess.list_sessions(60):
                    try:
                        updated = (sess.sessions_dir() / f"{i}.json").stat().st_mtime
                    except OSError:
                        updated = 0
                    rows.append({"id": i, "cwd": c, "title": t, "updated": updated})
                meta = sess.load_meta()
                for r in rows:
                    m = meta.get(r["id"]) or {}
                    if m.get("auto_title"):  # title generated from the first prompt; otherwise start of prompt
                        r["title"] = m["auto_title"]
                    if m.get("title"):
                        r["auto_title"], r["title"] = r["title"], m["title"]
                    r["pinned"] = bool(m.get("pinned"))
                return self._json(rows)
            if u.path == "/api/changes":
                return self._json(session_changes(state.agent.checkpoints, state.ctx.cwd))
            if u.path == "/api/projects":
                return self._json(state.projects())
            if u.path == "/api/project/templates":
                from alice.team import list_templates

                return self._json(list_templates())
            if u.path == "/api/project/check_guess":
                from alice.team import guess_check

                raw = (q.get("cwd") or [""])[0]
                path = state.ctx.resolve(raw) if raw else state.ctx.cwd
                return self._json({"check": guess_check(path) if path.is_dir() else ""})
            if u.path == "/api/project/diff":
                from alice.tools.base import ToolError

                try:
                    team = state.get_team((q.get("id") or [""])[0])
                    return self._json({"diff": team.diff((q.get("member") or [""])[0])})
                except ToolError as e:
                    return self._json({"ok": False, "error": str(e)}, 404)
            if u.path == "/api/project":
                from alice.tools.base import ToolError

                try:
                    return self._json(state.project_detail((q.get("id") or [""])[0], (q.get("member") or [""])[0]))
                except ToolError as e:
                    return self._json({"ok": False, "error": str(e)}, 404)
            if u.path == "/api/usage":
                # Session breakdown (Usage Breakdown dialog) + today's request count per model (Settings > Usage).
                return self._json({**state.agent.usage_breakdown(), "today": state.router.usage.today()})
            if u.path == "/api/prs":
                return self._json(state.pr_panel(force=(q.get("force") or [""])[0] == "1"))
            if u.path == "/api/subagents":
                return self._json(state.subagents_view())
            if u.path in ("/api/browser/frame", "/api/browser/tabs"):
                from alice.app.builtin_browser import BrowserError

                bb = state.builtin_browser()
                if not bb.running():
                    return self._json({"ok": False, "idle": True, "error": "yerleşik tarayıcı kapalı"})
                try:
                    if u.path == "/api/browser/tabs":
                        return self._json({"ok": True, "tabs": [{k: t[k] for k in ("id", "title", "url")}
                                                                for t in bb.tabs()], "current": bb.current()["id"]})
                    return self._json(bb.frame(int((q.get("q") or ["60"])[0])))
                except (BrowserError, OSError) as e:
                    return self._json({"ok": False, "error": str(e)[:200]})
            if u.path == "/api/file_preview":
                # "Preview" on send_file card: only files displayed to user by ALICE (not arbitrary paths).
                from alice.tools.meta import SENT_FILES

                return self._json(file_preview(str((q.get("path") or [""])[0]), SENT_FILES))
            if u.path == "/api/files":
                from alice.mentions import complete_paths

                return self._json(complete_paths(state.ctx.cwd, (q.get("q") or [""])[0]))
            if u.path == "/api/commands":
                from alice.commands import catalog

                return self._json(catalog(state.ctx.cwd))
            if u.path == "/api/dirs":  # directory picker: subdirectories, quick locations, recent paths
                from alice.app import folders
                from alice.tools.github import clone_root

                out = folders.listing((q.get("path") or [""])[0], (q.get("hidden") or [""])[0] == "1")
                if out.get("ok"):
                    out["places"] = folders.places(clone_root(state.cfg))
                    out["recent"] = folders.recent_dirs()
                return self._json(out)
            if u.path == "/api/github/repos":
                from alice.tools.base import ToolError

                try:
                    return self._json({"ok": True, "repos": state.github_repos((q.get("q") or [""])[0])})
                except ToolError as e:
                    return self._json({"ok": False, "error": str(e)})
            if u.path == "/api/connect":
                return self._json(state.connections())
            if u.path == "/api/assistant":
                return self._json(state.assistant())
            if u.path == "/api/voice":
                return self._json(state.voice_view())
            if u.path == "/api/models/scores":
                return self._json(state.setup.scores_view())
            if u.path == "/api/setup":
                return self._json(state.setup.view())
            if u.path == "/api/features":
                return self._json(state.features.view())
            if u.path == "/api/notes":
                return self._json(state.journal.notes_view((q.get("q") or [""])[0]))
            if u.path == "/api/logs":
                return self._json(state.journal.logs_view())
            if u.path == "/api/logs/eval":
                return self._json(state.journal.eval_view())
            if u.path == "/api/automations":
                return self._json(state.automations.view())
            if u.path == "/api/system":
                return self._json(state.system.view())
            if u.path == "/api/system/mictest":
                return self._json(state.system.mic_test.view())
            if u.path == "/api/voice/enroll":
                return self._json(state.enroll.view())
            if u.path == "/api/keys":
                return self._json(state.keys())
            if u.path == "/api/update":
                return self._json(state.check_update() if (q.get("refresh") or [""])[0] == "1"
                                  else (state.update.as_dict() if state.update is not None else {"available": False}))
            if u.path == "/api/tools":
                return self._json([{"name": t.name, "risk": t.risk, "description": t.description.split("\n")[0][:160]}
                                   for t in state.agent.tools.values()])
            if u.path == "/api/memory":
                return self._json(state.memory_view())
            if u.path == "/api/recap":
                return self._json(state.recap())
            if u.path == "/api/buddy":
                return self._json(state.buddy_view())
            if u.path == "/api/kairos":
                return self._json(state.kairos_view())
            if u.path == "/api/models/all":
                return self._json({"providers": state.router.catalog_view(),
                                   "brain": state.router.chain("brain"),
                                   "pinned": state.router.role_overrides.get("brain", [None])[0]})
            if u.path == "/api/models":
                return self._json({role: [{"ref": r, "status": st} for r, st in rows]
                                   for role, rows in state.router.status().items()})
            return self._send(404, b"not found", "text/plain")

        def _events(self) -> None:
            q = state.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(f"data: {json.dumps({'type': 'status', **state.status()})}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                state.bus.unsubscribe(q)

        # -------------------------------------------------------------- POST
        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if not self._host_ok() or not self._origin_ok() or not self._token_ok({}):
                return self._deny()
            try:
                d = self._body()
            except BadRequest as e:
                return self._json({"ok": False, "error": str(e)}, e.code)
            p = u.path
            try:
                if p == "/api/send":
                    text = str(d.get("text", "")).strip()
                    images = [str(i) for i in (d.get("images") or [])][:6]
                    if not text and not images:
                        return self._json({"ok": False, "error": "boş mesaj"}, 400)
                    ok, err = state.send(text or "Bu görsele bak.", images, str(d.get("sid") or ""))
                    return self._json({"ok": ok, "error": err}, 200 if ok else 409)
                if p == "/api/remote/action":
                    from alice.app import remote

                    try:
                        return self._json({"ok": True, "text": remote.action(d)})
                    except Exception as e:  # noqa: BLE001 - short reason for user
                        return self._json({"ok": False, "error": str(e)[:200]}, 400)
                if p == "/api/client_error":  # phone client page errors: ~/.alice/phone-errors.log
                    from datetime import datetime

                    try:
                        f = alice_home() / "phone-errors.log"
                        if f.exists() and f.stat().st_size > 500_000:
                            f.replace(f.with_suffix(".log.1"))
                        with f.open("a", encoding="utf-8") as fh:
                            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {str(d.get('text', ''))[:1500]}\n")
                    except OSError:
                        pass
                    return self._json({"ok": True})
                if p == "/api/watch":
                    if state.watch_store is None:
                        return self._json({"ok": False, "error": "saat verisi bu sunucuda kapalı"}, 404)
                    from alice.health.watch import ingest

                    return self._json({"ok": True, "added": ingest(state.watch_store, d)})
                if p == "/api/transcribe":
                    import base64

                    from alice.voice.stt import STTError

                    try:
                        data = base64.b64decode(str(d.get("audio", "")))
                        ext = "ogg" if "ogg" in str(d.get("mime", "")) else "webm"
                        return self._json({"ok": True, "text": state.transcribe(data, f"speech.{ext}")})
                    except (STTError, ValueError) as e:
                        return self._json({"ok": False, "error": str(e)}, 502)
                if p == "/api/connect/auth":
                    r = state.connect_auth(str(d.get("name", "")))
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/connect/logout":
                    return self._json(state.connect_logout(str(d.get("name", ""))))
                if p == "/api/assistant/set":
                    ok = state.assistant_set(str(d.get("section", "")), str(d.get("key", "")), d.get("value"))
                    return self._json({"ok": ok}, 200 if ok else 400)
                if p == "/api/voice/preview":
                    r = state.voice_preview(str(d.get("voice", "")))
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/features/action":
                    r = state.features.action(str(d.get("action", "")), d)
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/setup/action":
                    r = state.setup.action(str(d.get("action", "")), d)
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/notes/action":
                    r = state.journal.notes_action(str(d.get("action", "")), d)
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/logs/action":
                    r = state.journal.logs_action(str(d.get("action", "")), d)
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/automations/action":
                    r = state.automations.action(str(d.get("action", "")), d)
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/system/action":
                    r = state.system.action(str(d.get("action", "")), d)
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/voice/enroll":
                    r = state.enroll_action(str(d.get("action", "")))
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/voice/set":
                    r = state.set_voice(str(d.get("voice", "")))
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/buddy":
                    return self._json(state.buddy_action(str(d.get("action", "")), str(d.get("value", ""))))
                if p == "/api/kairos":
                    return self._json(state.kairos_action(str(d.get("action", ""))))
                if p == "/api/memory/action":
                    r = state.memory_action(str(d.get("action", "")), d)
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/feedback":
                    from alice.evals import record_feedback

                    new = record_feedback(str(d.get("say", ""))[:4000], str(d.get("reply", ""))[:4000],
                                          str(d.get("note", ""))[:1000], source="uygulama")
                    return self._json({"ok": True, "added": new})
                if p.startswith("/api/call/"):
                    return self._json(state.call_action(p.rsplit("/", 1)[1], d))
                if p.startswith("/api/cliproxy/"):
                    from alice.app import cliproxy

                    what = p.rsplit("/", 1)[1]
                    if what == "login":
                        return self._json(cliproxy.login_url(str(d.get("provider", ""))))
                    if what == "install":
                        from alice.app import cliproxy_setup

                        return self._json(cliproxy_setup.install_async())
                    if what == "remove":
                        return self._json(cliproxy.remove(str(d.get("name", ""))))
                if p == "/api/cliproxy/login/status":
                    from alice.app import cliproxy

                    return self._json(cliproxy.login_state(str(d.get("state", ""))))
                if p == "/api/cliproxy/login/cancel":
                    from alice.app import cliproxy

                    return self._json(cliproxy.cancel_login(str(d.get("state", ""))))
                if p == "/api/keys/set":
                    r = state.set_key(str(d.get("key", "")), str(d.get("value", "")))
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/keys/add":  # key pool: add another key to provider
                    r = state.add_key(str(d.get("base", "")), str(d.get("value", "")))
                    return self._json(r, 200 if r.get("ok") else 400)
                if p == "/api/keys/strategy":
                    return self._json(state.set_key_strategy(str(d.get("strategy", ""))))
                if p == "/api/providers/add":
                    return self._json(state.add_provider(d))
                if p == "/api/providers/remove":
                    return self._json(state.remove_provider(str(d.get("name", ""))))
                if p == "/api/assistant/action":
                    return self._json(state.assistant_action(str(d.get("action", "")), d))
                if p == "/api/update/mode":
                    mode = str(d.get("mode", ""))
                    ok = state.set_update_mode(mode)
                    return self._json({"ok": ok, "mode": state._update_mode()}, 200 if ok else 400)
                if p == "/api/update/check":
                    return self._json(state.check_update())
                if p == "/api/update/apply":
                    r = state.apply_update(force=bool(d.get("force")))
                    return self._json(r, 200 if r.get("ok") else 409)
                if p == "/api/stop":
                    state.stop(str(d.get("sid") or ""))
                    return self._json({"ok": True})
                if p == "/api/permission":
                    pid, ans = str(d.get("id")), str(d.get("answer"))
                    return self._json({"ok": any(ui.answer(pid, ans) for ui in state.all_uis())})
                if p == "/api/choice":
                    pid, ans = str(d.get("id")), str(d.get("answer") or "")
                    return self._json({"ok": any(ui.answer_choice(pid, ans) for ui in state.all_uis())})
                if p == "/api/reveal":
                    # Open file shown by send_file or reveal in folder. Only that list: prevent opening arbitrary
                    # paths from the UI.
                    from alice.tools.meta import SENT_FILES

                    path = str(d.get("path", ""))
                    if path not in SENT_FILES or not Path(path).exists():
                        return self._json({"ok": False, "error": "dosya bulunamadı"}, 404)
                    try:
                        if d.get("folder"):
                            if sys.platform == "win32":
                                subprocess.Popen(["explorer", "/select,", path])
                            else:
                                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open",
                                                  str(Path(path).parent)])
                        elif sys.platform == "win32":
                            os.startfile(path)  # type: ignore[attr-defined]
                        else:
                            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", path])
                    except OSError as e:
                        return self._json({"ok": False, "error": str(e)}, 500)
                    return self._json({"ok": True})
                if p == "/api/open":
                    url = str(d.get("url", ""))
                    if url.startswith(("http://", "https://")):
                        import webbrowser

                        webbrowser.open(url)
                        return self._json({"ok": True})
                    return self._json({"ok": False}, 400)
                if p == "/api/project/template/delete":
                    from alice.team import delete_template
                    from alice.tools.base import ToolError

                    try:
                        delete_template(str(d.get("name", "")))
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    return self._json({"ok": True})
                if p in ("/api/project/create", "/api/project/action"):
                    from alice.tools.base import ToolError

                    try:
                        if p == "/api/project/create":
                            pid = state.create_project(d)
                            return self._json({"ok": True, "id": pid})
                        pid = str(d.get("id", ""))
                        agent = d.get("agent") if isinstance(d.get("agent"), dict) else None
                        name = str(d.get("name", ""))
                        note = state.project_action(pid, str(d.get("action", "")),
                                                    name if d.get("action") == "answer" else name.lower(),
                                                    str(d.get("text", "")), agent)
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    team = state._teams().get(pid)
                    return self._json({"ok": True, "note": note, "team": team.snapshot() if team else None})
                if p == "/api/mode":
                    mode = d.get("mode")
                    if mode not in MODES:
                        return self._json({"ok": False}, 400)
                    state.agent.permissions.mode = mode
                    return self._json({"ok": True, **state.status()})
                # Switching chats does not stop running work: that chat continues in background (see Chat).
                if p == "/api/new":
                    if d.get("detached"):  # phone: new chat should not change the PC screen
                        return self._json({"ok": True, "sid": sess.new_session_id(), **state.status()})
                    err = state.new_session()
                    return self._json({"ok": not err, "error": err, **state.status()}, 409 if err else 200)
                if p == "/api/resume":
                    sid = str(d.get("id") or "").strip()
                    if not sid:  # empty ID opened the latest chat (sess.load("")): not the requested chat
                        return self._json({"ok": False, "error": "sohbet kimliği gerekli"}, 400)
                    if d.get("detached"):  # phone: open chat without switching the PC's foreground chat
                        hist = state.history_for(sid)
                        found = bool(hist) or state.find_chat(sid) is not None
                        return self._json({"ok": found, "sid": sid, "history": hist,
                                           "error": None if found else "sohbet bulunamadı", **state.status()})
                    ok = state.resume(sid)
                    return self._json({"ok": ok, "error": None if ok else "sohbet açılamadı (arkada çok fazla iş var "
                                       "ya da sohbet bulunamadı)",
                                       "history": state.history_for(),
                                       **state.status()})
                if p == "/api/dirs/mkdir":
                    from alice.app import folders

                    out = folders.make_dir(str(d.get("parent", "")), str(d.get("name", "")))
                    return self._json(out, 200 if out["ok"] else 400)
                if state.chat.busy and p in ("/api/cwd", "/api/compact", "/api/rewind", "/api/changes/revert",
                                             "/api/github/open"):
                    return self._json({"ok": False, "error": "önce mevcut işi durdur"}, 409)
                if p == "/api/cwd":
                    path = state.ctx.resolve(str(d.get("path", "")))
                    if not path.is_dir():
                        return self._json({"ok": False, "error": f"klasör yok: {path}"}, 400)
                    state.set_cwd(path)
                    return self._json({"ok": True, **state.status()})
                if p == "/api/github/open":
                    from alice.tools.base import ToolError

                    try:
                        note = state.open_github(str(d.get("repo", "")), str(d.get("branch", "")))
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    return self._json({"ok": True, "note": note, **state.status()})
                if p == "/api/rewind":
                    try:
                        out = state.rewind(int_field(d, "mi", -1))
                    except ValueError as e:  # invalid message index: client error
                        return self._json({"ok": False, "error": str(e)}, 400)
                    return self._json({"ok": True, **out, "history": state.history_for(),
                                       **state.status()})
                if p == "/api/changes/revert":
                    ok = revert_change(state.agent.checkpoints, str(d.get("path", "")))
                    return self._json({"ok": ok, "changes": session_changes(state.agent.checkpoints, state.ctx.cwd)},
                                      200 if ok else 404)
                if p.startswith(("/api/subagents/", "/api/agents/")):
                    return self._json(*state.subagents_action(p, d))
                if p.startswith("/api/prs/"):
                    # PR panel: watch / unwatch, behavior flags, rerun failed CI.
                    from alice import prwatch
                    from alice.tools.base import ToolError

                    repo, number = str(d.get("repo", "")), int_field(d, "number")
                    if not repo or not number:
                        return self._json({"ok": False, "error": "repo ve number gerekli"}, 400)
                    try:
                        if p == "/api/prs/watch":
                            local = ""
                            try:
                                from alice.tools.github import origin_repo, repo_root

                                if origin_repo(state.ctx.cwd) == repo:
                                    local = str(repo_root(state.ctx.cwd))
                            except ToolError:
                                pass
                            note = prwatch.watch(repo, number, local, session=state.sid)
                        elif p == "/api/prs/unwatch":
                            note = prwatch.unwatch(repo, number)
                        elif p == "/api/prs/flag":
                            if str(d.get("flag")) == "auto_merge" and d.get("value") and not d.get("confirmed"):
                                return self._json({"ok": False, "error": "otomatik birleştirme onay ister"}, 400)
                            if not prwatch.set_flag(repo, number, str(d.get("flag", "")), bool(d.get("value"))):
                                return self._json({"ok": False, "error": "izlenmiyor ya da bilinmeyen ayar"}, 404)
                            note = "kaydedildi"
                        elif p == "/api/prs/rerun":
                            from alice.tools.github import GitHubAPI, github_token, rerun_failed

                            api = GitHubAPI(github_token(state.cfg))
                            sha = api.get(f"/repos/{repo}/pulls/{number}")["head"]["sha"]
                            done = rerun_failed(api, repo, sha)
                            note = ("yeniden başlatıldı: " + ", ".join(done)) if done else "başarısız iş yok"
                        else:
                            return self._json({"ok": False, "error": "bilinmeyen işlem"}, 404)
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)[:200]})
                    prwatch._live_cache.pop(f"{repo}#{number}", None)
                    return self._json({"ok": True, "note": note})
                if p == "/api/browser/input":
                    # Built-in browser in panel: user takes over control by clicking and typing on the image.
                    from alice.app.builtin_browser import BrowserError

                    bb = state.builtin_browser()
                    kind = str(d.get("type", ""))
                    try:
                        if kind == "start":
                            bb.start()
                        elif kind == "click":
                            bb.click(float(d["x"]), float(d["y"]), str(d.get("button") or "left"), int(d.get("count") or 1))
                        elif kind == "scroll":
                            bb.scroll(float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("dy", 0)))
                        elif kind == "text":
                            bb.type_text(str(d.get("text", ""))[:5000])
                        elif kind == "key":
                            bb.key(str(d.get("key", "")), int(d.get("modifiers") or 0))
                        elif kind == "navigate":
                            bb.navigate(str(d.get("url", "")))
                        elif kind in ("back", "forward", "reload"):
                            bb.history(kind)
                        elif kind == "tab":
                            bb.select(str(d.get("id", "")))
                        elif kind == "new_tab":
                            bb.new_tab(str(d.get("url") or "about:blank"))
                        elif kind == "close_tab":
                            bb.close_tab(str(d.get("id", "")))
                        else:
                            return self._json({"ok": False, "error": "bilinmeyen işlem"}, 400)
                    except (BrowserError, OSError, KeyError, ValueError) as e:
                        return self._json({"ok": False, "error": str(e)[:200]})
                    return self._json({"ok": True})
                if p in ("/api/git/branches", "/api/git/switch"):
                    from alice.tools.base import ToolError
                    from alice.tools.github import github_token, list_branches, switch_branch

                    try:
                        if p == "/api/git/branches":
                            return self._json({"ok": True, "branches": list_branches(
                                state.ctx.cwd, fetch=True, token=github_token(state.cfg))[:60]})
                        if state.chat.busy or state.running():
                            # Background chats might read/write the same repo: don't switch branch under them.
                            return self._json({"ok": False, "error": "ALICE çalışırken dal değiştirilmez; arkadakiler "
                                               "dahil işler bitsin ya da durdur"}, 409)
                        note = switch_branch(state.ctx.cwd, str(d.get("branch", "")), bool(d.get("create")),
                                             github_token(state.cfg))
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    state.bus.publish({"type": "status", **state.status()})
                    return self._json({"ok": True, "note": note, **state.status()})
                if p == "/api/session/delete":
                    sid = str(d.get("id", ""))
                    live = state.find_chat(sid)
                    # Running chat (background or foreground: deletable from phone) would re-save itself on finish.
                    if live is not None and live.busy:
                        return self._json({"ok": False, "error": "bu sohbet arkada çalışıyor; önce bitsin"}, 409)
                    return self._json(state.delete_session(sid))
                if p in ("/api/session/rename", "/api/session/pin"):
                    sid = str(d.get("id", ""))
                    f = sess.sessions_dir() / f"{sid}.json"
                    if not sid or f.parent != sess.sessions_dir() or not f.exists():
                        return self._json({"ok": False, "error": "sohbet yok"}, 404)
                    if p == "/api/session/rename":
                        row = sess.set_meta(sid, title=str(d.get("title", "")).strip()[:120])
                    else:
                        row = sess.set_meta(sid, pinned=bool(d.get("pinned")))
                    return self._json({"ok": True, **row})
                if p == "/api/compact":
                    ok = state.agent.compact()
                    if ok:
                        state.chat.save()  # prevent archived messages in session file from appearing twice
                    return self._json({"ok": ok, **state.status()})
                if p == "/api/thinking":
                    from alice.config import set_config_value
                    from alice.providers.thinking import LEVELS

                    level = str(d.get("level", ""))
                    if level not in LEVELS:
                        return self._json({"ok": False, "error": f"seviye: {' | '.join(LEVELS)}"}, 400)
                    state.router.thinking_overrides["brain"] = level
                    set_config_value("thinking", "brain", level)  # persist across restarts
                    return self._json({"ok": True, **state.status()})
                if p == "/api/models/hide":
                    from alice.config import set_config_value

                    name, hide = str(d.get("name", "")).strip(), bool(d.get("hidden", True))
                    if not name or name.partition(":")[0] not in state.router.providers_cfg:
                        return self._json({"ok": False, "error": "bilinmeyen model ya da sağlayıcı"}, 400)
                    set_config_value("agent", "hidden_models", state.router.set_hidden(name, hide))
                    return self._json({"ok": True, **state.status()})
                if p == "/api/model":
                    role, ref = str(d.get("role", "")), str(d.get("ref", ""))
                    if ref == "reset":
                        state.router.role_overrides.pop(role, None)
                    else:
                        parse_ref(ref)
                        state.router.role_overrides[role] = [ref]
                    if role == "brain":
                        from alice.config import set_config_value

                        pin = "" if ref == "reset" else ref
                        state.cfg.data.setdefault("agent", {})["pinned_brain"] = pin
                        try:
                            set_config_value("agent", "pinned_brain", pin)  # persist across restarts
                        except OSError:
                            pass
                    return self._json({"ok": True})
            except BadRequest as e:
                return self._json({"ok": False, "error": str(e)}, e.code)
            except Exception as e:  # noqa: BLE001
                log_error()
                return self._json({"ok": False, "error": str(e)}, 500)
            return self._send(404, b"not found", "text/plain")

    return Handler


class QuietHTTPServer(ThreadingHTTPServer):
    """Browser/phone closing the connection itself (tab closed, page refreshed, network changed) is not an error.
    The default server dumped a 20-line traceback to core log on each one (65 times a day): real errors
    were lost in between."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class AppServer:
    def __init__(self, state: AppState, port: int = 0, host: str = "127.0.0.1", owns_state: bool = True):
        self.state = state
        self.owns_state = owns_state  # in core, quick bar/phone share state: shutdown is owner's responsibility
        self.httpd = QuietHTTPServer((host, port), make_handler(state))
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="alice-http")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/?t={self.state.token}"

    def start(self) -> "AppServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        if self.owns_state:
            self.state.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()

