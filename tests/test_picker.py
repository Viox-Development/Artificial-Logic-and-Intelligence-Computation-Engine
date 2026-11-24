from __future__ import annotations

from alice.providers import picker
from alice.providers.router import Router
from tests.conftest import FakeProvider, make_config


def router_with(models: list[str], small: str = ""):
    providers = {"p": {"base_url": "http://p", "api_key_env": "", "max_input_tokens": 200000, "vision_models": ["gemini*"]}}
    cfg = make_config({"brain": [f"p:{m}" for m in models], "fast": [f"p:{models[-1]}"], "vision": [f"p:{models[0]}"]},
                      providers=providers, auto_models=False)
    r = Router(cfg, provider_factory=lambda pc: FakeProvider("p", lambda *a, **k: "x"), sleep=lambda s: None)
    if small:
        r.size_limit[f"p:{small}"] = 16000
    return r


def test_classify_reads_the_task():
    assert picker.classify("Kurumsal sayfalar: about.html ve CSS tasarım sistemi") == "design"
    assert picker.classify("Ödeme akışını servis katmanına taşı, birim testlerini yaz") == "code"
    assert picker.classify("Tüm siteyi baştan sona gez, denetle, kırık linkleri doğrula") == "review"
    assert picker.classify("README güncelle, kullanım kılavuzu yaz") == "writing"
    assert picker.classify("Değişkenleri yeniden adlandır") == "light"
    assert picker.classify("") == "code"


def test_each_domain_gets_the_model_that_is_strong_there():
    r = router_with(["qwen3-coder-480b", "claude-sonnet-5", "gemini-3.1-pro", "deepseek-r2"])
    assert picker.chain_for(r, "design")[0] == "p:claude-sonnet-5"
    assert picker.chain_for(r, "vision")[0] == "p:gemini-3.1-pro"
    code = picker.chain_for(r, "code")
    assert code.index("p:qwen3-coder-480b") < code.index("p:deepseek-r2")
    review = picker.chain_for(r, "review")
    assert review.index("p:deepseek-r2") < review.index("p:qwen3-coder-480b")
    assert set(code) == {f"p:{m}" for m in ["qwen3-coder-480b", "claude-sonnet-5", "gemini-3.1-pro", "deepseek-r2"]}


def test_narrow_context_models_are_pushed_back_for_agent_work():
    r = router_with(["glm-5", "kimi-k3"], small="kimi-k3")
    assert picker.chain_for(r, "code")[0] == "p:glm-5"


def test_explain_is_short():
    assert picker.explain("nvidia:z-ai/glm-5.3", "design") == "design · glm-5.3"
