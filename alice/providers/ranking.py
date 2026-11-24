"""Measures free models live and ranks them ("auto" role chain).

The free catalog keeps changing: models get retired (404/410), the "free" version turns paid, and some only
open for certain tools. A fixed list rots. Here:

1. Candidates matching the good model families we know are picked from the providers' /models catalogs.
2. Each candidate is tried with a real tool-call request: whether it passed, total time, time to first token,
   generated tokens and tokens/sec are measured.
3. Role chains are built with quality first, speed second, and written to ~/.alice/models.json.

When "auto" is written in a role's list in config, the router uses this order. If the cache is older than a
day, the app refreshes it in the background at startup.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.i18n import _
from alice.config import alice_home
from alice.providers import arena

# Quality score (0-100): the first pattern found in the model ID wins. Order matters: specific patterns come first.
# Scores are for agent work (tool use, Turkish, code); new families are added here as they appear.
QUALITY: list[tuple[str, int]] = [
    ("gemini-3.1-pro", 97), ("gemini-pro", 97), ("gemini-2.5-pro", 90),
    ("flash-lite", 66), ("gemini-3.8-flash", 93), ("gemini-flash-latest", 93), ("gemini-3.7-flash", 91),
    ("gemini-3.6-flash", 89), ("gemini-3.5-flash", 87), ("gemini-3-flash", 84), ("gemini-2.5-flash", 78),
    ("kimi-k3", 92), ("glm-5.3-flash", 74), ("glm-5", 90), ("glm-4.7-flash", 72), ("glm-4.5-flash", 64),
    ("glm-4.6v-flash", 60), ("command-a-plus", 80), ("command-a-vision", 62), ("command-a", 76), ("deepseek-v4", 88), ("deepseek-r", 80),
    ("nemotron-3-ultra", 87), ("qwen3.8", 85), ("qwen3-coder", 84), ("gpt-oss-120b", 82), ("kimi-k2", 82),
    ("mistral-large", 78), ("nemotron-3-super", 78), ("laguna-s", 76), ("dots-3", 74), ("llama-3.3-70b", 70),
    ("north-mini-code", 72), ("devstral", 72), ("mistral-medium", 72), ("gemma-4-31b", 68), ("ling-3", 66),
    ("gemma-4-26b", 64), ("nemotron-3.5-lightning", 62), ("gpt-oss-20b", 60), ("laguna-xs", 58),
    ("llama-4", 60), ("mistral-small", 58), ("qwen3:8b", 40),
    ("deepseek-v3", 84), ("qwen3-235b", 82), ("qwen3-32b", 70),
]
# A model not in the table is still measured (models from providers like SambaNova were never tried before), but
# since its intelligence is unknown it does not enter the main/voice chain with this score; on screen it shows as
# "zekâ bilinmiyor" (intelligence unknown).
UNKNOWN_QUALITY = 35
# Minimum intelligence score for the main model's auto order (long agent work). Scale: leader 100, 2 Elo = 1 point
# Opus 5.5 ~86, gemini-3.8-flash ~76, 3.5-flash ~66, sonnet-5.5 ~64; flash-lite ~44, gemma-4-31b ~48, step-5 ~51:
# these last ones cannot carry out the tool chain.
BRAIN_MIN_QUALITY = 60
VOICE_MIN_QUALITY = 45  # voice assistant: short conversations, speed matters
FAST_MIN_QUALITY = 40   # quick jobs such as summaries and classification
# Models that do not do chat/tool work, or are unsuitable for an agent.
EXCLUDE = ("openrouter/free", "openrouter/auto", "tts", "image", "embed", "live", "audio", "guard", "safety",
           "reward", "parse", "translate", "transcribe", "robotics", "computer-use", "lyria", "veo", "banana",
           "customtools", "deep-research", "antigravity", "omni", "whisper", "orpheus", "aqa", "inkling",
           "content-safety", "vision-instruct", "kosmos", "fuyu", "deplot", "vila", "neva", "clip")
# "Router" models that send requests to a random model: only at the very end of the chain, as a last resort.
RANDOM_ROUTERS = ("openrouter:openrouter/free", "openrouter:openrouter/auto")

CACHE_MAX_AGE = 24 * 3600
PROBE_TIMEOUT = 20.0  # a model that cannot make a tool call within 20 s is not suited to daily use anyway
PER_PROVIDER = 15
# OpenRouter's free models share one daily limit per account (~50 requests on an account without credit):
# the daily measurement should spend only a small part of it.
PROVIDER_CAP = {"openrouter": 6, "chatgpt": 3}  # ChatGPT: the free plan's Codex limit is small
# Account-wide quota providers: after one model hits a quota error, the others are not tried (same quota).
ACCOUNT_QUOTA = ("openrouter",)
SLOW = 3.0  # seconds: in the brain role, models slower than this are pushed back

# Order used while there is no cache (first launch, measurement not finished yet): the 9 Oct 2026 measurement.
# It is replaced when the measurement arrives; models from providers with no key are skipped anyway.
DEFAULT_AUTO: dict[str, list[str]] = {
    "brain": [
        "gemini:gemini-flash-latest", "gemini:gemini-3.8-flash", "nvidia:z-ai/glm-5.3",
        "gemini:gemini-3.6-flash", "gemini:gemini-3-flash-preview",
        "openrouter:nvidia/nemotron-3-super-120b-a12b:free", "nvidia:nvidia/nemotron-3-super-120b-a12b",
        "openrouter:dots-studio/dots-3-note-preview:free", "openrouter:cohere/north-mini-code:free",
    ],
    "fast": [
        "groq:qwen/qwen3.8-27b", "gemini:gemini-flash-lite-latest", "groq:openai/gpt-oss-20b",
        "nvidia:poolside/laguna-xs-2.1", "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
    ],
    "vision": ["gemini:gemini-flash-latest", "gemini:gemini-3.8-flash", "gemini:gemini-3.6-flash"],
}


def excluded(ref: str) -> bool:
    """Is this model unsuitable for chat/agent work? Only the model name is checked: the "clip" pattern also matched
    the "cliproxy:" provider name, so all proxy models (Opus included) got 0 and showed "zekâ bilinmiyor"."""
    low = ref.lower()
    if low in RANDOM_ROUTERS:
        return True
    model = low.partition(":")[2] or low
    return any(x in model for x in EXCLUDE)


def table_quality(ref: str) -> int:
    """Estimate from the hand-written table (for models not found on LMArena)."""
    low = ref.lower()
    if excluded(ref):
        return 0
    for pat, score in QUALITY:
        if pat in low:
            return score
    return 0


_offset: tuple[float, int] = (0.0, 0)  # (time of the LMArena data, table -> LMArena scale offset)


def _table_offset() -> int:
    """Table scores are more generous than the LMArena scale (gemini-3.8-flash 93 -> 88, glm-5.3 90 -> 81). The median
    difference for models in both sources is added to the table estimates, so both sources share one scale."""
    global _offset
    data = arena.load()
    ts = float(data.get("fetched_ts") or 0)
    if _offset[0] == ts:
        return _offset[1]
    diffs = sorted(hit["q"] - score for pat, score in QUALITY
                   if (hit := (data.get("scores") or {}).get(arena.norm(pat))))
    off = round(diffs[len(diffs) // 2]) if len(diffs) >= 5 else 0
    _offset = (ts, off)
    return off


def quality(ref: str) -> int:
    """Intelligence score: the real LMArena ranking; if not found, the hand-written table (same scale)."""
    if excluded(ref):
        return 0
    hit = arena.lookup(ref)
    if hit is not None:
        return max(1, round(hit["q"]))
    q = table_quality(ref)
    return max(1, min(100, q + _table_offset())) if q else 0


@dataclass
class Probe:
    ref: str
    ok: bool
    quality: int
    latency: float = 0.0
    ttft: float = 0.0
    tokens: int = 0
    tps: float = 0.0
    tool: str = ""
    error_kind: str = ""
    error: str = ""
    ever_ok: bool = False  # whether it ran at least once in previous measurements
    context: int = 0  # tokens: smaller of the model's context and the provider's sendable limit (0 = unknown)

    @property
    def dead(self) -> bool:
        """Permanent error: model missing or retired, or not opening with this key (rate limits are temporary)."""
        return self.error_kind in ("not_found", "auth")


# ------------------------------------------------------------------ cache
def cache_path() -> Path:
    return alice_home() / "models.json"


def load_cache(path: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads((path or cache_path()).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def cache_age(cache: dict[str, Any]) -> float:
    try:
        return time.time() - float(cache.get("probed_ts", 0))
    except (TypeError, ValueError):
        return float("inf")


def uses_auto(roles: dict[str, list[str]]) -> bool:
    return any("auto" in (chain or []) for chain in roles.values())


# ------------------------------------------------------------------- candidates
def catalog_models(router, name: str) -> list[dict[str, Any]]:
    """The provider's model list: the models written in config if any (free ones only), otherwise /models."""
    pc = router.providers_cfg[name]
    fixed = getattr(pc, "models", None) or []
    if fixed:
        return [{"id": m} for m in fixed]
    return router.client(name).list_models()


