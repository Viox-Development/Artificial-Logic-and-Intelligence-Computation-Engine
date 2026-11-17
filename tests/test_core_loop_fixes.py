"""Tests for the bugs found in the core loop logic review."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from alice.agent import parse_args
from alice.council import Council
from alice.decider import Choice, Decider, Decision, Noul
from alice.memory import LongMemory
from alice.mentions import expand_mentions
from alice.multistep import parse_plan
from alice.permissions import Permissions
from alice.tools import ToolContext, all_tools
from tests.conftest import make_config
from tests.test_agent import make_agent


def assert_tool_pairs_valid(messages):
    """The rule of OpenAI-compatible APIs: every assistant message with tool_calls must be followed right away
    by the result of each call; a tool message with no owner is not allowed."""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "tool":
            raise AssertionError(f"sahipsiz tool mesajı: {i}")
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            j = i + 1
            got = []
            while j < len(messages) and messages[j]["role"] == "tool":
                got.append(messages[j]["tool_call_id"])
                j += 1
            assert sorted(got) == sorted(ids), (i, got, ids)
            i = j
            continue
        i += 1


# ------------------------------------------------------------------ agent
def test_repair_after_interrupt_keeps_results_next_to_their_calls(workdir):
    agent, _, _ = make_agent(["ok"], workdir)
    agent.messages += [
        {"role": "user", "content": "iki şey yap"},
        {"role": "assistant", "content": "bakıyorum", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "a"},
        # An interrupted, half-written text may have been added as an assistant message.
        {"role": "assistant", "content": "bakıyorum [durduruldu]"},
    ]
    agent._repair_after_interrupt()
    assert_tool_pairs_valid(agent.messages)
    assert agent.messages[-1]["content"] == "bakıyorum [durduruldu]"


def test_interrupt_while_describing_tool_image_leaves_valid_history(workdir, monkeypatch):
    from alice.tools.base import Tool, ToolResult

    class Shot(Tool):
        name = "shot"
        description = "fake screenshot"
        parallel_safe = False

        def run(self, ctx, **a):
            return ToolResult("ekran", images=["AAAA"])

    two = {"content": "bakıyorum", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}},
        {"id": "c2", "type": "function", "function": {"name": "shot", "arguments": "{}"}}]}
    agent, _, _ = make_agent([two, "ok"], workdir, keep_images=0)
    agent.tools["shot"] = Shot()
    monkeypatch.setattr(agent, "_brain_sees_images", lambda: False)

    def boom(images):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent, "_describe_images", boom)
    # Leave the text that arrived in the stream sitting in the sink (as in a real stream).
    orig = agent.router.chat

    def chat(role, messages, tools=None, stream=None, **kw):
        res = orig(role, messages, tools=tools, stream=None, **kw)
        if stream is not None and res.message.get("content"):
            stream.on_delta(res.message["content"])
        return res

    monkeypatch.setattr(agent.router, "chat", chat)
    agent.run_turn("bak")
    assert_tool_pairs_valid(agent.messages)
    # The same text must not be added a second time as "[durduruldu]".
    assert sum(1 for m in agent.messages if m["role"] == "assistant" and "bakıyorum" in (m.get("content") or "")) == 1


def test_malformed_tool_call_fields_are_fed_back_not_crash(workdir):
    weird = {"content": "", "tool_calls": [
        {"id": "n1", "type": "function", "function": {"name": None, "arguments": "{}"}},
        {"id": "n2", "type": "function", "function": {"name": "read_file", "arguments": [1, 2]}},
        {"id": "n3", "type": "function", "function": {"name": "read_file", "arguments": 7}}]}
    agent, _, _ = make_agent([weird, "ok"], workdir)
    assert agent.run_turn("dene") == "ok"
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["n1", "n2", "n3"]
    assert all(m["content"].startswith("HATA") for m in tools)
    calls = next(m for m in agent.messages if m.get("tool_calls"))["tool_calls"]
    # Calls sent back into history are in a form the API accepts: name and arguments are strings.
    assert all(isinstance(c["function"]["name"], str) and isinstance(c["function"]["arguments"], str) for c in calls)
    assert_tool_pairs_valid(agent.messages)


def test_parse_args_rejects_non_object_types_cleanly():
    import json

    for raw in ([1, 2], 7, 3.5, True):
        with pytest.raises(json.JSONDecodeError):
            parse_args(raw)


# ------------------------------------------------------------ permissions
def test_auto_mode_asks_for_patch_into_system_dir(workdir):
    tools = {t.name: t for t in all_tools()}
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), router=None)
    target = r"C:\Windows\system32\evil.txt" if sys.platform == "win32" else "/etc/cron.d/evil"
    patch = f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch"
    perms = Permissions("auto")
    assert perms.decide(tools["apply_patch"], {"patch": patch}, ctx) == "ask"
    ok = "*** Begin Patch\n*** Add File: a.txt\n+x\n*** End Patch"
    assert perms.decide(tools["apply_patch"], {"patch": ok}, ctx) == "allow"
    moved = f"*** Begin Patch\n*** Update File: a.txt\n*** Move to: {target}\n*** End Patch"
    assert perms.decide(tools["apply_patch"], {"patch": moved}, ctx) == "ask"


# ---------------------------------------------------------------- council
def _council(workdir, texts):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), router=None)
    c = Council(ctx, "work", "iş", "a\n" * 100, depth=1)
    c._parallel = lambda roles, make_prompt: dict(texts)
    return c


def test_council_conditions_given_as_string_are_not_split_into_letters(workdir):
    import json

    text = "```json\n" + json.dumps({"vote": "conditional", "reason": "r", "conditions": "testler eklenirse olur",
                                     "findings": {"severity": "major", "title": "test yok", "evidence": "a.py:1"}}) \
        + "\n```"
    c = _council(workdir, {"critic": text})
    c._round1()
    assert c.votes["critic"].conditions == ["testler eklenirse olur"]
    assert [f.title for f in c.findings] == ["test yok"]


def test_council_debate_with_string_conditions(workdir):
    import json

    c = _council(workdir, {"critic": "```json\n" + json.dumps({"vote": "reject", "reason": "r"}) + "\n```"})
    c._round1()
    c._parallel = lambda roles, make_prompt: {"critic": "```json\n" + json.dumps(
        {"vote": "conditional", "conditions": "şunu düzelt", "stances": {"id": "F1"}}) + "\n```"}
    c._debate()
    assert c.votes["critic"].conditions == ["şunu düzelt"]


# ---------------------------------------------------------------- decider
class _Engine:
    name = "e"

    def decide(self, state, questions):
        from alice.decider import Answer, _validate

        _validate(questions)
        return Decision({k: Answer("noul", 0.9, 0.9) for k in questions}, self.name, 0.0)


def test_invalid_question_does_not_mark_engines_broken():
    d = Decider([_Engine()])
    with pytest.raises(ValueError):
        d.decide("s", {"q": Choice("tek seçenek", {"a": "x"})})
    assert d.decide("s", {"q": Noul("evet mi")})["q"].value == 0.9


# ----------------------------------------------------------------- memory
def test_recall_and_profile_are_bounded_with_huge_fact():
    m = LongMemory(":memory:", {})
    m.remember("Kullanıcının kahve notları: " + "kahve " * 5000)
    m.remember("Kullanıcı kahveyi şekersiz içer.", kind="preference")
    out = m.recall("kahve")
    assert out and len(out) < 3000
    prof = m.profile(chars=500)
    assert "şekersiz" in prof  # a single huge fact must not push the rest out of the profile


# --------------------------------------------------------------- mentions
def test_mention_of_unreadable_folder_does_not_crash(workdir, monkeypatch):
    (workdir / "kilitli").mkdir()
    real = Path.iterdir

    def iterdir(self):
        if self.name == "kilitli":
            raise PermissionError("erişim yok")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    out = expand_mentions("bak @kilitli", workdir)
    assert out.startswith("bak @kilitli")


def test_mention_of_big_file_reads_only_the_limit(workdir):
    from alice import mentions

    (workdir / "big.txt").write_bytes(b"a" * (mentions.MAX_FILE + 50))
    out = expand_mentions("@big.txt", workdir)
    assert "(50 byte kesildi)" in out


def test_mention_with_nul_character_does_not_crash(workdir):
    (workdir / "a.txt").write_text("içerik", encoding="utf-8")
    out = expand_mentions("bak @ka\x00put ve @a.txt", workdir)
    assert out.startswith("bak @ka\x00put") and "içerik" in out


# -------------------------------------------------------------- multistep
def test_parse_plan_tolerates_odd_step_shapes():
    p = parse_plan("g", '{"steps": "Chrome\'u aç"}')
    assert [s.goal for s in p.steps] == ["Chrome'u aç"]
    p = parse_plan("g", '{"steps": [3, null, ["x"], {"goal": "dosyayı oku"}]}')
    assert [s.goal for s in p.steps] == ["dosyayı oku"]
