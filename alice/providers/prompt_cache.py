"""Prompt cache helpers shared by the agent and the providers.

- DYNAMIC_HEADING splits the system prompt into a static part (rules, personality, instructions, skills) and a dynamic
  part (environment: date, cwd; memory). The Claude client puts a cache marker on each part, so a new day or a new
  memory note rewrites only the dynamic tail and the long static part is still read from the cache.
- CacheBreakDetector (after Claude Code's promptCacheBreakDetection): before each model call it records hashes of the
  system prompt, every tool spec, the model and the request settings, and one hash per history message. When the next
  answer reads clearly fewer tokens from the cache than the previous one, it names the cause (system prompt changed,
  tool X changed, model switched, history rewritten from message N, TTL expired) and writes a diff of the system
  prompt when that changed. Drops ALICE causes on purpose (compaction, pruning) are announced with expect() and are
  not counted as unexpected.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("alice.cache")

DYNAMIC_HEADING = "# Session context (changes between chats)"
TTLS = ("5m", "1h")


def split_system(text: str) -> tuple[str, str]:
    """System prompt -> (static, dynamic). Without the heading everything is static."""
    i = text.find(DYNAMIC_HEADING)
    if i < 0:
        return text, ""
    return text[:i].rstrip(), text[i:]


def _sha(obj: Any) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


_LONG = 4096  # strings longer than this (base64 images, big tool outputs) are hashed by length + head + tail


def _lite(v: Any) -> Any:
    """A cheap stand-in for hashing: long strings shrink to length + head + tail. Screenshots are several MB of
    base64; serializing them in full before every step made the diagnostics cost O(steps x history)."""
    if isinstance(v, str):
        return v if len(v) <= _LONG else f"<{len(v)}:{v[:512]}…{v[-512:]}>"
    if isinstance(v, dict):
        return {k: _lite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_lite(x) for x in v]
    return v


def _msg_key(m: dict[str, Any]) -> str:
    # Only what the provider sees; ALICE's own bookkeeping keys (_image, _nudge, _display...) are ignored.
    return _sha(_lite({k: v for k, v in m.items() if not str(k).startswith("_")}))


def _mask_memory(text: str) -> str:
    """The memory section holds personal notes about the user: never copied into diff files on disk."""
    import re

    return re.sub(r"<memory>.*?</memory>", "<memory>[gizlendi]</memory>", text, flags=re.S)


@dataclass
class Snapshot:
    system: str
    system_hash: str
    tools: dict[str, str]
    ref: str
    settings: str
    messages: list[str]
    at: float


@dataclass
class CacheBreak:
    at: float
    ref: str
    before: int
    after: int
    reasons: list[str]
    expected: bool
    diff_path: str = ""

    def text(self) -> str:
        kind = "beklenen" if self.expected else "BEKLENMEYEN"
        return (f"{kind} cache break ({self.ref}): read {self.before:,} -> {self.after:,} tokens; "
                f"why: {'; '.join(self.reasons)}" + (f" (fark: {self.diff_path})" if self.diff_path else ""))


@dataclass
class CacheBreakDetector:
    log_dir: Path | None = None
    min_drop: int = 2000
    prev: Snapshot | None = None
    prev_cached: int | None = None
    pending: Snapshot | None = None
    expected: list[str] = field(default_factory=list)
    breaks: list[CacheBreak] = field(default_factory=list)
    unexpected: int = 0
    # id(message) -> (message object, key). Messages are replaced, not edited in place, when ALICE changes history
    # (pruning, trimming build new dicts), so the identity check is enough; the object is kept to make id() safe.
    _keys: dict[int, tuple[Any, str]] = field(default_factory=dict)

    def _key(self, m: dict[str, Any]) -> str:
        hit = self._keys.get(id(m))
        if hit is not None and hit[0] is m:
            return hit[1]
        k = _msg_key(m)
        self._keys[id(m)] = (m, k)
        return k

    def expect(self, reason: str) -> None:
        """ALICE is about to change the prefix on purpose (compaction, pruning, memory refresh)."""
        if reason not in self.expected:
            self.expected.append(reason)

    def reset(self) -> None:
        self.prev = self.pending = None
        self.prev_cached = None
        self.expected.clear()

    def before(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, ref: str,
               settings: Any = None) -> None:
        system = ""
        if messages and messages[0].get("role") == "system":
            c = messages[0].get("content")
            system = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)
        specs: dict[str, str] = {}
        for t in tools or []:
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            specs[str(fn.get("name") or "?")] = _sha(t)
        keys = [self._key(m) for m in messages[1:]]
        live = {id(m) for m in messages[1:]}
        if len(self._keys) > len(live) * 2 + 64:  # drop entries of messages no longer in the history
            self._keys = {i: v for i, v in self._keys.items() if i in live}
        self.pending = Snapshot(system, _sha(system), specs, ref, _sha(settings or {}), keys, time.monotonic())

    def after(self, cached: int, ref: str, ttl_s: float) -> CacheBreak | None:
        """Compare the answer's cache read with the previous one. Returns the break, if there was one."""
        cur = self.pending
        self.pending = None
        if cur is None:
            return None
        if ref:
            cur.ref = ref
        prev, prev_cached = self.prev, self.prev_cached
        self.prev, self.prev_cached = cur, cached
        expected = list(self.expected)
        self.expected.clear()
        if prev is None or prev_cached is None or prev_cached <= 0:
            return None
        drop = prev_cached - cached
        if drop < self.min_drop or cached > prev_cached * 0.95:
            return None
        reasons = self._reasons(prev, cur, ttl_s)
        diff_path = ""
        if prev.system_hash != cur.system_hash:
            diff_path = self._write_diff(prev.system, cur.system)
        # A model switch is a routing decision (already weighed against the cache by _sticky_role): reported, but not
        # counted as unexpected. Same for drops ALICE announced with expect().
        switched = bool(reasons) and all(r.startswith("model changed") for r in reasons)
        brk = CacheBreak(time.time(), cur.ref, prev_cached, cached, expected + reasons or ["bilinmiyor"],
                         expected=bool(expected) or switched, diff_path=diff_path)
        if not brk.expected:
            self.unexpected += 1
        self.breaks = (self.breaks + [brk])[-20:]
        self._log(brk)
        return brk

    @staticmethod
    def _reasons(prev: Snapshot, cur: Snapshot, ttl_s: float) -> list[str]:
        out: list[str] = []
        if prev.ref != cur.ref:
            out.append(f"model changed: {prev.ref} -> {cur.ref}")
        if prev.system_hash != cur.system_hash:
            out.append(f"system prompt changed ({len(cur.system) - len(prev.system):+d} characters)")
        added = [n for n in cur.tools if n not in prev.tools]
        removed = [n for n in prev.tools if n not in cur.tools]
        changed = [n for n in cur.tools if n in prev.tools and cur.tools[n] != prev.tools[n]]
        if added:
            out.append("tool added: " + ", ".join(added[:6]))
        if removed:
            out.append("tool removed: " + ", ".join(removed[:6]))
        if changed:
            out.append("tool schema changed: " + ", ".join(changed[:6]))
        if not added and not removed and list(prev.tools) != [n for n in cur.tools if n in prev.tools]:
            out.append("tool order changed")
        if prev.settings != cur.settings:
            out.append("request settings changed (effort/thinking/beta)")
        n = min(len(prev.messages), len(cur.messages))
        first = next((i for i in range(n) if prev.messages[i] != cur.messages[i]), None)
        if first is not None:
            out.append(f"the history changed from message {first + 1} on")
        elif len(cur.messages) < len(prev.messages):
            out.append("the history got shorter")
        if not out and cur.at - prev.at > ttl_s:
            out.append(f"TTL doldu ({(cur.at - prev.at) / 60:.0f} dk ara)")
        return out

    def _write_diff(self, before: str, after: str) -> str:
        if self.log_dir is None:
            return ""
        before, after = _mask_memory(before), _mask_memory(after)
        if before == after:
            return ""  # only the memory section changed: nothing to show without exposing it
        try:
            d = self.log_dir / "cache-breaks"
            d.mkdir(parents=True, exist_ok=True)
            files = sorted(d.glob("*.diff"))
            for old in files[:-19]:  # keep the last 20
                old.unlink(missing_ok=True)
            path = d / f"{time.strftime('%Y%m%d-%H%M%S')}.diff"
            path.write_text("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                         "before", "after")), encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    def _log(self, brk: CacheBreak) -> None:
        (log.info if brk.expected else log.warning)(brk.text())
        if self.log_dir is None:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / "cache_breaks.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {brk.text()}\n")
        except OSError:
            pass  # logging must not break a request
