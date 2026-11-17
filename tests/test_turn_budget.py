"""Per-turn token budget (like task_budget in the Agent SDK): at the limit the turn stops and is reported unfinished."""

from __future__ import annotations

from tests.conftest import tool_call
from tests.test_agent import make_agent


def _script(n: int) -> list:
    return [tool_call("list_dir", {"path": "."}, cid=f"c{i}") for i in range(n)] + ["bitti"]


def test_turn_halts_when_budget_spent(workdir):
    # FakeProvider spends 10 input + 5 output = 15 tokens per call: the 20-token budget runs out at the second step.
    agent, provs, _ = make_agent(_script(5), workdir, turn_token_budget=20)
    out = agent.run_turn("klasöre bak")
    assert "token bütçesini" in out and "devam et" in out
    assert sum(1 for m in agent.messages if m["role"] == "tool") == 2
    assert agent.messages[-1].get("_notice")  # also in the chat record, so the next turn knows the work is unfinished


def test_no_budget_by_default_and_counter_resets_each_turn(workdir):
    agent, provs, _ = make_agent(_script(3) + _script(3), workdir)
    assert agent.run_turn("bak") == "bitti"
    agent.config.agent["turn_token_budget"] = 100  # per turn: the previous turn's spending is not counted
    assert agent.run_turn("yine bak") == "bitti"


def test_cached_input_does_not_count(workdir):
    agent, provs, _ = make_agent(_script(1), workdir, turn_token_budget=1000)
    from alice.providers.openai_compat import ChatResult

    agent._turn_tokens = 0
    res = ChatResult({"role": "assistant", "content": "x"}, "a", "m",
                     {"prompt_tokens": 50_000, "completion_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 49_900}})
    agent._count_call(res, 0.0, 0.0)
    assert agent._turn_tokens == 200  # 100 uncached input + 100 output
