"""Smart model selection: the best order, by task domain, among the models the user can access.

Not every model family is equally good at every task: some stand out in UI and design, some in code, some in review
and reasoning, some in Turkish text. Here:

1. The task domain is extracted from the task text (design, code, review, writing, simple work, vision).
2. Candidate pool: the models the user has configured and that work in our measurements (models without a key,
   retired, or cooling down are skipped).
3. Score = general quality (ranking.QUALITY) + that family's strength in that domain; for agent work, models with a
   narrow context are pushed back. For simple work, speed comes first.

A project agent, manager, or council member that picks "auto" uses this order; a manually chosen model is left alone.
"""

from __future__ import annotations

import re
from typing import Any

from alice.i18n import _
from alice.providers import ranking

DOMAINS = ("design", "code", "review", "writing", "light", "vision")
# Display names (English keys, translated with _() where shown).
DOMAIN_TR = {"design": "design", "code": "code", "review": "review", "writing": "writing", "light": "simple job",
             "vision": "vision"}

# Patterns that extract the task domain from the task text (not the first match: the one with the most matches wins).
KEYWORDS: dict[str, str] = {
    "design": r"tasarım|arayüz|\bui\b|\bux\b|frontend|ön ?yüz|css|html|sayfa|site|landing|görsel kimlik|tipografi|"
              r"animasyon|responsive|mobil görünüm|tema|renk|layout|düzen|bileşen|component|figma|stil",
    "code": r"backend|api|sunucu|server|veritabanı|database|algoritma|fonksiyon|sınıf|refactor|yeniden düzenle|"
            r"hata ayıkla|debug|bug|performans|python|typescript|javascript|\.py\b|\.ts\b|\.js\b|modül|entegrasyon|"
            r"script|test yaz|birim test|unit test|cli|endpoint|şema|migration|servis|katman|uygula|implement",
    "review": r"incele|denetle|gözden geçir|review|audit|qa\b|kalite kontrol|doğrula|analiz|mimari|plan|risk|"
              r"güvenlik|security|kök neden",
    "writing": r"readme|doküman|dokümantasyon|metin|içerik|yazı|makale|blog|çeviri|açıklama yaz|kullanım kılavuzu|"
               r"copy|slogan|e-?posta|rapor yaz",
    "light": r"yeniden adlandır|rename|biçimlendir|format|dosyaları taşı|kopyala|küçük düzeltme|yazım hatası|typo|sil\b|"
             r"basit|mekanik|toplu değişiklik",
}

# Family -> domain bonus. The first matching pattern wins; specific patterns come first.
# Scores are relative (added to general quality).
# Source: general usage experience and public evaluations (2026); updated as new families appear.
STRENGTHS: list[tuple[str, dict[str, int]]] = [
    ("claude", {"design": 14, "code": 12, "writing": 12, "review": 9, "vision": 4}),
    ("gpt-5", {"review": 11, "code": 10, "writing": 7, "design": 8, "vision": 6}),
    (r"\bo[34]\b|o3-|o4-", {"review": 12, "code": 8}),
    ("gpt-4.1", {"code": 6, "writing": 6, "design": 4, "vision": 4}),
    ("grok", {"review": 7, "code": 6, "writing": 4}),
    ("gemini-3.1-pro|gemini-pro|gemini-2.5-pro", {"design": 9, "review": 8, "code": 7, "writing": 8, "vision": 12}),
    ("flash-lite", {"light": 6, "vision": 4}),
    ("gemini", {"vision": 10, "writing": 5, "design": 4, "light": 4}),
    ("kimi", {"code": 7, "design": 6, "writing": 4}),
    ("glm", {"code": 7, "design": 5, "review": 3}),
    ("deepseek-r", {"review": 9, "code": 4}),
    ("deepseek", {"code": 8, "review": 5}),
    ("qwen3-coder|devstral|north-mini-code|codestral", {"code": 9, "design": -3, "writing": -5}),
    ("qwen", {"code": 4, "light": 3}),
    ("gpt-oss", {"review": 4, "code": 3, "light": 3}),
    ("nemotron", {"review": 3, "code": 2}),
    ("mistral-large", {"writing": 4}),
    ("llama|gemma|mistral-small|laguna", {"light": 4, "design": -4, "review": -3}),
]

# Paid families not in the measurement table (free models) that the user added with their own key.
PAID_QUALITY: list[tuple[str, int]] = [
    ("claude-opus", 99), ("claude-sonnet", 96), ("claude-haiku", 84), ("claude", 92),
    ("gpt-5-nano", 78), ("gpt-5-mini", 88), ("gpt-5", 97), (r"\bo[34]-mini", 88), (r"\bo[34]\b|o3-|o4-", 95),
    ("gpt-4.1-mini", 80), ("gpt-4.1", 86), ("gpt-4o", 82), ("grok-4", 92), ("grok", 85), ("mistral-medium", 72),
    ("gpt-6", 90), ("kimi-k3", 92), ("kimi", 82),
]

