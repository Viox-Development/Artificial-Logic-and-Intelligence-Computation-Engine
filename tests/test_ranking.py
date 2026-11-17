from __future__ import annotations

import time

from alice.config import ProviderConfig
from alice.providers import ranking
from alice.providers.openai_compat import ProviderError
from alice.providers.ranking import Probe, build_chains, quality
from alice.providers.router import Router
from tests.conftest import FakeProvider, make_config

PROVIDERS = {
    "g": {"base_url": "http://g", "api_key_env": "", "max_input_tokens": 1000000, "vision_models": ["*"]},
    "n": {"base_url": "http://n", "api_key_env": "", "max_input_tokens": 128000},
    "o": {"base_url": "http://o", "api_key_env": "", "max_input_tokens": 128000},
    "q": {"base_url": "http://q", "api_key_env": "", "max_input_tokens": 8000},
    "openrouter": {"base_url": "http://or", "api_key_env": "", "max_input_tokens": 128000},
}


def pcs() -> dict[str, ProviderConfig]:
    return make_config({"brain": []}, PROVIDERS).providers()


def test_quality_table_and_exclusions():
    assert quality("gemini:gemini-3.8-flash") > quality("gemini:gemini-flash-lite-latest")
    assert quality("gemini:gemini-3.8-flash-lite") == quality("gemini:gemini-flash-lite-latest")
    assert quality("openrouter:openrouter/free") == 0  # random model: never enters the automatic order
    assert quality("gemini:gemini-3.8-flash-tts") == 0
    assert quality("nvidia:z-ai/glm-5.3") > quality("nvidia:z-ai/glm-5.3-flash")
    assert quality("x:unknown-model") == 0


def test_build_chains_quality_speed_diversity_and_limits():
    res = [
        Probe("g:gemini-3.8-flash", True, 93, latency=6),
        Probe("g:gemini-flash-latest", True, 93, latency=8),
        Probe("g:gemini-3.6-flash", True, 89, latency=2),
        Probe("n:z-ai/glm-5.3", True, 90, latency=5),
        Probe("o:slow-but-smart", True, 92, latency=80),        # very slow: pushed back in brain
        Probe("q:qwen3.8-27b", True, 85, latency=0.4),          # provider limited to 8K: does not enter brain
        Probe("o:dead", False, 90, error_kind="not_found"),
        Probe("o:busy", False, 87, error_kind="rate_limit"),     # transient: backup at the end
        Probe("n:timeout", False, 92, error_kind="network"),     # timeout: excluded
    ]
    ch = build_chains(res, pcs())
    brain = ch["brain"]
    # Speed counts too: each second over 3 s costs 1.5 points (the agent calls models many times in one task).
    assert brain[:3] == ["g:gemini-3.6-flash", "g:gemini-3.8-flash", "n:z-ai/glm-5.3"]
    assert "q:qwen3.8-27b" not in brain and "o:dead" not in brain and "n:timeout" not in brain
    assert brain.index("o:slow-but-smart") > brain.index("g:gemini-3.6-flash")
    assert brain[-1] == "o:busy"
    assert ch["fast"][0] == "q:qwen3.8-27b"  # fast role: the fastest that is good enough
    assert "o:slow-but-smart" not in ch["fast"]
    assert all(r.startswith("g:") for r in ch["vision"])


def test_quota_models_kept_as_backup_only_if_they_ever_worked():
    res = [Probe("n:ok", True, 80, latency=2),
           Probe("g:flash", False, 93, error_kind="quota", ever_ok=True),
           Probe("g:pro", False, 97, error_kind="quota")]
    assert build_chains(res, pcs())["brain"] == ["n:ok", "g:flash"]


def cache_with(chains, results=None, age=0.0):
    return {"probed_ts": time.time() - age, "chains": chains, "results": results or {}}


def test_router_expands_auto_skips_dead_and_pushes_random_router_last():
    cfg = make_config({"brain": ["openrouter:openrouter/free", "auto", "n:explicit", "o:dead-model"]}, PROVIDERS)
    cache = cache_with({"brain": ["g:best", "n:explicit", "x:unknown-provider"]},
                       {"o:dead-model": {"error_kind": "not_found"}, "g:best": {"error_kind": ""}})
    r = Router(cfg, provider_factory=lambda pc: None, auto_cache=cache)
    assert r.chain("brain") == ["g:best", "n:explicit", "openrouter:openrouter/free"]


def test_router_appends_auto_to_manual_chains_and_can_be_disabled():
    cache = cache_with({"brain": ["g:best"]})
    r = Router(make_config({"brain": ["n:mine"]}, PROVIDERS), provider_factory=lambda pc: None, auto_cache=cache)
    assert r.chain("brain") == ["n:mine", "g:best"]
    off = make_config({"brain": ["n:mine"]}, PROVIDERS, auto_models=False)
    assert Router(off, provider_factory=lambda pc: None, auto_cache=cache).chain("brain") == ["n:mine"]


