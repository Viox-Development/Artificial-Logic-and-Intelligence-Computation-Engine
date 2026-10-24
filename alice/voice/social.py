"""Seçici uyanma: kullanıcı başkalarıyla konuşurken ya da oyundayken ALICE daha az kulak kabartsın.

voice.log'da yanlış uyanmaların çoğu kullanıcının Discord'da arkadaşlarıyla konuşurken ALICE'ten bahsetmesinden
("Şuan Alice ya") ya da oyun sesinden geliyordu. Discord/TeamSpeak/Zoom/Teams mikrofonu o an kullanıyorsa ya da
DCS/War Thunder açıksa:

* "Alice" cümlenin başında olmalı ("Alice, haritayı aç" uyandırır; "bak şimdi Alice ..." uyandırmaz),
* "Alice" demeden devam etme penceresi kapanır (arkadaşına söylenen cümle ALICE'e gitmesin).

Mikrofonu kimin kullandığını Windows'un gizlilik kaydından okur (Ayarlar > Gizlilik > Mikrofon'daki "şu an
kullanıyor" bilgisi): uygulamanın LastUsedTimeStop değeri 0 ise mikrofon o an açık demektir. Discord'un sadece açık
olması yetmez, sesli kanalda olması gerekir. Config: [voice] social_mode = false ile kapanır; social_apps ile
liste genişletilir.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable

# exe adı (küçük harf, uzantısız parça) -> görünen ad. Mikrofonu o an kullanıyorsa seçici moda geçilir.
VOICE_APPS = {"discord": "Discord", "teamspeak": "TeamSpeak", "ts3client": "TeamSpeak", "zoom": "Zoom",
              "teams": "Teams", "skype": "Skype", "whatsapp": "WhatsApp", "telegram": "Telegram"}
# Açık olması yeter: oyun sesi ve oyun içi konuşma.
GAMES = {"dcs.exe": "DCS", "aces.exe": "War Thunder"}
MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def mic_users() -> list[str]:
    """Mikrofonu şu an kullanan uygulamalar (kayıttaki adlarıyla). Windows dışında boş."""
    if sys.platform != "win32":
        return []
    import winreg

    out: list[str] = []

    def scan(path: str) -> None:
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(root, name) as k:
                        start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                if start and not stop:
                    out.append(name)

    scan(MIC_KEY + r"\NonPackaged")  # masaüstü uygulamaları: "C:#Users#...#Discord.exe"
    scan(MIC_KEY)  # mağaza uygulamaları: "5319275A.WhatsAppDesktop_..."
    return out


def running_names() -> set[str]:
    try:
        import psutil

        return {(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
    except Exception:  # noqa: BLE001
        return set()


class SocialWatch:
    """reason(): seçici mod açıksa nedeni ("Discord sesli konuşmada"), değilse "". Sonuç birkaç saniye saklanır;
    her konuşmada kayıt ve süreç listesi taranmasın."""

    def __init__(self, cfg: Any = None, mics: Callable[[], list[str]] = mic_users,
                 procs: Callable[[], set[str]] = running_names, every: float = 10.0,
                 log: Callable[[str], None] = lambda s: None):
        v = ((getattr(cfg, "data", None) or {}).get("voice", {})) if cfg is not None else {}
        self.enabled = bool(v.get("social_mode", True))
        self.voice_apps = {**VOICE_APPS, **{str(a).lower(): str(a) for a in v.get("social_apps", []) or []}}
        self.mics, self.procs, self.every, self.log = mics, procs, every, log
        self._at = -1e9
        self._reason = ""
        self._lock = threading.Lock()

    def reason(self) -> str:
        if not self.enabled:
            return ""
        with self._lock:
            now = time.monotonic()
            if now - self._at < self.every:
                return self._reason
            self._at = now
            new = self._check()
            if new != self._reason:
                self.log(f"seçici uyanma: {new} ('Alice' cümlenin başında olmalı)" if new else "seçici uyanma kapandı")
            self._reason = new
            return new

    def _check(self) -> str:
        try:
            using = [u.lower().replace("#", "\\") for u in self.mics()]
        except Exception:  # noqa: BLE001
            using = []
        for key, label in self.voice_apps.items():
            if any(key in u.rsplit("\\", 1)[-1] for u in using):
                return f"{label} sesli konuşmada"
        names = self.procs()
        for exe, label in GAMES.items():
            if exe in names:
                return f"{label} açık"
        return ""
