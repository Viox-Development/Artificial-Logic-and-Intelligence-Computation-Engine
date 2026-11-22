"""Prompt cache and summarization in long chats (22 Nov): the history sent at each step must stay a prefix of the
previous request, summarization must start in time, and provider error texts must not reach the model or memory."""

from __future__ import annotations

import json
import time

import pytest

from alice.agent import AUTO_COMPACT_MAX, SUMMARY_HEAD
from alice.providers.router import clean_messages
from alice.tools.base import ToolError
from tests.conftest import tool_call
from tests.test_agent import make_agent
from tests.test_context import _agent, _cfg


def _prefix_breaks(sent: list[list[dict]]) -> list[tuple[int, int]]:
    out = []
    for i in range(1, len(sent)):
        prev, cur = sent[i - 1], sent[i]
        k = next((j for j, (a, b) in enumerate(zip(prev, cur)) if a != b), min(len(prev), len(cur)))
        if k < len(prev):
            out.append((i, k))
    return out


# ------------------------------------------------------------------------------- 1. old-output trimming
def test_warm_cache_history_is_never_rewritten(workdir):
    (workdir / "big.txt").write_text("satır\n" * 3000, encoding="utf-8")
    sent: list[list[dict]] = []
    tools_sent: list[list[str]] = []

    def script(model, messages, tools, **kw):
        sent.append(json.loads(json.dumps(messages)))
        tools_sent.append([t["function"]["name"] for t in tools or []])
        n = len(sent)
        return tool_call("read_file", {"path": "big.txt"}, cid=f"c{n}") if n <= 25 else "bitti"

    agent, _, _ = make_agent(script, workdir, compact_at_tokens=10**7, max_steps=40, finish_nudges=0,
                             verify_claims=False)
    agent.run_turn("dosyayı defalarca oku")
    assert len(sent) == 26
    # Previously ~20 old outputs were trimmed at once in the middle of the turn (message 3 changed): every later
    # request rewrote the whole conversation into the cache.
    assert _prefix_breaks(sent) == []
    assert all(t == tools_sent[0] for t in tools_sent)


def test_old_outputs_are_trimmed_once_the_cache_is_cold(workdir):
    agent, _, _ = make_agent(["x"], workdir)
    for i in range(10):
        agent.messages.append({"role": "tool", "tool_call_id": str(i), "name": "read_file", "content": "y" * 5000})
    agent._last_call_at = time.monotonic()
    agent._shrink_old_outputs()
    assert all(len(m["content"]) == 5000 for m in agent.messages[1:])
    agent._last_call_at = time.monotonic() - 3600 * 2
    agent._shrink_old_outputs()
    assert sum("kısaltıldı" in m["content"] for m in agent.messages[1:]) == 4  # all but the last six, in one go


# ------------------------------------------------------------------------------- 1b. compact threshold
def test_hand_written_48000_is_respected():
    # It used to be ignored as the "old default": with cliproxy's 1M window the chat was not summarized until 800K.
    assert _agent(_cfg(compact_at_tokens=48000)).compact_at == 48000
    assert _agent(_cfg(compact_at_tokens="64000")).compact_at == 64000


def test_auto_compaction_has_a_fixed_ceiling():
    a = _agent(_cfg(compact_at_tokens="auto"))
    assert a.compact_at == AUTO_COMPACT_MAX <= 120_000  # the model window (1M) alone would give ~840K
    assert _agent(_cfg(compact_at_tokens="auto", compact_max_tokens=0)).compact_at == int(1_048_576 * 0.8)
    assert _agent(_cfg(compact_at_tokens="auto", compact_max_tokens=60000)).compact_at == 60000


# ------------------------------------------------------------------------------- 2. summary shape
def test_summary_after_a_long_tool_chain_keeps_the_running_request(workdir):
    (workdir / "big.txt").write_text("satır\n" * 2000, encoding="utf-8")
    calls = {"n": 0}

    def script(model, messages, tools, **kw):
        if messages[0]["content"].startswith("Summarize"):
            return "ÖZET: dosya okunuyor"
        calls["n"] += 1
        return tool_call("read_file", {"path": "big.txt"}, cid=f"c{calls['n']}") if calls["n"] <= 8 else "bitti"

    agent, _, _ = make_agent(script, workdir, compact_at_tokens=8000, max_steps=20, finish_nudges=0,
                             verify_claims=False)
    agent.run_turn("C:/proje/rapor.md dosyasını özetle ve README'ye ekle")
    summary = next(m["content"] for m in agent.messages if str(m.get("content")).startswith(SUMMARY_HEAD))
    assert "ÖZET" in summary and "C:/proje/rapor.md dosyasını özetle" in summary
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert tools and tools[-1]["tool_call_id"] == "c8"  # the call-result pairs survive the cut


def test_older_summary_is_not_cut_to_2500_chars_in_the_next_summary(workdir):
    seen = []

    def script(model, messages, tools, **kw):
        if messages[0]["content"].startswith("Summarize"):
            seen.append(messages[1]["content"])
            return "YENİ ÖZET"
        return "cevap " + "x" * 400

    # Transcript path (the cache-sharing summary sends the older summary verbatim anyway).
    agent, _, _ = make_agent(script, workdir, compact_at_tokens=400, compact_shared_cache=False)
    agent.messages.append({"role": "user", "content": f"{SUMMARY_HEAD}\n" + "eski bilgi " * 600 + "SON_SATIR"})
    agent.messages.append({"role": "assistant", "content": "tamam"})
    for i in range(3):
        agent.run_turn(f"mesaj {i} " + "y" * 300)
    assert seen and "SON_SATIR" in seen[0]


