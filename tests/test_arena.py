"""Intelligence score from the real benchmark (LMArena): name matching, scoring, download, table merge."""

from __future__ import annotations

import time

import httpx
import pytest

from alice.providers import arena, ranking


def row(name, rating, cat="overall"):
    return {"model_name": name, "rating": rating, "category": cat, "leaderboard_publish_date": "2026-10-22"}


ROWS = [row("Gemini 4 Argon (High)", 1526), row("Gemini 3.8 Flash (High)", 1498), row("Gemini 3.6 Flash (High)", 1480),
        row("Gemini 3 Flash", 1466), row("Gemini 3 Flash (Minimal)", 1442), row("GLM-5.3 (Max)", 1474),
        row("GLM-4.7-Flash", 1351), row("NVIDIA-Nemotron-3-Super-120B-A12B", 1378), row("Mistral Large 4", 1432),
        row("Mistral Large 3", 1428), row("Mistral Medium 3.1 (2508)", 1424), row("Command A (03-2025)", 1331),
        row("Gemini 3.5 Flash-Lite", 1435), row("Qwen3.8-27B", 1440), row("Kimi K3 (Max)", 1475),
        row("Gemini 4 Argon (High)", 1530, "coding"), row("GLM-5.3 (Max)", 1500, "coding"),
        row("Gemini 3.8 Flash (High)", 1480, "coding"), row("Some Model", 1400, "japanese")]


def data():
    return {"fetched_ts": time.time(), "scores": arena.build(ROWS)}


@pytest.mark.parametrize("ref,key", [
    ("gemini:gemini-3.8-flash", "gemini-3.8-flash"), ("nvidia:z-ai/glm-5.3", "glm-5.3"),
    ("cloudflare:@cf/zai-org/glm-4.7-flash", "glm-4.7-flash"),
    ("openrouter:nvidia/nemotron-3-super-120b-a12b:free", "nemotron-3-super-120b-a12b"),
    ("NVIDIA-Nemotron-3-Super-120B-A12B", "nemotron-3-super-120b-a12b"), ("Gemini 3.8 Flash (High)", "gemini-3.8-flash"),
    ("groq:qwen/qwen3.8-27b", "qwen3.8-27b"), ("openrouter:google/gemma-4-31b-it:free", "gemma-4-31b"),
    ("ollama:qwen3:8b", "qwen3:8b"), ("cohere:command-a-03-2025", "command-a"),
])
def test_norm(ref, key):
    assert arena.norm(ref) == key


def test_build_scores_relative_to_leader_and_weights_categories():
    s = arena.build(ROWS)
    assert s["gemini-4-argon"]["q"] == 100
    assert s["gemini-3-flash"]["name"] == "Gemini 3 Flash"  # the variant without parentheses (not Minimal)
    # GLM-5.3: usually 52 Elo behind (74), 30 in coding (85): the weighted average is between the two (2 Elo = 1 point)
    assert 74 < s["glm-5.3"]["q"] < 85
    assert "some-model" not in s  # categories unrelated to agent work do not count
    assert s["mistral-medium-2508"]["name"] == "Mistral Medium 3.1 (2508)"  # the dated identity is found too


def test_lookup_exact_aliases_and_no_guessing():
    d = data()
    assert arena.lookup("gemini:gemini-flash-latest", d)["name"] == "Gemini 3.8 Flash (High)"  # newest Flash
    assert arena.lookup("gemini:gemini-flash-lite-latest", d)["name"] == "Gemini 3.5 Flash-Lite"
    assert arena.lookup("mistral:mistral-large-latest", d)["name"] == "Mistral Large 4"
    assert arena.lookup("mistral:mistral-medium-2508", d)["name"] == "Mistral Medium 3.1 (2508)"
    assert arena.lookup("cohere:command-a-03-2025", d)["name"] == "Command A (03-2025)"
    assert arena.lookup("cohere:command-a-plus-05-2026", d) is None  # does not take the score of "command-a"
    assert arena.lookup("gemini:gemini-3.8-flash-lite", d) is None  # not Flash's
    assert arena.lookup("openrouter:moonshotai/kimi-k3:free", d)["name"] == "Kimi K3 (Max)"


def test_quality_prefers_arena_and_rescales_table_guesses():
    assert ranking.quality("nvidia:z-ai/glm-5.3") == ranking.table_quality("nvidia:z-ai/glm-5.3") == 90  # no data
    arena.set_cache(data())
    assert ranking.quality("nvidia:z-ai/glm-5.3") == round(arena.lookup("nvidia:z-ai/glm-5.3")["q"])
    assert ranking.quality("cloudflare:@cf/zai-org/glm-4.7-flash") < 45  # it was 72 in the table: actually weak
    # Model not on LMArena: the table estimate is lowered by the gap seen for models present in both sources
    off = ranking._table_offset()
    assert off < 0 and ranking.quality("openrouter:dots-studio/dots-3-note-preview:free") == 74 + off
    assert ranking.quality("openrouter:openrouter/free") == 0  # excluded ones stay excluded


