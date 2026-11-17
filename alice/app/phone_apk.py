"""Latest version of the phone app (Android): downloaded by the PC, served to the phone over the local network.

CI (.github/workflows/android.yml) places the APK and version info into the "android-apk" branch of the repo
on each build (single commit, overwritten each time). Because the repo is private, the phone cannot download from
GitHub; the PC already has git access. ALICE fetches this branch periodically and stores it under ~/.alice/phone/;
the app on the phone queries the version via /api/phone/app, and if it is newer, downloads it from /api/phone/app.apk
and updates itself.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from alice.config import alice_home

BRANCH = "android-apk"
REF = "refs/alice/android-apk"  # separate ref so it does not mix into the user's branch list
EVERY = 30 * 60.0  # seconds: will not be fetched more frequently than this
_lock = threading.Lock()
_last = {"at": 0.0, "error": ""}


def folder() -> Path:
    return alice_home() / "phone"


def apk_path() -> Path:
    return folder() / "ALICE.apk"


def info() -> dict[str, Any] | None:
    """Version of the APK on the PC: {"version_code", "version_name", "size", "sha256"} or None."""
    try:
        meta = json.loads((folder() / "version.json").read_text(encoding="utf-8"))
        size = apk_path().stat().st_size
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or int(meta.get("version_code") or 0) <= 0:
        return None
    return {"version_code": int(meta["version_code"]), "version_name": str(meta.get("version_name") or ""),
            "size": size, "sha256": str(meta.get("sha256") or "")}


def _git(args: list[str], repo: Path, timeout: float = 60.0) -> tuple[int, str]:
    from alice.updater import _git as git

    return git(args, repo, timeout)


def _blob(repo: Path, name: str) -> bytes | None:
    """Bytes of the file at REF (read directly so git cat-file does not corrupt non-text content)."""
    import subprocess

    from alice.updater import NO_WINDOW
    try:
        r = subprocess.run(["git", "cat-file", "blob", f"{REF}:{name}"], cwd=str(repo), capture_output=True,
                           timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def refresh(force: bool = False, repo: Path | None = None) -> dict[str, Any] | None:
    """Fetch the android-apk branch; replace if there is a newer APK. Returns the version on the PC."""
    from alice.updater import repo_dir

    with _lock:
        if not force and time.monotonic() - _last["at"] < EVERY and _last["at"]:
            return info()
        _last["at"] = time.monotonic()
        repo = repo or repo_dir()
        if repo is None:
            _last["error"] = "ALICE bir git klasöründen çalışmıyor"
            return info()
        code, out = _git(["fetch", "--quiet", "origin", f"+refs/heads/{BRANCH}:{REF}"], repo)
        if code != 0:
            _last["error"] = (out.strip().splitlines() or ["git fetch başarısız"])[-1]
            return info()
        raw = _blob(repo, "version.json")
        try:
            meta = json.loads(raw or b"")
        except ValueError:
            meta = None
        if not isinstance(meta, dict):
            _last["error"] = "android-apk dalında version.json yok"
            return info()
        cur = info()
        if cur and cur["version_code"] >= int(meta.get("version_code") or 0):
            _last["error"] = ""
            return cur
        apk = _blob(repo, "ALICE.apk")
        if not apk or (meta.get("sha256") and hashlib.sha256(apk).hexdigest() != meta["sha256"]):
            _last["error"] = "APK eksik ya da bozuk"
            return cur
        meta["sha256"] = hashlib.sha256(apk).hexdigest()
        folder().mkdir(parents=True, exist_ok=True)
        tmp = apk_path().with_suffix(".tmp")
        tmp.write_bytes(apk)
        tmp.replace(apk_path())
        (folder() / "version.json").write_text(json.dumps(meta), encoding="utf-8")
        _last["error"] = ""
        return info()


def refresh_async() -> None:
    if time.monotonic() - _last["at"] < EVERY and _last["at"]:
        return
    threading.Thread(target=refresh, daemon=True, name="phone-apk").start()


def view() -> dict[str, Any]:
    out: dict[str, Any] = dict(info() or {})
    out["available"] = bool(out)
    if _last["error"]:
        out["error"] = _last["error"]
    return out
