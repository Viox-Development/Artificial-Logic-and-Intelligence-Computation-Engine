"""Automatically downloads and installs CLIProxyAPI on first use (`~/.alice/cliproxy`).

Instead of putting the binary in the repository (77 MB, platform-specific), it is downloaded from GitHub releases
and SHA-256 verified against `checksums.txt`. `config.yaml`, `keys.txt`, and `CLIPROXY_API_KEY` are generated
if missing; this way the user does not have to download a zip and extract it manually.
"""

from __future__ import annotations

import hashlib
import os
import platform
import secrets
import shutil
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx

from alice.app import cliproxy

REPO = "router-for-me/CLIProxyAPI"
PINNED_VERSION = "v8.0.57"  # tested version; can be changed via [cliproxy] version in config
_TIMEOUT = 60.0
_log: Callable[[str], None] = lambda s: None


def asset_name(version: str, system: str | None = None, machine: str | None = None) -> str:
    """File name to download for version + platform (e.g. CLIProxyAPI_8.0.57_windows_amd64.zip)."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "amd64"
    ver = version.strip().lstrip("vV")
    if system == "windows":
        return f"CLIProxyAPI_{ver}_windows_{arch}.zip"
    if system == "darwin":
        return f"CLIProxyAPI_{ver}_darwin_{arch}.tar.gz"
    return f"CLIProxyAPI_{ver}_linux_{arch}.tar.gz"


def _release_json(version: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{version}"
    headers = {"User-Agent": "ALICE", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": "ALICE"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest


def _asset_url(release: dict[str, Any], name: str) -> str:
    for a in release.get("assets") or []:
        if str(a.get("name")) == name:
            return str(a.get("browser_download_url") or "")
    raise FileNotFoundError(f"{name} bu sürümde yok")


def checksum_for(checksums_text: str, name: str) -> str:
    """SHA-256 of the file from `checksums.txt` (lowercase)."""
    for line in checksums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0].strip().lower()
    raise ValueError(f"{name} için checksum bulunamadı")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        if hasattr(tarfile, "data_filter"):
            # "data" filter: rejects absolute paths, "..", links outside folder, device files, and setuid.
            t.extractall(dest, filter="data")
        else:  # pragma: no cover - older Python without filter: perform the same checks manually
            root = dest.resolve()
            for m in t.getmembers():
                target = (dest / m.name).resolve()
                if (not (m.isfile() or m.isdir()) or os.path.isabs(m.name)
                        or (target != root and root not in target.parents)):
                    raise ValueError(f"arşivde güvensiz girdi: {m.name}")
            t.extractall(dest)
    # If tar archive contains a single directory, move contents up
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        for child in entries[0].iterdir():
            shutil.move(str(child), str(dest / child.name))
        entries[0].rmdir()


def _write_config(home: Path, keys: dict[str, str]) -> None:
    base = str(home).replace("\\", "/")
    config = f"""config-version: 8
# ALICE için yerel CLI proxy: Claude Code / ChatGPT hesaplarını OpenAI uyumlu uca çevirir.
# Bu dosya ALICE tarafından otomatik oluşturuldu; hesaplar uygulamadan bağlanır.
server:
  host: "127.0.0.1"
  port: 8317

oauth:
  auth-dir: "{base}/auths"

management:
  allow-remote: false
  secret-key: "{keys['management_key']}"

# Birden çok hesap: konuşmalar hesaplar arasında sırayla paylaştırılır; biri limite takılınca sıradakine geçilir.
# session-affinity: aynı konuşma aynı hesapta kalır (her hesabın prompt önbelleği ayrı; yoksa önbellek hep soğuk).
routing:
  strategy: "round-robin"
  session-affinity: true

access:
  api-keys:
    - "{keys['api_key']}"
"""
    _write_private(home / "config.yaml", config)


def _write_private(path: Path, text: str) -> None:
    """Write key-containing file so only the user can read it (0600 on POSIX; on Windows file is already in
    the user profile, ACL comes from there)."""
    if os.name == "nt":
        path.write_text(text, encoding="utf-8")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)  # if file already exists os.open mode is not applied


def ensure_config() -> dict[str, str]:
    """Generate config.yaml + keys.txt if missing; write CLIPROXY_API_KEY to ~/.alice/.env. Do not touch existing."""
    home = cliproxy.home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "auths").mkdir(exist_ok=True)
    keys_file = home / "keys.txt"
    keys: dict[str, str] = {}
    if keys_file.exists():
        for line in keys_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                keys[k.strip()] = v.strip()
    if not keys.get("api_key") or not keys.get("management_key"):
        keys = {"api_key": "alice-" + secrets.token_urlsafe(24), "management_key": secrets.token_urlsafe(32)}
        _write_private(keys_file, "\n".join(f"{k}={v}" for k, v in keys.items()) + "\n")
    if not (home / "config.yaml").exists():
        _write_config(home, keys)
    try:
        from alice.config import read_env, set_env_value

        if not read_env().get("CLIPROXY_API_KEY"):
            set_env_value("CLIPROXY_API_KEY", keys["api_key"])
    except Exception:  # noqa: BLE001 - if .env cannot be written proxy still works, key can be entered manually
        pass
    return keys


def _find_checksums(release: dict[str, Any]) -> str:
    for a in release.get("assets") or []:
        if str(a.get("name")) == "checksums.txt":
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
                r = c.get(str(a.get("browser_download_url") or ""), headers={"User-Agent": "ALICE"})
                r.raise_for_status()
                return r.text
    raise FileNotFoundError("sürümde checksums.txt yok")


def install(version: str = "", force: bool = False, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Download, verify, and install CLIProxyAPI into `~/.alice/cliproxy`. Existing config/accounts are preserved."""
    emit = log or _log
    home = cliproxy.home()
    binary = home / cliproxy.binary_name()
    # No api_key in result: UI does not use it (key is in keys.txt and .env), prevent leaking to logging/display.
    if binary.exists() and not force:
        ensure_config()
        return {"ok": True, "already": True, "path": str(home)}
    version = (version or PINNED_VERSION).strip()
    if version.lower() in ("", "latest"):
        version = PINNED_VERSION
    name = asset_name(version)
    emit(f"CLIProxyAPI {version} indiriliyor: {name}")
    release = _release_json(version)
    url = _asset_url(release, name)
    checksums = _find_checksums(release)
    want = checksum_for(checksums, name)
    with tempfile.TemporaryDirectory(prefix="alice-cliproxy-") as tmp:
        archive = _download(url, Path(tmp) / name)
        got = sha256_of(archive)
        if got != want:
            return {"ok": False, "error": f"checksum uyuşmadı ({got[:12]}… != {want[:12]}…)"}
        emit("checksum doğru, açılıyor")
        _extract(archive, home)
    binary = home / cliproxy.binary_name()
    if not binary.exists():
        return {"ok": False, "error": "arşivde cli-proxy-api bulunamadı"}
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | 0o111)
    ensure_config()
    emit("kurulum tamam")
    return {"ok": True, "version": version, "path": str(home)}


def install_async(version: str = "") -> dict[str, Any]:
    """Start installation without blocking the UI request."""
    threading.Thread(target=lambda: install(version, log=_log), daemon=True, name="alice-cliproxy-install").start()
    return {"ok": True, "started": True}


def ensure_installed(version: str = "") -> dict[str, Any]:
    """Install if not installed; complete config if installed."""
    home = cliproxy.home()
    if (home / cliproxy.binary_name()).exists():
        ensure_config()
        return {"ok": True, "already": True}
    return install(version)
