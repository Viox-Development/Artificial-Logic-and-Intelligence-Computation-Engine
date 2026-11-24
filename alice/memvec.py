"""Semantic search for memory: turn text into a vector (embedding) and measure similarity with cosine.

Word matching cannot link "Unity projem" (my Unity project) with "oyun motorunda yaptığım şey" (the thing I made
in a game engine); vectors can. Vectors are produced by a free embedding model (default Gemini
`gemini-embedding-001`, OpenAI-compatible /embeddings endpoint) and stored in ~/.alice/memory.db. If there is no key,
no network, or the provider returns an error, memory works as before with word search; after an error it is not
retried for a while, so every conversation turn does not have to wait.
"""

from __future__ import annotations

import math
import os
import threading
import time
from array import array
from collections import OrderedDict
from typing import Any

# Gemini's free embedding quota is ~100 texts per minute: 64-item batches got a 429 on the second batch, and the work
# done was discarded unsaved (memory ended up with no vectors). Small batches; wait a minute once the quota is full.
BATCH = 20
QUERY_CACHE = 64
RATE_WAIT = 70.0  # sec: retry this long after the per-minute quota is exhausted


class RateLimited(RuntimeError):
    pass


def pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def top(query: list[float], rows: list[tuple[Any, bytes]], k: int = 10, min_score: float = 0.0) -> list[tuple[Any, float]]:
    """Top k rows for normalized vectors: [(ref, cosine)]. Uses numpy if present (fast on thousands of rows)."""
    if not rows or not query:
        return []
    dim = len(query)
    rows = [r for r in rows if len(r[1]) == dim * 4]  # if the model changed, skip vectors of the old dimension
    if not rows:
        return []
    try:
        import numpy as np

        mat = np.frombuffer(b"".join(b for _, b in rows), dtype=np.float32).reshape(len(rows), dim)
        scores = (mat @ np.asarray(query, dtype=np.float32)).tolist()
    except ImportError:
        scores = [sum(a * b for a, b in zip(query, unpack(b))) for _, b in rows]
    out = [(ref, float(s)) for (ref, _), s in zip(rows, scores) if s >= min_score]
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:k]


class Embedder:
    """OpenAI-compatible /embeddings endpoint. embed() may raise; the caller catches it and returns to word search."""

    def __init__(self, base_url: str, key: str, model: str, timeout: float = 4.0, client: Any = None,
                 cooldown: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.model = model
        self.timeout = timeout
        self.client = client
        self.cooldown = cooldown
        self.down_until = 0.0
        self._cache: "OrderedDict[str, list[float]]" = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg, options: dict[str, Any] | None = None) -> "Embedder | None":
        o = options if options is not None else dict((cfg.data.get("memory") if cfg is not None else None) or {})
        provider = str(o.get("embed_provider", "gemini"))
        if cfg is None or provider in ("", "off", "none"):
            return None
        pc = cfg.providers().get(provider)
        if pc is None:
            return None
        key = os.environ.get(pc.api_key_env, "").strip() if pc.api_key_env else ""
        if not key:
            return None
        return cls(pc.base_url, key, str(o.get("embed_model", "gemini-embedding-001")))

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.down_until

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.available:
            raise RuntimeError("embedding is temporarily off")
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), BATCH):
                out += self._post(texts[i:i + BATCH])
        except Exception as e:
            self.down_until = time.monotonic() + (RATE_WAIT if isinstance(e, RateLimited) else self.cooldown)
            raise
        return out

    def query(self, text: str) -> list[float]:
        """Vector of the query; recent queries are cached so the same question doesn't go to the network twice."""
        text = " ".join(text.split())[:2000]
        with self._lock:
            hit = self._cache.get(text)
            if hit is not None:
                self._cache.move_to_end(text)
                return hit
        vec = self.embed([text])[0]
        with self._lock:
            self._cache[text] = vec
            while len(self._cache) > QUERY_CACHE:
                self._cache.popitem(last=False)
        return vec

    def _post(self, batch: list[str]) -> list[list[float]]:
        import httpx

        c = self.client or httpx
        r = c.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": batch},
                   headers={"Authorization": f"Bearer {self.key}"}, timeout=self.timeout)
        if r.status_code == 429:
            raise RateLimited(f"embedding kotası doldu: {r.text[:120]}")
        if r.status_code >= 400:
            raise RuntimeError(f"embedding HTTP {r.status_code}: {r.text[:200]}")
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(batch):
            raise RuntimeError("the embedding answer is incomplete")
        return [normalize([float(x) for x in d["embedding"]]) for d in data]
