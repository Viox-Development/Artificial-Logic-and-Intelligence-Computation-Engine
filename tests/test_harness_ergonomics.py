"""Harness ergonomics: smart output truncation, PowerShell noise/encoding, run_command python=, tool auto-load."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from alice.agent import Agent
from alice.permissions import Permissions
from alice.tools import builtin_tools, shell
from alice.tools.base import ToolContext, ToolError, smart_truncate, truncate
from tests.conftest import make_config


# ------------------------------------------------------------------------------------------- truncation
def _pytest_log(n: int = 1500, fail_at: int = 700) -> str:
    lines = [f"tests/test_mod.py::test_{i} PASSED" for i in range(n)]
    lines[fail_at] = f"tests/test_mod.py::test_{fail_at} FAILED"
    lines.insert(fail_at + 1, "E   AssertionError: expected 42, got 41")
    return "\n".join(lines + ["=== 1 failed, 1499 passed in 12.3s ==="])


def test_short_text_is_untouched():
    assert smart_truncate("hello", 100) == "hello"


def test_error_in_the_middle_survives():
    log = _pytest_log()
    old, new = truncate(log, 3000), smart_truncate(log, 3000, failed=True)
    assert "AssertionError" not in old  # the problem this fixes
    assert "test_700 FAILED" in new and "AssertionError" in new
    assert "1 failed, 1499 passed" in new  # the summary at the end
    assert len(new) <= 3300


def test_signal_lines_are_picked_from_a_cut_middle():
    # Lines that do not repeat (no collapse possible): the middle really is cut, errors are kept with line numbers.
    import random

    rnd = random.Random(1)
    lines = [f"step {i}: {''.join(rnd.choice('abcdefgh') for _ in range(40))}" for i in range(2000)]
    lines[900] = "Traceback (most recent call last):"
    lines[901] = "ValueError: bad input in parse_config"
    out = smart_truncate("\n".join(lines), 3000, failed=True)
    assert "important lines" in out and "ValueError: bad input" in out
    assert "|" in out.split("important lines", 1)[1]  # numbered rows


def test_repeated_lines_collapse_without_losing_the_error():
    log = "\n".join([f"Collecting dep{i % 3}" for i in range(400)] + ["ERROR: No matching distribution for foo"])
    out = smart_truncate(log, 3000)
    assert len(out) < 400 and "similar" in out and "No matching distribution" in out


def test_failed_command_gives_the_tail_more_room():
    import random

    rnd = random.Random(2)
    text = "\n".join("".join(rnd.choice("abcdefghij ") for _ in range(60)) for _ in range(2000))
    ok, bad = smart_truncate(text, 4000), smart_truncate(text, 4000, failed=True)
    assert len(bad.rsplit("karakter kesildi", 1)[-1]) > len(ok.rsplit("karakter kesildi", 1)[-1])


# -------------------------------------------------------------------------------------- PowerShell noise
NOISE = """git : Switched to a new branch 'demo'
At line:2 char:1
+ git checkout -b demo 2>&1
+ ~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : NotSpecified: (Switched to a new branch 'demo':String) [], RemoteException
    + FullyQualifiedErrorId : NativeCommandError
real output line"""


def test_native_command_error_noise_is_dropped():
    out = shell.clean_ps_noise(NOISE)
    assert out.splitlines() == ["Switched to a new branch 'demo'", "real output line"]


def test_clean_is_a_no_op_without_the_marker():
    assert shell.clean_ps_noise("At line:1 char:1 in my own log") == "At line:1 char:1 in my own log"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_shell_writes_utf8_not_utf16(tmp_path):
    import subprocess

    subprocess.run(shell.shell_argv("'abc' > a.txt; 'x' | Out-File b.txt; Set-Content c.txt 'y'"), cwd=tmp_path,
                   capture_output=True, check=False)
    for f in ("a.txt", "b.txt", "c.txt"):
        assert (tmp_path / f).read_bytes()[:2] != b"\xff\xfe", f


def test_github_token_from_alice_env_is_not_passed_to_commands(monkeypatch):
    from alice import config

    monkeypatch.setenv("GITHUB_TOKEN", "from-dotenv")
    monkeypatch.setenv("GH_TOKEN", "users-own")
    monkeypatch.setattr(config, "LOADED_ENV_KEYS", {"GITHUB_TOKEN"})
    env = shell._env()
    assert "GITHUB_TOKEN" not in env  # loaded by ALICE: would shadow the user's gh login
    assert env["GH_TOKEN"] == "users-own"  # set by the user themselves: kept


# ------------------------------------------------------------------------------------- run_command python=
def _ctx(workdir: Path) -> ToolContext:
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_python_runs_without_shell_quoting(workdir):
    code = 'import json\nprint(json.dumps({"q": "it\'s \\"fine\\" $HOME \\\\ ok"}))'
    out = shell.RunCommand().run(_ctx(workdir), python=code).text
    assert out.startswith("exit code 0") and '"it\'s \\"fine\\" $HOME \\\\ ok"' in out


def test_python_reports_its_error(workdir):
    out = shell.RunCommand().run(_ctx(workdir), python="raise SystemExit('boom')")
    assert out.is_error and "boom" in out.text


def test_python_and_command_are_exclusive(workdir):
    with pytest.raises(ToolError, match="only one of"):
        shell.RunCommand().run(_ctx(workdir), command="echo hi", python="print(1)")
    with pytest.raises(ToolError, match="command"):
        shell.RunCommand().run(_ctx(workdir))


def test_stdin_feeds_a_command(workdir):
    py = sys.executable.replace("\\", "/")
    out = shell.RunCommand().run(_ctx(workdir), command=f'& "{py}" -c "import sys; print(sys.stdin.read().upper())"'
                                 if sys.platform == "win32" else f'"{py}" -c "import sys; print(sys.stdin.read().upper())"',
                                 stdin="hello").text
    assert "HELLO" in out


def test_dangerous_python_still_needs_approval():
    assert shell.RunCommand().risk_for({"python": "import os; os.system('rm -rf /')"}) == "danger"


# ------------------------------------------------------------------------------------------ auto-load
def _agent(workdir: Path) -> Agent:
    cfg = make_config({"brain": ["a:m"]})
    return Agent(cfg, None, builtin_tools(), ToolContext(cwd=workdir, config=cfg), Permissions(mode="yolo"))


def test_calling_an_unloaded_tool_runs_it_and_loads_its_group(workdir):
    a = _agent(workdir)
    assert "system_info" not in {s["function"]["name"] for s in a.tool_specs()}
    prep = a._prepare({"id": "1", "function": {"name": "system_info", "arguments": "{}"}})
    assert isinstance(prep, tuple)  # runs now, no "load it first" error
    assert "system_info" in {s["function"]["name"] for s in a.tool_specs()}  # full definition from now on


def test_quiet_probe_does_not_load_groups(workdir):
    a = _agent(workdir)
    a._prepare({"id": "1", "function": {"name": "system_info", "arguments": "{}"}}, quiet=True)
    assert "system_info" not in {s["function"]["name"] for s in a.tool_specs()}
