"""Zekâ puanı gerçek benchmark'tan (LMArena): ad eşleştirme, puanlama, indirme, tabloyla birleştirme."""

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
    assert s["gemini-3-flash"]["name"] == "Gemini 3 Flash"  # parantezsiz çeşit (Minimal değil)
    # GLM-5.3: genelde 52 Elo geride (82.7), kodda 30 (90): ağırlıklı ortalama ikisinin arası
    assert 82.7 < s["glm-5.3"]["q"] < 90
    assert "some-model" not in s  # ajan işiyle ilgisiz kategori sayılmaz
    assert s["mistral-medium-2508"]["name"] == "Mistral Medium 3.1 (2508)"  # tarihli kimlik de bulunur


def test_lookup_exact_aliases_and_no_guessing():
    d = data()
    assert arena.lookup("gemini:gemini-flash-latest", d)["name"] == "Gemini 3.8 Flash (High)"  # en yeni Flash
    assert arena.lookup("gemini:gemini-flash-lite-latest", d)["name"] == "Gemini 3.5 Flash-Lite"
    assert arena.lookup("mistral:mistral-large-latest", d)["name"] == "Mistral Large 4"
    assert arena.lookup("mistral:mistral-medium-2508", d)["name"] == "Mistral Medium 3.1 (2508)"
    assert arena.lookup("cohere:command-a-03-2025", d)["name"] == "Command A (03-2025)"
    assert arena.lookup("cohere:command-a-plus-05-2026", d) is None  # "command-a"nın puanını almaz
    assert arena.lookup("gemini:gemini-3.8-flash-lite", d) is None  # Flash'ın değil
    assert arena.lookup("openrouter:moonshotai/kimi-k3:free", d)["name"] == "Kimi K3 (Max)"


def test_quality_prefers_arena_and_rescales_table_guesses():
    assert ranking.quality("nvidia:z-ai/glm-5.3") == ranking.table_quality("nvidia:z-ai/glm-5.3") == 90  # veri yok
    arena.set_cache(data())
    assert ranking.quality("nvidia:z-ai/glm-5.3") == round(arena.lookup("nvidia:z-ai/glm-5.3")["q"])
    assert ranking.quality("cloudflare:@cf/zai-org/glm-4.7-flash") < 45  # tabloda 72'ydi: gerçekte zayıf
    # LMArena'da olmayan model: tablodaki tahmin, iki kaynakta da olanların farkı kadar düşürülür
    off = ranking._table_offset()
    assert off < 0 and ranking.quality("openrouter:dots-studio/dots-3-note-preview:free") == 74 + off
    assert ranking.quality("openrouter:openrouter/free") == 0  # hariç tutulanlar yine hariç


def test_fetch_pages_in_parallel_and_keeps_only_agent_categories(tmp_path):
    rows = ROWS * 10  # 190 satır, 2 sayfa
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
    assert "alınamadı" in msgs[0]


def test_live_speed_replaces_single_probe_after_enough_calls():
    ref = "g:gemini-3.8-flash"
    r = {"ok": True, "quality": 88, "latency": 2, "ttft": 0.4, "tps": 200}
    for _ in range(2):
        ranking.record_outcome(ref, True, ttft=3.0, tps=20)
    assert ranking.with_context(r, ref, None)["ttft"] == 0.4  # henüz az çağrı: ölçüm geçerli
    ranking.record_outcome(ref, True, ttft=3.0, tps=20)
    live = ranking.with_context(r, ref, None)
    assert live["ttft"] == 3.0 and live["tps"] == 20 and live["live_calls"] == 3
    assert ranking.role_score(live, "voice") < ranking.role_score(r, "voice") - 20
    ranking.record_outcome(ref, True, ttft=1.0)  # üstel ortalama: yeni çağrı %30 ağırlıkla
    assert ranking.with_context(r, ref, None)["ttft"] == pytest.approx(2.4)
