"""A running command goes to the background (Stop, a message, the user, the timeout) instead of being killed."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from alice.tools import shell
from alice.tools.base import ToolContext
from tests.conftest import make_config, tool_call
from tests.test_agent import make_agent
from tests.test_app import app  # noqa: F401 - fixture

LOOP = ("import time\nprint('basladi', flush=True)\nfor i in range({n}):\n    time.sleep(0.1)\n"
        "print('bitti', flush=True)")
CALL = "& " if sys.platform == "win32" else ""


@pytest.fixture
def chat(workdir):
    got: list[str] = []
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["b:v"]}))
    ctx.state["deliver"] = got.append
    yield ctx, got
    shell.cleanup(ctx)


def until(fn, secs=20.0):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def running(ctx):
    return any(j.log.exists() and j.log.stat().st_size for j in list(ctx.state.get("fg_jobs", {}).values()))


def start(ctx, n=20):
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(res=shell.RunCommand().run(ctx, python=LOOP.format(n=n))))
    t.start()
    assert until(lambda: running(ctx))
    return t, box


def test_user_moves_a_command_and_the_chat_hears_when_it_ends(chat):
    ctx, got = chat
    t, box = start(ctx, n=30)
    view = shell.procs_view(ctx)[0]
    assert view["state"] == "foreground" and view["movable"]
    pid = shell.background_foreground(ctx, "user")[0]
    t.join(5)
    res = box["res"]
    assert not t.is_alive() and not res.is_error and "moved to the background" in res.text and "basladi" in res.text
    assert ctx.state["bg_procs"][pid].popen.poll() is None and not ctx.state["fg_jobs"]
    assert shell.pending(ctx) and shell.procs_view(ctx)[0]["state"] == "running"
    assert until(lambda: got)
    assert f"pid={pid} finished: exit code 0" in got[0] and "bitti" in got[0] and len(got) == 1
    assert not shell.pending(ctx)


def test_timeout_moves_instead_of_killing(chat):
    ctx, got = chat
    res = shell.RunCommand().run(ctx, python=LOOP.format(n=25), timeout=1)
    assert "did not finish within the timeout" in res.text and not res.is_error
    assert until(lambda: got) and "bitti" in got[0]


def test_message_gives_a_short_command_time_to_finish(chat):
    ctx, got = chat
    t, box = start(ctx, n=8)
    shell.background_foreground(ctx, "message", grace=8.0)
    t.join(12)
    assert "exit code 0" in box["res"].text and "bitti" in box["res"].text and not got
    assert not ctx.state.get("bg_procs")


def test_end_the_model_already_saw_is_not_announced(chat):
    ctx, got = chat
    t, _ = start(ctx, n=15)
    pid = shell.background_foreground(ctx, "user")[0]
    t.join(5)
    assert "finished (code 0)" in shell.ProcessWait().run(ctx, pid, timeout=20).text
    t, _ = start(ctx, n=300)
    pid2 = shell.background_foreground(ctx, "user")[0]
    t.join(5)
    shell.ProcessStop().run(ctx, pid2)
    time.sleep(1.8)
    assert got == []


def test_background_true_announces_its_end(chat):
    ctx, got = chat
    res = shell.RunCommand().run(
        ctx, command=f'{CALL}"{sys.executable}" -c "import time; time.sleep(4); print(42)"', background=True)
    assert "state: running" in res.text
    assert until(lambda: got) and "42" in got[0]


@pytest.mark.parametrize("case", ["no chat", "subagent", "turned off"])
def test_killed_where_the_chat_cannot_follow(chat, case):
    ctx, _ = chat
    if case == "no chat":
        ctx.state.pop("deliver")
    elif case == "subagent":
        ctx.state["agent"] = type("A", (), {"is_child": True, "cancel": threading.Event()})()
    else:
        ctx.config.data["agent"]["background_commands"] = False
    assert shell.background_foreground(ctx, "user") == []
    res = shell.RunCommand().run(ctx, python=LOOP.format(n=100), timeout=1)
    assert res.is_error and "TIMEOUT" in res.text and not ctx.state.get("bg_procs")


def test_closing_the_chat_leaves_nothing_behind(chat):
    ctx, got = chat
    t, _ = start(ctx, n=300)
    pid = shell.background_foreground(ctx, "user")[0]
    t.join(5)
    pop = ctx.state["bg_procs"][pid].popen
    t2, _ = start(ctx, n=300)
    shell.cleanup(ctx)
    t2.join(10)
    assert pop.poll() is not None and not t2.is_alive()
    time.sleep(1.3)
    assert got == []


def test_stop_keeps_the_command_and_tells_the_model_later(workdir):
    agent, _, _ = make_agent([tool_call("run_command", {"python": LOOP.format(n=300)}), "tamam"], workdir, mode="yolo")
    got: list[str] = []
    agent.ctx.state.update(deliver=got.append, agent=agent)
    t = threading.Thread(target=agent.run_turn, args=("calistir",))
    t.start()
    try:
        assert until(lambda: running(agent.ctx))
        agent.stop()
        t.join(10)
        assert not t.is_alive()
        assert until(lambda: agent.ctx.state.get("bg_procs"))
        proc = next(iter(agent.ctx.state["bg_procs"].values()))
        assert proc.popen.poll() is None and proc.reason == "stop" and not proc.notify
        agent.run_turn("ne oldu")
        msg = next(m for m in reversed(agent.messages) if m["role"] == "user")
        assert f"pid={proc.pid}" in msg["content"] and msg["_display"] == "ne oldu"
        assert "bg_notes" not in agent.ctx.state and got == []
    finally:
        shell.cleanup(agent.ctx)


def test_message_reaches_the_model_while_the_command_runs(workdir):
    agent, _, _ = make_agent([tool_call("run_command", {"python": LOOP.format(n=300)}), "gordum"], workdir, mode="yolo")
    agent.ctx.state.update(deliver=lambda text: None, agent=agent)
    t = threading.Thread(target=agent.run_turn, args=("calistir",))
    t.start()
    try:
        assert until(lambda: running(agent.ctx))
        assert agent.steer("bir de suna bak")
        t.join(12)
        assert not t.is_alive()
        texts = [str(m.get("content")) for m in agent.messages]
        assert any("the user wrote a message while it was running" in x for x in texts)
        assert any("bir de suna bak" in x for x in texts)
        assert next(iter(agent.ctx.state["bg_procs"].values())).popen.poll() is None
    finally:
        shell.cleanup(agent.ctx)


def test_status_bar_api(app):  # noqa: F811
    state, srv, client, _ = app
    assert client.get("/api/procs").json() == {"procs": []}
    assert client.post("/api/procs/background", json={}).json() == {"ok": True, "moved": []}
    t, box = start(state.chat.ctx, n=300)
    row = client.get("/api/procs").json()["procs"][0]
    assert row["state"] == "foreground" and row["movable"]
    assert client.post("/api/procs/background", json={}).json()["moved"] == [row["pid"]]
    t.join(5)
    assert client.get("/api/procs").json()["procs"][0]["state"] == "running"
    assert "basladi" in client.post("/api/procs/output", json={"pid": row["pid"]}).json()["text"]
    assert not client.post("/api/procs/kill", json={"pid": 4}).json()["ok"]  # only what ALICE started
    assert client.post("/api/procs/kill", json={"pid": row["pid"]}).json()["ok"]
    assert until(lambda: client.get("/api/procs").json()["procs"][0]["state"] == "done")
    page = client.get(srv.url).text
    assert "/static/procs.js" in page and client.get("/static/procs.js").status_code == 200


def test_chat_with_a_running_command_is_not_closed(app):  # noqa: F811
    state, _, _, _ = app
    chat = state.chat
    t, _ = start(chat.ctx, n=300)
    pid = shell.background_foreground(chat.ctx, "user")[0]
    t.join(5)
    closed = []
    chat.close = lambda wait=None: closed.append(1)
    state.background["x"] = chat
    front, state.chat = state.chat, object()
    try:
        chat.sid, sid = "x", chat.sid
        state._drop_if_idle(chat)
        assert not closed
        shell.ProcessStop().run(chat.ctx, pid)
        assert until(lambda: closed)  # its last command ended: nothing left to wait for
    finally:
        state.chat, chat.sid = front, sid
        state.background.pop("x", None)
