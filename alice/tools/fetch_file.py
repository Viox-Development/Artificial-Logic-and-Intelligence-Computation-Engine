"""Download a file directly into Downloads, without the browser.

When Opera's "her indirmede nereye kaydedileceğini sor" (ask where to save each download) is on, every browser download
(the address bar too, the extension's downloads.download even with saveAs=false and a filename) waited in the
"Farklı Kaydet" (Save As) window, and ALICE got stuck fighting that window. Public links such as installers are
downloaded directly here. Links that need login (a page, or a 401/403 response) fall back to the caller's browser path.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

CHUNK = 256 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
_JOBS: list[dict[str, Any]] = []  # recent downloads (shown by browser action=downloads)
_LOCK = threading.Lock()


class NeedsBrowser(Exception):
    """The link asks for login/session, or is not a file (an HTML page): it must be downloaded with the browser."""


def downloads_dir() -> Path:
    """The user's Downloads folder on Windows (it may have been moved), otherwise ~/Downloads."""
    try:
        import ctypes
        import uuid

        guid = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
        buf = (ctypes.c_byte * 16).from_buffer_copy(guid.bytes_le)
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(buf), 0, None, ctypes.byref(path)) == 0:
            out = Path(path.value)
            ctypes.windll.ole32.CoTaskMemFree(path)
            return out
    except Exception:  # noqa: BLE001 - not Windows, or the API is missing
        pass
    return Path.home() / "Downloads"


def _filename(url: str, headers: Any) -> str:
    cd = headers.get("Content-Disposition") or ""
    if cd:
        m = Message()
        m["content-disposition"] = cd
        name = m.get_filename()
        if name:
            return name
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    return name or "indirilen"


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "indirilen"


def _unique(folder: Path, name: str) -> Path:
    p = folder / name
    stem, suf = p.stem, p.suffix
    n = 1
    while p.exists() or p.with_name(p.name + ".part").exists():
        p = folder / f"{stem} ({n}){suf}"
        n += 1
    return p


def fetch(url: str, folder: Path | None = None, wait: float = 240.0, opener=None) -> dict[str, Any]:
    """Start the download. If it finishes within `wait` seconds, return its final state; if not, return the state
    still in progress (it continues in the background). Raises NeedsBrowser for links that ask for login/session."""
    folder = folder or downloads_dir()
    folder.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    open_ = opener or urllib.request.urlopen
    try:
        resp = open_(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 407):
            raise NeedsBrowser(f"the server asks for sign-in ({e.code})") from e
        raise
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype.startswith("text/html"):
        resp.close()
        raise NeedsBrowser("the link isn't a file, it opens a page (it may be a sign-in or download page)")
    path = _unique(folder, _safe(_filename(resp.geturl() if hasattr(resp, "geturl") else url, resp.headers)))
    total = int(resp.headers.get("Content-Length") or 0)
    job = {"file": str(path), "url": url, "state": "in_progress", "error": "", "bytes": 0, "total": total,
           "started": time.time()}
    with _LOCK:
        _JOBS.append(job)
        del _JOBS[:-10]
    done = threading.Event()

    def run() -> None:
        part = path.with_name(path.name + ".part")
        try:
            with resp, open(part, "wb") as f:
                while chunk := resp.read(CHUNK):
                    f.write(chunk)
                    job["bytes"] += len(chunk)
            part.replace(path)
            job["state"] = "complete"
        except Exception as e:  # noqa: BLE001
            job["state"], job["error"] = "interrupted", str(e)[:200]
            part.unlink(missing_ok=True)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="alice-download").start()
    done.wait(wait)
    return dict(job)


def jobs() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(j) for j in reversed(_JOBS)]


def download(url: str, wait: float = 240.0) -> dict[str, Any]:
    """Try a direct download first; if login is needed, use the ALICE browser extension (cookies are in the browser)."""
    from alice.tools.base import ToolError

    if "://" not in url:
        url = "https://" + url
    try:
        return fetch(url, wait=wait)
    except NeedsBrowser as e:
        from alice.tools import browser_ext

        hub = browser_ext.hub()
        if hub is None:
            raise ToolError(f"{e}; you must be signed in in the browser, but the ALICE browser extension isn't connected") from e
        d = browser_ext.ExtBrowserSession(hub).download(url, wait)
        d["via"] = "browser"
        return d


def describe(d: dict[str, Any], opened: bool = False) -> str:
    """One-line description of the download result for the model."""
    from alice.tools.base import ToolError

    if d.get("state") == "complete":
        size = f", {int(d['bytes']) / 1e6:.1f} MB" if d.get("bytes") else ""
        return f"downloaded{' (not opened)' if opened else ''}: {d.get('file')}{size} — open the installer/file at this path"
    if d.get("state") == "interrupted":
        raise ToolError(f"the download was cut off: {d.get('error') or 'unknown error'}")
    pct = f" %{100 * int(d.get('bytes') or 0) // int(d['total'])}" if d.get("total") else ""
    if d.get("via") == "browser" and not d.get("file"):
        return ("the browser is downloading but a save dialog may be open (Opera's 'ask where to save' "
                "is on): tell the user or press Save in the dialog")
    return f"still downloading{pct}: {d.get('file') or d.get('url')} (follow it with browser action=downloads; don't open it before it finishes)"


def source_name(url: str) -> str:
    """The file name the link refers to (for signed links such as GitHub's, from the "filename=" parameter)."""
    from urllib.parse import parse_qs

    q = parse_qs(urlparse(url).query)
    for key in ("rscd", "response-content-disposition"):
        for v in q.get(key, []):
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", v)
            if m:
                return unquote(m.group(1))
    return unquote(urlparse(url).path.rsplit("/", 1)[-1])
