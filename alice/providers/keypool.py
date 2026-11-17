"""Key pool: use multiple API keys of one provider together.

If GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3 ... exist in .env, requests are shared between them in turn
([agent] key_strategy = "round-robin", the default), or the first one is used until it runs out ("fill-first").
When a key returns an error:
  rate_limit (429) -> that key waits on that model for retry-after seconds (20 sec if none), and the same request
                      is tried with the next key
  quota            -> if it is a daily quota, that key is closed on that model until the reset (also written to
                      disk), otherwise 1 hour
  auth             -> the key is closed for this session (wrong or revoked), and work continues with the others
When all keys are used up, the last error goes to the router; the router puts that model in cooldown as usual.
Once the stream has started (the user has started seeing the text), the key is not changed;
the error goes to the router.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from alice.config import ProviderConfig, alice_home
from alice.providers.openai_compat import ProviderError

ROTATE = ("rate_limit", "quota", "auth")
COOLDOWN = {"rate_limit": 20.0, "quota": 3600.0}


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class KeyState:
    key: str
    client: Any
    dead: str = ""  # auth error: reason
    uses: int = 0
    errors: int = 0
    last_error: str = ""

    @property
    def fp(self) -> str:
        return fingerprint(self.key)


class KeyPool:
    def __init__(self, pc: ProviderConfig, make: Callable[[ProviderConfig], Any], strategy: str = "round-robin",
                 path: Path | None = None):
        self.pc = pc
        self.name = pc.name
        self.cfg = pc
        self.strategy = strategy if strategy in ("round-robin", "fill-first") else "round-robin"
        self.keys = [KeyState(k, make(replace(pc, key_override=k))) for k in pc.api_keys]
        self.until: dict[tuple[str, str], float] = {}  # (key fingerprint, model) -> monotonic
        self.kind: dict[tuple[str, str], str] = {}
        self._next = 0
        self._lock = threading.Lock()
        self.path = path or alice_home() / "key-cooldowns.json"
        self._load()

    # ------------------------------------------------------------------------------- persistent daily quota
    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8")).get(self.name) or {}
        except (OSError, ValueError, AttributeError):
            return
        wall, mono = time.time(), time.monotonic()
        for k, until in saved.items():
            fp, _, model = k.partition("|")
            try:
                left = float(until) - wall
            except (TypeError, ValueError):
                continue
            if left > 0:
                self.until[(fp, model)] = mono + left
                self.kind[(fp, model)] = "quota"

    def _save(self) -> None:
        wall, mono = time.time(), time.monotonic()
        mine = {f"{fp}|{model}": round(wall + (u - mono)) for (fp, model), u in self.until.items()
                if self.kind.get((fp, model)) == "quota" and u - mono > 600}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[self.name] = mine
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    # --------------------------------------------------------------------------------------- selection
    def _waiting(self, ks: KeyState, model: str, now: float) -> float:
        return max(self.until.get((ks.fp, model), 0.0), self.until.get((ks.fp, "*"), 0.0)) - now

    def order(self, model: str, now: float | None = None) -> list[KeyState]:
        now = time.monotonic() if now is None else now
        with self._lock:
            n = len(self.keys)
            start = self._next % n if self.strategy == "round-robin" and n else 0
            if self.strategy == "round-robin":
                self._next += 1
        ring = self.keys[start:] + self.keys[:start]
        return [k for k in ring if not k.dead and self._waiting(k, model, now) <= 0]

    def _penalize(self, ks: KeyState, model: str, e: ProviderError) -> None:
        from alice.providers.router import seconds_until_reset

        ks.errors += 1
        ks.last_error = str(e)[:200]
        if e.kind == "auth":
            ks.dead = ks.last_error
            return
        if e.kind == "quota":
            wait = seconds_until_reset(self.name, self.pc.base_url) if getattr(e, "daily", False) else COOLDOWN["quota"]
        else:
            wait = e.retry_after or COOLDOWN["rate_limit"]
        self.until[(ks.fp, model)] = time.monotonic() + max(1.0, min(wait, 36 * 3600))
        self.kind[(ks.fp, model)] = e.kind
        if e.kind == "quota":
            self._save()

    def _run(self, model: str, call: Callable[[Any, list[bool]], Any]) -> Any:
        keys = self.order(model)
        if not keys:
            now = time.monotonic()
            live = [k for k in self.keys if not k.dead]
            if not live:
                raise ProviderError("auth", f"{self.name}: bütün anahtarlar geçersiz")
            wait = min(self._waiting(k, model, now) for k in live)
            kinds = {self.kind.get((k.fp, model)) for k in live}
            kind = "quota" if kinds == {"quota"} else "rate_limit"
            raise ProviderError(kind, f"{self.name}: {len(live)} anahtarın hepsi beklemede ({int(wait)} sn)",
                                retry_after=wait, daily=kind == "quota" and wait > 3600)
        last: ProviderError | None = None
        for ks in keys:
            emitted = [False]
            try:
                res = call(ks.client, emitted)
            except ProviderError as e:
                if e.kind not in ROTATE or emitted[0]:
                    raise
                self._penalize(ks, model, e)
                last = e
                continue
            ks.uses += 1
            return res
        assert last is not None
        raise last

    # ----------------------------------------------------------------------- provider interface
    def chat(self, model: str, messages: list[dict[str, Any]], **kw: Any) -> Any:
        return self._run(model, lambda c, _e: c.chat(model, messages, **kw))

    def chat_stream(self, model: str, messages: list[dict[str, Any]], on_delta: Callable[[str], None],
                    **kw: Any) -> Any:
        def call(c: Any, emitted: list[bool]) -> Any:
            def delta(piece: str) -> None:
                emitted[0] = True
                on_delta(piece)

            return c.chat_stream(model, messages, delta, **kw)

        return self._run(model, call)

    def list_models(self) -> list[dict[str, Any]]:
        return self._run("*models*", lambda c, _e: c.list_models())

    def close(self) -> None:
        for ks in self.keys:
            close = getattr(ks.client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    def status(self) -> list[dict[str, Any]]:
        """For the UI: last 4 characters of each key, usage and waiting models (the key itself is not included)."""
        from alice.config import mask_secret

        now = time.monotonic()
        rows = []
        for i, ks in enumerate(self.keys):
            waits = {m: int(u - now) for (fp, m), u in self.until.items() if fp == ks.fp and u > now}
            state = "kapalı" if ks.dead else ("beklemede" if waits else "hazır")
            rows.append({"index": i + 1, "preview": mask_secret(ks.key), "state": state, "uses": ks.uses,
                         "errors": ks.errors, "waiting": waits, "error": ks.dead or ks.last_error})
        return rows


def is_pool(client: Any) -> bool:
    return isinstance(client, KeyPool)


def client_for(pc: ProviderConfig, make: Callable[[ProviderConfig], Any], strategy: str = "round-robin") -> Any:
    """A plain client for a provider with one key (or no key); a pool when there are several keys."""
    if len(pc.api_keys) > 1:
        return KeyPool(pc, make, strategy)
    return make(pc)