def candidates(router, per_provider: int = PER_PROVIDER,
               contexts: dict[str, int] | None = None) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """Candidates from the catalogs matching the good model families we know (the best per_provider per provider),
    catalog errors, and each provider's full model list (to filter out hand-written models that have been retired)."""
    from alice.providers.discovery import _generic, _openrouter

    errors: dict[str, str] = {}
    catalog: dict[str, list[str]] = {}
    by_provider: dict[str, list[tuple[int, str]]] = {}
    for name, pc in router.providers_cfg.items():
        if name == "ollama":
            continue
        if not pc.enabled:
            errors[name] = _("{env} is not set", env=pc.api_key_env)
            continue
        if not getattr(pc, "auto", True):
            continue  # paid-credit provider: only chosen by hand, and the measurement must not spend credit either
        try:
            models = catalog_models(router, name)
        except Exception as e:  # noqa: BLE001 - no error during discovery is fatal
            errors[name] = str(e)[:160]
            continue
        if not getattr(pc, "models", None):
            # A fixed list is not a real catalog: a hand-written paid model (zai:glm-5) must not count as "retired".
            catalog[name] = sorted({str(m.get("id") or m.get("name") or "").removeprefix("models/")
                                    for m in models} - {""})
        for f in (_openrouter(models) if name == "openrouter" else _generic(name, models)):
            q = quality(f.ref)
            if q == 0 and not excluded(f.ref) and name != "openrouter":
                q = UNKNOWN_QUALITY  # so OpenRouter's hundreds of unknown models do not clog the measurement
            if q > 0 and f.tools is not False:
                by_provider.setdefault(name, []).append((q, f.ref))
                if contexts is not None and f.context:
                    contexts[f.ref] = int(f.context)
    out = []
    for name, items in by_provider.items():
        items.sort(key=lambda x: -x[0])
        out += [ref for _score, ref in items[:min(per_provider, PROVIDER_CAP.get(name, per_provider))]]
    return out, errors, catalog


