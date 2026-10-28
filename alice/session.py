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
    """Saniyeye kadar zaman. Aynı saniyede açılan sohbetler (biri arkada çalışırken yenisi) aynı dosyaya yazmasın
    diye gerekirse sonuna -2, -3 eklenir."""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    with _issued_lock:
        sid, n = base, 1
        while sid in _issued or (sessions_dir() / f"{sid}.json").exists():
            n += 1
            sid = f"{base}-{n}"
        _issued.add(sid)
    return sid


def save(session_id: str, messages: list[dict[str, Any]], cwd: Path) -> Path:
    path = sessions_dir() / f"{session_id}.json"
    # Görüntüler diske yazılmaz, oturum dosyaları küçük kalsın.
    body = {"cwd": str(cwd), "saved": datetime.now().isoformat(timespec="seconds"),
            "messages": strip_images(messages[1:])}
    # Önce geçici dosyaya yaz: her adımda kaydederken yarıda kalan bir yazma oturumu bozmasın.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def list_sessions(limit: int = 15) -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(sessions_dir().glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        first = next((m["content"] for m in data.get("messages", [])
                      if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        out.append((p.stem, data.get("cwd", ""), first[:70].replace("\n", " ")))
    return out


def load(session_id: str | None = None) -> tuple[str, list[dict[str, Any]]] | None:
    files = sorted(sessions_dir().glob("*.json"), reverse=True)
    if session_id:
        files = [f for f in files if f.stem == session_id]
    # Bozuk ya da yarım kalmış bir dosya (list_sessions'ın da atladığı) devam etmeyi çökertmesin: sıradakine geç.
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        return f.stem, data.get("messages", [])
    return None
