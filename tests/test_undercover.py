"""Undercover mode: text sent to GitHub carries no signature or AI trace; ALICE recognises its own comments by ID."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from alice import prwatch, undercover
from alice.tools import github as gh
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config


def cfg(mode="on", words=None):
    c = make_config({"brain": ["a:m"]})
    c.data.setdefault("github", {})["undercover"] = mode
    if words:
        c.data["github"]["undercover_words"] = words
    return c


class API:
    token = "t"

    def __init__(self, private=False):
        self.private = private
        self.calls = []
        self.next_id = 900

    def get(self, path, **kw):
        self.calls.append(("GET", path))
        return {"private": self.private, "default_branch": "main"}

    def post(self, path, body):
        self.next_id += 1
        self.calls.append(("POST", path, body))
        return {"id": self.next_id, "html_url": "https://x", "number": 3}


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(undercover, "_public", {})


def test_scrub_removes_signature_lines():
    text = ("Önbellek anahtarını düzelt\n\nAyrıntı.\n\nCo-Authored-By: Claude Opus <noreply@anthropic.com>\n"
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n\n— ALICE")
    assert undercover.scrub(text) == "Önbellek anahtarını düzelt\n\nAyrıntı."
    # a human co-author is kept
    assert "Co-Authored-By: Ayşe" in undercover.scrub("x\n\nCo-Authored-By: Ayşe <ayse@ornek.com>")


def test_clean_rejects_remaining_tells():
    c = cfg(words=["Projekt-Kuzgun"])
    with pytest.raises(ToolError, match="ALICE"):
        undercover.clean(c, "ALICE bu hatayı düzeltti")
    with pytest.raises(ToolError, match="yapay zekâ"):
        undercover.clean(c, "As an AI, I fixed the bug")
    with pytest.raises(ToolError, match="Projekt-Kuzgun"):
        undercover.clean(c, "projekt-kuzgun için hazırlık")
    assert undercover.clean(c, "Boş listede çökme düzeltildi\n\n— ALICE") == "Boş listede çökme düzeltildi"


def test_modes_and_public_check():
    assert undercover.mode(cfg("off")) == "off" and undercover.mode(cfg("true")) == "on"
    assert undercover.mode(cfg("saçma")) == "off"
    assert undercover.active(cfg("on"), None)
    assert not undercover.active(cfg("off"), "o/r", API())
    assert undercover.active(cfg("auto"), "o/acik", API(private=False))
    assert not undercover.active(cfg("auto"), "o/gizli", API(private=True))


def test_own_ids_are_recognised_without_signature():
    undercover.remember_own(11, 12)
    assert gh.by_alice("imzasız cevap", 12) and not gh.by_alice("imzasız cevap", 13)
    data = {"threads": [{"id": "T1", "resolved": False, "path": "a.py", "line": 3, "comments": [
        {"id": 50, "author": "sahip", "body": "burayı düzelt", "trusted": True, "at": ""},
        {"id": 11, "author": "sahip", "body": "düzelttim", "trusted": True, "at": ""}]}],
        "reviews": [], "comments": [{"id": 12, "author": "sahip", "body": "tamam", "trusted": True, "at": ""}]}
    good, outside = prwatch.new_comments(prwatch.Watch("o/r", 7), data)
    assert good == [] and outside == []  # the last word is ALICE's, and so is the general comment


def test_github_tool_hidden_reply_and_pr(monkeypatch, tmp_path):
    api = API()
    ctx = ToolContext(cwd=tmp_path, config=cfg("on"))
    ctx.state["github_api"] = api
    tool = gh.GitHub()
    tool.run(ctx, "review_reply", repo="o/r", number=7, comment_id=5, body="Sınır kontrolü eklendi.")
    body = api.calls[-1][2]["body"]
    assert gh.ALICE_MARK not in body and undercover.is_own(api.next_id)
    tool.run(ctx, "pr_comment", repo="o/r", number=7, body="Testler eklendi.\n\n🤖 Generated with ALICE")
    assert api.calls[-1][2]["body"] == "Testler eklendi." and undercover.is_own(api.next_id)
    with pytest.raises(ToolError, match="gizli mod"):
        tool.run(ctx, "issue_create", repo="o/r", title="Claude ile bulunan hata", body="x")


def test_github_tool_signs_when_off(tmp_path):
    api = API()
    ctx = ToolContext(cwd=tmp_path, config=cfg("off"))
    ctx.state["github_api"] = api
    gh.GitHub().run(ctx, "review_reply", repo="o/r", number=7, comment_id=5, body="Düzeltildi.")
    assert api.calls[-1][2]["body"].endswith(gh.ALICE_MARK)


@pytest.mark.skipif(shutil.which("git") is None, reason="git yok")
def test_commit_message_is_cleaned(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    ctx = ToolContext(cwd=tmp_path, config=cfg("on"))
    tool = gh.Git()
    with pytest.raises(ToolError, match="gizli mod"):
        tool.run(ctx, "commit", message="ALICE: dosya eklendi", all=True)
    tool.run(ctx, "commit", message="Dosya eklendi\n\nCo-Authored-By: Claude <noreply@anthropic.com>", all=True)
    msg = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert msg.strip() == "Dosya eklendi"


def test_fix_prompt_hidden_rules():
    pr = {"head": {"ref": "d"}, "base": {"ref": "main"}, "title": "t"}
    p = prwatch.Problems(ci=[{"name": "test", "log": "E"}])
    w = prwatch.Watch("o/r", 7)
    assert gh.ALICE_MARK in prwatch.fix_prompt(w, pr, p)
    hidden = prwatch.fix_prompt(w, pr, p, hidden=True)
    assert gh.ALICE_MARK not in hidden and "Gizli mod" in hidden