# ---------------------------------------------------------------------- measurement
PROBE_TOOLS = ("list_dir", "run_command", "read_file", "process_list")
PROBE_PROMPT = "Masaüstümdeki (C:\\Users\\kullanici\\Desktop) dosyaları listele."


def _probe_specs() -> list[dict[str, Any]]:
    from alice.tools import all_tools

    return [t.spec() for t in all_tools() if t.name in PROBE_TOOLS]


def probe(router, ref: str, specs: list[dict[str, Any]] | None = None, timeout: float = PROBE_TIMEOUT) -> Probe:
    """Try a single model with a real tool-call request. Does not touch the router's penalty mechanism."""
    from alice.providers.openai_compat import ProviderError, redact
    from alice.providers.router import clean_messages, is_gemini, parse_ref

    p, model = parse_ref(ref)
    pc = router.providers_cfg[p]
    from alice.providers.chatgpt import make_client

    from alice.providers.keypool import client_for

    client = client_for(pc, lambda c: make_client(c, timeout=timeout))
    msgs = clean_messages([
        {"role": "system", "content": "You are ALICE, an agent on the user's Windows PC. Use tools to act."},
        {"role": "user", "content": PROBE_PROMPT},
    ], gemini=is_gemini(pc))
    res = Probe(ref, False, quality(ref) or UNKNOWN_QUALITY)
    first: list[float] = []
    t0 = time.monotonic()
    try:
        r = client.chat_stream(model, msgs, lambda s: None, tools=specs or _probe_specs(),
                               extra=router.config.model_extra(ref) or None,
                               on_first=lambda: first.append(time.monotonic()))
    except ProviderError as e:
        res.latency = round(time.monotonic() - t0, 2)
        res.error_kind, res.error = e.kind, str(e)[:300]
        return res
    except Exception as e:  # noqa: BLE001 - no error during measurement is fatal
        res.latency = round(time.monotonic() - t0, 2)
        res.error_kind, res.error = "network", redact(f"{type(e).__name__}: {e}", pc.api_key)[:300]
        return res
    finally:
        # Each measurement opens its own httpx client (~50 models a day). It was never closed before, so connections
        # and file handles piled up in the background thread.
        client.close()
    total =time.monotonic() - t0
    calls = r.message.get("tool_calls") or []
    res.latency = round(total, 2)
    res.ttft = round((first[0] - t0) if first else total, 2)
    out_chars = len(r.text) + sum(len(c["function"].get("arguments", "")) for c in calls)
    res.tokens = int((r.usage or {}).get("completion_tokens") or out_chars / 3.5)
    gen = total - res.ttft
    res.tps = round(res.tokens / gen, 1) if gen > 0.05 else 0.0
    good = [c for c in calls if c["function"]["name"] in PROBE_TOOLS]
    if good:
        try:
            json.loads(good[0]["function"].get("arguments") or "{}")
            res.ok, res.tool = True, good[0]["function"]["name"]
        except json.JSONDecodeError:
            res.error_kind, res.error = "bad_args", "the tool arguments are not valid JSON"
    else:
        res.error_kind, res.error = "no_tool", f"did not call the tool: {r.text[:120]!r}"
    return res


