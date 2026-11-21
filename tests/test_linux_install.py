"""Linux install: the freedesktop launcher (alice shortcut) and install.sh."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from alice.app import window

ROOT = Path(__file__).resolve().parent.parent
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="freedesktop launchers are Linux-only")


def _fields(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines()[1:] if "=" in line)


def test_desktop_entry_runs_the_venv_python_without_a_terminal():
    py, icon = PurePosixPath("/opt/alice/.venv/bin/python"), PurePosixPath("/opt/alice/icon.png")
    text = window.linux_desktop_entry(py, icon)
    assert text.startswith("[Desktop Entry]\n")
    f = _fields(text)
    assert f["Type"] == "Application" and f["Name"] == "ALICE"
    assert f["Exec"] == f"{py} -m alice app"
    assert f["Icon"] == str(icon) and f["Terminal"] == "false"


def _unescape_string(v: str) -> str:
    """Desktop Entry string-level unescape (\\\\ -> \\, \\s -> space, ...), the first of the spec's two passes."""
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(v[i + 1], "\\" + v[i + 1]))
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


def _split_exec(v: str) -> list[str]:
    """The spec's second pass: field codes (%% -> %) and quoted arguments with \\" \\$ \\` \\\\ escapes."""
    args, cur, quoted, i, s = [], [], False, 0, _unescape_string(v)
    while i < len(s):
        c = s[i]
        if c == "%" and s[i + 1:i + 2] == "%":
            cur.append("%")
            i += 2
            continue
        if quoted and c == "\\" and s[i + 1:i + 2] in ('"', "$", "`", "\\"):
            cur.append(s[i + 1])
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        elif c == " " and not quoted:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        args.append("".join(cur))
    return args


@pytest.mark.parametrize("path", ['/home/a b/$x/"q"/python', "/home/u/100%/alice/python", "/opt/a\\b/`c`/python",
                                  "/plain/.venv/bin/python"])
def test_desktop_entry_exec_round_trips_through_the_spec(path):
    f = _fields(window.linux_desktop_entry(PurePosixPath(path), PurePosixPath("/i.png")))
    # What a desktop environment ends up running must be exactly the venv Python plus "-m alice app".
    assert _split_exec(f["Exec"]) == [path, "-m", "alice", "app"]


def test_desktop_entry_escapes_percent_as_a_field_code():
    f = _fields(window.linux_desktop_entry(PurePosixPath("/home/u/100%/python"), PurePosixPath("/i.png")))
    assert "100%%" in f["Exec"]


@linux_only
def test_linux_shortcut_goes_to_the_menu_and_desktop(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setattr(window, "_desktop_dir", lambda: home / "Desktop")
    made = window.create_shortcut()
    menu = home / ".local" / "share" / "applications" / "alice.desktop"
    assert menu in made and (home / "Desktop" / "alice.desktop") in made
    assert "-m alice app" in menu.read_text(encoding="utf-8")
    assert menu.stat().st_mode & 0o111  # launchers on the desktop only run when executable


@linux_only
def test_linux_shortcut_without_a_desktop_folder_only_adds_the_menu(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setattr(window, "_desktop_dir", lambda: None)
    assert window.create_shortcut() == [tmp_path / "share" / "applications" / "alice.desktop"]


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")
def test_install_sh_parses_and_rejects_unknown_options():
    script = ROOT / "install.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    r = subprocess.run(["bash", str(script), "--bogus"], capture_output=True, text=True)
    assert r.returncode == 2 and "unknown option" in r.stderr
    h = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert h.returncode == 0 and "curl -fsSL" in h.stdout and "set -euo" not in h.stdout
    # Piped (`curl | bash -s -- --help`): $0 is "bash", so help must not depend on reading the script file.
    p = subprocess.run(["bash", "-s", "--", "--help"], input=script.read_text(encoding="utf-8"), capture_output=True,
                       text=True)
    assert p.returncode == 0 and "curl -fsSL" in p.stdout


def test_install_sh_is_committed_with_lf_endings():
    # A CRLF install.sh fails on Linux with "$'\r': command not found"; .gitattributes keeps it LF.
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attrs
