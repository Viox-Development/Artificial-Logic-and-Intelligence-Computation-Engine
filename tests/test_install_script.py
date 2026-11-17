"""The Windows install script and installer: no stray control characters, and the steps the app relies on."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def test_no_control_characters_in_install_files():
    # A "\a" written through a Python string once became a bell character in install.ps1, so PowerShell failed with
    # "Illegal characters in path" right after installing ALICE.
    for rel in ("install.ps1", "installer/alice.iss"):
        data = (ROOT / rel).read_bytes()
        m = CONTROL.search(data)
        assert m is None, f"{rel}: control character {m.group()!r} at byte {m.start()}"


def test_install_script_runs_the_post_install_steps():
    text = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    for step in (r"& .\.venv\Scripts\alice.exe init", r"& .\.venv\Scripts\alice.exe shortcut",
                 "set_wizard_state(wizard='pending')"):
        assert step in text, step
