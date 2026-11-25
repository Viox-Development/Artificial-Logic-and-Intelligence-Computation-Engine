"""macOS: the ALICE.app launcher made with osacompile."""

from __future__ import annotations

from types import SimpleNamespace

from alice.app import window


def test_mac_shortcut(monkeypatch, tmp_path):
    monkeypatch.setattr(window.sys, "platform", "darwin")
    monkeypatch.setattr(window.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(window.shutil, "which", lambda name: "/usr/bin/osacompile")
    seen = []
    monkeypatch.setattr(window.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or SimpleNamespace(returncode=0, stderr=""))
    assert window.create_shortcut() == [tmp_path / "Applications" / "ALICE.app"]
    assert seen[0][:3] == ["/usr/bin/osacompile", "-o", str(tmp_path / "Applications" / "ALICE.app")]
    assert "-m alice app" in seen[0][-1] and seen[0][-1].startswith("do shell script ")


def test_launcher_script_quotes_the_python_path():
    s = window.mac_launcher_script("/Users/a b/.venv/bin/python")
    assert s == 'do shell script "\'/Users/a b/.venv/bin/python\' -m alice app > /dev/null 2>&1 &"'
