from __future__ import annotations

from types import SimpleNamespace

from alice.providers import discovery
from alice.providers.discovery import PREFERRED, FoundModel, _generic, _openrouter, discover, suggest_chains
from alice.providers.openai_compat import ProviderError


def fm(ref, context=None, tools=None, vision=None):
    return FoundModel(ref=ref, context=context, tools=tools, vision=vision)


# ------------------------------------------------------------------ score
def test_score_follows_preferred_order():
    assert fm("x:qwen3-coder-480b").score == len(PREFERRED) - 1
    assert fm("x:laguna-xs").score == len(PREFERRED)
    assert fm("x:Gemma-3-27B").score == 1  # case-insensitive
    assert fm("x:unknown-model").score == 0


def test_score_first_match_wins():
    # "deepseek" comes before "gemini" in the list: if both match, the higher one is taken
    assert fm("x:deepseek-gemini-mix").score == fm("x:deepseek").score


# ------------------------------------------------------------- openrouter
def test_openrouter_keeps_only_free_models():
    models = [
        {"id": "a/free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 131072,
         "supported_parameters": ["tools", "temperature"], "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "b/paid", "pricing": {"prompt": "0.000001", "completion": "0"}},
        {"id": "c/half", "pricing": {"prompt": "0", "completion": "0.5"}},
        {"id": "d/nopricing"},
        {"id": "e/float", "pricing": {"prompt": "0.0", "completion": "0.0"}},
        {"id": "f/numeric", "pricing": {"prompt": 0, "completion": 0}},
    ]
    out = _openrouter(models)
    refs = [f.ref for f in out]
    assert refs == ["openrouter:a/free", "openrouter:e/float", "openrouter:f/numeric"]
    a = out[0]
    assert a.context == 131072 and a.tools is True and a.vision is True
    e = out[1]
    assert e.context is None and e.tools is False and e.vision is False


def test_openrouter_handles_null_fields():
    out = _openrouter([{"id": "x", "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": None, "architecture": None}])
    assert out[0].tools is False and out[0].vision is False


# ---------------------------------------------------------------- generic
def test_generic_filters_skip_inactive_and_nameless():
    models = [
        {"id": "llama-3.3-70b", "context_window": 8192},
        {"id": "whisper-large-v3"},
        {"id": "Text-EMBED-004"},
        {"id": "llama-guard-4"},
        {"id": "old-model", "active": False},
        {"id": "kept-active-none", "active": None},
        {},
        {"id": ""},
        {"name": "models/gemini-2.5-flash", "context_length": 1_000_000},
    ]
    out = _generic("groq", models)
    assert [f.ref for f in out] == ["groq:llama-3.3-70b", "groq:kept-active-none", "groq:gemini-2.5-flash"]
    assert out[0].context == 8192
    assert out[2].context == 1_000_000
    assert all(f.tools is None and f.vision is None for f in out)


def test_generic_context_window_preferred_over_length():
    out = _generic("p", [{"id": "m", "context_window": 10, "context_length": 20}])
    assert out[0].context == 10


# --------------------------------------------------------------- discover
class FakeClient:
    def __init__(self, result):
        self.result = result

    def list_models(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_router(spec):
    """spec: {name: (enabled, result)}"""
    cfgs = {n: SimpleNamespace(enabled=en, api_key_env=f"{n.upper()}_KEY") for n, (en, _) in spec.items()}
    clients = {n: FakeClient(res) for n, (_, res) in spec.items()}
    calls = []

    def client(name):
        calls.append(name)
        return clients[name]

    return SimpleNamespace(providers_cfg=cfgs, client=client, calls=calls)


def test_discover_collects_sorts_and_records_errors():
    router = fake_router({
        "openrouter": (True, [
            {"id": "z/plain:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 4000},
            {"id": "q/qwen3-coder:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 262000},
            {"id": "paid", "pricing": {"prompt": "1", "completion": "1"}},
        ]),
        "groq": (True, [{"id": "openai/gpt-oss-20b", "context_window": 131072},
                        {"id": "plain-big", "context_window": 999999}]),
        "nvidia": (False, None),
        "broken": (True, ProviderError("auth", "401 unauthorized " + "x" * 300, status=401)),
        "crash": (True, RuntimeError("boom")),
    })
    found, errors = discover(router)
    refs = [f.ref for f in found]
    # First by preference score; on a tie, larger context first
    assert refs == ["openrouter:q/qwen3-coder:free", "groq:openai/gpt-oss-20b", "groq:plain-big",
                    "openrouter:z/plain:free"]
    assert errors["nvidia"] == "NVIDIA_KEY tanımlı değil"
    assert errors["broken"].startswith("[auth] 401 unauthorized") and len(errors["broken"]) == 160
    assert errors["crash"] == "boom"
    assert "nvidia" not in router.calls  # disabled provider is not queried
    assert set(errors) == {"nvidia", "broken", "crash"}


def test_discover_empty_router():
    assert discover(fake_router({})) == ([], {})


def test_discover_none_context_sorts_last_within_score():
    router = fake_router({"p": (True, [{"id": "a"}, {"id": "b", "context_window": 5}])})
    found, _ = discover(router)
    assert [f.ref for f in found] == ["p:b", "p:a"]


# ---------------------------------------------------------- suggest_chains
def test_suggest_chains_brain_limits_per_provider_and_excludes_no_tools():
    found = [
        fm("openrouter:qwen/qwen3-coder", tools=True),
        fm("openrouter:deepseek/r1", tools=True),
        fm("openrouter:moonshot/kimi-k2", tools=True),  # at most 2 per provider
        fm("openrouter:glm/glm-4.5", tools=False),        # no tool support
        fm("groq:openai/gpt-oss-120b"),                   # tools None -> accepted
        fm("groq:llama-4-scout"),                         # score 2 < 3
    ]
    chains = suggest_chains(found)
    assert chains["brain"] == ["openrouter:qwen/qwen3-coder", "openrouter:deepseek/r1", "groq:openai/gpt-oss-120b"]


def test_suggest_chains_fast_one_per_provider():
    found = [fm("groq:gpt-oss-20b"), fm("groq:llama-3.1-8b"), fm("gemini:gemini-2.5-flash-lite"),
             fm("cerebras:qwen-big")]
    assert suggest_chains(found)["fast"] == ["groq:gpt-oss-20b", "gemini:gemini-2.5-flash-lite"]


def test_suggest_chains_vision_limit_and_gemini_name():
    found = [fm(f"p{i}:model-{i}", vision=True) for i in range(3)] + [fm("gemini:gemini-2.5-pro"),
                                                                       fm("x:no-vision", vision=False),
                                                                       fm("p9:extra", vision=True)]
    assert suggest_chains(found)["vision"] == ["p0:model-0", "p1:model-1", "p2:model-2", "gemini:gemini-2.5-pro"]


def test_suggest_chains_brain_limit_eight():
    found = [fm(f"p{i}:deepseek-{i}") for i in range(12)]
    assert len(suggest_chains(found)["brain"]) == 8


def test_suggest_chains_empty():
    assert suggest_chains([]) == {"brain": [], "fast": [], "vision": []}


def test_module_constants_sane():
    assert not set(discovery.PREFERRED) & set(discovery.SKIP)
