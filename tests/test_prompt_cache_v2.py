"""Prompt cache v2: TTL latch, read-only/none markers, static/dynamic system split, cache-break detection and the
compaction that shares the main thread's cached prefix."""
from __future__ import annotations

from types import SimpleNamespace as NS

from alice.prompts import build_system_prompt
from alice.providers.anthropic_api import from_message, mark_cache
from alice.providers.prompt_cache import DYNAMIC_HEADING, CacheBreakDetector, split_system
from tests.test_agent import make_agent

EPH = {"type": "ephemeral"}
EPH1H = {"type": "ephemeral", "ttl": "1h"}


def _kw(system="kurallar"):
    return {"system": system, "messages": [
        {"role": "user", "content": "selam"},
        {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
        {"role": "user", "content": [{"type": "text", "text": "devam"}]}]}


# ------------------------------------------------------------------------------------------------ markers
def test_ttl_1h_on_every_marker():
    kw = _kw()
    mark_cache(kw, ttl="1h")
    assert kw["system"][0]["cache_control"] == EPH1H
    assert kw["messages"][-1]["content"][-1]["cache_control"] == EPH1H


def test_system_split_into_static_and_dynamic_blocks():
    kw = _kw(f"statik kurallar\n\n{DYNAMIC_HEADING}\n- Date: bugün")
    mark_cache(kw)
    assert [b["text"].split("\n")[0] for b in kw["system"]] == ["statik kurallar", DYNAMIC_HEADING]
    assert all(b["cache_control"] == EPH for b in kw["system"])
    markers = sum("cache_control" in b for m in kw["messages"] for b in (m["content"] if isinstance(m["content"], list) else []))
    assert markers == 1  # system 2 + messages 1 = 3, under the limit of 4


def test_read_only_marks_second_to_last_and_none_marks_nothing():
    kw = _kw()
    mark_cache(kw, mode="read_only")
    assert "cache_control" not in kw["messages"][-1]["content"][-1]
    assert kw["messages"][-2]["content"][-1]["cache_control"] == EPH
    kw = _kw()
    mark_cache(kw, mode="none")
    assert kw["system"] == "kurallar" and kw["messages"][-1]["content"][-1].get("cache_control") is None


def test_provider_reads_cache_hints_from_extra():
    from alice.config import ProviderConfig
    from alice.providers.anthropic_api import AnthropicProvider

    p = AnthropicProvider(ProviderConfig(name="claude", base_url="https://api.anthropic.com", kind="anthropic"))
    kw = p._params("claude-sonnet-5-5", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                   None, None, None, {"_cache_ttl": "1h", "_cache_mode": "write"}, stream=True)
    assert kw["system"][0]["cache_control"] == EPH1H
    assert not any(k.startswith("_") for k in kw)


def test_openai_body_drops_private_hints():
    from alice.providers.openai_compat import OpenAICompatProvider

    body = OpenAICompatProvider._body("m", [], None, None, None, None, {"_cache_ttl": "1h", "reasoning_effort": "low"})
    assert body.get("reasoning_effort") == "low" and "_cache_ttl" not in body


def test_usage_keeps_1h_writes_apart():
    u = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=50,
           cache_creation=NS(ephemeral_1h_input_tokens=50, ephemeral_5m_input_tokens=0))
    _, usage = from_message(NS(content=[], usage=u))
    assert usage["cache_creation_input_tokens"] == 50 and usage["cache_write_1h_tokens"] == 50
    assert usage["prompt_tokens"] == 160


# ------------------------------------------------------------------------------------------------ system prompt
def test_system_prompt_has_no_clock_and_dynamic_part_is_last(workdir):
    text = build_system_prompt(workdir)
    static, dynamic = split_system(text)
    assert dynamic.startswith(DYNAMIC_HEADING) and "Working directory:" in dynamic
    assert "Working directory:" not in static and "# Safety" in static
    import re
    assert not re.search(r"Date: .*\d{2}:\d{2}", text)  # the minute changed the prompt of every new chat


def test_clock_rides_on_user_message_not_on_tool_guess(workdir):
    agent, provs, _ = make_agent(["tamam"], workdir)
    agent.run_turn("selam")
    user = [m for m in agent.messages if m["role"] == "user"][-1]
    assert "[saat " in user["content"] and user["_display"] == "selam"


# ------------------------------------------------------------------------------------------------ TTL latch
def test_ttl_latched_for_the_session(workdir):
    agent, _, _ = make_agent(["a", "b"], workdir)
    assert agent._cache_ttl_choice() == "1h"
    agent.config.agent["cache_ttl"] = "5m"  # changing the setting mid-chat does not flip it
    assert agent._cache_ttl_choice() == "1h"
    child = agent.spawn_child() if hasattr(agent, "spawn_child") else None
    if child is not None:
        assert child._cache_ttl_choice() == "5m"


def test_main_call_sends_cache_hint(workdir):
    agent, provs, _ = make_agent(["tamam"], workdir)
    seen = {}
    orig = agent.router.chat

    def chat(role, messages, **kw):
        seen.setdefault("cache", kw.get("cache"))
        return orig(role, messages, **kw)

    agent.router.chat = chat
    agent.run_turn("selam")
    assert seen["cache"] == {"ttl": "1h", "mode": "write"}


# ------------------------------------------------------------------------------------------------ break detector
def _msgs(system="s", n=3):
    return [{"role": "system", "content": system}] + [{"role": "user", "content": f"m{i}"} for i in range(n)]


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "oku", "parameters": {}}}]


