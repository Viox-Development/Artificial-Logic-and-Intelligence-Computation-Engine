"""Long-conversation bugs: a steer message sent while working, a silent turn end, a broken summary, a title,
an archive duplicate, the "okundu" (read) record lost on restart, a dead Google token, a remote branch switch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from alice import session as sess
from alice.agent import _fallback_summary, _valid_summary
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config, tool_call
from tests.test_agent import make_agent


# ------------------------------------------------------------------------------------------- steer
def test_message_sent_while_working_reaches_the_model_in_the_same_turn(workdir):
    seen: list[str] = []

    def script(model, messages, tools, **kw):
        seen.append(json.dumps(messages, ensure_ascii=False))
        if len(seen) == 1:
            # The user types while the first step is running (UI /api/send -> agent.steer).
            assert agent.steer("bir de README'ye bak")
            return tool_call("list_dir", {"path": "."})
        return "tamam, README'ye de baktım"

    agent, _, _ = make_agent(script, workdir)
    assert agent.run_turn("dosyaları listele") == "tamam, README'ye de baktım"
    assert "bir de README'ye bak" in seen[1]  # it reached the model at the next step; the turn did not wait for it
    steer = [m for m in agent.messages if m.get("_steer")]
    assert len(steer) == 1 and steer[0]["_display"] == "bir de README'ye bak"
    # No message was inserted between the tool call and its result.
    i = agent.messages.index(steer[0])
    assert agent.messages[i - 1]["role"] == "tool"
    assert not agent.steer("tur bitti") and not agent.take_inbox()  # when idle it is not accepted: it starts a new turn


def test_message_arriving_with_the_final_answer_keeps_the_turn_going(workdir):
    calls = {"n": 0}

    def script(model, messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            agent.steer("dur, Türkçe yaz")
            return "done"
        return "bitti"

    agent, _, _ = make_agent(script, workdir)
    assert agent.run_turn("selam") == "bitti" and calls["n"] == 2


# ------------------------------------------------------------------------------------ silent end
def test_turn_that_runs_out_of_models_says_so_in_the_chat(workdir):
    from alice.providers.openai_compat import ProviderError

    def script(model, messages, tools, **kw):
        raise ProviderError("server", "503 dolu", 503)

    agent, _, _ = make_agent(script, workdir, finish_nudges=0)
    agent.router.max_wait = 0
    out = agent.run_turn("bir şey yap")
    assert "half done" in out and "continue" in out
    last = agent.messages[-1]
    assert last["role"] == "assistant" and last.get("_notice") and "503" in last["content"]


def test_promise_to_report_after_background_work_is_not_nagged(workdir):
    agent, _, _ = make_agent(["x"], workdir)
    agent._turn_tools = {"team"}
    assert agent._unfinished("İşi ajanlara verdim. Bitince ne değiştiğini yazacağım.") == ""
    assert agent._unfinished("Şimdi testleri çalıştıracağım.")  # a real unfinished promise is still reminded


# --------------------------------------------------------------------------------------------- summary
def test_summary_that_is_a_tool_call_dump_is_rejected():
    assert not _valid_summary('<tool calls> <tool call name="read_file"> <parameter name="path">a.py</parameter>')
    assert not _valid_summary('-> git({"action": "commit"}) [tool] ok\n[assistant] x\n[tool] y\n[user] z')
    assert _valid_summary("ÖZET: dosyalar okundu, sıradaki iş testler")


def test_fallback_summary_keeps_user_requests_and_last_answer():
    old = [{"role": "user", "content": "WT uyarılarına susturma tuşu ekle"},
           {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {
               "name": "edit_file", "arguments": json.dumps({"path": "alice/voice/speaker.py"})}}]},
           {"role": "tool", "tool_call_id": "1", "content": "düzenlendi"},
           {"role": "user", "content": "[devam kontrolü] x", "_nudge": True},
           {"role": "assistant", "content": "Ctrl+Shift+M eklendi"}]
    s = _fallback_summary(old)
    assert "susturma tuşu" in s and "alice/voice/speaker.py" in s and "Ctrl+Shift+M" in s
    assert "devam kontrolü" not in s


# --------------------------------------------------------------------------------------- session
def test_session_title_comes_from_the_first_real_prompt_not_the_summary():
    sid = sess.new_session_id()
    msgs = [{"role": "user", "content": "[Önceki konuşmanın özeti]\n<tool calls>…"},
            {"role": "assistant", "content": "Özeti aldım"}]
    sess.archive(sid, [{"role": "user", "content": "[devam kontrolü] x", "_nudge": True},
                       {"role": "user", "content": "WT'de uyarı anonslarını kapatma tuşu ekle"}])
    assert sess.session_prompt(sid, msgs) == "WT'de uyarı anonslarını kapatma tuşu ekle"

    class R:
        def chat(self, role, messages, **kw):
            assert role == "fast" and "kapatma tuşu" in messages[-1]["content"]
            return type("Res", (), {"text": "War Thunder uyarı susturma"})()

    assert sess.ensure_title(R(), sid, msgs) == "War Thunder uyarı susturma"
    assert sess.load_meta()[sid]["auto_title"] == "War Thunder uyarı susturma"
    assert sess.ensure_title(None, sid, msgs) == "War Thunder uyarı susturma"  # generated only once


def test_bad_model_title_is_ignored():
    class R:
        def chat(self, role, messages, **kw):
            return type("Res", (), {"text": "<tool_call>{\"name\": \"x\"}</tool_call>"})()

    assert sess.make_title(R(), "bir şey yap") == ""


def test_archive_does_not_write_the_same_messages_twice():
    sid = sess.new_session_id()
    old = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    sess.archive(sid, old)
    sess.archive(sid, old)  # a record was written after summarizing, and the same messages were summarized again
    assert [m["content"] for m in sess.load_archive(sid)] == ["a", "b"]


# ------------------------------------------------------------------------------- read record
def test_reads_are_restored_after_restart_unless_the_file_changed(workdir):
    from alice.tools.files import require_read, restore_reads

    a, b = workdir / "a.py", workdir / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")
    msgs = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "1", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})}},
        {"id": "2", "function": {"name": "read_file", "arguments": json.dumps({"path": str(b)})}}]},
        {"role": "tool", "tool_call_id": "1", "content": "1 x = 1"},
        {"role": "tool", "tool_call_id": "2", "content": "1 y = 1"}]
    saved = time.time()
    later = saved + 30
    os.utime(b, (later, later))  # b changed after the chat was saved
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    assert restore_reads(ctx, msgs, saved) == 1
    require_read(ctx, a.resolve())
    with pytest.raises(ToolError, match="okunmadı"):
        require_read(ctx, b.resolve())



def test_apply_patch_writes_are_restored_as_read_after_restart(workdir):
    from alice.tools.files import require_read, restore_reads

    (workdir / "n.py").write_text("x = 2\n", encoding="utf-8")
    (workdir / "m.py").write_text("y = 2\n", encoding="utf-8")
    patch = ("*** Begin Patch\n*** Add File: n.py\n+x = 2\n*** Update File: old.py\n*** Move to: m.py\n-y = 1\n"
             "+y = 2\n*** Delete File: gone.py\n*** End Patch")
    msgs = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "1", "function": {"name": "apply_patch", "arguments": json.dumps({"patch": patch})}},
        {"id": "2", "function": {"name": "apply_patch", "arguments": "{bozuk"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "yama uygulandı"},
        {"role": "tool", "tool_call_id": "2", "content": "yama uygulandı"}]
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    assert restore_reads(ctx, msgs, time.time() + 1) == 2
    require_read(ctx, (workdir / "n.py").resolve())
    require_read(ctx, (workdir / "m.py").resolve())


# --------------------------------------------------------------------------------- Google sign-in
def test_dead_refresh_token_is_tried_once_then_reported_without_network(monkeypatch):
    from alice.connect import oauth, store
    from alice.connect.manifest import Auth

    store.save("google_calendar", access_token="a", refresh_token="r1", expires_at=1.0)
    calls = []

    class Resp:
        status_code = 400
        text = '{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'

    def post(*a, **k):
        calls.append(1)
        return Resp()

    monkeypatch.setattr("httpx.post", post)
    auth = Auth(kind="oauth2", token_url="https://t")
    for _ in range(3):
        with pytest.raises(oauth.OAuthError, match="yeniden giriş"):
            oauth.refresh(auth, "google_calendar", "cid")
    assert len(calls) == 1  # no more requests were sent with the dead token
    assert oauth.needs_sign_in("google_calendar")
    store.save("google_calendar", refresh_token="r2")  # the user reconnected
    assert not oauth.needs_sign_in("google_calendar")


# ----------------------------------------------------------------------------------------- git
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t",
           # The temp dir's owner may look different (sandbox, CI); git must not reject it as "dubious ownership".
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=env)
    assert r.returncode == 0, (args, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace"))


@needs_git
def test_switch_to_a_branch_that_only_exists_on_github(tmp_path):
    from alice.tools.github import current_branch, list_branches, switch_branch

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    one = tmp_path / "one"
    one.mkdir()
    _git(one, "init", "-b", "main")
    (one / "a.txt").write_text("a\n", encoding="utf-8")
    _git(one, "add", "a.txt")
    _git(one, "commit", "-m", "ilk")
    _git(one, "remote", "add", "origin", str(remote))
    _git(one, "push", "-u", "origin", "main")
    _git(one, "switch", "-c", "ozellik")
    (one / "b.txt").write_text("b\n", encoding="utf-8")
    _git(one, "add", "b.txt")
    _git(one, "commit", "-m", "özellik")
    _git(one, "push", "-u", "origin", "ozellik")
    two = tmp_path / "two"
    _git(tmp_path, "clone", "-q", str(remote), str(two))
    assert current_branch(two) == "main"
    names = {b["name"]: b for b in list_branches(two)}
    assert names["ozellik"]["remote"] and not names["main"]["remote"]
    switch_branch(two, "ozellik")  # not local: a branch tracking origin's copy is created
    assert current_branch(two) == "ozellik" and (two / "b.txt").exists()
    assert "already on" in switch_branch(two, "ozellik")
    with pytest.raises(ToolError, match="ne yerelde ne de origin"):
        switch_branch(two, "yok-boyle-dal")
    switch_branch(two, "yeni-dal", create=True)
    assert current_branch(two) == "yeni-dal"


def test_client_config_error_is_not_treated_as_a_dead_token(monkeypatch):
    """unauthorized_client is a client config error: the token is not locked and is retried once it is fixed."""
    from alice.connect import oauth, store
    from alice.connect.manifest import Auth

    store.save("spotify", access_token="a", refresh_token="r1", expires_at=1.0)
    calls = []

    class Resp:
        status_code = 400
        text = '{"error": "unauthorized_client"}'

    monkeypatch.setattr("httpx.post", lambda *a, **k: calls.append(1) or Resp())
    for _ in range(2):
        with pytest.raises(oauth.OAuthError, match="unauthorized_client"):
            oauth.refresh(Auth(kind="oauth2", token_url="https://t"), "spotify", "cid")
    assert len(calls) == 2 and not oauth.needs_sign_in("spotify")
    assert "Google" not in oauth.dead_message("spotify") and "Google" in oauth.dead_message("google_calendar")
    assert "--account ev" in oauth.dead_message("gmail:ev")


def test_archive_dedupe_only_matches_whole_trailing_lines():
    sid = sess.new_session_id()
    sess.archive(sid, [{"role": "user", "content": "xa"}])
    sess.archive(sid, [{"role": "user", "content": "a"}])  # the "xa" line ends in "a", but it is a different message
    assert [m["content"] for m in sess.load_archive(sid)] == ["xa", "a"]


def test_background_chat_runs_leftover_steer_message_before_closing(monkeypatch, workdir):
    """If the background chat's turn ends before it picks up a message typed in between, that message becomes a
    new turn in the same chat; the chat is not closed first, so the message is not added to a closed queue."""
    import threading

    from alice.app.server import AppState

    state = AppState(make_config({"brain": ["a:m"], "fast": ["a:m"]}), workdir, "auto",
                     provider_factory=lambda pc: None)
    chat = state.chat
    done = threading.Event()
    seen: list[str] = []

    def fake_turn(prompt, images=None):
        seen.append(prompt)
        if len(seen) == 1:
            chat.agent.busy = chat.agent._steering = True
            assert chat.agent.steer("bir de bunu yap")
            chat.agent.busy = chat.agent._steering = False  # the turn ended without picking up the message
        else:
            done.set()
        return "ok"

    monkeypatch.setattr(chat.agent, "run_turn", fake_turn)
    closed = []
    monkeypatch.setattr(state, "_finished", lambda c: closed.append(len(seen)))
    assert state.send("ilk iş")[0]
    assert done.wait(10)
    assert seen == ["ilk iş", "bir de bunu yap"]
    for _ in range(50):
        if closed:
            break
        time.sleep(0.05)
    assert closed == [2]  # the chat closed only after the second turn finished
    state.shutdown()
