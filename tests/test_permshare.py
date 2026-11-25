"""Permission suggestions (always-approved actions) and sharing via Gist."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

from alice import permissions as perm
from alice.tools.base import ToolContext, ToolError
from alice.tools.permshare import PermissionsTool, Share
from alice.tools.shell import RunCommand
from tests.conftest import make_config


def ctx_for(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_suggests_rules_approved_often_and_never_denied(workdir):
    rc = RunCommand()
    for _ in range(3):
        perm.record_answer(rc, {"command": "npm test -- --watch=false"}, workdir, "exec", "y")
    for _ in range(4):
        perm.record_answer(rc, {"command": "git push origin x"}, workdir, "exec", "y")
    perm.record_answer(rc, {"command": "git push origin y"}, workdir, "exec", "n")  # rejected once
    for _ in range(5):
        perm.record_answer(rc, {"command": "rm -rf build"}, workdir, "danger", "y")  # dangerous: never suggested
    rules = [s["rule"] for s in perm.suggestions(workdir)]
    assert rules == ["run_command(npm test*)"]
    out = PermissionsTool().run(ctx_for(workdir), "suggest")
    assert "npm test" in out.text and "3 kez" in out.text


def test_apply_writes_project_rules_and_they_stop_being_suggested(workdir):
    rc = RunCommand()
    for _ in range(3):
        perm.record_answer(rc, {"command": "npm test"}, workdir, "exec", "y")
    ctx = ctx_for(workdir)
    PermissionsTool().run(ctx, "apply", rules=["run_command(npm test*)"])
    assert "run_command(npm test*)" in perm.project_rules(workdir)["allow"]
    assert perm.suggestions(workdir) == []
    with pytest.raises(ToolError, match="too broad"):
        PermissionsTool().run(ctx, "apply", rules=["*"])


def test_share_publishes_secret_gist_and_refuses_secrets(workdir):
    class API:
        def __init__(self):
            self.body = None

        def post(self, path, body):
            self.body = body
            return {"html_url": "https://gist.github.com/x/1",
                    "files": {"rapor.html": {"raw_url": "https://gist.githubusercontent.com/x/1/raw/rapor.html"}}}

    ctx = ctx_for(workdir)
    api = API()
    ctx.state["github_api"] = api
    (workdir / "rapor.html").write_text("<h1>Rapor</h1>", encoding="utf-8")
    out = Share().run(ctx, ["rapor.html"], title="Haftalık")
    assert api.body["public"] is False and "rapor.html" in api.body["files"]
    hosts = {urlparse(u).hostname for u in re.findall(r"https://\S+", out.text)}
    assert "gistcdn.githack.com" in hosts and "secret" in out.text
    (workdir / ".env").write_text("OPENAI_API_KEY=sk-abcdefabcdefabcdefabcdefabcdef", encoding="utf-8")
    with pytest.raises(ToolError, match="not shared"):
        Share().run(ctx, [".env"])
    assert Share().risk == "send"


def test_record_answer_concurrent_writers_keep_every_row(workdir):
    """Chats writing answers at the same time do not erase each other's rows (lock + atomic write)."""
    import threading

    rc = RunCommand()

    def write(i):
        for _ in range(10):
            perm.record_answer(rc, {"command": f"echo {i}"}, workdir, "exec", "y")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = perm.history_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 60
    assert not list(perm.history_path().parent.glob("*.tmp"))


def test_suggestions_skip_corrupt_rows(workdir):
    """A corrupt row or a record without "rule" does not wipe out all suggestions."""
    rc = RunCommand()
    for _ in range(3):
        perm.record_answer(rc, {"command": "npm test"}, workdir, "exec", "y")
    p = perm.history_path()
    p.write_text(p.read_text(encoding="utf-8") + '{"yarım\n{"tool": "x", "answer": "y"}\n[1, 2]\n'
                 '{"rule": "r", "ts": "dün"}\n', encoding="utf-8")
    assert [s["rule"] for s in perm.suggestions(workdir)] == ["run_command(npm test*)"]
