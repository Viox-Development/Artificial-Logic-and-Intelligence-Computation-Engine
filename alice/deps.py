"""Find missing packages and install them automatically into the Python ALICE runs on.

ALICE can start from two different Python environments (.venv when a session opens, the system Python when started
by hand); when a package was in one but not the other, a feature silently turned off (OCR looked "not installed").
The updater's extra guess also checked only one package per extra, so it missed the package that was still missing.

Here the packages of every installed extra (and the base dependencies) are checked one by one against this platform's
markers; if any are missing, `sys.executable -m pip install -e .[extras]` runs in the background. The install command
shown to the user also always shows this Python (a bare "pip" may install into a different Python).
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _pyproject() -> dict[str, Any]:
    path = REPO / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _applies(marker: str) -> bool:
    """Whether the requirement marker (sys_platform == 'win32' or ...) applies on this machine."""
    if not marker.strip():
        return True
    try:
        from packaging.markers import Marker
    except ImportError:
        try:
            from pip._vendor.packaging.markers import Marker  # type: ignore[no-redef]
        except ImportError:
            return True
    try:
        return bool(Marker(marker).evaluate())
    except Exception:  # noqa: BLE001
        return True


def _name(req: str) -> tuple[str, str]:
    spec, _, marker = req.partition(";")
    name = re.split(r"[<>=!~\[\s(]", spec.strip(), maxsplit=1)[0]
    return name, marker


def _installed(name: str) -> bool:
    from importlib import metadata

    try:
        metadata.version(name)
        return True
    except metadata.PackageNotFoundError:
        return False


def missing(extras: list[str] | None = None) -> dict[str, list[str]]:
    """extra -> packages required on this platform but not installed ("" = base dependencies)."""
    data = _pyproject().get("project") or {}
    groups: dict[str, list[str]] = {"": list(data.get("dependencies") or [])}
    opt = data.get("optional-dependencies") or {}
    if extras is None:
        from alice import updater

        extras = updater.installed_extras()
    for e in extras:
        groups[e] = [r for r in opt.get(e, []) if not r.startswith("alice-harness")]
    out: dict[str, list[str]] = {}
    for group, reqs in groups.items():
        lack = [n for n, m in map(_name, reqs) if _applies(m) and not _installed(n)]
        if lack:
            out[group] = lack
    return out


def install_command(extras: list[str]) -> str:
    """Command shown to the user: with this Python, for this copy."""
    target = f"{REPO}[{','.join(extras)}]" if extras else str(REPO)
    return f'"{sys.executable}" -m pip install -e "{target}"'


def heal(log: Callable[[str], None] = lambda s: None, run=subprocess.run) -> list[str]:
    """Install missing packages if any. Returns what was installed (empty if nothing was missing or install failed)."""
    if not (REPO / "pyproject.toml").exists():
        return []
    lack = missing()
    if not lack:
        return []
    names = sorted({n for v in lack.values() for n in v})
    extras = sorted(e for e in lack if e)
    from alice import updater

    extras = sorted(set(extras) | set(updater.installed_extras()))
    target = f".[{','.join(extras)}]" if extras else "."
    log(f"eksik paketler kuruluyor ({', '.join(names)}): {sys.executable} -m pip install -e {target}")
    try:
        r = run([sys.executable, "-m", "pip", "install", "-q", "-e", target], cwd=str(REPO), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=900, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"eksik paketler kurulamadı: {e}")
        return []
    if r.returncode != 0:
        log(f"eksik paketler kurulamadı: {(r.stderr or r.stdout or '').strip()[-300:]}")
        return []
    try:  # retry OCR, which was cached as "not installed" in an earlier attempt
        from alice.reflex import perceive

        perceive._ocr_error = ""
    except Exception:  # noqa: BLE001
        pass
    log(f"eksik paketler kuruldu: {', '.join(names)}")
    return names


def heal_in_background(log: Callable[[str], None] = lambda s: None) -> threading.Thread:
    t = threading.Thread(target=lambda: heal(log), daemon=True, name="alice-deps")
    t.start()
    return t
