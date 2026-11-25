"""Which models the model menu shows.

Providers list everything they have: old generations, dated copies, image/speech models, effort variants. The menu used to
show all of it (25 November: 140 rows, 75 of them under one proxy). Like the pickers of Claude Code and Codex, it now
shows the newest model of each family, a few per provider; the rest stays under "More models" and in the settings.
"""

from __future__ import annotations

import re
from typing import Any

PER_GROUP = 3
PER_CLAUDE = 4  # Fable, Opus, Sonnet, Haiku

# CLIProxyAPI's Claude endpoint lists the other accounts' models as Claude models with the real name written backwards
# ("claude-fable-5-dd-7.4-korg" is grok-4.7). The same models come under their own names from the OpenAI endpoint.
_DISGUISED = re.compile(r"^claude-[a-z]+-\d+-dd-")
# Models you cannot chat with. Whole words only: "live" must not match "deliver".
_NOT_CHAT = re.compile(
    r"(?:^|[^a-z0-9])(?:image|imagine|video|tts|stt|asr|audio|speech|live|whisper|voice|transcri\w*|translate|riva|"
    r"embed\w*|rerank\w*|guard\w*|safety|moderation|reward|detector|pii|gliner\w*|ocr|clip|nvclip|cosmos\w*|calibration|"
    r"aqa|deep-research|computer-use|customtools|lyria|veo|robotics|auto-review|starcoder\w*)(?![a-z])")
# Words that do not make another family: the same model at another speed, effort or context size, or a preview of it.
_VARIANT = {"preview", "latest", "exp", "thinking", "reasoning", "non", "chat", "fast", "highspeed", "high", "xhigh",
            "medium", "low", "minimal", "max", "tiered", "instruct", "it", "free", "customtools"}


def disguised(model: str) -> bool:
    return bool(_DISGUISED.match(model.lower()))


def chat_model(model: str) -> bool:
    return not _NOT_CHAT.search(model.lower())


def family(model: str) -> tuple[str, tuple[float, ...], int]:
    """(family, version, number of variant words): "claude-opus-4-8" -> ("claude opus", (4, 8), 0)."""
    name = re.split(r"[/]", model.lower())[-1].split(":")[0]
    words: list[str] = []
    version: list[float] = []
    variants = 0
    for tok in re.split(r"[-_ ]+", name):
        if re.fullmatch(r"\d+k", tok) or re.fullmatch(r"a?\d+[bt]|\d+x\d+b", tok):  # 256k context, 120b size
            variants += tok.endswith("k")
            if not tok.endswith("k"):
                words.append(tok)
        elif re.fullmatch(r"\d+(\.\d+)?", tok):
            if len(tok.split(".")[0]) < 4:  # a longer number is a date
                version.append(float(tok))
        elif tok in _VARIANT:
            variants += 1
        elif m := re.fullmatch(r"([a-z]+?)v?(\d+(?:\.\d+)?)", tok):  # k3.1, qwen3.8, v4.1
            words.append(m.group(1))
            version.append(float(m.group(2)))
        else:
            words.append(tok)
    return " ".join(words), tuple(version), variants


def mark(entries: list[dict[str, Any]], keep: set[str] = frozenset()) -> None:
    """Set row["main"] on every model row: True if the menu shows it without opening "More models". `keep`: refs that are
    always shown (the pinned model). row["chat"] is False for models that cannot chat."""
    native = {r["model"] for e in entries if _native(e) for r in e["models"] if r["ok"] and not r["hidden"]}
    for e in entries:
        claude = e["name"].endswith("claude-*") or e["name"] == "anthropic" or _native(e)
        best: dict[str, dict[str, Any]] = {}
        for r in e["models"]:
            r["chat"] = chat_model(r["model"])
            # The same Claude account answers on two endpoints: shown once, on the native one (prompt cache).
            r["twin"] = e["name"] == "cliproxy:claude-*" and r["model"] in native
            r["main"] = False
            if not r["chat"] or r["hidden"] or r["twin"]:
                continue
            fam, version, variants = family(r["model"])
            rank = (bool(r["ok"]), version, -variants, -len(r["model"]))
            if fam not in best or rank > best[fam]["rank"]:
                best[fam] = {"rank": rank, "row": r}
        top = sorted((b["row"] for b in best.values()), key=lambda r: (not r["ok"], -r["quality"], r["model"]))
        for r in top[:PER_CLAUDE if claude else PER_GROUP]:
            r["main"] = True
        for r in e["models"]:
            if r["ref"] in keep:
                r["main"] = True


def _native(e: dict[str, Any]) -> bool:
    return bool(e.get("native")) and bool(e.get("enabled")) and not e.get("hidden")
