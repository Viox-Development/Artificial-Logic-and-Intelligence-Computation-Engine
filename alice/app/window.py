"""Open the application window.

Order: pywebview (native app window via Edge WebView2 on Windows) ->
Edge/Chrome --app mode (standalone window without an address bar) -> default browser.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path, PurePath
from typing import Any, Callable

from alice.config import alice_home


def _browser_candidates() -> list[str]:
    out: list[str] = []
    if sys.platform == "win32":
        pf = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            out += [str(Path(base) / r"Microsoft\Edge\Application\msedge.exe"),
                    str(Path(base) / r"Google\Chrome\Application\chrome.exe")]
    elif sys.platform == "darwin":
        out += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        w = shutil.which(name)
        if w:
            out.append(w)
    return [p for p in out if p and Path(p).exists()]


def _watch(watch: Callable[[], str | None], close: Callable[[], None], load: Callable[[str], None]) -> None:
    """watch() every second: "" = continue, new address = go to it, None = close (see alice/app/host.py follow)."""
    def loop() -> None:
        while True:
            time.sleep(1.0)
            try:
                r = watch()
            except Exception:  # noqa: BLE001 - watcher error should not close the window
                continue
            try:
                if r is None:
                    close()
                    return
                if r:
                    load(r)
            except Exception:  # noqa: BLE001 - window might already be closed
                return

    threading.Thread(target=loop, daemon=True, name="alice-window-watch").start()


def gaming() -> bool:
    """Is game/flight mode active (from core state file)? If so, the window must not come to front and steal focus."""
    try:
        from alice.core.service import read_state

        s = read_state()
        return bool(s.get("running")) and s.get("mode") == "flight"
    except Exception:  # noqa: BLE001
        return False


def open_window(url: str, title: str = "ALICE", prefer: str = "auto",
                watch: Callable[[], str | None] | None = None, quiet: bool | None = None) -> str:
    """Opens the window and waits until it closes. Returns which method was used.

    If watch is given (window bound to the app in core), the window closes when core shuts down, and switches to the
    new address when core restarts.

    While a game is active (quiet), the window opens minimized to the taskbar: popping in front
    and backgrounding the game caused the user to die in-game."""
    quiet = gaming() if quiet is None else quiet
    if prefer in ("auto", "webview"):
        try:
            import webview  # pywebview

            # pywebview on Windows (WinForms) opens the icon with System.Drawing.Icon and crashes with
            # an uncatchable .NET error when given a .png; use .ico there.
            icon = Path(__file__).parent / "static" / ("alice.ico" if sys.platform == "win32" else "icon.png")
            win = webview.create_window(title, url, width=1320, height=880, min_size=(820, 560),
                                        background_color="#16161a", minimized=quiet)
            if watch is not None:
                _watch(watch, win.destroy, win.load_url)
            kwargs = {"icon": str(icon)} if icon.exists() else {}
            try:
                webview.start(**kwargs)
            except TypeError:
                webview.start()
            return "webview"
        except ImportError:
            if prefer == "webview":
                raise
    if prefer in ("auto", "app"):
        for exe in _browser_candidates():
            profile = alice_home() / "app-window"
            profile.mkdir(parents=True, exist_ok=True)
            # The address of an app-mode window cannot be changed externally: when the core restarts, the window
            # is closed and reopened with the new address.
            box: dict[str, Any] = {"url": url, "next": None, "proc": None}

            def relaunch(u: str, box: dict[str, Any] = box) -> None:
                box["next"] = u
                box["proc"].terminate()

            if watch is not None:
                _watch(watch, lambda box=box: box["proc"].terminate(), relaunch)
            while True:
                box["proc"] = subprocess.Popen([exe, f"--app={box['url']}", "--window-size=1320,880",
                                                f"--user-data-dir={profile}", "--no-first-run",
                                                "--no-default-browser-check"] + (["--start-minimized"] if quiet else []))
                box["proc"].wait()
                if not box["next"]:
                    return "app"
                box["url"], box["next"] = box["next"], None
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return "browser"


def _xdg_dir(env: str, default: str) -> Path:
    v = os.environ.get(env, "").strip()
    return Path(v) if v else Path.home() / default


def _desktop_dir() -> Path | None:
    """The user's desktop folder on Linux (localised names like ~/Masaüstü come from user-dirs.dirs)."""
    try:
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5, check=False)
        p = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        p = None
    if p is None:
        p = Path.home() / "Desktop"
    # xdg-user-dir prints $HOME when no desktop is configured: never drop a launcher straight into the home folder.
    return p if p.is_dir() and p != Path.home() else None


