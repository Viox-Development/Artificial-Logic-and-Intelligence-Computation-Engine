"""Gerçek benchmark: LMArena (lmarena.ai) sıralamasından modellerin zekâ puanı.

LMArena milyonlarca gerçek kullanıcının iki cevabı kör karşılaştırıp oyladığı, her gün güncellenen açık sıralama
(Hugging Face'te lmarena-ai/leaderboard-dataset, CC-BY-4.0). Elle yazılmış tablonun (ranking.QUALITY) yerine bu
kullanılır; sıralamada bulunamayan model için tablo yedek kalır.

Ajan işine yakın kategoriler alınır: genel, kod, talimata uyma, İngilizce dışı diller (Türkçe ayrı yok), çok turlu
konuşma. Her kategoride puan liderle farka göre 0-100'e çevrilir (lider 100, her 3 Elo puanı 1 puan), sonra
ağırlıklı ortalaması alınır. Veri ~/.alice/arena.json'da tutulur, üç günde bir arka planda yenilenir.
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
ELO_PER_POINT = 3.0  # liderin bu kadar Elo altı = 1 puan eksik (lider 100, 150 Elo geride 50)
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
    keep_date: tarihli kimlik ('mistral-medium-2508', 'command-a-03-2025') tarihiyle kalsın."""
    s = name.lower().strip()
    if re.match(r"^[a-z0-9_-]+:", s) and not re.match(r"^[a-z0-9.-]+:\d", s):  # "gemini:..." sağlayıcı öneki
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
            if path is not None:
                return data if isinstance(data, dict) else {}
            _cache = data if isinstance(data, dict) else {}
        return _cache


def set_cache(data: dict[str, Any]) -> None:
    global _cache
    with _lock:
        _cache = data


def age(data: dict[str, Any] | None = None) -> float:
    try:
        return time.time() - float((data if data is not None else load()).get("fetched_ts", 0))
    except (TypeError, ValueError):
        return float("inf")


def build(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Ham satırlardan model -> puan. Aynı modelin çeşitlerinden ((High), (Max)...) parantezsizi, yoksa en iyisi."""
    best: dict[str, dict[str, tuple[bool, float, str]]] = {}  # kategori -> norm ad -> (parantezsiz mi, elo, ad)
    for r in rows:
        cat, name = r.get("category"), str(r.get("model_name") or "")
        if cat not in CATEGORIES or not name or not isinstance(r.get("rating"), (int, float)):
            continue
        key = norm(name)
        if not key:
            continue
        cand = ("(" not in name, float(r["rating"]), name)
        keys = [key]
        if m := _PAREN_DATE.search(name):  # "Mistral Medium 3.1 (2508)" -> "mistral-medium-2508" de bulunsun
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
    """LMArena'nın son sıralaması (~11 bin satır, 100'lük sayfalar paralel, birkaç saniye). Süzme sunucuda
    (filter) sayfa başına 10-90 sn sürüyordu; düz satırlar hızlı, kategoriler burada ayıklanır."""
    import httpx

    client = None
    if get is None:
        client = httpx.Client(timeout=TIMEOUT)
        get = client.get

    def page(offset: int) -> dict[str, Any]:
        r = get(ROWS_URL, params={"dataset": DATASET, "config": "text", "split": "latest", "offset": offset,
                                  "length": PAGE})
        r.raise_for_status()
        return r.json()

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
    """İndir, puanla, kaydet. Olmazsa eski veri kalır."""
    try:
        rows = fetch(get)
    except Exception as e:  # noqa: BLE001 - ağ yok, site değişti: eski veriyle devam
        emit(f"LMArena sıralaması alınamadı, eski veriyle devam: {e}")
        return load()
    scores = build(rows)
    if not scores:
        emit("LMArena sıralaması boş geldi, eski veriyle devam")
        return load()
    published = max((str(r.get("leaderboard_publish_date") or "") for r in rows), default="")
    data = {"fetched_ts": time.time(), "published": published, "scores": scores}
    p = path or cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    set_cache(data)
    emit(f"LMArena sıralaması güncellendi ({len(scores)} model, {published})")
    return data


_ALIASES = (  # Google'ın "en yeni" takma adları: sıralamadaki en yeni sürümün puanını alsın
    (re.compile(r"^gemini-flash-latest$"), re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$")),
    (re.compile(r"^gemini-flash-lite-latest$"), re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash-lite$")),
    (re.compile(r"^gemini-pro-latest$"), re.compile(r"^gemini-(\d+(?:\.\d+)?)-pro$")),
)


def _newest(scores: dict[str, Any], family: re.Pattern[str]) -> dict[str, Any] | None:
    vers = [(tuple(int(x) for x in m.group(1).split(".")), k) for k in scores if (m := family.match(k))]
    return scores[max(vers)[1]] if vers else None


def lookup(ref: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Model referansının sıralamadaki karşılığı ({q, name, elo}); bulunamazsa None. Sadece tam eşleşme
    (tahminle "command-a-plus"a "command-a"nın puanını vermektense tablodaki tahmine düşmek daha doğru)."""
    scores = (data if data is not None else load()).get("scores") or {}
    if not scores:
        return None
    dated = norm(ref, keep_date=True)
    for alias, family in _ALIASES:
        if alias.match(dated):
            return _newest(scores, family)
    if dated.endswith("-latest"):  # "mistral-large-latest" -> sıralamadaki en yeni "mistral-large-N"
        base = dated[: -len("-latest")]
        hit = _newest(scores, re.compile(rf"^{re.escape(base)}-(\d{{1,2}}(?:\.\d+)?)$"))
        if hit is not None:
            return hit
    return scores.get(dated) or scores.get(norm(ref)) or scores.get(re.sub(r"-latest$", "", norm(ref)))
