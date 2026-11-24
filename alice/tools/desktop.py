"""Desktop: windows, app launching, clipboard and the Windows UI Automation tree.

ui_elements turns the clickable elements of the on-screen window into a list with IDs.
This list is given both to the brain model (so it can click without guessing pixels) and
to the Jev reflex loop as a candidate menu.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from alice.tools.base import COMPUTER, EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

INTERACTIVE = {"Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "MenuItem", "ListItem", "TabItem",
               "Hyperlink", "TreeItem", "SplitButton", "Slider", "Spinner", "DataItem", "MenuBar", "Document"}


# ------------------------------------------------------------------ windows
def fold(s: str) -> str:
    """Comparison key that ignores case and the Turkish i/ı distinction. "İ".lower() "i̇" (i + combining
    dot), so "indirilenler" could not be found in the title "İndirilenler"; ı and i are also mapped to each other."""
    return (s or "").casefold().replace("\u0307", "").replace("ı", "i")


def pick_window(wins: list[Any], title: str, text_of=lambda w: w.title or "", strict: bool = False) -> Any:
    """Pick a window by title: exact match first, then titles starting with the fragment, then titles containing it.
    With strict=True (for irreversible actions such as closing), if several different windows match it raises an
    error and lists the candidates, so the wrong one is not closed."""
    want = fold(title.strip())
    if not want:
        raise ToolError("the window title can't be empty")
    hits = [w for w in wins if want in fold(text_of(w))]
    if not hits:
        # Also list the open windows: the model kept guessing the title ("Setup - Sonora", "Setup - Sonora version 0.47.0")
        # and repeating the same error; seeing the real title, it picks the right one in a single step.
        names = list(dict.fromkeys(t.strip() for t in (text_of(w) for w in wins) if t and t.strip()))
        seen = "; ".join(n[:60] for n in names[:12])
        raise ToolError(f"no window titled '{title}'" + (f". Open windows: {seen}" if seen else ""))
    for test in (lambda t: t == want, lambda t: t.startswith(want)):
        best = [w for w in hits if test(fold(text_of(w)).strip())]
        if best:
            hits = best
            break
    names = list(dict.fromkeys(text_of(w) for w in hits))
    if strict and len(names) > 1:
        raise ToolError(f"'{title}' matches more than one window: " + " | ".join(names[:8])
                        + ". Say which one with the full title.")
    return hits[0]


def list_windows() -> list[str]:
    if IS_WIN or IS_MAC:
        try:
            import pygetwindow as gw
        except ImportError as e:
            raise ToolError("pygetwindow is not installed: pip install 'alice-harness[computer]'") from e
        return [t for t in gw.getAllTitles() if t.strip()]
    if shutil.which("wmctrl"):
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout
        return [line.split(None, 3)[-1] for line in out.splitlines() if len(line.split(None, 3)) == 4]
    raise ToolError("the window list on Linux needs wmctrl (sudo apt install wmctrl)")


def focus_window(title: str) -> str:
    if IS_WIN or IS_MAC:
        import pygetwindow as gw

        w = pick_window(gw.getAllWindows(), title)
        try:
            if getattr(w, "isMinimized", False):
                w.restore()
            w.activate()
        except Exception:  # noqa: BLE001 - Windows sometimes refuses to bring a window to the front (while a game is in front)
            if not IS_WIN:
                raise
        if IS_WIN and not force_foreground(getattr(w, "_hWnd", 0)):
            raise ToolError(f"could not bring '{w.title}' to the front (Windows didn't allow it; a fullscreen game may be open)")
        return w.title
    if not title.strip():
        raise ToolError("the window title can't be empty")
    _wmctrl(["-a", title], title)
    return title


def _wmctrl(args: list[str], title: str) -> None:
    try:
        r = subprocess.run(["wmctrl", *args], check=False, capture_output=True, timeout=10)
    except FileNotFoundError as e:
        raise ToolError("window actions on Linux need wmctrl (sudo apt install wmctrl)") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("wmctrl did not answer within 10 s") from e
    if r.returncode != 0:  # no matching window: must not silently report "brought to front"
        raise ToolError(f"no window titled '{title}'")


def force_foreground(hwnd: int) -> bool:
    """Really bring the window to the front. When the app in front (e.g. a full-screen game) belongs to another
    process, Windows silently rejects SetForegroundWindow; retry with Alt-key and thread-input attach tricks.
    Finally verify that the window is in front."""
    if not IS_WIN or not hwnd:
        return True
    import ctypes

    u = ctypes.windll.user32
    if u.GetForegroundWindow() == hwnd:
        return True
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    u.keybd_event(0x12, 0, 0, 0)  # press and release Alt: counts as "the last input was ours", which unlocks the foreground
    u.keybd_event(0x12, 0, 2, 0)
    u.SetForegroundWindow(hwnd)
    if u.GetForegroundWindow() != hwnd:
        fg = u.GetForegroundWindow()
        fg_thread = u.GetWindowThreadProcessId(fg, None)
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = fg_thread and fg_thread != me and u.AttachThreadInput(me, fg_thread, True)
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(me, fg_thread, False)
    time.sleep(0.1)
    return u.GetForegroundWindow() == hwnd


def window_op(title: str, op: str) -> str:
    ops = ("minimize", "maximize", "restore", "close")
    if op not in ops:
        raise ToolError(f"unknown window action: {op}. Valid: {', '.join(ops)}")
    if not title.strip():
        raise ToolError("the window title can't be empty")
    if not (IS_WIN or IS_MAC):
        if op == "close":
            _wmctrl(["-c", title], title)
        elif op == "maximize":
            _wmctrl(["-r", title, "-b", "add,maximized_vert,maximized_horz"], title)
        else:
            raise ToolError(f"Linux'ta {op} desteklenmiyor")
        return title
    import pygetwindow as gw

    w = pick_window(gw.getAllWindows(), title, strict=op == "close")
    {"minimize": w.minimize, "maximize": w.maximize, "restore": w.restore, "close": w.close}[op]()
    return w.title


def _shortcut_roots() -> list[Path]:
    return [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
            Path.home() / "Desktop", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]


SHORTCUT_SUFFIXES = (".lnk", ".url", ".appref-ms")
# URI (ms-settings:, mailto:, steam://...); the scheme is at least two letters, so a drive letter like "C:\..." does not match
URI_RX = re.compile(r"^[a-z][a-z0-9+.-]+:", re.I)
# File path that starts with a drive or contains a folder (not an app name): if it doesn't exist, no open is attempted
FILE_PATH_RX = re.compile(r"^([a-z]:[\\/]|\\\\|~[\\/]|%\w+%[\\/]|/)", re.I)


def missing_file(p: Path) -> str:
    """Missing file message + the newest similar files in the same folder (maybe downloaded under another name)."""
    msg = f"file not found: {p}"
    try:
        same = sorted((f for f in p.parent.iterdir() if f.is_file() and f.suffix.lower() == p.suffix.lower()),
                      key=lambda f: f.stat().st_mtime, reverse=True)[:5]
    except OSError:
        same = []
    if same:
        msg += ". Newest " + p.suffix + " files in the same folder: " + ", ".join(f.name for f in same) + \
            " (the file may have downloaded under another name; if unsure, download it again with browser action=download)"
    return msg


# Files that download when opened in the browser (installers, archives): downloaded by the extension, not the address bar
DOWNLOAD_RX = re.compile(r"\.(exe|msi|msix|zip|7z|rar|dmg|appimage|deb|iso)$", re.I)
SHORTCUT_TTL = 60.0  # sec: the shortcut list stays cached this long
_shortcut_cache: dict[tuple[str, ...], tuple[float, list[Path]]] = {}


def _shortcut_files() -> list[Path]:
    """Start menu shortcuts (with subfolders) and desktop shortcuts (top level only), briefly cached. The desktop
    used to be scanned with its subfolders too: on a desktop full of projects and videos each app launch took ~20 sec."""
    roots = _shortcut_roots()
    key = tuple(str(r) for r in roots)
    hit = _shortcut_cache.get(key)
    if hit and time.monotonic() - hit[0] < SHORTCUT_TTL:
        return hit[1]
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        found = root.glob("*") if root.name.lower() == "desktop" else root.rglob("*")
        files += [p for p in found if p.suffix.lower() in SHORTCUT_SUFFIXES]
    _shortcut_cache[key] = (time.monotonic(), files)
    return files


def _start_menu_lookup(name: str) -> Path | None:
    needle = fold(name.strip())
    if not needle:
        return None
    best: Path | None = None
    for p in _shortcut_files():
        stem = fold(p.stem)
        if stem == needle:
            return p
        if needle in stem and (best is None or len(stem) < len(best.stem)):
            best = p
    return best


def _norm(s: str) -> str:
    return "".join(ch for ch in fold(s) if ch.isalnum())


def _resolve_lnks(lnks: list[Path]) -> dict[Path, str]:
    """Resolve the target exe paths of the shortcuts in a single PowerShell call."""
    lnks = [p for p in lnks if p.suffix.lower() == ".lnk"]
    if not lnks:
        return {}
    paths = ",".join("'" + str(p).replace("'", "''") + "'" for p in lnks)
    ps = (f"$w=New-Object -ComObject WScript.Shell;foreach($p in @({paths}))"
          "{try{Write-Output ($p+'|'+$w.CreateShortcut($p).TargetPath)}catch{}}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15,
                           creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[Path, str] = {}
    for line in r.stdout.decode("utf-8", errors="replace").splitlines():
        lnk, _, target = line.strip().partition("|")
        if target:
            out[Path(lnk)] = target
    return out


def _steam_libraries() -> list[Path]:
    import re

    roots = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
             Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
    libs: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        libs.append(root / "steamapps" / "common")
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps" / "common")
        except OSError:
            pass  # unreadable Steam library list: use the default folder
    return [p for p in dict.fromkeys(libs) if p.is_dir()]


def _exes_in(folder: Path, needle: str, depth: int = 3) -> list[Path]:
    """Exes in the folder whose name contains the searched word (otherwise all exes in the folder); shallow search."""
    found: list[Path] = []
    stack = [(folder, 0)]
    while stack and len(found) < 40:
        d, lvl = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_file() and e.suffix.lower() == ".exe":
                found.append(e)
            elif e.is_dir() and lvl < depth:
                stack.append((e, lvl + 1))
    named = [f for f in found if needle in _norm(f.stem)]
    skip = ("unins", "setup", "crash", "update", "helper")
    return [f for f in (named or found) if not any(k in f.stem.lower() for k in skip)]


_START_APPS: list[tuple[str, str]] | None = None


def start_apps(refresh: bool = False) -> list[tuple[str, str]]:
    """ALL apps in the Start menu (including Microsoft Store / UWP: WhatsApp, Spotify...) -> (name, AppID).
    Store apps have no exe or shortcut; they are opened with shell:AppsFolder\\<AppID>."""
    global _START_APPS
    if _START_APPS is not None and not refresh:
        return _START_APPS
    apps: list[tuple[str, str]] = []
    if IS_WIN:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
                                 capture_output=True, timeout=15, creationflags=0x08000000)
            text = out.stdout.decode("utf-8", "replace") if out.returncode == 0 else ""
            for line in text.splitlines():
                name, _, app_id = line.partition("	")
                if name.strip() and app_id.strip():
                    apps.append((name.strip(), app_id.strip()))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # no Start menu app list: other sources still apply
    _START_APPS = apps
    return apps


def match_start_app(name: str) -> tuple[str, str] | None:
    needle = _norm(name)
    if not needle:
        return None
    apps = start_apps()
    for n, app_id in apps:  # exact match first, then partial match
        if _norm(n) == needle:
            return n, app_id
    for n, app_id in apps:
        if needle in _norm(n):
            return n, app_id
    return None


def find_installed_apps(name: str, limit: int = 15) -> list[str]:
    """Find where an installed program is: Start menu, registry (Uninstall, App Paths),
    Program Files, LOCALAPPDATA\\Programs and Steam libraries. One 'source: path' per line."""
    if not IS_WIN:
        exe = shutil.which(name)
        return [f"PATH: {exe}"] if exe else []
    needle = _norm(name)
    if not needle:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(src: str, path: str) -> None:
        path = path.strip().strip('"')
        if path and path.lower() not in seen and Path(path).exists():
            seen.add(path.lower())
            out.append(f"{src}: {path}")

    w = shutil.which(name)
    if w:
        add("PATH", w)
    for n, app_id in start_apps():
        if needle in _norm(n) and len(out) < limit:
            out.append(f"Start app ({n}): shell:AppsFolder\\{app_id}")

    # Start menu / desktop shortcuts (together with their target exes)
    lnks = [p for p in _shortcut_files() if needle in _norm(p.stem)]
    targets = _resolve_lnks(lnks[:20])
    for lnk in lnks[:20]:
        t = targets.get(lnk, "")
        if t.lower().endswith(".exe"):
            add("shortcut target", t)
        add("shortcut", str(lnk))

    # Registry
    try:
        import winreg

        hives = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
        for hive, path in hives:
            try:
                root = winreg.OpenKey(hive, path)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
                    disp = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                except OSError:
                    continue
                if needle not in _norm(disp):
                    continue
                for val in ("DisplayIcon", "InstallLocation"):
                    try:
                        v = str(winreg.QueryValueEx(sub, val)[0]).split(",")[0].strip('"')
                    except OSError:
                        continue
                    if v.lower().endswith(".exe"):
                        add(f"registry ({disp})", v)
                    elif v and Path(v).is_dir():
                        for exe in _exes_in(Path(v), needle, depth=2)[:3]:
                            add(f"registry ({disp})", str(exe))
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                root = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                key = winreg.EnumKey(root, i)
                if needle in _norm(Path(key).stem):
                    try:
                        add("App Paths", str(winreg.QueryValue(root, key)))
                    except OSError:
                        pass  # broken App Paths entry: skip it
    except ImportError:
        pass  # not Windows: no registry to search

    # Program folders and Steam: exes inside folders whose name matches
    bases = [Path(os.environ.get(k, "")) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    bases.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")
    bases += _steam_libraries()
    for base in dict.fromkeys(bases):
        if not str(base) or not base.is_dir():
            continue
        try:
            subs = list(base.iterdir())
        except OSError:
            continue
        for d in subs:
            if not d.is_dir():
                continue
            # also look one level deeper, e.g. "Blender Foundation\Blender 4.2"
            cands = [d] if needle in _norm(d.name) else []
            if not cands:
                try:
                    cands = [c for c in d.iterdir() if c.is_dir() and needle in _norm(c.name)]
                except OSError:
                    cands = []
            for c in cands:
                for exe in _exes_in(c, needle)[:3]:
                    add("folder", str(exe))
        if len(out) >= limit:
            break
    return out[:limit]


SCRIPT_EXT = (".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".scr", ".hta", ".com",
              ".reg", ".pif", ".cpl", ".jar", ".msp")
CMD_META = set('&|<>^%"')


def is_script_target(target: str) -> bool:
    t = target.strip().strip('"').lower()
    if re.match(r"^https?://", t):  # a web address opens in the browser; it is not executed
        return False
    # Windows ignores a trailing dot/space in a file name and the ::$DATA stream: "x.bat." runs x.bat.
    # file:///C:/x.bat also works via ShellExecute.
    t = re.sub(r"::\$data$", "", t.split("?", 1)[0] if t.startswith("file:") else t).rstrip(". ")
    return t.endswith(SCRIPT_EXT)


def open_app(target: str) -> str:
    """Open an app name, exe path, file or URL."""
    target = (target or "").strip()
    if not target:  # On Windows Path("") becomes "."; an empty target was opening the working folder
        raise ToolError("the target to open is empty")
    if re.match(r"https?://", target, re.I) and DOWNLOAD_RX.search(target.split("?", 1)[0]):
        # Installer/archive link: in the browser, Opera showed a "Farklı Kaydet" (Save As) window and waited there.
        # ALICE tried to run the installer before the file arrived. Download straight into İndirilenler (Downloads).
        from alice.tools import fetch_file

        return fetch_file.describe(fetch_file.download(target), opened=True)
    path = Path(os.path.expandvars(target)).expanduser()
    if FILE_PATH_RX.match(target) and not path.exists():
        # Nonexistent file path: on Windows the "last resort" launch opened a "not found" error window, and ALICE
        # kept pressing Enter in it (the missing Sonora-Setup.exe; the file had been downloaded as e.exe).
        raise ToolError(missing_file(path))
    if IS_WIN:
        if URI_RX.match(target):
            # ms-settings:nightlight, mailto:, steam:// ...: open directly; via search, Settings took 42 sec to open.
            os.startfile(target)  # type: ignore[attr-defined]
            return f"opened: {target}"
        p = Path(os.path.expandvars(target)).expanduser()
        if p.exists() or "://" in target or shutil.which(target):
            os.startfile(str(p) if p.exists() else (shutil.which(target) or target))  # type: ignore[attr-defined]
            return f"opened: {target}"
        if target.lower().startswith("shell:appsfolder"):
            subprocess.Popen(["explorer.exe", target])
            return f"opened: {target}"
        lnk = _start_menu_lookup(target)
        if lnk:
            os.startfile(str(lnk))  # type: ignore[attr-defined]
            return f"opened: {lnk.stem} ({lnk})"
        app = match_start_app(target)
        if app:  # every Start menu app, including Store apps (WhatsApp etc.)
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app[1]}"])
            return f"opened: {app[0]}"
        for line in find_installed_apps(target):
            path = line.split(": ", 1)[1]
            if path.lower().endswith(".exe"):
                os.startfile(path)  # type: ignore[attr-defined]
                return f"opened: {path}"
        # Last resort: the shell's own resolution (including App Paths entries). cmd.exe treats & | ^ as command
        # separators: "x&calc" must not run a second command (the target may have come from a web page).
        if CMD_META & set(target):
            raise ToolError(f"'{target}' not found (it contains an invalid character)")
        subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
        return (f"'{target}' was not found among installed programs; the start command was sent anyway. "
                "If no window opened, the program isn't installed or has a different name.")
    if IS_MAC:
        if Path(target).expanduser().exists() or "://" in target:
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["open", "-a", target])
        return f"opened: {target}"
    exe = shutil.which(target)
    if exe:
        subprocess.Popen([exe], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("gtk-launch") and not Path(target).exists():
        subprocess.Popen(["gtk-launch", target], start_new_session=True)
    else:
        subprocess.Popen(["xdg-open", target], start_new_session=True)
    return f"opened: {target}"


BROWSER_TITLES = {"opera": "Opera", "chrome": "Chrome", "msedge": "Edge", "brave": "Brave", "firefox": "Firefox",
                  "vivaldi": "Vivaldi", "yandex": "Yandex"}


def browser_window_title() -> str:
    """The name in the window title of the user's default browser (e.g. 'Opera')."""
    from alice.tools.browser import default_browser_exe

    exe = (default_browser_exe() or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for key, name in BROWSER_TITLES.items():
        if key in exe:
            return name
    return "Opera"


BLANK_TABS = ("yeni sekme", "new tab", "speed dial", "hızlı arama", "gx corner", "start page", "başlangıç")
SITE_TITLES = {"mail.google": "gmail", "music.youtube": "youtube music", "web.whatsapp": "whatsapp",
               "youtu.be": "youtube", "x.com": "/ x", "twitter": "/ x"}


def site_key(target: str) -> str | None:
    """The name a URL shows in the tab title: youtube.com/results?... -> 'youtube'. None for search text."""
    t = target.strip().lower()
    if " " in t or ("." not in t and "://" not in t):
        return None
    host = re.sub(r"^[a-z]+://", "", t).split("/", 1)[0].split(":", 1)[0]
    host = host[4:] if host.startswith("www.") else host
    for key, name in SITE_TITLES.items():
        if host.startswith(key):
            return name
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def is_site_root(target: str) -> bool:
    """Is it just a site address such as 'youtube.com' or 'https://www.youtube.com/' (no path or query)?"""
    rest = re.sub(r"^[a-z]+://", "", target.strip().lower()).split("/", 1)
    return len(rest) == 1 or rest[1].strip("/") == ""


def active_title() -> str:
    try:
        import pygetwindow as gw

        w = gw.getActiveWindow()
        return (w.title or "") if w else ""
    except Exception:  # noqa: BLE001
        return ""


def switch_tab(want: str, title: str | None = None, max_tabs: int = 40) -> str | None:
    """Switch to the tab whose title contains `want` in the user's open browser (cycling with Ctrl+Tab).
    Returns the tab title if found, otherwise None (and it goes back to the tab it started on)."""
    from alice.tools.computer import press_keys

    want = fold(want.strip())
    if not want:
        raise ToolError("the tab title can't be empty")
    focus_window(title or browser_window_title())
    time.sleep(0.2)
    start = active_title()
    if want in fold(start):
        return start
    for _ in range(max_tabs):
        press_keys("ctrl+tab")
        time.sleep(0.12)
        cur = active_title()
        if want in fold(cur):
            return cur
        if cur == start:
            break
    return None


def current_tab_url(title: str | None = None) -> str:
    """Address of the front tab in the user's browser (copied from the address bar; the clipboard is restored)."""
    from alice.tools.computer import press_keys

    try:
        import pyperclip
    except ImportError as e:
        raise ToolError("pyperclip is not installed") from e
    focus_window(title or browser_window_title())
    time.sleep(0.2)
    old = None
    try:
        old = pyperclip.paste()
    except Exception:  # noqa: BLE001
        pass
    pyperclip.copy("")
    press_keys("ctrl+l")
    time.sleep(0.1)
    press_keys("ctrl+c")
    time.sleep(0.15)
    press_keys("esc")
    url = (pyperclip.paste() or "").strip()
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:  # noqa: BLE001
            pass
    return url


def navigate_tab(target: str, title: str | None = None, new_tab: bool | None = None) -> str:
    """Navigate in the user's open browser (no CDP needed). If the site is already open in a tab, switch to it;
    if the front tab is blank, use it there, otherwise open a new tab. new_tab=True/False forces this."""
    from alice.tools.computer import press_keys, type_text

    name = title or browser_window_title()
    try:
        focus_window(name)
    except ToolError:
        open_app(target if "://" in target or site_key(target) else f"https://www.google.com/search?q={target}")
        return f"{name} wasn't open; opened in the default browser: {target}"
    where = None
    key = site_key(target)
    if new_tab is None and key:
        found = switch_tab(key, name)
        if found and is_site_root(target):  # just opening the site: switching to the open tab is enough, don't disturb what is in it
            return f"switched to the already open tab: {found}"
        if found:
            new_tab, where = False, f"in the open '{found}' tab"
    if new_tab is None:
        cur = active_title().lower()
        new_tab = not any(b in cur for b in BLANK_TABS)
    time.sleep(0.15)
    press_keys("ctrl+t" if new_tab else "ctrl+l")
    time.sleep(0.15)
    type_text(target)
    press_keys("enter")
    return f"opened {where or ('in a new tab' if new_tab else 'in the front tab')}: {target}"


# ------------------------------------------------------------------- UIA tree
def _foreground_uia(title: str | None):
    try:
        from pywinauto import Desktop
    except ImportError as e:
        raise ToolError("pywinauto is not installed: pip install 'alice-harness[computer]'") from e
    desk = Desktop(backend="uia")
    if title and title.strip():
        return pick_window(desk.windows(), title, text_of=lambda w: w.window_text() or "")
    import ctypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return desk.window(handle=hwnd).wrapper_object()


def collect_ui_elements(ctx: ToolContext, title: str | None = None, limit: int = 150,
                        budget: float = 5.0, hwnd: int = 0) -> tuple[str, list[dict[str, Any]]]:
    """Collect interactive elements of the front window (or the one given by title/handle). In Chromium/WebView2
    apps (WhatsApp, Discord...) the tree is huge: return whatever was found within budget seconds. hwnd: finding a
    window by title scans all windows (~1 sec); reflex looks at the same window on every step."""
    if not IS_WIN and sys.platform.startswith("linux"):
        from alice.tools import atspi

        try:
            title_text, items, wrappers = atspi.collect(title, limit, budget)
        except RuntimeError as e:
            raise ToolError(str(e)) from None
        ctx.state["ui_elems"] = wrappers
        ctx.state["ui_items"] = {i["id"]: i for i in items}
        return title_text, items
    if not IS_WIN:
        raise ToolError("ui_elements works on Windows (UI Automation) and Linux (AT-SPI) for now. On macOS use the computer "
                        "tool.")
    if hwnd:
        from pywinauto import Desktop

        win = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    else:
        win = _foreground_uia(title)
    deadline = time.monotonic() + budget
    if hasattr(win, "iter_descendants"):
        descendants = win.iter_descendants(depth=14)  # lazy, so we can stop once the time is up
    else:
        try:
            descendants = win.descendants(depth=14)
        except TypeError:
            descendants = win.descendants()
    items: list[dict[str, Any]] = []
    wrappers: dict[int, Any] = {}
    for el in descendants:
        if time.monotonic() > deadline:
            break
        try:
            info = el.element_info
            ctype = info.control_type
            if ctype not in INTERACTIVE or not el.is_visible():
                continue
            r = info.rectangle
            if r.width() <= 1 or r.height() <= 1:
                continue
            name = (info.name or "").strip()
            value = ""
            # A Document's value is the whole page text: in a WebView2 window (ALICE itself, Discord) reading it
            # could take a minute alone, and more than 80 characters is not used anyway.
            if ctype in ("Edit", "ComboBox"):
                try:
                    value = (el.get_value() if hasattr(el, "get_value") else el.window_text()) or ""
                except Exception:  # noqa: BLE001
                    value = ""
            if not name and not value and not info.automation_id and ctype != "Document":
                continue
            idx = len(items) + 1
            wrappers[idx] = el
            items.append({
                "id": idx, "role": ctype, "name": name[:80], "value": str(value)[:80],
                "auto_id": (info.automation_id or "")[:40],
                "enabled": el.is_enabled(),
                "center": ((r.left + r.right) // 2, (r.top + r.bottom) // 2),
                "rect": (r.left, r.top, r.right, r.bottom),
            })
            if len(items) >= limit:
                break
        except Exception:  # noqa: BLE001 - UIA elements can disappear while scanning
            continue
    ctx.state["ui_elems"] = wrappers
    ctx.state["ui_items"] = {i["id"]: i for i in items}
    return win.window_text(), items


def format_ui(title: str, items: list[dict[str, Any]]) -> str:
    rows = [f"window: {title}"]
    for i in items:
        bits = [f"[{i['id']}] {i['role']}"]
        if i["name"]:
            bits.append(f"'{i['name']}'")
        if i["value"]:
            bits.append(f"value='{i['value']}'")
        if not i["enabled"]:
            bits.append("(pasif)")
        rows.append(" ".join(bits))
    return "\n".join(rows)


SET_TEXT_SETTLE = 0.6  # sec: wait this long, then check whether the field value reverted


def _edit_value(el: Any) -> str | None:
    """The field's current value, or None if unreadable (then the typed text is assumed kept, so it is not typed twice)."""
    try:
        v = el.get_value() if hasattr(el, "get_value") else el.window_text()
    except Exception:  # noqa: BLE001
        return None
    return None if v is None else str(v)


def ui_act(ctx: ToolContext, element_id: int, action: str, text: str | None = None) -> str:
    from alice.tools.computer import as_number

    element_id = as_number(element_id, "element_id", int)
    text = None if text is None else str(text)
    wrappers = ctx.state.get("ui_elems") or {}
    el = wrappers.get(element_id)
    if el is None:
        raise ToolError("element not found; call ui_elements first (the list is refreshed on every call)")
    if action == "click":
        try:
            el.invoke()
        except Exception:  # noqa: BLE001 - elements without an invoke pattern
            el.click_input()
    elif action == "double_click":
        el.double_click_input()
    elif action == "right_click":
        el.right_click_input()
    elif action == "focus":
        el.set_focus()
    elif action == "set_text":
        if text is None:
            raise ToolError("set_text needs text")
        try:
            el.set_edit_text(text)
            # Some fields revert the value: Explorer's address bar (Gezgin), outside edit mode, went back to the old
            # address after ~1 sec, and Enter refreshed the old folder. If readable, check the value; otherwise type it.
            time.sleep(SET_TEXT_SETTLE)
            kept = _edit_value(el)
            ok = kept is None or kept.strip() == text.strip()
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            from alice.tools.computer import press_keys, type_text

            try:
                el.click_input()
            except Exception:  # noqa: BLE001
                el.set_focus()
            press_keys("ctrl+a")
            type_text(text)
    elif action == "type":
        el.set_focus()
        from alice.tools.computer import type_text

        type_text(text or "")
    elif action in ("toggle", "expand", "collapse", "select"):
        getattr(el, action)()
    else:
        raise ToolError(f"bilinmeyen aksiyon: {action}")
    time.sleep(0.2)
    item = (ctx.state.get("ui_items") or {}).get(element_id, {})
    return f"{action} -> [{element_id}] {item.get('role', '')} '{item.get('name', '')}'"


# ----------------------------------------------------------------------- tools
class Windows(Tool):
    name = "windows"
    description = ("Desktop windows and apps: open apps/files, bring a window to the front, minimize, close; find "
                   "an installed program; switch tabs and open addresses in the user's browser with the keyboard.\n"
                   "- To open an app, open is the first choice (Start menu and Store apps included); don't try to "
                   "click its icon.\n"
                   "- To find out whether a program is installed, or its exe path, use find; don't search with glob "
                   "or the shell.\n"
                   "- Work in an open window: find it with list, bring it forward with focus, then ui_elements/ui_act.\n"
                   "- To read/click on a web page use the browser tool first. Use navigate and switch_tab when "
                   "browser says it can't connect, or just to open a site/search.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "focus", "minimize", "maximize", "restore", "close", "open", "find",
                                              "navigate", "switch_tab"],
                   "description": "list: open windows | focus/minimize/maximize/restore/close: the window matching title | "
                   "open: open target (app name, exe, file or URL; a URL opens in the default browser) | find: "
                   "find the exe path of the target program (Start menu, registry, Program Files, Steam) | navigate: "
                   "go to target in the user's browser; uses the tab if the site is already open in one, the front "
                   "tab if it is empty, otherwise a new tab | switch_tab: switch to the tab whose title contains "
                   "target."},
        "title": {"type": "string", "description": "Part of the window title, e.g. 'Discord' or 'Not Defteri' (Notepad). "
                  "Required for focus/minimize/maximize/restore/close; for navigate/switch_tab, the browser window."},
        "target": {"type": "string", "description": "open: 'Discord', 'notepad', 'C:/path/file.pdf' or "
                   "'https://...' | find: program name | navigate: URL or search phrase, e.g. 'github.com' | "
                   "switch_tab: part of the tab title, e.g. 'Gmail'."},
        "new_tab": {"type": "boolean", "description": "For navigate: true new tab, false the front tab. Empty: "
                    "automatic."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("action") in ("list", "find"):
            return READ
        if args.get("action") == "open" and is_script_target(str(args.get("target") or "")):
            return EXEC  # running a script/exe is the same as running a command: it needs approval, like run_command
        return COMPUTER

    def run(self, ctx: ToolContext, action: str, title: str | None = None, target: str | None = None,
            new_tab: bool | None = None) -> ToolResult:
        from alice.tools.computer import as_bool

        title = str(title).strip() if title is not None else None
        target = str(target).strip() if target is not None else None
        new_tab = None if new_tab is None else as_bool(new_tab, "new_tab")
        if action == "list":
            return ToolResult("\n".join(list_windows()) or "no windows")
        if action == "find":
            if not target:
                raise ToolError("find needs target")
            hits = find_installed_apps(target)
            return ToolResult("\n".join(hits) if hits else f"'{target}' was not found among installed programs "
                              "(searched the Start menu, the registry, Program Files, Steam).")
        if action == "open":
            if not target:
                raise ToolError("open needs target")
            msg = open_app(target)
            if "://" not in target:  # wait for the app window to appear; opening a site is not worth waiting for
                time.sleep(1.5)
            return ToolResult(msg)
        if action == "navigate":
            if not target:
                raise ToolError("navigate needs target")
            return ToolResult(navigate_tab(target, title, new_tab))
        if action == "switch_tab":
            if not target:
                raise ToolError("switch_tab needs target (part of the tab title)")
            found = switch_tab(target, title)
            return ToolResult(f"switched to tab: {found}" if found else
                              f"no open tab has '{target}' in its title; you can open it with navigate")
        if not title:
            raise ToolError(f"{action} needs title")
        if action == "focus":
            return ToolResult(f"brought to the front: {focus_window(title)}")
        return ToolResult(f"{action}: {window_op(title, action)}")


class UIElements(Tool):
    name = "ui_elements"
    description = ("Windows (UI Automation) and Linux (AT-SPI): lists the elements of the foreground (or "
                   "title-matched) desktop window, such as "
                   "buttons, fields and menus, with an [ID] (UI Automation); then click/type by ID with ui_act. You "
                   "can also pass action to list and click in one call (e.g. action='click', element_id=3). "
                   "Use this instead of guessing coordinates.")
    parameters = obj({
        "title": {"type": "string", "description": "Part of the window title. Empty: the foreground window."},
        "limit": {"type": "integer", "description": "Maximum elements. Default 150."},
        "action": {"type": "string", "enum": ["click", "double_click", "right_click", "focus", "set_text", "type",
                                              "toggle", "expand", "collapse", "select"],
                   "description": "Act on an element right away (if empty, only lists)."},
        "element_id": {"type": "integer", "description": "ID of the element to act on (when action is given)."},
        "text": {"type": "string", "description": "Text for set_text or type."},
    })

    def run(self, ctx: ToolContext, title: str | None = None, limit: int = 150,
            action: str | None = None, element_id: int | None = None, text: str | None = None) -> ToolResult:
        from alice.tools.computer import as_number

        if action and element_id is not None:
            return ToolResult(ui_act(ctx, element_id, action, text))
        limit = max(1, min(as_number(150 if limit is None else limit, "limit", int), 500))
        t, items = collect_ui_elements(ctx, title, limit)
        return ToolResult(format_ui(t, items))


class UIAct(Tool):
    name = "ui_act"
    description = ("Acts on a desktop element from the last ui_elements list: click, type text, check box, "
                   "expand/collapse a menu or tree, select in a list.\n"
                   "- element_id is only valid for the LAST ui_elements output; if the window or page changed, call "
                   "ui_elements again first.\n"
                   "- If the element is in the list, use this instead of clicking by coordinates (computer); it is "
                   "more reliable.\n"
                   "- Ask the user before pressing irreversible buttons such as send, delete or buy.")
    parameters = obj({
        "element_id": {"type": "integer", "description": "The [ID] from the ui_elements output."},
        "action": {"type": "string", "enum": ["click", "double_click", "right_click", "focus", "set_text", "type",
                                              "toggle", "expand", "collapse", "select"],
                   "description": "set_text: replace the field's content with text | type: type text at the cursor | "
                   "toggle: check box | expand/collapse: tree/menu | select: select a list item."},
        "text": {"type": "string", "description": "Text for set_text and type."},
    }, ["element_id", "action"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, element_id: int, action: str, text: str | None = None) -> ToolResult:
        return ToolResult(ui_act(ctx, element_id, action, text))


class Clipboard(Tool):
    name = "clipboard"
    description = ("Reads the clipboard text (get), puts text on the clipboard (set) or lists what was copied in "
                   "the last 12 hours (history). For 'what I copied', 'on the clipboard' use get; for 'what I copied "
                   "just now/10 min ago' and 'the thing I copied before that' use history. When asked to translate/"
                   "summarize/fix/reply to what was copied: get (or history) -> process -> put the result on the "
                   "clipboard with set and say it is on the clipboard, ready to paste.")
    parameters = obj({
        "action": {"type": "string", "enum": ["get", "set", "history"],
                   "description": "get: read | set: put text on the clipboard | history: recent copies (newest first)."},
        "text": {"type": "string", "description": "Text for set."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") in ("get", "history") else COMPUTER

    def run(self, ctx: ToolContext, action: str, text: str | None = None) -> ToolResult:
        if action == "history":
            from datetime import datetime

            from alice.core import clipwatch

            items = clipwatch.load()[::-1]
            return ToolResult("\n".join(f"{datetime.fromtimestamp(i['ts']):%H:%M} | {i['text'][:300]}" for i in items)
                              or "the clipboard history is empty (it is kept while the core runs; things that look like passwords aren't saved)")
        try:
            import pyperclip
        except ImportError as e:
            raise ToolError("pyperclip is not installed") from e
        if action == "get":
            return ToolResult(pyperclip.paste() or "(clipboard is empty)")
        if action != "set":
            raise ToolError(f"unknown action: {action}. Valid: get, set, history")
        if text is None:  # a missing text used to silently empty the clipboard (the user's copied content was lost)
            raise ToolError("set needs text")
        pyperclip.copy(str(text))
        return ToolResult("copied to the clipboard")


TOOLS = [Windows(), UIElements(), UIAct(), Clipboard()]
