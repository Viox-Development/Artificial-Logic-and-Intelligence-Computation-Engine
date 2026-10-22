"""Kullanıcının sık kullandığı uygulama ve site adları: konuşma tanımaya ve Live'a ipucu.

Türkçe cümle içindeki İngilizce ürün adları ("GitHub", "YouTube Music") konuşma tanımada bozuluyor ("kitabım",
"Yusuf müziği"). Adları önceden bilmek hem Whisper'ın doğru yazmasına (initial_prompt) hem de modelin yanlış
duyduğu bir kelimeyi doğru ada eşlemesine yardım eder. Kaynaklar: genel bir liste, [voice] vocab ayarı, Başlat
menüsündeki kurulu programlar ve tarayıcı yer imleri. Hepsi yerel okunur; siteye/uygulamaya özel kod yok.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Iterable

COMMON = ["YouTube", "YouTube Music", "GitHub", "Gmail", "Google", "Google Drive", "Spotify", "Discord", "WhatsApp",
          "Instagram", "Twitter", "Netflix", "Twitch", "Steam", "Reddit", "ChatGPT", "Claude", "Gemini", "Opera",
          "Chrome", "Visual Studio Code", "Notepad"]

# Başlat menüsünde ad olarak işe yaramayan kısayollar
_SKIP = re.compile(r"uninstall|kaldır|readme|help|yardım|website|documentation|release notes|license|\.url$|"
                   r"manual|settings$|^\W|setup|update|^windows |powershell|command prompt|odbc|iscsi|recovery", re.I)

_SKIP_EXACT = {"administrative tools", "control panel", "run", "samples", "magnify", "narrator", "on-screen keyboard",
               "voiceaccess", "livecaptions", "idle", "desktop overlay host"}
_SKIP_END = ("module docs", "installer", "notifier")
_SYSTEM_DIRS = {"windows tools", "administrative tools", "accessibility", "system tools", "windows accessories",
                "windows powershell", "windows sistem araçları", "erişilebilirlik"}

PROFILES = [
    ("APPDATA", r"Opera Software\Opera GX Stable\Default"), ("APPDATA", r"Opera Software\Opera Stable\Default"),
    ("APPDATA", r"Opera Software\Opera GX Stable"), ("APPDATA", r"Opera Software\Opera Stable"),
    ("LOCALAPPDATA", r"Google\Chrome\User Data\Default"), ("LOCALAPPDATA", r"Microsoft\Edge\User Data\Default"),
    ("LOCALAPPDATA", r"BraveSoftware\Brave-Browser\User Data\Default"),
]


def _profiles() -> list[Path]:
    return [Path(os.environ[e]) / sub for e, sub in PROFILES if os.environ.get(e)]


def _clean(name: str) -> str:
    name = re.sub(r"\s*[\(\[].*?[\)\]]", "", name)  # "(x64)", "[Beta]"
    name = re.split(r"\s+[-|–:•]\s+", name)[0]  # "GitHub - Where the world builds" -> "GitHub"
    name = re.sub(r"[^\w\s.+&'-]", "", name)  # emoji vb.
    return re.sub(r"\s+", " ", name).strip(" .")


def start_menu_apps(dirs: Iterable[Path] | None = None) -> list[str]:
    if dirs is None:
        dirs = [Path(os.environ[e]) / r"Microsoft\Windows\Start Menu\Programs"
                for e in ("APPDATA", "PROGRAMDATA") if os.environ.get(e)]
    out = []
    for d in dirs:
        try:
            files = list(Path(d).rglob("*.lnk"))
        except OSError:
            continue
        for f in files:
            name = _clean(f.stem)
            low = name.lower()
            if (f.parent.name.lower() in _SYSTEM_DIRS or _SKIP.search(f.stem) or low in _SKIP_EXACT
                    or low.endswith(_SKIP_END)):
                continue
            out.append(name)
    return out


def bookmark_names(files: Iterable[Path] | None = None) -> list[str]:
    if files is None:
        files = [d / "Bookmarks" for d in _profiles()]
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "url" and node.get("name"):
                out.append(_clean(str(node["name"])))
            for v in node.get("children", []) if isinstance(node.get("children"), list) else []:
                walk(v)
            if "roots" in node:
                for v in node["roots"].values():
                    walk(v)

    for f in files:
        try:
            walk(json.loads(Path(f).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def merge(groups: Iterable[Iterable[str]], limit: int = 80) -> list[str]:
    seen, out = set(), []
    for group in groups:
        for n in group:
            n = n.strip()
            if not 2 <= len(n) <= 28 or len(n.split()) > 4 or n.lower() in seen:
                continue
            seen.add(n.lower())
            out.append(n)
            if len(out) >= limit:
                return out
    return out


_cache: list[str] | None = None
_lock = threading.Lock()


def project_names(cfg=None) -> list[str]:
    """Kullanıcının projelerinin adları ("ALICE reposu", "FPS-Game"): sesli komutta en çok bozulan adlar bunlar."""
    from alice import projects

    return [n for n in projects.names(cfg, 15) if 2 < len(n) <= 40]


def memory_names(limit: int = 25) -> list[str]:
    """Hafızadaki özel adlar (kişiler, oyunlar, uygulamalar): cümle başı olmayan büyük harfli kelimeler."""
    from alice.config import alice_home

    if not (alice_home() / "memory.db").is_file():
        return []
    from alice.memory import shared_memory

    mem = shared_memory()
    if mem is None:
        return []
    out: list[str] = []
    for f in mem.facts(200):
        words = f.text.split()
        for w in words[1:]:
            w = w.strip(".,;:!?()\"'").split("'")[0].split("’")[0]
            if len(w) > 2 and w[0].isupper() and w not in out and w.lower() not in ("kullanıcı", "kullanıcının"):
                out.append(w)
        if len(out) >= limit:
            break
    return out[:limit]


def known_names(cfg=None, refresh: bool = False) -> list[str]:
    """Adlar listesi (ilk çağrıda hesaplanır, sonra önbellekten)."""
    global _cache
    with _lock:
        if _cache is not None and not refresh:
            return _cache
        v = (cfg.data.get("voice", {}) if cfg is not None else {})
        extra = [str(x) for x in v.get("vocab", []) or []]
        groups: list[Iterable[str]] = [extra, COMMON]
        if v.get("vocab_auto", True):
            for source in (lambda: memory_names(), lambda: project_names(cfg), bookmark_names, start_menu_apps):
                try:
                    groups.append(source())
                except Exception:  # noqa: BLE001 - ipucu; okunamazsa genel listeyle devam
                    pass
        _cache = merge(groups, int(v.get("vocab_limit", 80)))
        return _cache


def whisper_prompt(names: list[str], base: str = "Hey Alice.", max_chars: int = 600) -> str:
    """Whisper initial_prompt: ~224 jeton sınırı, uzun olursa baştakiler (en önemlileri) kalsın."""
    text = base
    for n in names:
        if len(text) + len(n) + 2 > max_chars:
            break
        text += (" " if text.endswith(".") else ", ") + n
    return text + "."
