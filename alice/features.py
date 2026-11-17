"""Optional features: what each one needs, whether it is installed, and installing it from the app.

The Windows installer only sets up ALICE itself and the app window. Voice, computer control, the browser, the
background core and the rest are optional: the first-run wizard and Settings → Features install the ones a person
picks, with pip in the background, and show the progress.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
WIN = os.name == "nt"


@dataclass(frozen=True)
class Feature:
    id: str
    extras: tuple[str, ...]        # pyproject extras to install
    modules: tuple[str, ...]       # importable when installed (all of them)
    size_mb: int                   # rough download size, shown on the card
    title: dict[str, str] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)
    needs: tuple[str, ...] = ()    # other features this one builds on
    windows_only: bool = False
    recommended: bool = False
    flag: tuple[str, str] | None = None  # config switch that must also be on, e.g. ("phone", "enabled")


FEATURES: tuple[Feature, ...] = (
    Feature("voice", ("voice",), ("sounddevice", "edge_tts", "webrtcvad", "pynput"), 60,
            {"en": "Voice", "tr": "Ses"},
            {"en": "Talk to ALICE: \"Hey Alice\", push-to-talk and spoken replies.",
             "tr": "ALICE ile konuş: \"Hey Alice\", bas-konuş ve sesli cevaplar."}, recommended=True),
    Feature("voice_local", ("voice-local",), ("faster_whisper",), 350,
            {"en": "Offline speech recognition", "tr": "Çevrimdışı konuşma tanıma"},
            {"en": "Hears \"Hey Alice\" on this PC instead of the cloud, and keeps working when cloud quota runs out.",
             "tr": "\"Hey Alice\"i buluta göndermeden bu bilgisayarda duyar; bulut kotası bitince de çalışır."},
            needs=("voice",)),
    Feature("computer", ("computer",), ("mss", "pyautogui"), 40,
            {"en": "Computer control", "tr": "Bilgisayar kontrolü"},
            {"en": "Screenshots, mouse and keyboard, app windows. Also needed for the game and flight coach.",
             "tr": "Ekran görüntüsü, fare ve klavye, uygulama pencereleri. Oyun ve uçuş koçu için de gerekir."},
            recommended=True),
    Feature("browser", ("browser",), ("playwright",), 60,
            {"en": "Browser automation", "tr": "Tarayıcı otomasyonu"},
            {"en": "ALICE opens pages, fills forms and reads sites in its own browser window.",
             "tr": "ALICE kendi tarayıcı penceresinde sayfa açar, form doldurur, siteleri okur."}, recommended=True),
    Feature("core", ("core",), ("pystray",), 5,
            {"en": "Background core", "tr": "Arka plan çekirdeği"},
            {"en": "Runs in the tray: notifications, scheduled tasks, \"Hey Alice\" and system warnings, "
                   "starting when you sign in.",
             "tr": "Tepside çalışır: bildirimler, zamanlanmış görevler, \"Hey Alice\" ve sistem uyarıları; "
                   "oturum açınca başlar."}, recommended=True),
    Feature("phone", ("home",), ("websockets",), 5,
            {"en": "Phone app", "tr": "Telefon uygulaması"},
            {"en": "Chat, notifications and remote control from your Android phone on the same Wi-Fi.",
             "tr": "Aynı Wi-Fi'daki Android telefondan sohbet, bildirim ve uzaktan kontrol."}, needs=("core",),
            flag=("phone", "enabled")),
)
BY_ID = {f.id: f for f in FEATURES}


def installed(f: Feature, cfg: Any = None) -> bool:
    """Packages importable and, when given a config, the feature's switch on (the phone shares voice's packages)."""
    if not all(importlib.util.find_spec(m) is not None for m in f.modules):
        return False
    if cfg is not None and f.flag is not None:
        return bool(cfg.data.get(f.flag[0], {}).get(f.flag[1]))
    return True


# ---------------------------------------------------------------------------------------------------- wizard state
def _state_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "setup.json"


def wizard_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_wizard_state(**changes: Any) -> dict[str, Any]:
    data = {**wizard_state(), **changes}
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------------------------------- install
def _python() -> str:
    """The venv's console python (the app may run under pythonw, whose pip output goes nowhere)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def pip_command(extras: list[str]) -> list[str]:
    spec = ",".join(sorted(set(extras)))
    target = f"{ROOT}[{spec}]" if (ROOT / "pyproject.toml").exists() else f"alice-harness[{spec}]"
    cmd = [_python(), "-m", "pip", "install", "--disable-pip-version-check"]
    return cmd + (["-e", target] if (ROOT / "pyproject.toml").exists() else [target])


