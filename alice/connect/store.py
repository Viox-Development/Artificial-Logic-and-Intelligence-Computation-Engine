"""Bağlantı kimlikleri: ~/.alice/connect.json.

API anahtarları `.env`'de durur (orası zaten anahtar yeri). Burada duran şey OAuth akışından dönen
token'lar: erişim token'ı, yenileme token'ı ve bitiş zamanı. Dosya yalnız kullanıcıya okunur
(chmod 600); Windows'ta zaten kullanıcı profilinin altında.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from alice.config import alice_home

_lock = threading.Lock()


def path() -> Path:
    return alice_home() / "connect.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Yazarken yarıda kalırsa eski dosya bozulmasın: önce geçici dosya, sonra yerine koy.
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".connect-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get(name: str) -> dict[str, Any]:
    with _lock:
        row = _read().get(name)
    return dict(row) if isinstance(row, dict) else {}


def save(name: str, **fields: Any) -> None:
    with _lock:
        data = _read()
        row = data.get(name)
        data[name] = {**(row if isinstance(row, dict) else {}), **fields}
        _write(data)


def forget(name: str) -> bool:
    with _lock:
        data = _read()
        if name not in data:
            return False
        del data[name]
        _write(data)
    return True


def names() -> list[str]:
    with _lock:
        return sorted(_read())


def expired(row: dict[str, Any], skew: float = 60.0) -> bool:
    """Erişim token'ı bitti mi (bitişten bir dakika önce bitmiş say, istek yolda eskimesin)."""
    exp = float(row.get("expires_at") or 0)
    return bool(exp and time.time() + skew >= exp)
