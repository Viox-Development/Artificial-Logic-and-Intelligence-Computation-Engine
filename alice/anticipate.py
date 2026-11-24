"""Actions of the anticipation engine: predicts from habits what to do and prepares it ahead of time.

  * Before a game (now.Anticipator, ~20 min before the expected War Thunder session): announcement voices are prepared
    (a hook of the voice module) and the game's Steam update is checked. If an update is pending, Steam is started in
    the background (`-silent`, without opening a window). If the game is set to "only update when launched" in Steam,
    Steam won't download it in advance: it says so once and does not change the setting for you.
  * In a match: when switching to flight mode, Windows "Rahatsız etmeyin" (Do Not Disturb) is turned on (notification
    popups of all apps, including Discord, are silenced; voice chat is not affected). When the flight ends it goes
    back to how it was; if it was already on, it is left alone. Windows has no official API for this: it is done via
    the focus assist state (WNF), and if that doesn't work it silently gives up.
  * Morning: the first things you asked in the morning over recent weeks (weather, schedule, e-mail...) are extracted
    from the logs. The answers are usually prepared before the time you wake up, and the voice assistant says them
    without waiting.

State is in ~/.alice/anticipate.json. The parts can be switched off individually with the [anticipate] settings.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from alice.config import alice_home

WT_APPID = "236390"
DND_STATE = 0x0D83063EA3BF1C75   # WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED: 0 off, 1 priority only, 2 alarms only
DND_ALARMS_ONLY = 2
MORNING = (5, 12)                # hours that count as morning
MIN_DAYS = 3                     # minimum number of days for a habit
MORNING_BEFORE = 20 * 60         # prepare this long before the time you usually wake up
READY_UNTIL = 13                 # a prepared answer counts as stale in the afternoon

TOPICS = {  # first thing asked in the morning -> topic
    "hava": r"\bhava\b|yağmur|sıcaklık|kaç derece|şemsiye",
    "program": r"program|takvim|bugün ne var|dersim|toplantı|staj",
    "posta": r"\bmail|e-?posta|gmail|mesaj(ım|lar)",
    "oyun": r"war thunder|güncelleme|\bdcs\b",
}


def state_path() -> Path:
    return alice_home() / "anticipate.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Two threads write the state file (QuietGame on mode change, pre-game preparation, morning). A lockless
# read-modify-write let one erase what the other had written (e.g. the note "Rahatsız etmeyin was off before"):
# DND stayed on after the flight. Changes are made under the lock with state_edit(), and the file is replaced
# atomically.
_STATE_LOCK = threading.RLock()


def save_state(st: dict[str, Any], path: Path | None = None) -> None:
    p = path or state_path()
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:  # Windows: a reader/antivirus scanner is holding the file for a moment
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass  # a leftover temp file is harmless


@contextmanager
def state_edit(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Read the state under the lock; when the block ends (if no error), write it atomically under the same lock."""
    with _STATE_LOCK:
        st = load_state(path)
        yield st
        save_state(st, path)


# ------------------------------------------------------------------ Steam
def _steam_root() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return None


def _vdf_values(text: str, key: str) -> list[str]:
    return re.findall(rf'"{key}"\s+"([^"]*)"', text)


def steam_manifest(appid: str = WT_APPID, root: Path | None = None) -> dict[str, str] | None:
    """Fields of the game's Steam manifest file (appmanifest_<id>.acf); None if not found."""
    root = root or _steam_root()
    if root is None:
        return None
    libs = [root]
    try:
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libs += [Path(p.replace("\\\\", "\\")) for p in _vdf_values(text, "path")]
    except OSError:
        pass  # no library list: only the main Steam folder is searched
    for lib in libs:
        f = lib / "steamapps" / f"appmanifest_{appid}.acf"
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return {k: (_vdf_values(text, k) or [""])[0] for k in ("name", "StateFlags", "AutoUpdateBehavior", "buildid")}
    return None


def update_state(manifest: dict[str, str] | None) -> dict[str, Any] | None:
    """{pending: is an update waiting, on_launch_only: does Steam download only when the game is launched}."""
    if not manifest:
        return None
    try:
        flags = int(manifest.get("StateFlags") or 0)
    except ValueError:
        return None
    return {"pending": bool(flags & 2) and not flags & 1024, "downloading": bool(flags & 1024),
            "on_launch_only": (manifest.get("AutoUpdateBehavior") or "0") == "1",
            "build": manifest.get("buildid") or "", "name": manifest.get("name") or "oyun"}


def steam_running(processes: set[str]) -> bool:
    return any(p.lower() == "steam.exe" for p in processes)


def start_steam_silent(root: Path | None = None) -> bool:
    root = root or _steam_root()
    exe = root / "steam.exe" if root else None
    if exe is None or not exe.exists():
        return False
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(exe), "-silent"], creationflags=flags, close_fds=True)  # noqa: S603
        return True
    except OSError:
        return False


