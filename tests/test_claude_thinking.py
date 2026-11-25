"""Claude's thinking blocks are kept between the steps of a tool loop (they used to be dropped: the model worked its plan
out again at every step and ran out of time before editing anything)."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace as NS

import pytest

from alice.config import ProviderConfig
from alice.providers import anthropic_api as api
from alice.providers.openai_compat import ProviderError
from alice.providers.router import clean_messages

TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {}}}}]
BOUND = ("messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different conversation. "
         "Remove the block, or set `thinking.block_binding.prefix_mismatch_behavior` to \"drop_block\".")


def _answer(n: int, extra=()):
    return NS(content=[NS(type="thinking", thinking=f"plan {n}", signature=f"sig{n}"), NS(type="text", text="Bakıyorum. "),
                       NS(type="thinking", thinking="", signature=f"mid{n}"),
                       NS(type="tool_use", id=f"t{n}", name="read", input={"n": n}), *extra],
              stop_reason="tool_use", stop_details=None, model="claude-opus-5-5",
              usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class SdkError(Exception):
    """An error as the SDK raises it; the provider turns it into a ProviderError."""


class Stream(list):
    def __init__(self, final):
        super().__init__()
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.final


def _provider(sent: list, replies: list):
    def step(kw):
        sent.append(copy.deepcopy(kw))
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    fake = NS(beta=NS(messages=NS(create=lambda **kw: step(kw), stream=lambda **kw: Stream(step(kw)))))
    p = api.AnthropicProvider(ProviderConfig(name="c", base_url="https://api.anthropic.com", api_key_env="X",
                                             kind="anthropic"), client=fake)
    p._error = lambda e: ProviderError("bad_request", str(e), 400)
    return p


def _loop(steps: int = 2):
    """A conversation of `steps` tool calls, plus the provider that made it."""
    sent: list = []
    p = _provider(sent, [_answer(n) for n in range(1, steps + 1)])
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "düzelt"}]
    for n in range(1, steps + 1):
        res = p.chat_stream("claude-opus-5-5", clean_messages(msgs, claude=True), lambda s: None, tools=TOOLS)
        msgs += [res.message, {"role": "tool", "tool_call_id": f"t{n}", "content": "x" * 50}]
    return p, msgs, sent


def _thinking(kw):
    return [b.get("signature") for m in kw["messages"] if m["role"] == "assistant" for b in m["content"]
            if b["type"] == "thinking"]


def _params(p, msgs, tools=TOOLS):
    return p._params("claude-opus-5-5", clean_messages(msgs, claude=True), tools, None, None, None, True)


def test_thinking_goes_back_unchanged_and_in_order():
    p, msgs, sent = _loop()
    assert _thinking(sent[0]) == [] and "block_binding" not in sent[0]["thinking"]
    assert _thinking(sent[1]) == ["sig1", "mid1"]
    kw = _params(p, msgs)
    assert kw["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "plan 1", "signature": "sig1"}, {"type": "text", "text": "Bakıyorum. "},
        {"type": "thinking", "thinking": "", "signature": "mid1"},
        {"type": "tool_use", "id": "t1", "name": "read", "input": {"n": 1}}]
    assert _thinking(kw) == ["sig1", "mid1", "sig2", "mid2"]
    assert kw["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"} and api.BINDING_BETA in kw["betas"]


def test_thinking_survives_a_saved_session():
    p, msgs, _ = _loop()
    assert _thinking(_params(p, json.loads(json.dumps(msgs, ensure_ascii=False)))) == ["sig1", "mid1", "sig2", "mid2"]


def test_other_providers_never_see_it():
    _, msgs, _ = _loop()
    assert api.THINKING_KEY in msgs[2]
    assert all(api.THINKING_KEY not in m for m in clean_messages(msgs))


@pytest.mark.parametrize("edit", ["system", "user", "tools"])
def test_blocks_from_before_an_edit_are_left_out(edit):
    p, msgs, _ = _loop()
    tools = TOOLS
    if edit == "system":
        msgs[0] = {"role": "system", "content": "S2"}
    elif edit == "user":
        msgs[1] = {"role": "user", "content": "başka"}
    else:
        tools = [*TOOLS, {"type": "function", "function": {"name": "yeni", "parameters": {}}}]
    kw = _params(p, msgs, tools)
    assert _thinking(kw) == [] and [b["type"] for b in kw["messages"][1]["content"]] == ["text", "tool_use"]


def test_shortened_output_drops_only_what_the_edit_invalidates_and_new_blocks_count_again():
    p, msgs, _ = _loop(3)
    msgs[3] = {**msgs[3], "content": "kısaltıldı"}  # output of step 1: steps 2 and 3 were made after seeing the original
    assert _thinking(_params(p, msgs)) == []
    p.client.beta.messages.stream = lambda **kw: Stream(_answer(4))
    res = p.chat_stream("claude-opus-5-5", clean_messages(msgs, claude=True), lambda s: None, tools=TOOLS)
    msgs += [res.message, {"role": "tool", "tool_call_id": "t4", "content": "x"}]
    assert _thinking(_params(p, msgs)) == ["sig4", "mid4"]


def test_changed_message_is_sent_without_its_thinking():
    p, msgs, _ = _loop(1)
    msgs[2] = {**msgs[2], "content": "Bakıyorum. [durduruldu]"}
    assert _thinking(_params(p, msgs)) == []


def test_answer_with_an_unknown_block_keeps_no_thinking():
    assert api.thinking_layout(_answer(1, [NS(type="fallback")]).content) == []
    assert api.thinking_layout([NS(type="text", text="a")]) == []


@pytest.mark.parametrize("stream", [True, False])
def test_rejected_blocks_are_dropped_and_the_request_is_sent_again(stream):
    p, msgs, _ = _loop()
    sent: list = []
    q = _provider(sent, [SdkError(BOUND), _answer(9)])
    cm = clean_messages(msgs, claude=True)
    res = (q.chat_stream("claude-opus-5-5", cm, lambda s: None, tools=TOOLS) if stream
           else q.chat("claude-opus-5-5", cm, tools=TOOLS))
    assert [len(_thinking(k)) for k in sent] == [4, 0] and res.message[api.THINKING_KEY]
    assert q._binding and _thinking(_params(q, msgs)) == []  # not sent again later either


def test_endpoint_without_block_binding_still_gets_the_thinking():
    p, msgs, _ = _loop()
    sent: list = []
    q = _provider(sent, [SdkError("thinking.adaptive.block_binding: Extra inputs are not permitted"), _answer(9)])
    q.chat("claude-opus-5-5", clean_messages(msgs, claude=True), tools=TOOLS)
    assert "block_binding" not in sent[1]["thinking"] and api.BINDING_BETA not in sent[1].get("betas", [])
    assert len(_thinking(sent[1])) == 4


def test_unrelated_errors_are_not_retried():
    sent: list = []
    q = _provider(sent, [SdkError("max_tokens too large")])
    with pytest.raises(ProviderError):
        q.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], tools=TOOLS)
    assert len(sent) == 1