MIN_AGENT_CONTEXT = 32_000  # models with a narrower context get stuck quickly in agent work


def classify(text: str) -> str:
    """The task domain from the task text. If nothing matches, code (most agent work is code)."""
    low = (text or "").lower()
    hits = {d: len(re.findall(rx, low)) for d, rx in KEYWORDS.items()}
    best = max(hits, key=lambda d: hits[d])
    if not hits[best]:
        return "code"
    if best == "light" and hits["light"] < 2 and (hits["code"] or hits["design"]):
        return "design" if hits["design"] >= hits["code"] else "code"
    return best


def base_quality(ref: str) -> int:
    low = ref.lower()
    for pat, q in PAID_QUALITY:
        if re.search(pat, low):
            return q
    return 0


def quality(ref: str) -> int:
    """Quality score on one scale: LMArena / measurement table; if absent, a paid-family estimate
    (scaled to the same range). 0 = unknown."""
    q = ranking.quality(ref)
    if q:
        return q
    b = base_quality(ref)
    return max(1, min(100, b + ranking._table_offset())) if b else 0


def bonus(ref: str, domain: str) -> int:
    low = ref.lower()
    for pat, table in STRENGTHS:
        if re.search(pat, low):
            return table.get(domain, 0)
    return 0


def pool(router) -> list[str]:
    """Models the user can access that are usable right now (tool-capable)."""
    refs: list[str] = []
    for role in ("brain", "fast", "vision"):
        try:
            refs += router.chain(role)
        except KeyError:
            continue
    res = (getattr(router, "auto_cache", None) or {}).get("results") or {}
    refs += [r for r, v in res.items() if isinstance(v, dict) and (v.get("ok") or v.get("ever_ok"))]
    from alice.providers.router import COOLING_RX  # router imports picker

    out: list[str] = []
    for r in refs:
        manual = getattr(router, "manual_only", None)
        if r in out or r in ranking.RANDOM_ROUTERS or r in getattr(router, "no_tools", set()) \
                or ranking.excluded(r) or (callable(manual) and manual(r)):
            continue  # manual_only: top-tier models (Fable and the like) are picked by hand only
        try:
            ok, why = router.available(r)
        except Exception:  # noqa: BLE001 - undefined provider, etc.
            continue
        if ok or COOLING_RX.search(why):  # a briefly busy model should not drop out of the pool
            # (the router tries models in order anyway)
            out.append(r)
    return out


def score(router, ref: str, domain: str) -> float:
    q = quality(ref) or 60  # unknown but user-selected model: medium
    s = q + bonus(ref, domain)
    try:
        ctx = router.context_limit(ref)
    except Exception:  # noqa: BLE001
        ctx = MIN_AGENT_CONTEXT
    if domain != "light" and ctx < MIN_AGENT_CONTEXT:
        s -= 25
    if domain == "light":  # for simple work speed matters: measured latency lowers the score
        res = ((getattr(router, "auto_cache", None) or {}).get("results") or {}).get(ref) or {}
        s -= min(20.0, float(res.get("latency") or 0) * 3)
    return s


def chain_for(router, domain: str, fallback: str = "brain") -> list[str]:
    """Model order for a domain: best first; at the end, the fallback role's chain
    (including models not in the pool)."""
    if domain == "vision":
        vis = [r for r in pool(router) if _vision(router, r)]
        ranked = sorted(vis, key=lambda r: -score(router, r, domain))
    else:
        ranked = sorted(pool(router), key=lambda r: -score(router, r, domain))
        strong = getattr(router, "strong_enough", None)
        if domain != "light" and callable(strong):
            # Agent/council work is main work too: don't drop to small models kept at the tail as backups.
            ranked = strong(ranked)
    try:
        rest = router.chain(fallback)
    except KeyError:
        rest = []
    return list(dict.fromkeys(ranked + rest))


def _vision(router, ref: str) -> bool:
    p = ref.split(":", 1)[0]
    pc = getattr(router, "providers_cfg", {}).get(p)
    try:
        return bool(pc and pc.supports_vision(ref.split(":", 1)[1])) and ref not in getattr(router, "no_vision", set())
    except Exception:  # noqa: BLE001
        return False


def pick(router, text: str, fallback: str = "brain") -> tuple[str, list[str]]:
    """(domain, model order) for the task text."""
    domain = classify(text)
    return domain, chain_for(router, domain, fallback="fast" if domain == "light" else fallback)


def explain(ref: str, domain: str) -> str:
    """Short label for the UI: 'tasarım · glm-5.3'."""
    return f"{_(DOMAIN_TR.get(domain, domain))} · {ref.split(':', 1)[-1].split('/')[-1]}"


def table(router) -> list[dict[str, Any]]:
    """For settings/screen: the top three models in each domain."""
    return [{"domain": d, "label": _(DOMAIN_TR[d]), "models": chain_for(router, d)[:3]} for d in DOMAINS]