def prep_game_update(processes: set[str], notify: Callable[[str, str], object], st: dict[str, Any],
                     manifest: dict[str, str] | None = None, start: Callable[[], bool] | None = None) -> str:
    """Before the game: if an update is pending, open Steam in the background, or say once if a setting is needed."""
    u = update_state(manifest if manifest is not None else steam_manifest())
    if not u or not (u["pending"] or u["downloading"]):
        return ""
    if u["downloading"]:
        return f"{u['name']} güncellemesi Steam'de iniyor."
    if u["on_launch_only"]:
        if st.get("update_told") != u["build"]:
            st["update_told"] = u["build"]
            notify(f"{u['name']} güncellemesi bekliyor",
                   "Steam'de bu oyun 'sadece oyunu açınca güncelle' ayarında, o yüzden güncelleme oyunu açınca inecek. "
                   "Steam → War Thunder → Özellikler → Güncellemeler → 'Bu oyunu her zaman güncel tut' seçersen "
                   "oyundan önce ben indirtirim.")
        return f"{u['name']} güncellemesi oyun açılınca inecek (Steam ayarı)."
    if not steam_running(processes) and (start or start_steam_silent)():
        return f"{u['name']} güncellemesi için Steam arka planda açıldı."
    return f"{u['name']} güncellemesi Steam'de bekliyor."


# ------------------------------------------------------------ do not disturb
def _wnf() -> tuple[Callable[[], int | None], Callable[[int], bool]] | None:
    if sys.platform != "win32":
        return None
    import ctypes

    nt = ctypes.windll.ntdll
    name = ctypes.c_ulonglong(DND_STATE)

    def read() -> int | None:
        stamp, size = ctypes.c_ulong(0), ctypes.c_ulong(4)
        buf = ctypes.c_uint32(0)
        st = nt.NtQueryWnfStateData(ctypes.byref(name), None, None, ctypes.byref(stamp), ctypes.byref(buf),
                                    ctypes.byref(size))
        return int(buf.value) if st == 0 and size.value == 4 else None

    def write(value: int) -> bool:
        buf = ctypes.c_uint32(value)
        st = nt.NtUpdateWnfStateData(ctypes.byref(name), ctypes.byref(buf), 4, None, None, 0, 0)
        return st == 0 and read() == value

    return read, write


class QuietGame:
    """Windows "Rahatsız etmeyin" (Do Not Disturb) in flight mode; restored when the flight ends. It remembers
    what it turned on itself (even if the core crashes, it reverts it on the next startup)."""

    def __init__(self, enabled: bool = True, wnf: tuple[Callable[[], int | None], Callable[[int], bool]] | None = None,
                 path: Path | None = None):
        self.enabled = enabled
        self.path = path
        self.wnf = wnf if wnf is not None else (_wnf() if enabled else None)

    def mode(self, mode: str) -> str:
        """Called when the mode changes; returns what it did ("" = nothing)."""
        if not self.enabled or self.wnf is None:
            return ""
        read, write = self.wnf
        with _STATE_LOCK:  # so another thread can't erase dnd_prev between read and write
            st = load_state(self.path)
            if mode == "flight":
                cur = read()
                if cur is None or cur != 0 or "dnd_prev" in st:
                    return ""  # unreadable, already on, or we already turned it on
                if write(DND_ALARMS_ONLY):
                    st["dnd_prev"] = cur
                    save_state(st, self.path)
                    return "Rahatsız etmeyin açıldı"
                return ""
            if "dnd_prev" in st:
                try:
                    prev = int(st.pop("dnd_prev"))
                except (TypeError, ValueError):
                    prev = 0
                save_state(st, self.path)
                if read() == DND_ALARMS_ONLY:  # if you changed it in the meantime, leave it alone
                    write(prev)
                    return "Rahatsız etmeyin eski haline döndü"
        return ""


# --------------------------------------------------------------------- morning
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2} .*?duyulan(?: \[[^\]]*\])?: (.*?)(?: \| cevap:|$)")


def morning_habits(lines: list[str]) -> tuple[Counter, list[int]]:
    """voice.log lines -> (counts of the topics asked first in the morning, each day's first voice event in minutes)."""
    first: dict[str, tuple[int, str]] = {}
    for ln in lines:
        m = _LINE.match(ln)
        if not m or not m.group(4).strip():
            continue
        day, h, mi, text = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        if MORNING[0] <= h < MORNING[1] and day not in first:
            first[day] = (h * 60 + mi, text)
    topics: Counter = Counter()
    for _minute, text in first.values():
        low = text.lower()
        for name, rx in TOPICS.items():
            if re.search(rx, low):
                topics[name] += 1
                break
    return topics, [m for m, _ in first.values()]


