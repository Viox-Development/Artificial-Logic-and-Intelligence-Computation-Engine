"""Chats running at the same time use the screen/browser one after another (alice/tools/base.py: DesktopLock)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from alice.agent import Agent
from alice.permissions import Permissions
from alice.tools.base import COMPUTER, DesktopLock, Tool, ToolContext, ToolResult
from tests.conftest import make_config, tool_call
from tests.test_agent import make_agent


def ctx() -> ToolContext:
    return ToolContext(cwd=Path("."), config=None)  # type: ignore[arg-type]


class FakeBrowser:
    def __init__(self):
        self.aborted = ""

    def abort(self, reason):
        self.aborted = reason


def test_second_chat_waits_until_first_releases():
    lock, a, b = DesktopLock(), ctx(), ctx()
    assert lock.acquire(a, lambda: False) and lock.acquire(a, lambda: False)  # the same chat can take it again
    waited, got = [], []
    t = threading.Thread(target=lambda: got.append(lock.acquire(b, lambda: False, on_wait=lambda: waited.append(1))))
    t.start()
    time.sleep(0.3)
    assert got == [] and waited == [1]  # announced once, now waiting
    lock.release(b)  # a non-owner cannot release it
    assert lock.owner is a
    lock.release(a)
    t.join(2)
    assert got == [True] and lock.owner is b


def test_waiting_chat_can_be_stopped():
    lock, a, b = DesktopLock(), ctx(), ctx()
    lock.acquire(a, lambda: False)
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    assert lock.acquire(b, stop.is_set) is False and lock.owner is a


def test_handoff_drops_previous_browser_connection():
    # The browser bridge serves a single client: the previous chat's connection must not silently stay dead.
    lock, a, b = DesktopLock(), ctx(), ctx()
    a.state["browser"] = br = FakeBrowser()
    lock.acquire(a, lambda: False)
    lock.release(a)
    lock.acquire(a, lambda: False)  # same chat: leave it alone
    assert br.aborted == ""
    lock.release(a)
    lock.acquire(b, lambda: False)
    assert br.aborted == "handoff"


class Mouse(Tool):
    name = "fare"
    risk = COMPUTER

    def __init__(self):
        self.calls = 0

    def run(self, ctx, **args):
        self.calls += 1
        return ToolResult("tıklandı")


def test_agent_holds_desktop_for_the_turn_and_releases(workdir):
    agent, provs, ui = make_agent([tool_call("fare", {}), "bitti"], workdir)
    agent.tools["fare"] = mouse = Mouse()
    lock = DesktopLock()
    agent.ctx.state["desktop_lock"] = lock
    seen = []
    orig = mouse.run
    mouse.run = lambda ctx, **a: (seen.append(lock.owner is agent.ctx), orig(ctx, **a))[1]
    assert agent.run_turn("tıkla") == "bitti"
    assert seen == [True] and mouse.calls == 1 and lock.owner is None


def test_agent_stopped_while_waiting_for_desktop(workdir):
    agent, provs, ui = make_agent([tool_call("fare", {}), "bitti"], workdir)
    agent.tools["fare"] = mouse = Mouse()
    lock = DesktopLock()
    other = ctx()
    lock.acquire(other, lambda: False)  # another chat is on the screen
    agent.ctx.state["desktop_lock"] = lock
    threading.Timer(0.3, agent.stop).start()
    agent.run_turn("tıkla")
    assert mouse.calls == 0 and lock.owner is other


def test_no_lock_outside_app(workdir):
    # The CLI, voice and core agents have no lock: nothing waits.
    cfg = make_config({"brain": ["a:m"]})
    a = Agent(cfg, None, [Mouse()], ToolContext(cwd=workdir, config=cfg), Permissions("auto"), None,  # type: ignore[arg-type]
              system_prompt="sys")
    assert a._take_desktop(a.tools["fare"]) is True
