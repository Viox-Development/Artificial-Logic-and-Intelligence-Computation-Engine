"""Undercover mode: keep ALICE and AI traces out of writing that goes to GitHub.

When on, commit messages, PR titles and bodies, issues and comments are sent as if the user wrote them:
* signature lines ("Co-Authored-By: …", "Generated with …", "— ALICE") are removed automatically;
* if the text still gives away ALICE or an AI, the tool rejects it and the agent rewrites it;
* PR tracking recognizes its own replies by the IDs of the comments it wrote (``own.json``), not by signature;
* independent review findings are not posted to the PR, only reported to the user: comments written from the
  repo owner's account would be mistaken by tracking for "the owner's request" and applied.

Limit: if someone directly and seriously asks whether a bot wrote it, ALICE does not lie; it does not answer
that question and leaves it to the user.

Setting: ``[github] undercover = "off" | "on" | "auto"`` ("auto": only public repos). Extra hidden words
(such as internal code names): ``[github] undercover_words = ["..."]``.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

MODES = ("off", "on", "auto")
OWN_FILE = "github-own.json"
OWN_KEEP = 3000

# A line that is entirely a signature: removed.
_SIGN_LINE = re.compile(
    r"(?im)^\s*(?:"
    r"co-authored-by:.*(?:claude|anthropic|alice|openai|chatgpt|gpt|gemini|copilot|cursor|\bai\b|bot\b|noreply@).*"
    r"|(?:🤖\s*)?(?:generated|written|created|made|co-?authored|assisted)\s+(?:with|by|using)\s+.*"
    r"|🤖.*"
    r"|[—–-]{1,2}\s*alice\s*"
    r"|.*\b(?:alice|yapay zek[aâ]|ai)\s+(?:ile|tarafından|yardımıyla)\s+(?:yazıldı|oluşturuldu|üretildi|hazırlandı).*"
    r")$")
_BLANKS = re.compile(r"\n{3,}")

# If left in place, these give the writing away: the agent must rewrite it.
_TELLS = [
    (re.compile(r"(?i)\bALICE\b"), "ALICE"),
    (re.compile(r"(?i)\b(?:claude|anthropic|chatgpt|openai|gemini|copilot)\b"), "model/şirket adı"),
    (re.compile(r"(?i)\b(?:as an ai|an ai assistant|language model|ai[- ]generated|llm)\b"), "yapay zekâ sözü"),
    (re.compile(r"(?i)yapay zek[aâ]|dil modeli|asistan(?:ı|ın)?\s+olarak"), "yapay zekâ sözü"),
    (re.compile(r"(?i)\b(?:otomatik olarak düzeltildi|auto-?fix(?:ed)? by|addressed by)\b"), "otomasyon izi"),
]

_lock = threading.Lock()
_public: dict[str, bool] = {}


def settings(cfg: Any) -> dict[str, Any]:
    data = getattr(cfg, "data", None) or {}
    return dict(data.get("github") or {})


def mode(cfg: Any) -> str:
    m = str(settings(cfg).get("undercover", "off")).strip().lower()
    if m in ("true", "1", "yes", "evet"):
        return "on"
    return m if m in MODES else "off"


def is_public(api: Any, repo: str) -> bool:
    """Is the repo public? Cached for the session. If it can't be checked, assume public: hiding is safer when unsure."""
    if repo in _public:
        return _public[repo]
    try:
        val = not bool(api.get(f"/repos/{repo}").get("private"))
    except Exception:  # noqa: BLE001
        return True
    _public[repo] = val
    return val


def active(cfg: Any, repo: str | None, api: Any = None) -> bool:
    m = mode(cfg)
    if m == "on":
        return True
    if m == "off" or not repo:
        return False
    if api is None:
        from alice.tools.github import GitHubAPI, github_token

        api = GitHubAPI(github_token(cfg))
    return is_public(api, repo)


def scrub(text: str) -> str:
    """Remove signature lines and collapse extra blank lines."""
    out = _SIGN_LINE.sub("", text or "")
    return _BLANKS.sub("\n\n", out).strip()


def tells(text: str, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Give-away phrases left after scrubbing (no duplicates)."""
    found: list[str] = []
    for rx, label in _TELLS:
        if rx.search(text or "") and label not in found:
            found.append(label)
    for w in extra:
        w = str(w).strip()
        if w and re.search(rf"(?i)\b{re.escape(w)}\b", text or "") and w not in found:
            found.append(w)
    return found


def clean(cfg: Any, text: str, what: str = "metin") -> str:
    """Remove signatures; if a give-away phrase remains, reject with ToolError (the agent rewrites the text)."""
    from alice.tools.base import ToolError

    out = scrub(text)
    hits = tells(out, settings(cfg).get("undercover_words") or [])
    if hits:
        raise ToolError(f"gizli mod açık: {what} şunları ele veriyor: {', '.join(hits)}. Kullanıcının kendi yazısı "
                        "gibi, ALICE'ten ya da yapay zekâdan söz etmeden yeniden yaz ve tekrar dene.")
    return out


RULES = (
    "Gizli mod açık: commit, PR, issue ve yorumları depo sahibi kendisi yazmış gibi yaz. ALICE'ten, yapay zekâdan, "
    "modelden ya da otomasyondan söz etme; imza ekleme. Biri doğrudan bunu bir botun mu yazdığını sorarsa inkâr "
    "etme ve cevaplama: o konuyu kullanıcıya bırak, son mesajında söyle."
)


# --------------------------------------------------------------------------- comments written by ALICE
def _own_path() -> Path:
    from alice.config import alice_home

    return alice_home() / OWN_FILE


def _load_own() -> list[int]:
    try:
        return [int(x) for x in json.loads(_own_path().read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def remember_own(*ids: Any) -> None:
    """IDs of comments/reviews ALICE wrote on GitHub: PR tracking does not count these as new comments."""
    new = [int(i) for i in ids if i]
    if not new:
        return
    with _lock:
        rows = _load_own()
        rows = [i for i in rows if i not in new] + new
        path = _own_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows[-OWN_KEEP:]), encoding="utf-8")
        tmp.replace(path)


def is_own(cid: Any) -> bool:
    if not cid:
        return False
    try:
        return int(cid) in set(_load_own())
    except (TypeError, ValueError):
        return False