def _read_log_tail(path: Path, max_bytes: int = 4_000_000) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def session_lines(folder: Path | None = None, limit: int = 300) -> list[str]:
    """First question of the typed chats, in voice.log line format (file name is the start time: 20261102-074512)."""
    folder = folder or alice_home() / "sessions"
    out = []
    for f in sorted(folder.glob("*.json"), reverse=True)[:limit]:
        m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$", f.stem)
        if not m:
            continue
        try:
            msgs = json.loads(f.read_text(encoding="utf-8")).get("messages") or []
        except (OSError, ValueError, AttributeError):
            continue
        first = next((x.get("content") for x in msgs if x.get("role") == "user" and isinstance(x.get("content"), str)),
                     "")
        if first:
            y, mo, d, h, mi, s = m.groups()
            out.append(f"{y}-{mo}-{d} {h}:{mi}:{s} sohbet duyulan: {' '.join(first.split())[:200]}")
    return sorted(out)


def morning_plan(lines: list[str]) -> tuple[list[str], int | None]:
    """(topics to prepare, preparation time in minutes). ([], None) if there isn't enough history."""
    topics, starts = morning_habits(lines)
    if len(starts) < MIN_DAYS:
        return [], None
    wanted = [t for t, n in topics.most_common() if n >= MIN_DAYS]
    if not wanted:
        return [], None
    return wanted, max(MORNING[0] * 60, int(statistics.median(starts)) - MORNING_BEFORE // 60)


def prepare_morning(cfg: Any, topics: list[str], now: datetime,
                    fetch: dict[str, Callable[[], str]] | None = None) -> dict[str, str]:
    """Prepare the answers for the topics now. A topic that raises an error is skipped."""
    fetch = fetch if fetch is not None else _fetchers(cfg, now)
    out: dict[str, str] = {}
    for t in topics:
        fn = fetch.get(t)
        if fn is None:
            continue
        try:
            text = " ".join(str(fn() or "").split())
        except Exception:  # noqa: BLE001 - even if one source fails, the others still get prepared
            continue
        if text:
            out[t] = text[:300]
    return out


def _fetchers(cfg: Any, now: datetime) -> dict[str, Callable[[], str]]:
    from alice.extras import brief

    b = dict((getattr(cfg, "data", None) or {}).get("brief") or {})

    def program() -> str:
        return str(brief.gather(b, now=now, weather_fn=lambda c: "").get("bugün") or "")

    def posta() -> str:
        from alice.presence import unread_mail

        mails = unread_mail(cfg)
        return f"{len(mails)} önemli okunmamış e-posta: " + "; ".join(mails[:3]) if mails else "önemli yeni e-posta yok"

    def oyun() -> str:
        u = update_state(steam_manifest())
        if not u:
            return ""
        return f"{u['name']} güncellemesi bekliyor" if u["pending"] else f"{u['name']} güncel"

    return {"hava": lambda: brief.weather(str(b.get("city") or ""), auto=True), "program": program,
            "posta": posta, "oyun": oyun}


def ready_line(now: datetime | None = None, path: Path | None = None) -> str:
    """Morning answers prepared for today (added to the voice assistant's context); empty if none or stale."""
    now = now or datetime.now()
    st = load_state(path)
    ready = st.get("morning") or {}
    if ready.get("day") != f"{now:%Y-%m-%d}" or now.hour >= READY_UNTIL:
        return ""
    answers = ready.get("answers") or {}
    return "; ".join(f"{k}: {v}" for k, v in answers.items())


class Morning:
    """In the core loop: if the morning preparation time has come, prepare once a day."""

    def __init__(self, cfg: Any, log: Path | None = None, path: Path | None = None,
                 fetch: dict[str, Callable[[], str]] | None = None):
        self.cfg, self.log, self.path, self.fetch = cfg, log or alice_home() / "voice.log", path, fetch
        self._plan: tuple[list[str], int | None] | None = None
        self._plan_day = ""

    def check(self, now: datetime | None = None) -> dict[str, str] | None:
        now = now or datetime.now()
        day = f"{now:%Y-%m-%d}"
        st = load_state(self.path)
        if (st.get("morning") or {}).get("day") == day:
            return None
        if self._plan_day != day:
            lines = sorted(_read_log_tail(self.log) + session_lines(self.log.parent / "sessions"))  # voice + typed
            self._plan, self._plan_day = morning_plan(lines), day
        topics, at = self._plan or ([], None)
        if not topics or at is None or not at <= now.hour * 60 + now.minute < MORNING[1] * 60:
            return None
        answers = prepare_morning(self.cfg, topics, now, self.fetch)
        with state_edit(self.path) as st:
            st["morning"] = {"day": day, "at": time.time(), "answers": answers}
        return answers