def has_system_browser() -> bool:
    if not WIN:
        return any(shutil.which(b) for b in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"))
    pf, pf86 = os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")
    local = os.environ.get("LOCALAPPDATA", "")
    return any(Path(p).exists() for p in (
        rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Google\Chrome\Application\chrome.exe",
        rf"{local}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf}\Microsoft\Edge\Application\msedge.exe"))


def with_needs(ids: list[str]) -> list[str]:
    """The picked features plus what they build on, in catalog order, unknown ids dropped."""
    want: set[str] = set()

    def add(i: str) -> None:
        f = BY_ID.get(i)
        if f is None or i in want:
            return
        want.add(i)
        for n in f.needs:
            add(n)

    for i in ids:
        add(i)
    return [f.id for f in FEATURES if f.id in want]


Runner = Callable[[list[str], Callable[[str], None]], int]


def run_streaming(cmd: list[str], say: Callable[[str], None]) -> int:
    flags = subprocess.CREATE_NO_WINDOW if WIN else 0
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", creationflags=flags)
    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip()
        # pip prints a lot; keep the lines that say what is happening.
        if line.startswith(("Collecting", "Downloading", "Installing", "Successfully", "ERROR", "error")):
            say(line[:200])
    return p.wait()


class Installer:
    """One background install at a time; the app polls view()."""

    def __init__(self, runner: Runner = run_streaming,
                 after: Callable[[list[str], dict[str, Any]], list[str]] | None = None):
        self.runner, self.after = runner, after
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.s: dict[str, Any] = {"state": "idle", "lines": [], "ids": [], "done": [], "error": ""}

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {**self.s, "lines": list(self.s["lines"][-14:])}

    def _say(self, line: str) -> None:
        with self._lock:
            self.s["lines"].append(line)

    def start(self, ids: list[str], opts: dict[str, Any] | None = None) -> dict[str, Any]:
        ids = with_needs(ids)
        if not ids:
            return {"ok": False, "error": "no feature picked"}
        if self._thread is not None and self._thread.is_alive():
            return {"ok": False, "error": "an install is already running"}
        with self._lock:
            self.s = {"state": "running", "lines": [], "ids": ids, "done": [], "error": "", "started": time.time()}
        self._thread = threading.Thread(target=self._run, args=(ids, opts or {}), daemon=True, name="alice-features")
        self._thread.start()
        return {"ok": True, "ids": ids}

    def _run(self, ids: list[str], opts: dict[str, Any]) -> None:
        try:
            todo = [BY_ID[i] for i in ids if not installed(BY_ID[i])]
            extras = [e for f in todo for e in f.extras]
            if extras:
                self._say("pip install " + ", ".join(sorted(set(extras))))
                code = self.runner(pip_command(extras), self._say)
                if code != 0:
                    raise RuntimeError(f"pip exited with code {code}")
                importlib.invalidate_caches()
            if "browser" in ids and not has_system_browser():
                self._say("no Chrome or Edge found: downloading Chromium for the browser tools")
                if self.runner([_python(), "-m", "playwright", "install", "chromium"], self._say) != 0:
                    self._say("Chromium download failed; browser tools will retry when first used")
            missing = [i for i in ids if not installed(BY_ID[i])]
            if self.after is not None and not missing:
                for line in self.after(ids, opts):  # turn the features on: core autostart, voice, phone
                    self._say(line)
            with self._lock:
                self.s["done"] = [i for i in ids if i not in missing]
            with self._lock:
                self.s.update(state="error" if missing else "done",
                              error=("not importable after install: " + ", ".join(missing)) if missing else "")
        except Exception as e:  # noqa: BLE001 - shown in the app
            with self._lock:
                self.s.update(state="error", error=str(e) or type(e).__name__)


def catalog(lang: str = "en", cfg: Any = None) -> list[dict[str, Any]]:
    lang = lang if lang in ("en", "tr") else "en"
    out = []
    for f in FEATURES:
        if f.windows_only and not WIN:
            continue
        out.append({"id": f.id, "title": f.title.get(lang, f.id), "text": f.text.get(lang, ""), "size_mb": f.size_mb,
                    "needs": list(f.needs), "recommended": f.recommended, "installed": installed(f, cfg)})
    return out