def probe_all(router, refs: list[str], emit: Callable[[str], None] = lambda s: None,
              timeout: float = PROBE_TIMEOUT) -> list[Probe]:
    """A separate worker per provider; several concurrent requests within a provider (1-2 for Gemini/Groq)."""
    specs = _probe_specs()
    groups: dict[str, list[str]] = {}
    for ref in refs:
        groups.setdefault(ref.split(":", 1)[0], []).append(ref)
    results: list[Probe] = []
    lock = threading.Lock()

    def run_group(provider: str, items: list[str]) -> None:
        workers = {"gemini": 2, "groq": 1, "cerebras": 2}.get(provider, 4)
        if provider in ACCOUNT_QUOTA:
            workers = 1  # one at a time: once the quota runs out, the rest get no requests
        quota_hit: list[Probe] = []

        def one(ref: str) -> Probe:
            if quota_hit:
                return Probe(ref, False, quality(ref), error_kind="quota",
                             error=_("not tried: the provider's daily free quota is used up"))
            pr = probe(router, ref, specs, timeout)
            if pr.error_kind == "quota" and provider in ACCOUNT_QUOTA:
                quota_hit.append(pr)
            return pr

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for pr in pool.map(one, items):
                with lock:
                    results.append(pr)
                state = f"OK {pr.latency:.1f}s" if pr.ok else f"{pr.error_kind}"
                emit(f"{pr.ref}: {state}")

    threads = [threading.Thread(target=run_group, args=(p, items), daemon=True) for p, items in groups.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# -------------------------------------------------------------------- ranking
# Each model scores 0-100 on five criteria; the overall score is weighted by role. Weights are editable in the app
# (Settings → Models → Scoring) and saved to ~/.alice/model-weights.json.
METRICS = {"zeka": "intelligence (tool use, Turkish, code)", "hiz": "speed (time to finish the job)",
           "tepki": "time to first word", "akis": "streaming (tokens per second)",
           "baglam": "context (how long a conversation, file or page fits)",
           "guven": "reliability (answering without getting stuck or talking nonsense in real use)"}
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    # An agent calls the model 10-20 times: intelligence comes first, but slowness and stalling are costly too.
    # Since LMArena gave intelligence, gaps are small (flash-lite 67, 3.5-flash 82): a fast but weak flash-lite
    # at 45% was moving to the front as the main model. A stalling model still drops back via "güven"
    # (reliability), and a very slow one via "hız" (speed). Context: the agent reads long files and pages and
    # conversations grow; a small-context model stops fitting in the middle of a job.
    "brain": {"zeka": 65, "hiz": 15, "tepki": 5, "akis": 0, "baglam": 5, "guven": 10},
    # Voice assistant: time to the first word matters almost as much as intelligence; conversations are short.
    "voice": {"zeka": 40, "hiz": 10, "tepki": 35, "akis": 5, "baglam": 0, "guven": 10},
    # Quick jobs (summaries, classification): good enough, and finish fast; a bit of context for summaries.
    "fast": {"zeka": 30, "hiz": 45, "tepki": 10, "akis": 5, "baglam": 5, "guven": 5},
    # Vision: accuracy comes first in screen reading; a one-second difference matters less.
    "vision": {"zeka": 60, "hiz": 15, "tepki": 5, "akis": 5, "baglam": 10, "guven": 5},
}

# ------------------------------------------------------------------ reliability in real use
# The measurement is one short request, so it does not show whether a model stalls in a long conversation. The router
# writes every real call's result here; a model that stalls (timeout), talks nonsense or returns 5xx drops in score.
# The numbers halve every week: a model that gets better returns to its old place over time.
HEALTH_HALF_LIFE = 7 * 24 * 3600
BAD_KINDS = ("network", "garbled", "server")  # the model's own fault; quota, rate limit and request errors do not count
UNRELIABLE = (3.0, 0.4)  # at least this many errors and reliability below this value puts it at the end of the chain
_health: dict[str, dict[str, float]] | None = None
_health_lock = threading.Lock()
_health_dirty = 0


def health_path() -> Path:
    return alice_home() / "model-health.json"


def load_health() -> dict[str, dict[str, float]]:
    global _health
    with _health_lock:
        if _health is None:
            try:
                data = json.loads(health_path().read_text(encoding="utf-8"))
                _health = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                _health = {}
        return _health