# ------------------------------------------------------------------------------- 3. load_tools all / tool list
def test_load_tools_all_is_refused(workdir):
    agent, _, _ = make_agent(["x"], workdir)
    agent._enable_groups_for("")
    with pytest.raises(ToolError) as e:
        agent.tools["load_tools"].run(agent.ctx, groups=["all"])
    assert "'all' kabul edilmiyor" in str(e.value) and "browser" in str(e.value)
    assert "browser" not in agent.ctx.state.get("tool_groups", set())


def test_new_tools_wait_while_the_cache_is_warm(workdir):
    agent, _, _ = make_agent(["x"], workdir)
    before = [s["function"]["name"] for s in agent.tool_specs()]
    agent.messages += [{"role": "user", "content": "u" * 80000}, {"role": "assistant", "content": "a"},
                       {"role": "user", "content": "devam"}]
    agent._last_call_at = time.monotonic()
    res = agent.tools["load_tools"].run(agent.ctx, groups=["browser"])
    assert [s["function"]["name"] for s in agent.tool_specs()] == before  # the cached prefix stays the same
    assert '"name": "browser"' in res.text  # the definition reaches the model at the end of the conversation
    agent._last_call_at = time.monotonic() - 3600 * 2  # cold: the waiting tools go in, appended at the end
    after = [s["function"]["name"] for s in agent.tool_specs()]
    assert after[:len(before)] == before and "browser" in after


def test_catalog_is_not_rewritten_while_the_cache_is_warm(workdir):
    agent, _, _ = make_agent(["x"], workdir)
    agent.sync_catalog()
    system = agent.messages[0]["content"]
    agent.messages += [{"role": "user", "content": "u" * 80000}, {"role": "assistant", "content": "a"},
                       {"role": "user", "content": "x"}]
    agent._last_call_at = time.monotonic()

    class Extra:
        name = "mcp__yeni__x"

    agent.tools["mcp__yeni__x"] = agent.tools["read_file"]  # e.g. an MCP server connected mid-chat
    agent.sync_catalog()
    assert agent.messages[0]["content"] == system


# ------------------------------------------------------------------------------- 4. provider error texts
def test_provider_error_is_shown_but_not_sent_or_remembered(workdir):
    from alice.providers.openai_compat import ProviderError

    seen: list[list[dict]] = []
    recorded: list[tuple[str, str]] = []
    state = {"fail": True}

    def script(model, messages, tools, **kw):
        if state["fail"]:
            raise ProviderError("network", "zaman aşımı", None)
        seen.append(messages)
        return "tamam"

    agent, _, _ = make_agent(script, workdir, finish_nudges=0)
    agent.router.max_wait = 0

    class Mem:
        def record(self, user, reply, channel="", sid=""):
            recorded.append((user, reply))

        def learn(self, *a, **k):
            pass

        def recall(self, *a, **k):
            return ""

    agent.memory = Mem()
    agent.refresh_memory = lambda force=False: None
    out = agent.run_turn("bir şey yap")
    assert "iş yarıda kaldı" in out  # the user sees it
    err = agent.messages[-1]
    assert err["role"] == "assistant" and err.get("_error") and err.get("_notice")
    assert recorded == [("bir şey yap", "")]  # memory: not saved as ALICE's answer
    state["fail"] = False
    agent.router.cooldown_until.clear()
    agent.router.strikes.clear()
    agent.run_turn("devam et")
    assert not any("iş yarıda kaldı" in str(m.get("content")) for m in seen[0])
    roles = [m["role"] for m in seen[0][1:]]
    assert roles == ["user", "user"]  # the two user messages, with no fake assistant turn in between


def test_clean_messages_drops_display_only():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "hata", "_error": True},
            {"role": "assistant", "content": "not", "_display_only": True}, {"role": "assistant", "content": "ok"}]
    assert [m["content"] for m in clean_messages(msgs)] == ["a", "ok"]


def test_session_roundtrip_keeps_the_error_flag(workdir, tmp_path):
    from alice import session as sess

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "x"},
            {"role": "assistant", "content": "Seçtiğin model cevap veremedi", "_error": True, "_notice": True}]
    sess.save("t-err", msgs, workdir)
    _, back = sess.load("t-err")
    assert back[-1].get("_error") and clean_messages(back) == [{"role": "user", "content": "x"}]


# ------------------------------------------------------------------------------- measurement
def test_successful_requests_are_logged_to_cache_log(workdir, tmp_path):
    agent, _, _ = make_agent(["tamam"], workdir)
    agent.router.log_path = tmp_path / "logs" / "providers.log"
    agent.run_turn("selam")
    line = (tmp_path / "logs" / "cache.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    assert " brain a:m prompt=10 cached=0 write=0 msgs=2 " in line and " th=" in line and " sh=" in line