def test_fetch_pages_in_parallel_and_keeps_only_agent_categories(tmp_path):
    rows = ROWS * 10  # 190 rows, 2 pages
    seen = []

    def get(url, params):
        seen.append(params["offset"])
        part = rows[params["offset"]:params["offset"] + params["length"]]
        return httpx.Response(200, json={"num_rows_total": len(rows), "rows": [{"row": r} for r in part]},
                              request=httpx.Request("GET", url))

    out = arena.refresh(get=get, path=tmp_path / "a.json")
    assert sorted(seen) == [0, 100] and out["published"] == "2026-10-22" and "gemini-3.8-flash" in out["scores"]
    assert arena.load(tmp_path / "a.json")["scores"] == out["scores"]


def test_refresh_failure_keeps_old_data():
    arena.set_cache(data())
    msgs = []

    def boom(url, params):
        raise httpx.ConnectError("yok")

    assert arena.refresh(msgs.append, get=boom)["scores"] == data()["scores"]
    assert "could not get" in msgs[0]


def test_live_speed_replaces_single_probe_after_enough_calls():
    ref = "g:gemini-3.8-flash"
    r = {"ok": True, "quality": 88, "latency": 2, "ttft": 0.4, "tps": 200}
    for _ in range(2):
        ranking.record_outcome(ref, True, ttft=3.0, tps=20)
    assert ranking.with_context(r, ref, None)["ttft"] == 0.4  # too few calls so far: the measurement stands
    ranking.record_outcome(ref, True, ttft=3.0, tps=20)
    live = ranking.with_context(r, ref, None)
    assert live["ttft"] == 3.0 and live["tps"] == 20 and live["live_calls"] == 3
    assert ranking.role_score(live, "voice") < ranking.role_score(r, "voice") - 20
    ranking.record_outcome(ref, True, ttft=1.0)  # exponential average: the new call gets 30% weight
    assert ranking.with_context(r, ref, None)["ttft"] == pytest.approx(2.4)


def test_hyphen_between_name_and_version_does_not_matter():
    assert arena.lookup("cerebras:qwen-3.8-27b", data())["name"] == "Qwen3.8-27B"


def test_brain_prefers_smart_enough_over_fastest():
    """User's table: flash-lite (intelligence 67, very fast) was ranked first for brain; 3.5-flash (82) was 5th."""
    lite = {"quality": 67, "latency": 0.6, "ttft": 0.6, "context": 1_000_000}
    flash = {"quality": 82, "latency": 2.6, "ttft": 2.6, "context": 1_000_000}
    assert ranking.role_score(flash, "brain") > ranking.role_score(lite, "brain")
    assert ranking.role_score(lite, "voice") > ranking.role_score(flash, "voice")  # the first word matters for voice


def test_proxy_names_dashed_versions_and_effort_suffixes():
    """Subscription proxy names (claude-opus-5-5, gemini-3.8-flash-high) are found in the ranking: before this,
    all proxy models, Opus included, showed "zekâ bilinmiyor" (intelligence unknown)."""
    rows = ROWS + [row("Claude Opus 5.5 (High)", 1513), row("Claude Fable 5.1 (Max)", 1513)]
    d = {"fetched_ts": time.time(), "scores": arena.build(rows)}
    assert arena.lookup("cliproxy:claude-opus-5-5", d)["name"] == "Claude Opus 5.5 (High)"
    assert arena.lookup("cliproxy:gemini-3.8-flash-high", d)["name"] == "Gemini 3.8 Flash (High)"
    near = arena.lookup("cliproxy:claude-fable-5-2", d)  # a newer minor version not in the ranking: the previous one's score
    assert near["name"] == "Claude Fable 5.1 (Max)" and near["estimated"]
    assert arena.lookup("cliproxy:claude-fable-6-0", d) is None  # major version gap: no estimate


def test_old_scale_cache_is_rescaled_on_load(tmp_path):
    import json

    p = tmp_path / "arena.json"
    p.write_text(json.dumps({"fetched_ts": time.time(), "scores": {"x": {"q": 70.0, "name": "X", "elo": 1}}}),
                 encoding="utf-8")
    d = arena.load(p)
    assert d["elo_per_point"] == arena.ELO_PER_POINT and d["scores"]["x"]["q"] == 55.0  # 30 behind -> 45 behind