def _decayed(h: dict[str, float], now: float) -> tuple[float, float]:
    f = 0.5 ** (max(0.0, now - float(h.get("ts", now))) / HEALTH_HALF_LIFE)
    return float(h.get("ok", 0)) * f, float(h.get("bad", 0)) * f


SPEED_ALPHA = 0.3  # weight of a new call in the real speed measurement (exponential average)
SPEED_MIN = 3  # once there are at least this many real calls, real-use speed is counted instead of the measurement


def record_outcome(ref: str, ok: bool, kind: str = "", now: float | None = None, ttft: float = 0.0,
                   tps: float = 0.0) -> None:
    """The result of a real call (if successful, time to first token and tokens per second). Errors are written to disk
    immediately, successes from time to time."""
    global _health_dirty
    if not ok and kind not in BAD_KINDS:
        return
    now = time.time() if now is None else now
    health = load_health()
    with _health_lock:
        prev = health.get(ref, {})
        good, bad = _decayed(prev, now)
        row = {"ok": round(good + ok, 3), "bad": round(bad + (not ok), 3), "ts": now}
        for key, val in (("ttft", ttft), ("tps", tps)):
            old, n = prev.get(key), int(prev.get(key + "_n", 0))
            if ok and val > 0:
                row[key] = round(val if old is None else old + SPEED_ALPHA * (val - old), 3)
                row[key + "_n"] = n + 1
            elif old is not None:
                row[key], row[key + "_n"] = old, n
        health[ref] = row
        _health_dirty += 1
        if ok and _health_dirty < 20:
            return
        _health_dirty = 0
        # Written inside the lock, with a unique temp file + os.replace: writing outside the lock let two threads'
        # writes interleave and leave the file half-written, or let an old snapshot overwrite a newer one.
        _write_health(json.dumps(health, ensure_ascii=False))


def _write_health(text: str) -> None:
    path = health_path()
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".model-health-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        tmp = ""
    except OSError:
        pass  # health data is rebuilt from the next requests
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # a leftover temp file is harmless


def reliability(ref: str, now: float | None = None) -> tuple[float, float]:
    """(0-1 reliability, error count). No data means 1: a model that worked in the measurement counts as blameless."""
    h = load_health().get(ref)
    if not h:
        return 1.0, 0.0
    good, bad = _decayed(h, time.time() if now is None else now)
    return (good + 2) / (good + bad + 2), bad


def live_speed(ref: str) -> dict[str, float]:
    """Time to first token and tokens per second measured in real use (if there are enough calls). The daily measurement
    is one short request: it depends on luck and does not show the speed in a long conversation."""
    h = load_health().get(ref) or {}
    out: dict[str, float] = {}
    for key in ("ttft", "tps"):
        if int(h.get(key + "_n", 0)) >= SPEED_MIN and h.get(key):
            out[key] = float(h[key])
    if out:
        out["live_calls"] = float(max(int(h.get("ttft_n", 0)), int(h.get("tps_n", 0))))
    return out


def unreliable(ref: str) -> bool:
    rel, bad = reliability(ref)
    return bad >= UNRELIABLE[0] and rel < UNRELIABLE[1]


def weights_path() -> Path:
    return alice_home() / "model-weights.json"


def load_weights() -> dict[str, dict[str, float]]:
    out = {role: dict(w) for role, w in DEFAULT_WEIGHTS.items()}
    try:
        saved = json.loads(weights_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for role, w in (saved if isinstance(saved, dict) else {}).items():
        if role in out and isinstance(w, dict):
            for k, v in w.items():
                if k in METRICS and isinstance(v, (int, float)) and 0 <= v <= 100:
                    out[role][k] = float(v)
    return out


def save_weights(weights: dict[str, dict[str, float]]) -> None:
    p = weights_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(weights, ensure_ascii=False, indent=1), encoding="utf-8")


