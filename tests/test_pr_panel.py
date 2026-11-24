"""PR panel: live status of watched PRs, behavior buttons and cache. GitHub is faked."""

from __future__ import annotations

import pytest

from alice import prwatch


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(prwatch, "alice_home", lambda: tmp_path)
    prwatch._live_cache.clear()
    return tmp_path


class FakeAPI:
    def __init__(self, ci="failure"):
        self.ci, self.calls = ci, 0

    def get(self, path, **kw):
        self.calls += 1
        return {"title": "Panel", "html_url": "https://github.com/o/r/pull/7", "state": "open", "merged": False,
                "draft": False, "mergeable": False, "head": {"ref": "dal", "sha": "abcdef123"}, "base": {"ref": "main"}}

    def checks(self, repo, sha):
        return {"state": self.ci, "failed": ["test (3.12)"] if self.ci == "failure" else [], "pending": [], "total": 3}

    def graphql(self, q, **v):
        return {}


def test_set_flag_toggles_only_known_flags(home):
    prwatch.watch("o/r", 7)
    assert prwatch.set_flag("o/r", 7, "auto_merge", True)
    w = prwatch.load()[0]
    assert w.auto_merge and "auto_merge on" in w.status
    assert not prwatch.set_flag("o/r", 7, "path", True)  # an unknown field cannot be changed
    assert not prwatch.set_flag("o/r", 8, "auto_fix", False)  # PR not being watched


def test_live_status_row_and_cache(home, monkeypatch):
    monkeypatch.setattr("alice.tools.github.review_threads",
                        lambda api, repo, n: {"threads": [{"resolved": False}, {"resolved": True}]})
    api = FakeAPI()
    row = prwatch.live_status(api, "o/r", 7)
    assert row["ci"] == "failure" and row["failed"] == ["test (3.12)"] and row["open_threads"] == 1
    assert row["mergeable"] is False and row["head"] == "dal" and row["sha"] == "abcdef1"
    prwatch.live_status(api, "o/r", 7)
    assert api.calls == 1  # 30 sec cache
    prwatch.live_status(api, "o/r", 7, cache=False)
    assert api.calls == 2


def test_pr_panel_lists_watches_with_live_state(home, monkeypatch):
    from alice.app import server

    prwatch.watch("o/r", 7, session="s1")
    monkeypatch.setattr("alice.tools.github.github_token", lambda cfg=None: "t")
    monkeypatch.setattr("alice.tools.github.GitHubAPI", lambda token: FakeAPI("success"))
    monkeypatch.setattr("alice.tools.github.review_threads", lambda api, repo, n: {"threads": []})
    monkeypatch.setattr("alice.tools.github.origin_repo", lambda cwd: None)
    st = type("S", (), {"cfg": None, "ctx": type("C", (), {"cwd": home})()})()
    out = server.AppState.pr_panel(st)
    w = out["watching"][0]
    assert out["ok"] and w["number"] == 7 and w["ci"] == "success" and w["session"] == "s1"
    assert w["auto_fix"] and not w["auto_merge"] and out["suggest"] is None
