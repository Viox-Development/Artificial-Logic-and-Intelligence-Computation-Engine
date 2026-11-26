"""Each chat keeps its own model, effort, ultracode and folder; the model's throwaway files go to a scratch folder.

Before: every chat used the app's one router, so a model or effort picked in one chat changed all of them, and a saved
chat reopened in whatever folder the app was in. Helper scripts and commit messages were left in the working folder."""
from __future__ import annotations

import copy
import os
import time

import httpx
import pytest

from alice.app.server import AppServer, AppState
from alice.config import alice_home
from alice.providers.router import ChatRouter
from tests.conftest import FakeProvider, make_config


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", [])
    prov.script = lambda model, messages, tools, **kw: "tamam"
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client
    srv.stop()


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _say(state, client, text="selam"):
    """One finished turn, so the chat is saved and can be reopened."""
    assert client.post("/api/send", json={"text": text}).json()["ok"]
    assert _wait(lambda: not state.chat.busy and any(m.get("role") == "assistant" for m in state.agent.messages))
    state.chat.save()


def test_model_effort_and_ultracode_stay_with_their_chat(app):
    state, client = app
    _say(state, client)
    a = state.sid
    client.post("/api/model", json={"role": "brain", "ref": "a:other"})
    client.post("/api/thinking", json={"level": "high"})
    client.post("/api/ultracode", json={"on": True})
    state.chat.save()

    st = client.post("/api/new", json={}).json()
    # A new chat: the model picked last is the default, the effort and ultracode are not carried over.
    assert st["model"] == "a:other" and st["thinking"] != "high" and not st["ultracode"]
    client.post("/api/model", json={"role": "brain", "ref": "a:third"})
    client.post("/api/thinking", json={"level": "low"})
    _say(state, client)
    b = state.sid

    st = client.post("/api/resume", json={"id": a}).json()
    assert st["ok"] and st["model"] == "a:other" and st["thinking"] == "high" and st["ultracode"]
    st = client.post("/api/resume", json={"id": b}).json()
    assert st["model"] == "a:third" and st["thinking"] == "low" and not st["ultracode"]
    # The effort menu no longer rewrites the default in the config file.
    assert 'brain = "low"' not in (alice_home() / "config.toml").read_text(encoding="utf-8")


def test_automatic_picked_in_one_chat_stays_there(app):
    state, client = app
    client.post("/api/model", json={"role": "brain", "ref": "a:other"})
    _say(state, client)
    a = state.sid
    client.post("/api/new", json={})
    client.post("/api/model", json={"role": "brain", "ref": "reset"})
    assert not state.status()["brain_pinned"]
    _say(state, client)
    assert client.post("/api/resume", json={"id": a}).json()["model"] == "a:other"


def test_saved_chat_opens_in_its_own_folder(app, workdir):
    state, client = app
    one, two = workdir / "bir", workdir / "iki"
    one.mkdir()
    two.mkdir()
    client.post("/api/cwd", json={"path": str(one)})
    _say(state, client)
    a = state.sid
    st = client.post("/api/new", json={}).json()
    assert st["cwd"] == str(one)  # a new chat starts where the last one was
    client.post("/api/cwd", json={"path": str(two)})
    _say(state, client)
    b = state.sid

    st = client.post("/api/resume", json={"id": a}).json()
    assert st["cwd"] == str(one) and str(one) in state.agent.messages[0]["content"]
    assert client.post("/api/resume", json={"id": b}).json()["cwd"] == str(two)


def test_background_chats_do_not_share_choices(app):
    state, client = app
    first = state.chat.router
    from alice.app.server import Chat

    other = Chat(state, state.ctx.cwd, "auto")
    try:
        other.router.role_overrides["brain"] = ["a:x"]
        other.router.thinking_overrides["brain"] = "max"
        assert first.role_overrides.get("brain") != ["a:x"] and first.thinking_level("brain") != "max"
        assert other.ctx.router is other.router and other.agent.router is other.router
    finally:
        other.close()


def test_chat_router_shares_everything_else(app):
    state, _client = app
    base, view = state.router, state.chat.router
    assert isinstance(view, ChatRouter)
    view.cooldown_until["a:m"] = 123.0  # health and cooldowns are shared
    assert base.cooldown_until["a:m"] == 123.0
    view._probe_checked = True  # a plain attribute written through a view lands on the shared router
    assert base._probe_checked is True and "_probe_checked" not in view.__dict__
    view.last_ref["brain"] = "a:served"
    assert "brain" not in base.last_ref
    # Subagents and workflow agents copy the router: the copy is still a view of the same shared router.
    child = copy.copy(view)
    child.role_overrides = {"brain": ["a:child"]}
    child.on_event = lambda s: None
    assert view.role_overrides.get("brain") != ["a:child"] and view.on_event is not child.on_event
    assert child.cooldown_until is base.cooldown_until and child.usage is base.usage


def test_restore_ignores_bad_or_unknown_values(app):
    state, _client = app
    view = state.chat.router
    view.restore({"model": ["nope:x"], "thinking": "turbo", "ultracode": 1})
    assert view.role_overrides.get("brain") != ["nope:x"] and view.thinking_level("brain") != "turbo"
    assert view.ultracode is True
    view.restore(None)  # a chat saved before choices were kept
    view.restore({"model": []})
    assert "brain" not in view.role_overrides


def test_prompt_names_a_scratch_folder_and_old_files_are_pruned(workdir):
    from alice.prompts import build_system_prompt, prune_scratch, scratch_dir

    assert str(scratch_dir()) in build_system_prompt(workdir) and scratch_dir().parent == alice_home()
    scratch_dir().mkdir(parents=True, exist_ok=True)
    old, new = scratch_dir() / "eski" / "_commitmsg.txt", scratch_dir() / "_probe.py"
    old.parent.mkdir(exist_ok=True)
    old.write_text("x", encoding="utf-8")
    new.write_text("y", encoding="utf-8")
    t = time.time() - 30 * 86400
    os.utime(old, (t, t))
    assert prune_scratch() == 1
    assert not old.exists() and not old.parent.exists() and new.exists()


def test_writing_to_scratch_is_not_outside_the_project(workdir):
    from alice.permissions import Permissions
    from alice.prompts import scratch_dir
    from alice.tools import ToolContext
    from alice.tools.base import WRITE

    class T:
        name = "write_file"

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    risky = Permissions._risky_after_untrusted
    assert not risky(T(), {"path": str(scratch_dir() / "_x.py")}, WRITE, ctx)
    assert not risky(T(), {"path": str(workdir / "a.py")}, WRITE, ctx)
    assert risky(T(), {"path": str(workdir.parent / "baska.py")}, WRITE, ctx)