def test_detector_names_system_prompt_change(tmp_path):
    d = CacheBreakDetector(log_dir=tmp_path)
    d.before(_msgs("eski"), TOOLS, "a:m")
    assert d.after(50000, "a:m", 300) is None
    d.before(_msgs("yeni system"), TOOLS, "a:m")
    brk = d.after(1000, "a:m", 300)
    assert brk and not brk.expected and any("system prompt" in r for r in brk.reasons)
    assert brk.diff_path and (tmp_path / "cache_breaks.log").exists() and d.unexpected == 1


def test_detector_names_changed_tool_and_history(tmp_path):
    d = CacheBreakDetector(log_dir=tmp_path)
    d.before(_msgs(), TOOLS, "a:m")
    d.after(40000, "a:m", 300)
    tools2 = [{"type": "function", "function": {"name": "read_file", "description": "OKU!", "parameters": {}}}]
    msgs = _msgs()
    msgs[2]["content"] = "değişti"
    d.before(msgs, tools2, "a:m")
    brk = d.after(500, "a:m", 300)
    assert any("araç şeması değişti: read_file" in r for r in brk.reasons)
    assert any("2. mesajdan" in r for r in brk.reasons)


def test_detector_expected_and_ttl_and_small_drops(tmp_path):
    d = CacheBreakDetector(log_dir=tmp_path)
    d.before(_msgs(), TOOLS, "a:m")
    d.after(40000, "a:m", 300)
    d.expect("konuşma özetlendi")
    d.before(_msgs(n=1), TOOLS, "a:m")
    brk = d.after(100, "a:m", 300)
    assert brk.expected and "konuşma özetlendi" in brk.reasons and d.unexpected == 0
    # small drop: ignored
    d.before(_msgs(n=1), TOOLS, "a:m")
    d.after(5000, "a:m", 300)
    d.before(_msgs(n=1), TOOLS, "a:m")
    assert d.after(4000, "a:m", 300) is None
    # nothing changed, only time passed: TTL
    d.before(_msgs(n=1), TOOLS, "a:m")
    d.pending.at += 1000
    brk = d.after(0, "a:m", 300)
    assert brk and any("TTL" in r for r in brk.reasons)


def test_detector_model_switch_is_reported_but_expected(tmp_path):
    d = CacheBreakDetector(log_dir=tmp_path)
    d.before(_msgs(), TOOLS, "a:m")
    d.after(40000, "a:m", 300)
    d.before(_msgs(), TOOLS, "b:x")
    brk = d.after(0, "b:x", 300)
    assert brk.expected and brk.reasons == ["model değişti: a:m -> b:x"]


# ------------------------------------------------------------------------------------------------ compaction
def test_compaction_shares_the_cached_prefix(workdir):
    calls = []

    def script(model, messages, tools, **kw):
        calls.append({"messages": messages, "tools": tools, "extra": kw.get("extra")})
        if str(messages[-1].get("content") or "").startswith("Summarize"):
            return "ÖZET: kullanıcı dosyalarla uğraşıyor"
        return "cevap " + "x" * 400

    agent, _, ui = make_agent(script, workdir, compact_at_tokens=400)
    for i in range(6):
        agent.run_turn(f"mesaj {i} " + "y" * 300)
    summary_calls = [c for c in calls if str(c["messages"][-1].get("content") or "").startswith("Summarize")]
    assert summary_calls, "özet ana konuşmanın üstünden istenmeli"
    c = summary_calls[0]
    # same system prompt and tools as the main thread: the provider can read the whole history from the cache
    assert c["messages"][0]["role"] == "system" and c["messages"][0]["content"] == agent.messages[0]["content"]
    assert c["tools"]
    assert not any(str(m.get("content")).startswith("Summarize") for m in c["messages"][:1])
    assert any("ÖZET" in str(m.get("content")) for m in agent.messages)


def test_compaction_falls_back_to_transcript_when_model_calls_tools(workdir):
    from tests.conftest import tool_call

    def script(model, messages, tools, **kw):
        if str(messages[-1].get("content") or "").startswith("Summarize"):
            return tool_call("read_file", {"path": "x"})  # ignores the instruction
        if str(messages[0]["content"]).startswith("Summarize"):
            return "ÖZET: transcript yolu"
        return "cevap " + "x" * 400

    agent, _, _ = make_agent(script, workdir, compact_at_tokens=400)
    for i in range(6):
        agent.run_turn(f"mesaj {i} " + "y" * 300)
    assert any("ÖZET: transcript yolu" in str(m.get("content")) for m in agent.messages)
    assert not any(m.get("tool_calls") and m["tool_calls"][0]["function"]["name"] == "read_file"
                   for m in agent.messages)


def test_compaction_shared_cache_can_be_turned_off(workdir):
    seen = []

    def script(model, messages, tools, **kw):
        seen.append(str(messages[-1].get("content") or "")[:9])
        if str(messages[0]["content"]).startswith("Summarize"):
            return "ÖZET: eski yol"
        return "cevap " + "x" * 400

    agent, _, _ = make_agent(script, workdir, compact_at_tokens=400, compact_shared_cache=False)
    for i in range(6):
        agent.run_turn(f"mesaj {i} " + "y" * 300)
    assert "Summarize" not in seen and any("ÖZET: eski yol" in str(m.get("content")) for m in agent.messages)
