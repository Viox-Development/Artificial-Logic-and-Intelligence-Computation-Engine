"""Official Claude API: prompt cache markers (system + last block of the last message)."""

from alice.providers.anthropic_api import mark_cache


def test_marks_system_and_last_block():
    kw = {"system": "kurallar", "messages": [
        {"role": "user", "content": "selam"},
        {"role": "assistant", "content": [{"type": "text", "text": "x"}, {"type": "tool_use", "id": "t", "name": "a",
                                                                           "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "çıktı"}]}]}
    mark_cache(kw)
    assert kw["system"] == [{"type": "text", "text": "kurallar", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"][0]["content"] == "selam"  # only the last message is marked (limit: at most 4 markers)


def test_string_message_and_unmarkable_blocks():
    kw = {"messages": [{"role": "user", "content": "soru"}]}
    mark_cache(kw)
    assert kw["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"} and "system" not in kw
    kw = {"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]}]}
    mark_cache(kw)
    assert "cache_control" not in kw["messages"][0]["content"][0]  # a thinking block cannot be marked
    kw = {"messages": [{"role": "user", "content": "   "}]}
    mark_cache(kw)
    assert kw["messages"][0]["content"] == "   "
