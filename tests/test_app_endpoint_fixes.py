"""Edge cases of the app endpoints: malformed input returns 400, not 500; partial record fields don't erase data."""

from __future__ import annotations

import httpx
import pytest

from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", [])
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client
    srv.stop()


def test_usage_has_session_breakdown_and_today(app):
    state, client = app
    state.router.usage.record("a:m", {"prompt_tokens": 10, "completion_tokens": 2}, True)
    u = client.get("/api/usage").json()
    # The usage breakdown panel (models/tools/ctx) and Settings > Usage (today) are fed from the same response.
    assert {"models", "tools", "ctx", "steps"} <= set(u)
    assert u["today"]["a:m"]["requests"] == 1


@pytest.mark.parametrize("mi", ["abc", [1], {"x": 1}, True, 99])
def test_rewind_bad_index_is_400(app, mi):
    state, client = app
    r = client.post("/api/rewind", json={"mi": mi})
    assert r.status_code == 400 and r.json()["ok"] is False


@pytest.mark.parametrize("path", ["/api/prs/watch", "/api/prs/unwatch", "/api/prs/flag", "/api/prs/rerun"])
@pytest.mark.parametrize("number", ["abc", "1.5x", [3], {"n": 1}])
def test_prs_bad_number_is_400(app, path, number):
    state, client = app
    r = client.post(path, json={"repo": "o/r", "number": number})
    assert r.status_code == 400 and r.json()["ok"] is False
    assert client.post(path, json={"repo": "o/r"}).status_code == 400  # a missing number is 400 too


def test_resume_empty_id_is_400_and_keeps_current_chat(app, monkeypatch):
    from alice import session as sess

    state, client = app
    before = state.chat.sid
    calls = []
    monkeypatch.setattr(sess, "load", lambda sid, *a, **k: calls.append(sid))
    for body in ({}, {"id": ""}, {"id": "  "}, {"id": None}, {"id": "", "detached": True}):
        r = client.post("/api/resume", json=body)
        assert r.status_code == 400 and r.json()["ok"] is False, body
    assert state.resume("") is False
    assert calls == [] and state.chat.sid == before  # the most recent chat was not opened