def _exec_arg(p: PurePath) -> str:
    """One argument for a Desktop Entry Exec line. The spec unescapes the value twice: first as a string (\\\\ -> \\),
    then as a quoted argument (\\" \\$ \\` \\\\), and a bare % starts a field code, so a literal one is %%."""
    s = str(p).replace("%", "%%")
    if any(c in s for c in " \t\n\r=\"'\\$`<>~|&;*?#()"):
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
    # The string-level escape on top of the argument-level one; a raw newline would end the Exec line.
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def _string_value(p: PurePath) -> str:
    """A Desktop Entry string value: backslash and control characters escaped (\\\\, \\n, \\t, \\r)."""
    return str(p).replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")


def linux_desktop_entry(python: PurePath, icon: PurePath) -> str:
    """The freedesktop launcher. Exec uses the venv's Python directly so it works without the venv on PATH."""
    return ("[Desktop Entry]\nType=Application\nName=ALICE\n"
            "Comment=Artificial Logic and Intelligence Computation Engine\n"
            f"Exec={_exec_arg(python)} -m alice app\nIcon={_string_value(icon)}\nTerminal=false\n"
            "Categories=Utility;Development;\nStartupWMClass=ALICE\n")


def _create_linux_shortcut() -> list[Path]:
    from alice import runtime

    icon = Path(__file__).parent / "static" / "icon.png"
    text = linux_desktop_entry(runtime.python(), icon)
    made = []
    apps = _xdg_dir("XDG_DATA_HOME", ".local/share") / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    targets = [apps / "alice.desktop"]
    desk = _desktop_dir()
    if desk is not None:
        targets.append(desk / "alice.desktop")
    for p in targets:
        p.write_text(text, encoding="utf-8")
        p.chmod(0o755)  # GNOME/KDE only run desktop-folder launchers that are executable
        made.append(p)
        if p.parent == desk and shutil.which("gio"):
            # GNOME also wants the launcher marked trusted, otherwise it shows "Untrusted application launcher".
            subprocess.run(["gio", "set", str(p), "metadata::trusted", "true"], capture_output=True, check=False,
                           timeout=5)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(apps)], capture_output=True, check=False, timeout=15)
    return made


def create_shortcut() -> list[Path]:
    """Place an ALICE shortcut that opens the app without a console.

    Windows: Desktop and Start menu (also finds a Desktop moved to OneDrive; with the Start menu entry it can be opened
    by pressing the Windows key and typing "ALICE"). Linux: the application menu (~/.local/share/applications) and the
    desktop folder when there is one."""
    if sys.platform.startswith("linux"):
        return _create_linux_shortcut()
    if sys.platform != "win32":
        raise RuntimeError("kısayol oluşturma şimdilik Windows ve Linux'ta")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    target = pyw if pyw.exists() else Path(sys.executable)
    icon = Path(__file__).parent / "static" / "alice.ico"
    ps = (
        "foreach($f in 'Desktop','Programs'){"
        "$d=[Environment]::GetFolderPath($f);if(-not $d){continue};$p=Join-Path $d 'ALICE.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        f"$s.TargetPath='{target}';$s.Arguments='-m alice app';$s.WorkingDirectory='{Path.home()}';"
        f"$s.IconLocation='{icon}';$s.Description='ALICE';$s.Save();Write-Output $p}}"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, text=True)
    # Windows caches icons; if we do not refresh, the Start menu keeps showing the old icon.
    try:
        subprocess.run(["ie4uinit.exe", "-show"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    return [Path(line.strip()) for line in r.stdout.splitlines() if line.strip()]
