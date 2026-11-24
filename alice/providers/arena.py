"""Real benchmark: model intelligence scores from the LMArena (lmarena.ai) leaderboard.

LMArena is an open leaderboard, refreshed daily, built from millions of real users' blind pairwise votes on two
answers (Hugging Face: lmarena-ai/leaderboard-dataset, CC-BY-4.0). It is used in place of the hand-written table
(ranking.QUALITY); the table stays as a fallback for models not found on the leaderboard.

Categories close to agent work are taken: overall, coding, instruction following, non-English languages (no separate
Turkish category), and multi-turn conversation. In each category the score is converted to 0-100 by its gap to the
leader (leader 100, every 3 Elo points = 1 point), then a weighted average is taken. The data is kept in
~/.alice/arena.json and refreshed in the background every three days.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

ROWS_URL = "https://datasets-server.huggingface.co/rows"
DATASET = "lmarena-ai/leaderboard-dataset"
CATEGORIES = {"overall": 0.25, "coding": 0.25, "instruction_following": 0.2, "non_english": 0.15, "multi_turn": 0.15}
PAGE = 100
# Each this many Elo below the leader = 1 point (100 Elo behind = 50). At 3 the scale was too tight: Opus 5.5 91,
# flash-lite 62, small models looked "adequate" for the main job. With 2: Opus ~87, flash ~75, flash-lite ~45.
ELO_PER_POINT = 2.0
MAX_AGE = 3 * 24 * 3600
TIMEOUT = 90.0

_NOISE_PREFIX = re.compile(r"^(nvidia|meta-llama|meta|mistralai|google|deepseek-ai|zai-org|z-ai|moonshotai|openai|"
                           r"microsoft|ibm-granite|cohere|xai)-")
_DATE = r"\d{2}-\d{4}|\d{4}-\d{2}-\d{2}|\d{8}|\d{6}|\d{4}"
_NOISE_SUFFIX = re.compile(r"-(instruct|it|chat|fp8|fp8-fast|fast|nvfp4|bf16|preview|exp|thinking|free)$")
_DATE_SUFFIX = re.compile(rf"-({_DATE})$")
_PAREN_DATE = re.compile(rf"\(([^)]*?\b)?({_DATE})\)")
_VERSION_SUFFIX = re.compile(r"-v?\d+(\.\d+)*$")


def norm(name: str, keep_date: bool = False) -> str:
    """'Gemini 3.8 Flash (High)', 'gemini:gemini-3.8-flash', 'nvidia:z-ai/glm-5.3' -> 'gemini-3.8-flash', 'glm-5.3'.
    keep_date: a dated ID ('mistral-medium-2508', 'command-a-03-2025') keeps its date."""
    s = name.lower().strip()
    if re.match(r"^[a-z0-9_-]+:", s) and not re.match(r"^[a-z0-9.-]+:\d", s):  # "gemini:..." provider prefix
        s = s.split(":", 1)[1]
    s = re.sub(r":(free|beta|extended|thinking)$", "", s)
    s = re.sub(r"\(.*?\)", " ", s)
    s = s.split("/")[-1]
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    for _ in range(4):
        t = _NOISE_SUFFIX.sub("", _NOISE_PREFIX.sub("", s))
        if not keep_date:
            t = _DATE_SUFFIX.sub("", t)
        if t == s or not t:
            break
        s = t
    return s


def cache_path() -> Path:
    return alice_home() / "arena.json"


_cache: dict[str, Any] | None = None
_lock = threading.Lock()


def load(path: Path | None = None) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None or path is not None:
            try:
                data = json.loads((path or cache_path()).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data = _rescaled(data) if isinstance(data, dict) else {}
            if path is not None:
                return data
            _cache = data
        return _cache


def _rescaled(data: dict[str, Any]) -> dict[str, Any]:
    """If the data on disk was scored on the old Elo scale (3), convert it to the current scale: the score is a linear
    function of the gap to the leader, no re-download needed. Thresholds are then never compared with old scores."""
    old = data.get("elo_per_point") or 3.0
    scores = data.get("scores")
    if not isinstance(scores, dict) or old == ELO_PER_POINT:
        return data
    fixed = {k: ({**v, "q": round(max(0.0, 100 - (100 - float(v["q"])) * old / ELO_PER_POINT), 1)}
                 if isinstance(v, dict) and isinstance(v.get("q"), (int, float)) else v) for k, v in scores.items()}
    return {**data, "scores": fixed, "elo_per_point": ELO_PER_POINT}


def set_cache(data: dict[str, Any]) -> None:
    global _cache
    with _lock:
        _cache = data


def age(data: dict[str, Any] | None = None) -> float:
    data = data if data is not None else load()
    try:
        return time.time() - float(data.get("fetched_ts", 0))
    except (TypeError, ValueError):
        return float("inf")


def build(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Raw rows -> model -> score. Among a model's variants ((High), (Max)...) take the unparenthesized one, else the best."""
    best: dict[str, dict[str, tuple[bool, float, str]]] = {}  # category -> norm name -> (no parens?, elo, name)
    for r in rows:
        cat, name = r.get("category"), str(r.get("model_name") or "")
        if cat not in CATEGORIES or not name or not isinstance(r.get("rating"), (int, float)):
            continue
        key = norm(name)
        if not key:
            continue
        cand = ("(" not in name, float(r["rating"]), name)
        keys = [key]
        if m := _PAREN_DATE.search(name):  # so "mistral-medium-2508" is also findable for "Mistral Medium 3.1 (2508)"
            keys += [f"{key}-{m.group(2)}", f"{_VERSION_SUFFIX.sub('', key)}-{m.group(2)}"]
        for k in dict.fromkeys(keys):
            cur = best.setdefault(cat, {}).get(k)
            if cur is None or cand[:2] > cur[:2]:
                best[cat][k] = cand
    if "overall" not in best:
        return {}
    tops = {cat: max(v[1] for v in models.values()) for cat, models in best.items()}
    scores: dict[str, Any] = {}
    for key, (_, _, name) in best["overall"].items():
        parts = {}
        for cat in CATEGORIES:
            hit = best.get(cat, {}).get(key)
            if hit is not None:
                parts[cat] = max(0.0, min(100.0, 100 - (tops[cat] - hit[1]) / ELO_PER_POINT))
        total = sum(CATEGORIES[c] for c in parts)
        scores[key] = {"q": round(sum(parts[c] * CATEGORIES[c] for c in parts) / total, 1), "name": name,
                       "elo": round(best["overall"][key][1])}
    return scores


