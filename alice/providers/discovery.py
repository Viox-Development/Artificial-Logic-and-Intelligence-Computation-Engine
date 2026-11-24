"""Live free model discovery.

The free catalog changes monthly (OpenRouter's models are fully replaced every few months, and Groq removed Llama
and Kimi). Instead of relying on a fixed list, `alice models --refresh` queries the providers' /models endpoints,
shows what is really open, and writes it to the config if asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alice.i18n import _
from alice.providers.openai_compat import ProviderError
from alice.providers.router import Router

# Model families we know are strong for coding/agent work; they are moved to the front of the ranking.
PREFERRED = (
    "laguna", "qwen3-coder", "qwen3.", "gpt-oss-120b", "deepseek", "kimi", "glm", "devstral",
    "north", "nemotron", "gemini", "gpt-oss-20b", "llama-4", "gemma",
)
SKIP = ("embed", "whisper", "tts", "guard", "rerank", "moderation", "audio", "image-gen", "orpheus")


@dataclass
class FoundModel:
    ref: str
    context: int | None
    tools: bool | None
    vision: bool | None
    note: str = ""

    @property
    def score(self) -> int:
        name = self.ref.lower()
        for i, key in enumerate(PREFERRED):
            if key in name:
                return len(PREFERRED) - i
        return 0


def _openrouter(models: list[dict[str, Any]]) -> list[FoundModel]:
    out = []
    for m in models:
        pricing = m.get("pricing") or {}
        free = str(pricing.get("prompt", "1")) in ("0", "0.0") and str(pricing.get("completion", "1")) in ("0", "0.0")
        if not free:
            continue
        params = m.get("supported_parameters") or []
        modality = (m.get("architecture") or {}).get("input_modalities") or []
        out.append(FoundModel(
            ref=f"openrouter:{m['id']}",
            context=m.get("context_length"),
            tools="tools" in params,
            vision="image" in modality,
        ))
    return out


def _generic(provider: str, models: list[dict[str, Any]]) -> list[FoundModel]:
    out = []
    for m in models:
        mid = m.get("id") or m.get("name")
        if not mid or any(s in mid.lower() for s in SKIP):
            continue
        if m.get("active") is False:
            continue
        out.append(FoundModel(
            ref=f"{provider}:{mid.removeprefix('models/')}",
            context=m.get("context_window") or m.get("context_length"),
            tools=None,
            vision=None,
        ))
    return out


def discover(router: Router) -> tuple[list[FoundModel], dict[str, str]]:
    found: list[FoundModel] = []
    errors: dict[str, str] = {}
    for name, pc in router.providers_cfg.items():
        if not pc.enabled:
            errors[name] = _("{env} is not set", env=pc.api_key_env)
            continue
        if not getattr(pc, "auto", True):
            continue
        try:
            from alice.providers.ranking import catalog_models

            models = catalog_models(router, name)
        except (ProviderError, Exception) as e:  # noqa: BLE001 - no error is fatal during discovery
            errors[name] = str(e)[:160]
            continue
        found.extend(_openrouter(models) if name == "openrouter" else _generic(name, models))
    found.sort(key=lambda f: (-f.score, -(f.context or 0)))
    return found, errors


def suggest_chains(found: list[FoundModel]) -> dict[str, list[str]]:
    """Suggest role chains from the models found. The best 2 models from each provider are taken,
    so the chain does not break when one provider's quota runs out."""

    def pick(pred, per_provider=2, limit=8):
        seen: dict[str, int] = {}
        out = []
        for f in found:
            if not pred(f):
                continue
            p = f.ref.split(":", 1)[0]
            if seen.get(p, 0) >= per_provider:
                continue
            seen[p] = seen.get(p, 0) + 1
            out.append(f.ref)
            if len(out) >= limit:
                break
        return out

    brain = pick(lambda f: f.tools is not False and f.score >= 3)
    fast = pick(lambda f: any(k in f.ref for k in ("20b", "flash-lite", "mini", "xs", "8b", "small")), per_provider=1)
    vision = pick(lambda f: f.vision is True or "gemini" in f.ref, per_provider=2, limit=4)
    return {"brain": brain, "fast": fast, "vision": vision}
