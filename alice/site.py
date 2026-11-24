"""The project's website: finding the entry page, serving it safely, and taking screenshots.

The preview tab and the board's "user eye" review use this. Hidden files (.env, .git ...) are never
served.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".alice"}
ENTRY_DIRS = ("", "dist", "build", "public", "site", "www", "docs", "src", "web", "frontend")
VIEWPORTS = {"masaüstü": (1366, 860), "telefon": (390, 844)}
# When studying reference sites: the first screen, the lower part of the page (section rhythm), and the phone view.
STUDY_VIEWPORTS = {"masaüstü": (1440, 900), "uzun": (1440, 2600), "telefon": (390, 844)}


def find_entry(root: Path) -> str:
    """The site's entry page (path relative to the root), or empty. Known folders first, then two levels deep."""
    root = Path(root)
    for d in ENTRY_DIRS:
        f = root / d / "index.html"
        if f.is_file():
            return f.relative_to(root).as_posix()
    try:
        for sub in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS
                          and not p.name.startswith(".")):
            for f in [sub / "index.html", *sorted(sub.glob("*/index.html"))]:
                if f.is_file() and not any(part in SKIP_DIRS or part.startswith(".") for part in f.relative_to(root).parts):
                    return f.relative_to(root).as_posix()
    except OSError:
        pass  # unreadable folder: no entry page found
    return ""


def safe_path(root: Path, rel: str) -> Path | None:
    """Safe path relative to the root: paths that leave it, are hidden (.env, .git) or in a skipped folder give None."""
    root = Path(root).resolve()
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") or p in SKIP_DIRS for p in parts):
        return None
    path = (root / Path(*parts)).resolve() if parts else root
    try:
        inside = path.relative_to(root)
    except ValueError:
        return None
    if any(p.startswith(".") or p in SKIP_DIRS for p in inside.parts):  # if a link (symlink) points to a hidden file
        return None
    if path.is_dir():
        path = path / "index.html"
    return path if path.is_file() else None


def content_type(path: Path) -> str:
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix in (".js", ".mjs"):
        ctype = "text/javascript"
    return ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else "")


def last_change(root: Path, limit: int = 3000) -> float:
    """Latest modification time of the site's files (so the preview can refresh itself)."""
    newest, n = 0.0, 0
    stack = [Path(root)]
    while stack and n < limit:
        d = stack.pop()
        try:
            for p in d.iterdir():
                if p.name.startswith(".") or p.name in SKIP_DIRS:
                    continue
                if p.is_dir():
                    stack.append(p)
                else:
                    n += 1
                    newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401
        pass

    def guess_type(self, path):  # declare text files as utf-8 (otherwise the browser garbles Turkish letters)
        return content_type(Path(str(path)))

    def send_head(self):  # never serve hidden files (checked after decoding, so %2eenv-style escapes are caught)
        from urllib.parse import unquote

        rel = unquote(self.path.split("?", 1)[0].split("#", 1)[0])
        if safe_path(Path(self.directory), rel) is None:
            self.send_error(404)
            return None
        return super().send_head()


