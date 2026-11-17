"""The agent eval set (evals/): every task must fail as shipped and pass with its reference solution, and the runner
must record results and the council comparison. No real model is called here; the evals themselves are run by hand
(python -m evals), not in CI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evals import runner
from tests.conftest import FakeProvider, make_config, tool_call

TASKS = runner.load_tasks()


def _work(task: runner.Task, tmp_path: Path) -> Path:
    work = tmp_path / "work"
    shutil.copytree(task.root / "repo", work)
    return work


def _solve(task: runner.Task, work: Path) -> tuple[str, list[str]]:
    ref = task.root / "reference"
    if (ref / "files").is_dir():
        shutil.copytree(ref / "files", work, dirs_exist_ok=True)
    if (ref / "solve.py").is_file():
        subprocess.run([sys.executable, str(ref / "solve.py")], cwd=work, check=True)
    reply = (ref / "reply.txt").read_text(encoding="utf-8") if (ref / "reply.txt").is_file() else ""
    tools = (ref / "tools.txt").read_text(encoding="utf-8").split() if (ref / "tools.txt").is_file() else []
    return reply, tools


def test_set_is_broad_enough():
    assert 10 <= len(TASKS) <= 20
    cats = {t.category for t in TASKS}
    assert {"bugfix", "tests", "refactor", "log-reading", "multi-file", "shell", "web"} <= cats
    for t in TASKS:
        assert (t.root / "reference").is_dir(), f"{t.id} has no reference solution"
        assert all(c.get("type") in runner.CHECKS for c in t.checks), t.id


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_task_fails_untouched(task, tmp_path):
    work = _work(task, tmp_path)
    checks = runner.run_checks(task, work, "", [])
    assert not all(c["ok"] for c in checks), f"{task.id} passes without any work"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_reference_solution_passes(task, tmp_path, monkeypatch):
    if "git" in task.needs and shutil.which("git") is None:
        pytest.skip("git not installed")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    work = _work(task, tmp_path)
    reply, tools = _solve(task, work)
    checks = runner.run_checks(task, work, reply, tools)
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


def test_checks(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 2\n", encoding="utf-8")
    ok, _ = runner.check_file({"path": "*.py", "pattern": r"x =", "max_count": 1}, tmp_path, "", [])
    assert not ok
    ok, _ = runner.check_file({"path": "**/*.py", "pattern": r"\by\b", "absent": True}, tmp_path, "", [])
    assert ok
    assert runner.check_reply({"pattern": "rfc 9562"}, tmp_path, "It is RFC 9562.", [])[0]
    assert not runner.check_tool({"any": ["web_search"]}, tmp_path, "", ["read_file"])[0]
    ok, detail = runner.check_command({"run": "python -c \"print('hi')\"", "output": "^hi$"}, tmp_path, "", [])
    assert ok, detail


def test_parse_chains():
    assert runner.parse_chains("a:x, b:y", ["council=c:z"]) == {"*": ["a:x", "b:y"], "council": ["c:z"]}
    with pytest.raises(SystemExit):
        runner.parse_chains("nocolon", None)


HALF = (TASKS[0].root.parent / "fix-pagination" / "repo" / "pager.py").read_text(encoding="utf-8").replace(
    "start = page * per_page", "start = (page - 1) * per_page")
FULL = HALF.replace("return len(items) // per_page", "return -(-len(items) // per_page)")
CRITIC = ('Sorun var.\n```json\n{"vote": "reject", "reason": "page_count", "findings": [{"severity": "major", '
          '"title": "page_count rounds down", "evidence": "pager.py", "fix": "round up"}]}\n```')
VERIFIER = ('Doğru.\n```json\n{"checks": [{"id": "F1", "status": "confirmed", "note": "test fails"}], '
            '"vote": "reject", "reason": "test fails"}\n```')


def fake_model(model, messages, tools, **kw):
    last = next(m["content"] for m in reversed(messages) if m["role"] == "user" and isinstance(m["content"], str))
    if "# İnceleme kurulu" in messages[0]["content"]:
        return VERIFIER if "Diğer rollerin bulguları" in last else CRITIC
    start = max(i for i, m in enumerate(messages) if m["role"] == "user")
    done = sum(1 for m in messages[start:] if m["role"] == "tool")
    content = FULL if "review council" in last else HALF  # the first attempt fixes only one of the two bugs
    if done == 0:
        return tool_call("read_file", {"path": "pager.py"}, f"r{len(messages)}")
    if done == 1:
        return tool_call("write_file", {"path": "pager.py", "content": content}, f"w{len(messages)}")
    return "Fixed pager.py."


def test_runner_end_to_end_with_council(tmp_path, monkeypatch):
    """Plain run fails (one bug left), the council finds it, the fix turn makes the task pass."""
    from alice.providers.router import Router

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    fake = FakeProvider("a", fake_model)
    router = Router(cfg, provider_factory=lambda pc: fake, usage_path=tmp_path / "usage.json")
    runner.restrict_models(router, {"*": ["a:m"]})
    meter = runner.Meter(router)
    task = next(t for t in TASKS if t.id == "fix-pagination")
    out = tmp_path / "out"
    rows = [runner.run_task(task, cfg, router, meter, runner.Settings(council=c, transcripts=out), log=lambda s: None)
            for c in (False, True)]
    plain, council = rows
    assert not plain["passed"] and plain["steps"] == 3 and plain["tool_calls"] == 2
    assert plain["tokens_in"] == 30 and plain["council_tokens"] == 0
    assert council["council_review"]["blocking"] and council["council_review"]["fix_round"]
    assert council["passed"], council["checks"]
    assert council["council_calls"] >= 2 and council["council_tokens"] > 0
    assert (out / "fix-pagination-council.json").is_file()
    md = runner.scoreboard({"meta": {"started": "now", "models": "a:m", "tasks": 1, "mode": "auto",
                                     "alice_version": "x", "commit": "", "python": "3", "platform": "test"},
                            "runs": rows})
    assert "Does the council help?" in md and "Passed only with the council: fix-pagination" in md
    json.dumps(rows)  # results must be JSON-serialisable
