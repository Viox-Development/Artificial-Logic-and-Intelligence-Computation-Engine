"""Selective wake-up: ALICE listens less closely while the user is talking with others or playing a game.

Most false wake-ups in voice.log came from the user mentioning ALICE while talking with friends on Discord
("Şuan Alice ya", roughly "right now, Alice, you know") or from game sound. When Discord/TeamSpeak/Zoom/Teams
is using the microphone right now, or DCS/War Thunder is open:

* "Alice" must be at the start of the sentence ("Alice, haritayı aç" = "Alice, open the map", wakes it;
  "bak şimdi Alice ..." = "look, now Alice ...", does not),
* the follow-up window without "Alice" closes (a sentence said to a friend must not go to ALICE).

Who is using the microphone is read from the Windows privacy log (the "currently in use" info under Settings >
Privacy > Microphone): if an app's LastUsedTimeStop value is 0, the microphone is open right now. On Linux the
recording streams come from PulseAudio/PipeWire (`pactl list source-outputs`). Discord being
open is not enough; it must be in a voice channel. Config: [voice] social_mode = false turns it off; social_apps
extends the list.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable

# exe name (lowercase, without extension) -> display name. Selective mode kicks in if it is using the microphone.
VOICE_APPS = {"discord": "Discord", "teamspeak": "TeamSpeak", "ts3client": "TeamSpeak", "zoom": "Zoom",
              "teams": "Teams", "skype": "Skype", "whatsapp": "WhatsApp", "telegram": "Telegram"}
# Being open is enough: game sound and in-game voice chat.
GAMES = {"dcs.exe": "DCS", "aces.exe": "War Thunder", "aces": "War Thunder"}  # "aces": War Thunder on Linux
MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def pactl_mic_users(text: str) -> list[str]:
    """App names from `pactl list source-outputs` (each recording stream): its binary, else its name."""
    out: list[str] = []
    for block in re.split(r"(?m)^Source Output #", text)[1:]:
        props = dict(re.findall(r'(?m)^\s*([\w.]+) = "([^"]*)"', block))
        name = props.get("application.process.binary") or props.get("application.name") or ""
        if name and name not in out:
            out.append(name)
    return out


def mic_users() -> list[str]:
    """Apps currently using the microphone (registry names on Windows, binaries on Linux). Empty elsewhere."""
    if sys.platform.startswith("linux"):
        try:
            r = subprocess.run(["pactl", "list", "source-outputs"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return []  # no PulseAudio/PipeWire tools: selective mode then only looks at running games
        return pactl_mic_users(r.stdout) if r.returncode == 0 else []
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

    scan(MIC_KEY + r"\NonPackaged")  # desktop apps: "C:#Users#...#Discord.exe"
    scan(MIC_KEY)  # Store apps: "5319275A.WhatsAppDesktop_..."
    return out


def running_names() -> set[str]:
    try:
        import psutil

        return {(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
    except Exception:  # noqa: BLE001
        return set()


class SocialWatch:
    """reason(): if selective mode is on, the reason (e.g. "Discord sesli konuşmada", "in a Discord voice call"),
    otherwise "". The result is cached for a few seconds so the registry and process list are not scanned on every
    utterance."""

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