def fetch(get: Callable[..., Any] | None = None, workers: int = 8) -> list[dict[str, Any]]:
    """LMArena's latest leaderboard (~11k rows, 100-row pages in parallel, a few seconds). Filtering on the server
    (filter) took 10-90 s per page; plain rows are fast, so categories are extracted here."""
    import httpx

    client = None
    if get is None:
        client = httpx.Client(timeout=TIMEOUT)
        get = client.get

    def page(offset: int) -> dict[str, Any]:
        # The server sometimes returns 502/503 for a single page; one page failing used to waste the whole refresh.
        for attempt in range(4 if client is not None else 1):  # a supplied get (test, custom client) gets a single attempt
            try:
                r = get(ROWS_URL, params={"dataset": DATASET, "config": "text", "split": "latest", "offset": offset,
                                          "length": PAGE})
                r.raise_for_status()
                return r.json()
            except Exception:  # noqa: BLE001 - re-raised on the last attempt
                if client is None or attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("unreachable")

    try:
        first = page(0)
        total = int(first.get("num_rows_total") or 0)
        pages = [first]
        with ThreadPoolExecutor(workers) as ex:
            pages += list(ex.map(page, range(PAGE, total, PAGE)))
    finally:
        if client is not None:
            client.close()
    return [r["row"] for p in pages for r in p.get("rows") or []
            if isinstance(r.get("row"), dict) and r["row"].get("category") in CATEGORIES]


def refresh(emit: Callable[[str], None] = lambda s: None, get: Callable[..., Any] | None = None,
            path: Path | None = None) -> dict[str, Any]:
    """Download, score, save. If that fails, the old data stays."""
    try:
        rows = fetch(get)
    except Exception as e:  # noqa: BLE001 - no network or site changed: continue with old data
        emit(f"could not get the LMArena ranking; continuing with the old data: {e}")
        return load()
    scores = build(rows)
    if not scores:
        emit("the LMArena ranking came back empty; continuing with the old data")
        return load()
    published = max((str(r.get("leaderboard_publish_date") or "") for r in rows), default="")
    data = {"fetched_ts": time.time(), "published": published, "scores": scores, "elo_per_point": ELO_PER_POINT}
    p = path or cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # the cache is rebuilt on the next fetch
    set_cache(data)
    emit(f"LMArena ranking updated ({len(scores)} models, {published})")
    return data


