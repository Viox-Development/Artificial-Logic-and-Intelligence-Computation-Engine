"""Finds missing packages and installs them into the Python that runs ALICE."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from alice import deps, updater

# tests/conftest.py deliberately stubs heal_in_background for every test; keep the real one for its own test.
_REAL_HEAL_IN_BACKGROUND = deps.heal_in_background


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


def test_applies_markers_on_this_platform():
    assert deps._applies("") is True
    assert deps._applies("   ") is True
    assert deps._applies("sys_platform == 'win32'") is (sys.platform == "win32")
    assert deps._applies("sys_platform == 'not-a-real-platform'") is False
    assert deps._applies("python_version >= '3.0'") is True
    assert deps._applies("python_version < '3.0'") is False
    assert deps._applies("this is not a marker") is True  # unparseable markers fail open


def test_pyproject_reads_file_and_handles_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(deps, "REPO", tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["httpx>=0.27"]\n', encoding="utf-8"
    )
    assert deps._pyproject()["project"]["dependencies"] == ["httpx>=0.27"]
    (tmp_path / "pyproject.toml").unlink()
    assert deps._pyproject() == {}


def test_installed_checks_distribution_metadata(monkeypatch):
    from importlib import metadata

    def version(name):
        if name == "present":
            return "1.2.3"
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", version)
    assert deps._installed("present") is True
    assert deps._installed("absent-for-sure") is False


def test_heal_with_nothing_missing_never_calls_pip(monkeypatch):
    monkeypatch.setattr(deps, "missing", lambda extras=None: {})
    logs = []

    def run(*args, **kwargs):
        raise AssertionError("pip must not be called when nothing is missing")

    assert deps.heal(logs.append, run=run) == []
    assert logs == []


def test_heal_reports_failing_pip(monkeypatch):
    monkeypatch.setattr(deps, "missing", lambda extras=None: {"": ["missing-package"]})
    monkeypatch.setattr(updater, "installed_extras", lambda: [])
    logs = []

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="pip failed")

    assert deps.heal(logs.append, run=run) == []
    assert any("kurulamad" in line for line in logs)


def test_heal_in_background_starts_the_thread(monkeypatch):
    seen = []
    monkeypatch.setattr(deps, "heal", lambda log: seen.append(log) or ["ok"])
    logs = []
    thread = _REAL_HEAL_IN_BACKGROUND(logs.append)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert seen == [logs.append]