def test_old_cache_does_not_mark_models_dead():
    cfg = make_config({"brain": ["n:m"]}, PROVIDERS, auto_models=False)
    cache = cache_with({}, {"n:m": {"error_kind": "not_found"}}, age=8 * 24 * 3600)
    assert Router(cfg, provider_factory=lambda pc: None, auto_cache=cache).chain("brain") == ["n:m"]


def test_fallback_is_announced_and_logged(isolated_home):
    events = []
    provs = {"n": FakeProvider("n", [ProviderError("server", "503 busy", 503)]), "g": FakeProvider("g", ["merhaba"])}
    cfg = make_config({"brain": ["n:m1", "g:m2"]}, PROVIDERS, auto_models=False)
    r = Router(cfg, provider_factory=lambda pc: provs[pc.name], usage_path=isolated_home / "usage.json",
               on_event=events.append, auto_cache={})
    assert r.chat("brain", [{"role": "user", "content": "selam"}]).text == "merhaba"
    assert any("n:m1 (503) hata verdi, g:m2 modeline geçildi" in e for e in events)
    log = (isolated_home / "logs" / "providers.log").read_text(encoding="utf-8")
    assert "n:m1" in log and "HATA" in log and "geçildi" in log


def test_refresh_writes_cache(monkeypatch, tmp_path):
    cfg = make_config({"brain": ["auto"]}, PROVIDERS)
    r = Router(cfg, provider_factory=lambda pc: None, auto_cache={})
    found = (["g:gemini-3.8-flash", "n:dead"], {}, {"g": ["gemini-3.8-flash"]})
    monkeypatch.setattr(ranking, "candidates", lambda router, per_provider=15, contexts=None: found)
    fake = {"g:gemini-3.8-flash": Probe("g:gemini-3.8-flash", True, 93, latency=3, ttft=1, tokens=20, tps=10),
            "n:dead": Probe("n:dead", False, 0, error_kind="not_found")}
    monkeypatch.setattr(ranking, "probe", lambda router, ref, specs=None, timeout=45: fake[ref])
    monkeypatch.setattr(ranking, "_probe_specs", lambda: [])
    cache = ranking.refresh(r, path=tmp_path / "models.json")
    assert cache["chains"]["brain"] == ["g:gemini-3.8-flash"]
    assert ranking.load_cache(tmp_path / "models.json")["results"]["n:dead"]["error_kind"] == "not_found"
    r.reload_auto(cache)
    assert r.chain("brain") == ["g:gemini-3.8-flash"]
    assert any("gemini-3.8-flash" in line for line in ranking.format_table(cache))


def test_openrouter_probing_stops_at_daily_quota(monkeypatch):
    cfg = make_config({"brain": ["auto"]}, PROVIDERS)
    r = Router(cfg, provider_factory=lambda pc: None, auto_cache={})
    calls = []

    def fake_probe(router, ref, specs=None, timeout=20):
        calls.append(ref)
        return Probe(ref, False, 80, error_kind="quota")

    monkeypatch.setattr(ranking, "probe", fake_probe)
    monkeypatch.setattr(ranking, "_probe_specs", lambda: [])
    refs = [f"openrouter:m{i}:free" for i in range(5)] + ["g:a", "g:b"]
    res = ranking.probe_all(r, refs)
    assert len(res) == 7
    assert [c for c in calls if c.startswith("openrouter:")] == ["openrouter:m0:free"]  # only the first was really tried
    assert sum(1 for c in calls if c.startswith("g:")) == 2  # per-model quota providers: all are tried
    assert all(p.error_kind == "quota" for p in res)


def test_manual_models_missing_from_catalog_are_skipped():
    cfg = make_config({"brain": ["n:gone-model", "n:still-here", "o:not-fetched"]}, PROVIDERS, auto_models=False)
    cache = cache_with({})
    cache["catalog"] = {"n": ["still-here", "other"]}
    r = Router(cfg, provider_factory=lambda pc: None, auto_cache=cache)
    assert r.chain("brain") == ["n:still-here", "o:not-fetched"]


def test_voice_chain_prefers_low_ttft():
    res = [Probe("n:glm", True, 95, latency=12, ttft=5.5), Probe("g:flash", True, 90, latency=3, ttft=0.8),
           Probe("q:tiny", True, 90, latency=1, ttft=0.3)]
    ch = build_chains(res, pcs())
    assert ch["brain"][0] == "g:flash"  # the 12 s model is behind despite 5 points higher quality
    assert ch["voice"] == ["q:tiny", "g:flash", "n:glm"]  # küçük bağlamlı hızlı model de sese girer