_ALIASES = (  # Google's "newest" aliases: they take the score of the newest version on the leaderboard
    (re.compile(r"^gemini-flash-latest$"), re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$")),
    (re.compile(r"^gemini-flash-lite-latest$"), re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash-lite$")),
    (re.compile(r"^gemini-pro-latest$"), re.compile(r"^gemini-(\d+(?:\.\d+)?)-pro$")),
)


def _newest(scores: dict[str, Any], family: re.Pattern[str]) -> dict[str, Any] | None:
    vers = [(tuple(int(x) for x in m.group(1).split(".")), k) for k in scores if (m := family.match(k))]
    return scores[max(vers)[1]] if vers else None


def lookup(ref: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A model reference's leaderboard entry ({q, name, elo}), or None if not found. Exact match only
    (guessing that "command-a-plus" has "command-a"'s score is worse than falling back to the table's estimate)."""
    scores = (data if data is not None else load()).get("scores") or {}
    if not scores:
        return None
    dated = norm(ref, keep_date=True)
    for alias, family in _ALIASES:
        if alias.match(dated):
            return _newest(scores, family)
    if dated.endswith("-latest"):  # "mistral-large-latest" -> the newest "mistral-large-N" on the leaderboard
        base = dated[: -len("-latest")]
        hit = _newest(scores, re.compile(rf"^{re.escape(base)}-(\d{{1,2}}(?:\.\d+)?)$"))
        if hit is not None:
            return hit
    key = norm(ref)
    hit = scores.get(dated) or scores.get(key) or scores.get(re.sub(r"-latest$", "", key))
    if hit is None:  # "qwen-3.8-27b" (Cerebras) = "qwen3.8-27b": the hyphen between letters and version is insignificant
        squashed = {squash(k): v for k, v in scores.items()}
        hit = squashed.get(squash(dated)) or squashed.get(squash(key))
    if hit is None:
        # Subscription proxy's names: "claude-opus-5-5" (dashed version), "gemini-3.8-flash-high" (effort suffix).
        # Previously none of these were found, and everything including Opus looked "intelligence unknown".
        alt = dotted(_EFFORT_SUFFIX.sub("", key))
        hit = scores.get(alt) or _older_sibling(scores, alt)
    return hit


_EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|xhigh|max|tiered|highspeed|\d+k)$")
_VERSIONED = re.compile(r"^(.*?)(\d+(?:\.\d+)?)(.*)$")


def dotted(key: str) -> str:
    """'claude-opus-5-5' -> 'claude-opus-5.5' (two single-digit version parts are joined with a dot)."""
    return _DASHED.sub(lambda m: f"{m.group(1)}.{m.group(2)}", key)


_DASHED = re.compile(r"(?<=-)(\d)-(\d)(?=$|-)")


def _older_sibling(scores: dict[str, Any], key: str) -> dict[str, Any] | None:
    """A newer version not on the leaderboard yet ('claude-fable-5.2', 'gpt-6.1-terra'): the score of the closest
    older version in the family. Assumes a new version is not worse than the old one: a guess, better than "unknown"."""
    m = _VERSIONED.match(key)
    if not m:
        return None
    head, ver, tail = m.groups()
    want = tuple(int(x) for x in ver.split("."))
    rx = re.compile(rf"^{re.escape(head)}(\d+(?:\.\d+)?){re.escape(tail)}$")
    def near(v: tuple[int, ...]) -> bool:
        # Only the previous minor of the same major (fable 5.2 -> 5.1). Farther is invention, not a guess:
        # giving gemini-3.8-flash-lite the score of 3.5 would be wrong.
        return v < want and v[0] == want[0] and len(v) == len(want) == 2 and want[1] - v[1] <= 1

    older = []
    for k in scores:
        mm = rx.match(k)
        if mm:
            v = tuple(int(x) for x in mm.group(1).split("."))
            if near(v):
                older.append((v, k))
    if not older:
        return None
    hit = scores[max(older)[1]]
    return {**hit, "estimated": True}


def squash(key: str) -> str:
    return re.sub(r"(?<=[a-z])-(?=\d)", "", key)
