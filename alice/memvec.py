"""Hafıza için anlam araması: metni vektöre çevir (embedding), benzerliği kosinüsle ölç.

Kelime eşleşmesi "Unity projem" ile "oyun motorunda yaptığım şey"i bağlayamıyor; vektörler bağlıyor. Vektörler
ücretsiz bir embedding modeliyle (varsayılan Gemini `gemini-embedding-001`, OpenAI uyumlu /embeddings ucu)
üretilir ve ~/.alice/memory.db içinde saklanır. Anahtar yoksa, ağ yoksa ya da sağlayıcı hata verirse hafıza
eskisi gibi kelime aramasıyla çalışır; hata sonrası bir süre denenmez ki her konuşma turu beklemesin.
"""

from __future__ import annotations

import math
import os
import threading
import time
from array import array
from collections import OrderedDict
from typing import Any

BATCH = 64
QUERY_CACHE = 64


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
    """Normalize edilmiş vektörlerle en benzer k satır: [(ref, kosinüs)]. numpy varsa onunla (binlerce satırda hızlı)."""
    if not rows or not query:
        return []
    dim = len(query)
    rows = [r for r in rows if len(r[1]) == dim * 4]  # model değişmişse eski boyuttaki vektörler atlanır
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
    """OpenAI uyumlu /embeddings ucu. embed() hata fırlatabilir; çağıran yakalar ve kelime aramasına döner."""

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
            raise RuntimeError("embedding geçici olarak kapalı")
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), BATCH):
                out += self._post(texts[i:i + BATCH])
        except Exception:
            self.down_until = time.monotonic() + self.cooldown
            raise
        return out

    def query(self, text: str) -> list[float]:
        """Arama cümlesinin vektörü (son aramalar hatırlanır: aynı soru iki kez ağa çıkmasın)."""
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
        if r.status_code >= 400:
            raise RuntimeError(f"embedding HTTP {r.status_code}: {r.text[:200]}")
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(batch):
            raise RuntimeError("embedding cevabı eksik")
        return [normalize([float(x) for x in d["embedding"]]) for d in data]
