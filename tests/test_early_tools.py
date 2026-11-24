"""Safe tool calls that finish while the reply streams start right away; results are ready when it ends."""

from __future__ import annotations

import json
import time

from alice.agent import Agent, NullUI
from alice.permissions import Permissions
from alice.providers.openai_compat import ChatResult, _announce_done, normalize_message
from alice.providers.router import Router
from alice.tools import ToolContext
from alice.tools.base import READ, WRITE, Tool, ToolResult, obj
from tests.conftest import make_config

STARTED: dict[str, float] = {}


class Look(Tool):
    name = "look"
    description = "salt okuma"
    parameters = obj({"x": {"type": "string", "description": "x"}}, ["x"])
    risk = READ
    parallel_safe = True

    def run(self, ctx, x: str) -> ToolResult:
        STARTED[x] = time.monotonic()
        time.sleep(0.3)
        return ToolResult(f"baktım {x}")


class Touch(Look):
    name = "touch"
    risk = WRITE
    parallel_safe = False


def call(cid, name, x):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps({"x": x})}}


class Streamer:
    """Announces the first two calls mid-stream, then 'writes' for 0.6 more seconds."""

    def __init__(self, calls, announce):
        self.calls, self.announce, self.n = calls, announce, 0
        self.ended = 0.0

    def chat(self, model, messages, **kw):
        return ChatResult(normalize_message({"content": "bitti"}), "a", model, {})

    def chat_stream(self, model, messages, on_delta, on_call=None, **kw):
        self.n += 1
        if self.n > 1:
            return ChatResult(normalize_message({"content": "bitti"}), "a", model, {})
        for c in self.announce:
            if on_call:
                on_call(c)
        time.sleep(0.6)
        self.ended = time.monotonic()
        return ChatResult(normalize_message({"content": "", "tool_calls": self.calls}), "a", model, {})


def run_agent(workdir, calls, announce):
    STARTED.clear()
    cfg = make_config({"brain": ["a:m"]})
    prov = Streamer(calls, announce)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    agent = Agent(cfg, router, [Look(), Touch()], ctx, Permissions("auto"), NullUI("y"), system_prompt="s")
    agent.run_turn("bak")
    return agent, prov


def test_safe_calls_start_while_reply_streams(workdir):
    calls = [call("c1", "look", "a"), call("c2", "look", "b"), call("c3", "look", "c")]
    agent, prov = run_agent(workdir, calls, calls[:2])
    assert STARTED["a"] < prov.ended and STARTED["b"] < prov.ended  # started before the reply ended
    assert STARTED["c"] >= prov.ended  # via the normal path, since it was not announced
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert [m["content"] for m in tools] == ["baktım a", "baktım b", "baktım c"]  # order and results correct, run once


def test_unsafe_or_changed_calls_are_not_reused(workdir):
    calls = [call("c1", "touch", "a"), call("c2", "look", "b2")]
    announce = [call("c1", "touch", "a"), call("c2", "look", "b")]  # c2's final form differs
    agent, prov = run_agent(workdir, calls, announce)
    assert STARTED["a"] >= prov.ended  # a writing tool does not start early
    tools = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert tools == ["baktım a", "baktım b2"]


def test_openai_stream_announces_finished_calls():
    got = []
    calls = {0: {"id": "x", "function": {"name": "look", "arguments": '{"x": "a"}'}},
             1: {"id": "y", "function": {"name": "look", "arguments": '{"x"'}}}
    seen: set[int] = set()
    _announce_done(calls, seen, got.append)
    _announce_done(calls, seen, got.append)  # the same call only once
    assert [c["id"] for c in got] == ["x"]
    calls[2] = {"id": "", "function": {"name": "look", "arguments": "{}"}}
    calls[1]["function"]["arguments"] = '{"x": "b"}'
    _announce_done(calls, seen, got.append)
    assert [c["id"] for c in got] == ["x", "y"]


def test_reset_discards_early_results(workdir):
    cfg = make_config({"brain": ["a:m"]})
    agent = Agent(cfg, None, [Look()], ToolContext(cwd=workdir, config=cfg), Permissions("auto"), NullUI("y"),
                  system_prompt="s")
    agent._early_start(call("c1", "look", "z"))
    assert agent._take_early(call("c1", "look", "z")) is not None
    agent._early_start(call("c2", "look", "z"))
    agent._early_clear()
    assert agent._take_early(call("c2", "look", "z")) is None


class Slow(Look):
    name = "slow"

    def run(self, ctx, x: str) -> ToolResult:
        time.sleep(5)
        return ToolResult(f"bitti {x}")


def test_stop_does_not_wait_for_parallel_tools(workdir):
    """Pressing Durdur (Stop) does not wait for parallel read-only tools to finish."""
    import threading

    cfg = make_config({"brain": ["a:m"]})
    agent = Agent(cfg, None, [Slow()], ToolContext(cwd=workdir, config=cfg), Permissions("auto"), NullUI("y"),
                  system_prompt="s")
    threading.Timer(0.3, agent.cancel.set).start()
    t = time.monotonic()
    res = agent._execute_calls([call("c1", "slow", "a"), call("c2", "slow", "b")])
    assert time.monotonic() - t < 3
    assert all(r.is_error and "stopped" in r.text for r in res)