def test_voice_chain_from_old_cache_sorts_brain_by_ttft():
    cache = {"probed_ts": time.time(), "chains": {"brain": ["n:glm", "g:flash"]},
             "results": {"n:glm": {"quality": 95, "ttft": 5.5}, "g:flash": {"quality": 90, "ttft": 0.8}}}
    r = Router(make_config({"voice": ["auto"]}, PROVIDERS), auto_cache=cache)
    assert r.auto_chain("voice") == ["g:flash", "n:glm"]


def test_backup_with_failed_probe_stays_after_working_models():
    # Backup with a transient probe error (saved latency 0) looked "çok hızlı" and jumped ahead of working ones.
    cache = {"probed_ts": time.time(), "chains": {"brain": ["n:a", "o:b", "o:busy"]}, "results": {
        "n:a": {"ok": True, "quality": 85, "latency": 3, "ttft": 1},
        "o:b": {"ok": True, "quality": 80, "latency": 2, "ttft": 1},
        "o:busy": {"ok": False, "quality": 90, "latency": 0, "ttft": 0, "error_kind": "rate_limit"}}}
    r = Router(make_config({"brain": ["auto"]}, PROVIDERS), auto_cache=cache)
    assert r.auto_chain("brain")[-1] == "o:busy"


def test_real_use_failures_push_a_hanging_model_back():
    """glm-5.3 probed well: smart and working, but hung 5 min in real use; lower its score so it moves to the end."""
    res = [Probe("n:z-ai/glm-5.3", True, 90, latency=3), Probe("g:gemini-3-flash", True, 84, latency=3)]
    assert build_chains(res, pcs())["brain"][0] == "n:z-ai/glm-5.3"
    now = time.time()
    for _ in range(3):
        ranking.record_outcome("n:z-ai/glm-5.3", False, "network", now=now)
    ranking.record_outcome("n:z-ai/glm-5.3", False, "rate_limit", now=now)  # quota/rate limit is not the model's fault
    rel, bad = ranking.reliability("n:z-ai/glm-5.3", now=now)
    assert bad == 3 and round(rel, 2) == 0.4 and not ranking.unreliable("n:z-ai/glm-5.3")
    ranking.record_outcome("n:z-ai/glm-5.3", False, "garbled", now=now)
    assert ranking.unreliable("n:z-ai/glm-5.3")
    assert build_chains(res, pcs())["brain"] == ["g:gemini-3-flash", "n:z-ai/glm-5.3"]  # stays as a backup
    assert ranking.health_path().exists()  # errors are saved immediately
    # A week later the counts halve; a model that recovers returns to its old place.
    for _ in range(6):
        ranking.record_outcome("n:z-ai/glm-5.3", True, now=now + 14 * 24 * 3600)
    assert not ranking.unreliable("n:z-ai/glm-5.3")


def test_reliability_shows_in_scores():
    r = {"quality": 90, "latency": 3, "reliability": 0.29}
    assert ranking.metric_scores(r)["guven"] < 10
    assert ranking.role_score(r, "brain") < ranking.role_score({**r, "reliability": 1.0}, "brain") - 8


def test_router_orders_brain_by_reliability(tmp_path):
    cfg = make_config({"brain": ["auto"]}, PROVIDERS)
    r = Router(cfg, provider_factory=lambda pc: FakeProvider(pc.name, []), sleep=lambda s: None)
    r.reload_auto({"chains": {"brain": ["n:z-ai/glm-5.3", "g:gemini-3-flash"]},
                   "results": {"n:z-ai/glm-5.3": {"ok": True, "quality": 90, "latency": 3},
                               "g:gemini-3-flash": {"ok": True, "quality": 84, "latency": 3}}})
    assert r.auto_chain("brain")[0] == "n:z-ai/glm-5.3"
    for _ in range(5):
        ranking.record_outcome("n:z-ai/glm-5.3", False, "network")
    assert r.auto_chain("brain") == ["g:gemini-3-flash", "n:z-ai/glm-5.3"]


def test_health_writes_are_serialized_and_atomic(monkeypatch):
    """Writes happen under a lock: concurrent errors do not corrupt the file, and no temporary file leftovers remain."""
    import json
    import threading

    active, overlap = [0], [False]
    real = ranking._write_health

    def slow_write(text):
        active[0] += 1
        overlap[0] = overlap[0] or active[0] > 1
        time.sleep(0.002)
        real(text)
        active[0] -= 1

    monkeypatch.setattr(ranking, "_write_health", slow_write)
    now = time.time()
    threads = [threading.Thread(target=lambda i=i: [ranking.record_outcome(f"n:m{i}", False, "network", now=now)
                                                    for _ in range(5)]) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap[0]
    data = json.loads(ranking.health_path().read_text(encoding="utf-8"))
    assert all(data[f"n:m{i}"]["bad"] == 5 for i in range(8))
    assert not list(ranking.health_path().parent.glob(".model-health-*"))
