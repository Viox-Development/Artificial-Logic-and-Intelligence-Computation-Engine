"""ALICE's single Python.

ALICE used to start from two environments on the same machine: the shortcut venv on Windows startup, or system Python when
started manually or from an app. Because packages varied between environments, a feature was sometimes present and sometimes
missing (like OCR). Here, the Python that the core runs on is recorded once (~/.alice/python.json); a core started from another
Python restarts itself with the recorded one, and the startup shortcut and background launches always use it too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REEXEC_ENV = "ALICE_REEXEC"  # restarted process should not redirect again (prevent loop)


def _path() -> Path:
    from alice.config import alice_home

    return alice_home() / "python.json"


def gui_python(exe: str | Path) -> Path:
    """The counterpart that doesn't open a console window (pythonw.exe on Windows)."""
    exe = Path(exe)
    if sys.platform == "win32":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return w
    return exe


def canonical() -> Path | None:
    """The recorded Python (if still present)."""
    try:
        p = Path(json.loads(_path().read_text(encoding="utf-8"))["python"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return p if p.exists() else None


def remember(exe: str | Path | None = None) -> Path:
    p = gui_python(exe or sys.executable)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"python": str(p)}, ensure_ascii=False), encoding="utf-8")
    return p


def python() -> Path:
    """Core and helper processes are started with this: the recorded one, or current if none."""
    return canonical() or gui_python(sys.executable)


def same(a: str | Path, b: str | Path) -> bool:
    """Are they the same environment (python.exe and pythonw.exe in the same folder: same)."""
    try:
        return Path(a).resolve().parent == Path(b).resolve().parent
    except OSError:
        return False


def redirect_core(argv: list[str], popen=subprocess.Popen) -> bool:
    """If core started with a Python other than the recorded one, restart with the recorded one; returns True if restarted
    (this process should exit). If no record on first run, saves current Python."""
    if os.environ.get(REEXEC_ENV):
        return False
    want = canonical()
    if want is None:
        remember()
        return False
    if same(want, sys.executable):
        return False
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "cwd": str(Path.home()), "env": {**os.environ, REEXEC_ENV: "1"}}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    popen([str(want), "-m", "alice", "core", *argv], **kwargs)
    return True
