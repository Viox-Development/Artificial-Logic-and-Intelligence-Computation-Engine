"""Finds missing packages and installs them into the Python that runs ALICE."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from alice import deps, updater


def test_missing_checks_every_package_of_installed_extras(monkeypatch):
    """Live recording: .venv had the "computer" extra's mss but no OCR packages; a single-package guess missed it,
    so OCR silently stayed off."""
    monkeypatch.setattr(deps, "_pyproject", lambda: {"project": {
        "dependencies": ["httpx>=0.27", "winrt-Windows.Media.Ocr>=2; sys_platform == 'win32'",
                         "tomli>=2; python_version < '3.0'"],
        "optional-dependencies": {"computer": ["mss>=9", "pywinauto>=0.6.8; sys_platform == 'win32'"],
                                  "all": ["alice-harness[computer]"]}}})
    have = {"httpx", "mss"}
    monkeypatch.setattr(deps, "_installed", lambda n: n in have)
    lack = deps.missing(["computer"])
    if sys.platform == "win32":
        assert lack == {"": ["winrt-Windows.Media.Ocr"], "computer": ["pywinauto"]}
    else:  # the marker does not apply on this platform: not requested
        assert lack == {}
    have |= {"winrt-Windows.Media.Ocr", "pywinauto"}
    assert deps.missing(["computer"]) == {}


def test_heal_installs_into_the_running_python(monkeypatch):
    monkeypatch.setattr(deps, "missing", lambda extras=None: {"": ["pypdf"], "computer": ["pywinauto"]})
    monkeypatch.setattr(updater, "installed_extras", lambda: ["computer", "voice"])
    ran, logs = [], []

    def run(cmd, **kw):
        ran.append((cmd, kw["cwd"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert deps.heal(logs.append, run=run) == ["pypdf", "pywinauto"]
    cmd, cwd = ran[0]
    assert cmd[:5] == [sys.executable, "-m", "pip", "install", "-q"] and cmd[-1] == ".[computer,voice]"
    assert cwd == str(deps.REPO) and "kuruldu" in logs[-1]

    monkeypatch.setattr(deps, "missing", lambda extras=None: {})
    assert deps.heal(logs.append, run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("kurmamalı"))) == []


def test_install_command_names_this_python():
    cmd = deps.install_command(["computer"])
    assert cmd.startswith(f'"{sys.executable}" -m pip install -e "') and cmd.endswith('[computer]"')