def serve(root: Path) -> tuple[ThreadingHTTPServer, str]:
    """Temporarily serve the folder on 127.0.0.1 (for screenshots). Returns (server, address); shut down when done."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="site-preview").start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


def screenshots(root: Path, entry: str = "", timeout: float = 20.0) -> dict[str, str]:
    """The site's desktop and phone screenshots (base64 PNG). If there is no browser or the site fails to open,
    an empty dict is returned and the review continues without images."""
    entry = entry or find_entry(root)
    if not entry:
        return {}
    httpd, base = serve(root)
    try:
        return capture(base + entry, timeout)
    finally:
        httpd.shutdown()
        httpd.server_close()


def reachable(url: str, timeout: float = 12.0) -> tuple[bool, str]:
    """Does the external address open? (A browser also screenshots the error page of a site that won't open,
    so that page must not be taken for a "reference"). Returns: (opens or not, final address or error)."""
    import httpx

    try:
        r = httpx.get(url, follow_redirects=True, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    if "html" not in r.headers.get("content-type", "html"):
        return False, f"web sayfası değil ({r.headers.get('content-type')})"
    return True, str(r.url)


def capture(url: str, timeout: float = 20.0, settle: int = 600,
            viewports: dict[str, tuple[int, int]] | None = None) -> dict[str, str]:
    """Screenshots of the address (default: desktop + phone): Playwright first, otherwise the command-line browser."""
    vp = viewports or VIEWPORTS
    return _pw_capture(url, timeout, settle, vp) or _cli_capture(url, timeout, settle, vp)


def _pw_capture(url: str, timeout: float, settle: int, viewports: dict[str, tuple[int, int]] | None = None
                ) -> dict[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}
    out: dict[str, str] = {}
    try:
        with sync_playwright() as p:
            browser = _launch(p)
            try:
                for name, (w, h) in (viewports or VIEWPORTS).items():
                    page = browser.new_page(viewport={"width": w, "height": h})
                    page.goto(url, wait_until="load", timeout=timeout * 1000)
                    page.wait_for_timeout(settle)  # let entry animations settle
                    out[name] = base64.b64encode(page.screenshot(full_page=False)).decode()
                    page.close()
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 - no browser, crash, or site not opening: try the command-line browser
        return out
    return out


MAC_BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")


def browser_exe() -> str:
    """A Chromium-based browser that can take screenshots without opening a window: ALICE_CHROMIUM, then Edge
    (always installed on Windows), Chrome, Chromium. Empty if none is found."""
    exe = os.environ.get("ALICE_CHROMIUM", "")
    if exe and Path(exe).is_file():
        return exe
    cands: list[Path] = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe"]
    cands += [Path(p) for p in MAC_BROWSERS]
    for c in cands:
        if c.is_file():
            return str(c)
    for name in ("msedge", "chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def cli_screenshots(root: Path, entry: str = "", timeout: float = 20.0) -> dict[str, str]:
    """Without Playwright: serve the local site and capture it with the command-line browser."""
    entry = entry or find_entry(root)
    if not entry or not browser_exe():
        return {}
    httpd, base = serve(root)
    try:
        return _cli_capture(base + entry, timeout)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _cli_capture(url: str, timeout: float = 20.0, settle: int = 600,
                 viewports: dict[str, tuple[int, int]] | None = None) -> dict[str, str]:
    """Run the browser invisibly (headless) from the command line and take a screenshot; no extra setup needed."""
    exe = browser_exe()
    if not exe:
        return {}
    import time

    out: dict[str, str] = {}
    # ignore_cleanup_errors: on Windows a file a dying browser still holds must not turn a finished capture into
    # an error; at worst the temp folder is left behind.
    with tempfile.TemporaryDirectory(prefix="alice-shot-", ignore_cleanup_errors=True) as tmp:
        # Viewport sizes are captured at once (each with its own profile): capturing them in turn doubled the time.
        jobs: list[tuple[str, Path, subprocess.Popen]] = []
        for i, (name, (w, h)) in enumerate((viewports or VIEWPORTS).items()):
            png = Path(tmp) / f"{i}.png"
            cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                   "--no-default-browser-check", "--disable-extensions", "--mute-audio",
                   f"--user-data-dir={Path(tmp) / f'profile{i}'}",  # don't attach to an open browser
                   f"--window-size={w},{h}", f"--virtual-time-budget={1900 + settle}", f"--screenshot={png}", url]
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                cmd.insert(1, "--no-sandbox")
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy and not url.startswith("http://127.0.0.1"):
                cmd.insert(1, f"--proxy-server={proxy}")
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                break
            jobs.append((name, png, proc))
        procs = [proc for _, _, proc in jobs]
        # Don't wait once the image is written: headless browsers sometimes take seconds to exit after a screenshot.
        deadline = time.monotonic() + timeout
        sizes: dict[str, int] = {}
        while jobs and time.monotonic() < deadline:
            for name, png, proc in list(jobs):
                size = png.stat().st_size if png.is_file() else 0
                done = proc.poll() is not None or (size > 1000 and sizes.get(name) == size)  # size stopped changing
                sizes[name] = size
                if done:
                    jobs.remove((name, png, proc))
                    if size > 1000:
                        out[name] = base64.b64encode(png.read_bytes()).decode()
            if jobs:
                time.sleep(0.2)
        # Close browsers that got their image but did not exit or timed out, so the temp profile can be deleted.
        # Killing only the parent was not enough: on Windows Chrome's child processes (renderer, crashpad) kept
        # files in the profile open and the temp folder cleanup failed (WinError 32).
        for proc in procs:
            if proc.poll() is None:
                _kill_children(proc)
                proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass  # killed; the OS reaps it
        _kill_profile_procs(tmp)
    return out


def _kill_children(proc) -> None:
    """Kill the browser's child processes (renderer, crashpad) before the parent, so none is orphaned."""
    import psutil

    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        children = psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return  # the parent already exited: _kill_profile_procs catches what it left behind
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass  # the child already exited


def _kill_profile_procs(folder: str) -> int:
    """Kill leftover processes started with a profile under folder (children that outlived their parent).
    Returns how many were killed."""
    import psutil

    killed = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(folder in arg for arg in (p.info.get("cmdline") or [])):
                p.kill()
                killed.append(p)
        except psutil.Error:
            pass  # the process already exited or is not ours to read/kill: nothing to clean up
    psutil.wait_procs(killed, timeout=3)
    return len(killed)


def _launch(p):
    """First the browser given by ALICE_CHROMIUM, then Playwright's own browser, then the system's Chrome/Edge."""
    exe = os.environ.get("ALICE_CHROMIUM", "")
    if exe:
        return p.chromium.launch(executable_path=exe)
    try:
        return p.chromium.launch()
    except Exception:  # noqa: BLE001 - if no downloaded browser, use the system's Chrome/Edge
        for channel in ("chrome", "msedge"):
            try:
                return p.chromium.launch(channel=channel)
            except Exception:  # noqa: BLE001
                continue
        raise