def _get(r: Any, key: str) -> float:
    v = r.get(key) if isinstance(r, dict) else getattr(r, key, 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def context_of(ref: str, providers_cfg: dict[str, Any] | None, catalog: int | None = None) -> int:
    """The model's usable context: the smaller of the catalog value and the limit ALICE sends to that provider."""
    pc = (providers_cfg or {}).get(ref.partition(":")[0])
    limit = int(getattr(pc, "max_input_tokens", 0) or 0)
    vals = [v for v in (catalog, limit) if v]
    return min(vals) if vals else 0


def with_context(r: dict[str, Any], ref: str, providers_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """If the old measurement has no context, fill it with the provider's limit. Real-use reliability is added too."""
    out = r if r.get("context") else {**r, "context": context_of(ref, providers_cfg)}
    out = {**out, "reliability": reliability(ref)[0]}
    q = quality(ref)  # if LMArena was updated, use the current score instead of the one from measurement time
    if q:
        out["quality"] = q
    return {**out, **live_speed(ref)}


def metric_scores(r: Any) -> dict[str, float]:
    """0-100 sub-scores from a measurement (a Probe or cache dict). Curves are smooth: 1 s -> 2 s
    difference matters much more than an 8 s -> 9 s difference."""
    lat, ttft, tps = _get(r, "latency"), _get(r, "ttft"), _get(r, "tps")
    return {
        "zeka": max(0.0, min(100.0, _get(r, "quality"))),
        "hiz": round(100 / (1 + max(0.0, lat - 1) / 4), 1),        # 1 s 100, 3 s 67, 5 s 50, 13 s 25
        "tepki": round(100 / (1 + max(0.0, ttft - 0.3) / 1.0), 1),  # 0.3 s 100, 0.8 s 67, 1.3 s 50, 5 s 18
        # 150 tokens/s and above = 100. 0 = not measured (answer came in one piece): no penalty, middle score.
        "akis": round(min(100.0, tps / 1.5), 1) if tps > 0 else 50.0,
        # 8K 20, 16K 40, 32K 60, 64K 80, 128K and above 100; unknown = middle
        "baglam": (round(max(0.0, min(100.0, 20 * math.log2(ctx / 4000))), 1) if (ctx := _get(r, "context")) else 50.0),
        # Squared: 2 successes, 2 stalls (67%) give 44; always stalling (29%) gives 8. Stalling drops it regardless of IQ.
        "guven": round(100 * _rel(r) ** 2, 1),
    }


def _rel(r: Any) -> float:
    v = r.get("reliability") if isinstance(r, dict) else getattr(r, "reliability", None)
    return 1.0 if v is None else max(0.0, min(1.0, float(v)))


def role_score(r: Any, role: str = "brain", weights: dict[str, dict[str, float]] | None = None) -> float:
    w = (weights or load_weights()).get(role) or DEFAULT_WEIGHTS.get(role) or DEFAULT_WEIGHTS["brain"]
    total = sum(w.values()) or 1.0
    m = metric_scores(r)
    return round(sum(m[k] * w.get(k, 0) for k in METRICS) / total, 1)


def brain_score(pr: Probe, weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score(pr, "brain", weights)


def brain_score_of(quality: float, latency: float, ttft: float = 0.0, tps: float = 0.0,
                   weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score({"quality": quality, "latency": latency, "ttft": ttft, "tps": tps}, "brain", weights)


def fast_score(pr: Probe, weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score(pr, "fast", weights)


def voice_score(pr: Probe, weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score(pr, "voice", weights)


def _diversify(ranked: list[Probe], score: Callable[[Probe], float], gap: float = 12.0) -> list[Probe]:
    """Do not put more than two models from the same provider in a row (if the score gap is smaller than gap), so that
    when one provider returns 503 under load or runs out of quota, the rest of the chain does not fall with it."""
    left = list(ranked)
    out: list[Probe] = []
    while left:
        pick = left[0]
        prov = [r.ref.split(":", 1)[0] for r in out[-2:]]
        if len(prov) == 2 and prov[0] == prov[1] == pick.ref.split(":", 1)[0]:
            alt = next((r for r in left if r.ref.split(":", 1)[0] != prov[0]
                        and score(pick) - score(r) <= gap), None)
            pick = alt or pick
        out.append(pick)
        left.remove(pick)
    return out


def vision_backups(results: dict[str, Any], providers_cfg: dict[str, Any], have: list[str],
                   limit: int = 2) -> list[str]:
    """The head of the vision chain is usually Gemini: they all share one daily quota, so screen tasks were failing with
    "görme modeli yok" (no vision model). The best vision models that worked in the measurement, from providers not in
    the chain (one per provider), are appended at the end as backups."""
    used = {r.split(":", 1)[0] for r in have}
    out: list[str] = []
    for ref, r in sorted(results.items(),
                         key=lambda kv: -role_score(with_context(kv[1], kv[0], providers_cfg), "vision")):
        prov, _sep, model = ref.partition(":")
        pc = providers_cfg.get(prov)
        if (len(out) < limit and r.get("ok") and prov not in used and ref not in have and pc
                and pc.supports_vision(model) and quality(ref) > 0):
            used.add(prov)
            out.append(ref)
    return out


def build_chains(results: list[Probe], providers_cfg: dict[str, Any], limit: int = 10,
                 weights: dict[str, dict[str, float]] | None = None) -> dict[str, list[str]]:
    weights = weights or load_weights()

    def live(r: Probe) -> dict[str, Any]:
        return {**asdict(r), "reliability": reliability(r.ref)[0], **live_speed(r.ref)}

    # A model that often stalls in real use goes behind the working ones, whatever its score (it stays as a backup).
    def brain_score(r: Probe) -> float:
        return role_score(live(r), "brain", weights) - 1000 * unreliable(r.ref)

    def voice_score(r: Probe) -> float:
        return role_score(live(r), "voice", weights) - 1000 * unreliable(r.ref)

    def fast_score(r: Probe) -> float:
        return role_score(live(r), "fast", weights) - 1000 * unreliable(r.ref)

    def vision_score(r: Probe) -> float:
        return role_score(live(r), "vision", weights) - 1000 * unreliable(r.ref)

    ok = [r for r in results if r.ok and r.quality > 0]

    def provider_cfg(ref: str):
        return providers_cfg.get(ref.split(":", 1)[0])

    # Brain requests (system prompt + tools) are ~8-12K tokens: providers with a small TPM (Groq) do not fit the brain.
    def roomy(ref: str) -> bool:
        pc = provider_cfg(ref)
        return bool(pc and pc.max_input_tokens >= 16000)

    brain = [r for r in ok if r.quality >= BRAIN_MIN_QUALITY and roomy(r.ref)]
    if not brain:  # only small models are available: better to use those than no model at all
        brain = [r for r in ok if r.quality >= VOICE_MIN_QUALITY and roomy(r.ref)]
    brain = _diversify(sorted(brain, key=lambda r: -brain_score(r)), brain_score)
    # Good models with a temporary error during measurement (rate limit, 5xx) also go at the end as backups.
    # Those that timed out do not: a model that slow is useless for the brain.
    # Models whose daily quota is used up but that were seen working before also go in (the quota resets the next day);
    # quota errors from models that never worked (e.g. pro models with a 0 quota on the free tier) do not.
    transient = [r for r in results if not r.ok and r.quality >= BRAIN_MIN_QUALITY
                 and (r.error_kind in ("rate_limit", "server") or (r.error_kind == "quota" and r.ever_ok))
                 and roomy(r.ref)]
    transient = sorted(transient, key=lambda r: -r.quality)[:3]
    brain = brain[:limit - len(transient)] + transient
    # Fast small-context providers (Groq) also get in: if a request does not fit, the router skips to the next.
    voice = [r for r in ok if r.quality >= VOICE_MIN_QUALITY]
    voice = _diversify(sorted(voice, key=lambda r: -voice_score(r)), voice_score)
    fast = [r for r in ok if r.quality >= FAST_MIN_QUALITY and r.latency <= 15]
    fast.sort(key=lambda r: -fast_score(r))
    vision = [r for r in ok if (pc := provider_cfg(r.ref)) and pc.supports_vision(r.ref.split(":", 1)[1])]
    vision.sort(key=lambda r: -vision_score(r))
    vision = vision[:4]
    vision += [next(r for r in ok if r.ref == ref) for ref in vision_backups(
        {r.ref: asdict(r) for r in ok}, providers_cfg, [r.ref for r in vision])]
    return {"brain": [r.ref for r in brain][:limit], "voice": [r.ref for r in voice][:6] + [r.ref for r in transient],
            "fast": [r.ref for r in fast][:6],
            "vision": [r.ref for r in vision]}


def refresh(router, emit: Callable[[str], None] = lambda s: None, path: Path | None = None,
            per_provider: int = PER_PROVIDER, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    if getattr(router, "_real_clients", False) and arena.age() > arena.MAX_AGE:
        arena.refresh(emit)  # intelligence scores: real benchmark (every three days, a few seconds)
    contexts: dict[str, int] = {}
    refs, errors, catalog = candidates(router, per_provider, contexts=contexts)
    emit(_("measuring {n} candidates ({providers})", n=len(refs), providers=", ".join(sorted({r.split(":", 1)[0] for r in refs}))))
    results = probe_all(router, refs, emit, timeout)
    prev = (load_cache(path).get("results") or {})
    for r in results:
        old = prev.get(r.ref) or {}
        r.ever_ok = r.ok or bool(old.get("ever_ok") or old.get("ok"))
        r.context = context_of(r.ref, router.providers_cfg, contexts.get(r.ref))
    results.sort(key=lambda r: (not r.ok, -brain_score(r)))
    cache = {
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "probed_ts": time.time(),
        "catalog_errors": errors,
        "catalog": catalog,
        "results": {r.ref: asdict(r) for r in results},
        "chains": build_chains(results, router.providers_cfg),
    }
    p = path or cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # the ranking is still returned; it is measured again next time
    return cache


def rebuild(router, weights: dict[str, dict[str, float]] | None = None, path: Path | None = None) -> dict[str, Any]:
    """Rebuild the chains from the last measurement's results (new weights), without measuring the models again."""
    cache = load_cache(path)
    results = []
    for d in (cache.get("results") or {}).values():
        try:
            d = with_context(d, str(d.get("ref", "")), router.providers_cfg)
            results.append(Probe(**{k: v for k, v in d.items() if k in Probe.__dataclass_fields__}))
        except TypeError:
            continue
    if not results:
        return cache
    cache["chains"] = build_chains(results, router.providers_cfg, weights=weights)
    p = path or cache_path()
    try:
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # the chains are still returned; the cache is rewritten next time
    router.reload_auto(cache)
    return cache


def left_out(r: dict[str, Any], role: str, providers_cfg: dict[str, Any] | None) -> str:
    """Why a model is not in this role's order even though its score is high (the build_chains conditions)."""
    q, lat = float(r.get("quality") or 0), float(r.get("latency") or 0)
    prov, _sep, model = str(r.get("ref", "")).partition(":")
    pc = (providers_cfg or {}).get(prov)
    if not quality(str(r.get("ref", ""))):
        return _("intelligence score unknown")
    floor = BRAIN_MIN_QUALITY if role == "brain" else VOICE_MIN_QUALITY
    if role in ("brain", "voice") and q < floor:
        return _("intelligence below {floor}", floor=floor)
    if role == "brain" and pc is not None and pc.max_input_tokens < 16000:
        return _("small context ({k}K); doesn't fit the main model's requests", k=pc.max_input_tokens // 1000)
    if role == "fast" and (q < FAST_MIN_QUALITY or lat > 15):
        return _("not good or fast enough")
    if role == "vision" and pc is not None and not pc.supports_vision(model):
        return _("doesn't support images")
    return _("didn't fit at the end of the order")


def score_table(cache: dict[str, Any] | None = None, weights: dict[str, dict[str, float]] | None = None,
                providers_cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The on-screen score table: sub-scores per model, overall score per role, and the reason if not in the order."""
    cache = load_cache() if cache is None else cache
    weights = weights or load_weights()
    chains = cache.get("chains") or {}
    rows = []
    for ref, r in (cache.get("results") or {}).items():
        r = with_context(r, ref, providers_cfg)
        hit = arena.lookup(ref)
        row = {"ref": ref, "context": r.get("context") or 0, "ok": bool(r.get("ok")), "error": r.get("error_kind") or "",
               "error_text": str(r.get("error") or "")[:160],
               "known": quality(ref) > 0, "arena": {"name": hit["name"], "elo": hit["elo"]} if hit else None,
               "live_calls": int(r.get("live_calls") or 0), "latency": r.get("latency"), "ttft": r.get("ttft"), "tps": r.get("tps"),
               "metrics": metric_scores(r)}
        row["scores"] = {role: role_score(r, role, weights) for role in DEFAULT_WEIGHTS} if row["ok"] else {}
        row["why"] = {role: left_out({**r, "ref": ref}, role, providers_cfg) for role in DEFAULT_WEIGHTS
                      if row["ok"] and ref not in (chains.get(role) or [])}
        rows.append(row)
    rows.sort(key=lambda x: (not x["ok"], -(x["scores"].get("brain") or 0)))
    return rows


_refreshing = threading.Lock()


def refresh_in_background(router, emit: Callable[[str], None] = lambda s: None,
                          max_age: float = CACHE_MAX_AGE) -> bool:
    """If the cache is old (and config uses "auto"), refresh it in the background and update the router when done."""
    wanted = uses_auto(router.config.roles) or router.config.agent.get("auto_models", True)
    if not wanted or cache_age(load_cache()) < max_age:
        return False
    if not _refreshing.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            emit(_("measuring the models (in the background, once a day)"))
            cache = refresh(router)
            router.reload_auto(cache)
            emit(_("model order updated: ") + ", ".join(cache["chains"].get("brain", [])[:3]))
        except Exception as e:  # noqa: BLE001 - a background job must not crash the app
            emit(_("model measurement failed: {error}", error=e))
        finally:
            _refreshing.release()

    threading.Thread(target=work, daemon=True, name="alice-model-probe").start()
    return True


def format_table(cache: dict[str, Any]) -> list[str]:
    rows = [f"{'model':58s} {'state':10s} {'quality':>7s} {'time':>6s} {'ttft':>6s} {'token':>6s} {'tok/s':>6s}"]
    for r in cache.get("results", {}).values():
        state = "OK" if r["ok"] else r["error_kind"][:10]
        rows.append(f"{r['ref'][:58]:58s} {state:10s} {r['quality']:6d} {r['latency']:6.1f} {r['ttft']:6.1f} "
                    f"{r['tokens']:6d} {r['tps']:6.1f}")
    return rows
