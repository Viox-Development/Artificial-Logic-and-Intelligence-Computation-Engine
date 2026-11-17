"""ALICE's single Python: a core started from another environment restarts with the remembered Python."""

from __future__ import annotations

import sys
from pathlib import Path

from alice import runtime


def test_first_run_remembers_current_python(monkeypatch):
    monkeypatch.delenv(runtime.REEXEC_ENV, raising=False)
    assert runtime.canonical() is None
    assert not runtime.redirect_core(["run"], popen=lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert runtime.same(runtime.canonical(), sys.executable)


def test_core_from_other_python_restarts_with_remembered_one(monkeypatch, tmp_path):
    """Live log: the startup shortcut ran from .venv and a manual start from the system Python;
    the packages differed (no OCR)."""
    monkeypatch.delenv(runtime.REEXEC_ENV, raising=False)
    other = tmp_path / "Python311" / "pythonw.exe"
    other.parent.mkdir()
    other.write_text("")
    runtime.remember(other)
    started = []
    assert runtime.redirect_core(["run", "--tray"], popen=lambda cmd, **k: started.append((cmd, k["env"])))
    cmd, env = started[0]
    assert cmd == [str(other), "-m", "alice", "core", "run", "--tray"] and env[runtime.REEXEC_ENV] == "1"
    monkeypatch.setenv(runtime.REEXEC_ENV, "1")  # the restarted process does not redirect again (no loop)
    assert not runtime.redirect_core(["run"], popen=lambda *a, **k: (_ for _ in ()).throw(AssertionError))


def test_same_environment_is_not_redirected(monkeypatch):
    monkeypatch.delenv(runtime.REEXEC_ENV, raising=False)
    runtime.remember(Path(sys.executable))
    assert not runtime.redirect_core(["run"], popen=lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert runtime.python() == runtime.canonical()
