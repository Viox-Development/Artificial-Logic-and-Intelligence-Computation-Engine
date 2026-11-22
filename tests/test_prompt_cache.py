"""Prompt cache: usage is measured correctly, and the tool list and system prompt do not change needlessly mid-chat."""
from __future__ import annotations

import json

import httpx

from alice.config import ProviderConfig
from alice.memory import LongMemory
from alice.providers.openai_compat import OpenAICompatProvider, merge_usage
from alice.providers.router import UsageTracker, cache_write_tokens, cached_tokens
from tests.conftest import tool_call
from tests.test_agent import make_agent


def sse(*chunks) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def provider(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider(ProviderConfig("x", "http://x"),
                                client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_stream_asks_for_usage_and_merges_partial_chunks():
    seen = []
    body = sse({"choices": [{"delta": {"content": "a"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 0}},
               {"choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 7,
                          "prompt_tokens_details": {"cached_tokens": 90, "cache_write_tokens": 10}}},
               {"choices": [], "usage": {"completion_tokens": 0}})

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=body)

    res = provider(handler).chat_stream("m", [{"role": "user", "content": "x"}], lambda t: None)
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert res.usage["completion_tokens"] == 7 and cached_tokens(res.usage) == 90  # last empty chunk didn't erase it
    assert cache_write_tokens(res.usage) == 10


def test_stream_options_rejected_once_then_dropped():
    seen = []

    def handler(req):
        b = json.loads(req.content)
        seen.append("stream_options" in b)
        if "stream_options" in b:
            return httpx.Response(400, text='{"error": "Unrecognized request argument: stream_options"}')
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}}]}))

    p = provider(handler)
    assert p.chat_stream("m", [], lambda t: None).text == "ok"
    assert p.chat_stream("m", [], lambda t: None).text == "ok"
    assert seen == [True, False, False]


def test_merge_usage_keeps_nested_fields():
    a = {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3}}
    assert merge_usage(a, {"prompt_tokens_details": {"cached_tokens": 0, "x": 1}}) == \
        {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3, "x": 1}}


def test_usage_tracker_records_cache(tmp_path):
    t = UsageTracker(tmp_path / "u.json")
    t.record("a:m", {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 8,
                     "cache_creation_input_tokens": 2}, ok=True)
    row = t.today()["a:m"]
    assert (row["prompt_tokens"], row["completion_tokens"], row["cached_tokens"], row["cache_write_tokens"]) == \
        (10, 2, 8, 2)


def _long_chat(agent, n=6):
    for i in range(n):
        agent.messages.append({"role": "user", "content": f"soru {i} " + "uzun bağlam " * 400})
        agent.messages.append({"role": "assistant", "content": "cevap " * 50})


def test_warm_chat_hints_groups_instead_of_changing_tools(workdir):
    seen = []

    def script(model, messages, tools, **kw):
        seen.append([t["function"]["name"] for t in tools or []])
        return "tamam"

    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("merhaba")
    _long_chat(agent)
    agent.run_turn("blender'da bir ev yap")
    assert seen[-1] == seen[0]  # tool list unchanged (cache not broken)
    last_user = [m for m in agent.messages if m["role"] == "user"][-1]
    assert "load_tools" in last_user["content"] and "blender" in last_user["content"]
    assert last_user["_display"] == "blender'da bir ev yap"  # the note is not shown on screen


def test_first_turn_or_setting_off_loads_groups_directly(workdir):
    seen = []

    def script(model, messages, tools, **kw):
        seen.append({t["function"]["name"] for t in tools or []})
        return "tamam"

    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("blender'da bir ev yap")  # first request of the chat: no cache yet, so groups load directly
    assert "blender" in seen[-1]
    agent2, _, _ = make_agent(script, workdir, cache_stable_tools=False)  # setting off: old behavior
    _long_chat(agent2)
    agent2.run_turn("blender'da bir ev yap")
    assert "blender" in seen[-1]


def test_model_still_loads_hinted_group_when_needed(workdir):
    seen = []
    steps = iter([tool_call("load_tools", {"groups": ["blender"]}, cid="l1"), "tamam"])

    def script(model, messages, tools, **kw):
        seen.append({t["function"]["name"] for t in tools or []})
        return next(steps)

    agent, _, _ = make_agent(script, workdir)
    _long_chat(agent)
    agent.run_turn("blender'da bir ev yap")
    assert "blender" not in seen[0] and "blender" in seen[1]


def test_system_prompt_stable_mid_chat_until_user_edits(workdir):
    agent, _, _ = make_agent(["bir", "iki", "üç", "dört"], workdir)
    agent.memory = LongMemory(":memory:", {})
    agent.memory.save_note("eski", "Kullanıcı Unciv oynar", "user", "Kullanıcı Unciv oynar", source="kullanıcı")
    agent.run_turn("selam")
    system1 = agent.messages[0]["content"]
    agent.memory.save_note("oto", "Kullanıcı çay sever", "user", "Kullanıcı çay sever", source="otomatik")
    agent.run_turn("nasılsın")
    assert agent.messages[0]["content"] == system1  # automatic extraction did not break the chat's cache

    # Deleting a note that is in the prompt (not just undoing the automatic one) must reach the open chat.
    agent.memory.delete_note("eski")
    agent.run_turn("peki")
    assert agent.messages[0]["content"] != system1  # when the user deletes it, an open chat updates too
    agent.reset()
    agent.refresh_memory()  # new chat: refreshes again when notes change
    fresh = agent.messages[0]["content"]
    agent.memory.save_note("oto2", "Kullanıcı kahve sever", "user", "Kullanıcı kahve sever", source="otomatik")
    agent.refresh_memory()
    assert agent.messages[0]["content"] != fresh


def test_cache_ttl_follows_provider_and_tracks_written_tokens(workdir):
    import time

    agent, _, _ = make_agent(["tamam"], workdir)
    assert agent._cache_ttl("cliproxy:claude-opus-5-5") > 3000 and agent._cache_ttl("gemini:flash") == 300
    agent.router.last_ref[agent.role] = "cliproxy:claude-opus-5-5"
    agent._last_call_at = time.monotonic() - 600  # 10 min: the 5-min cache went cold, cliproxy's is still warm
    assert not agent._cache_cold()
    agent.router.last_ref[agent.role] = "gemini:flash"
    assert agent._cache_cold()
    agent.run_turn("selam")
    assert "written" in agent.stats_view()
