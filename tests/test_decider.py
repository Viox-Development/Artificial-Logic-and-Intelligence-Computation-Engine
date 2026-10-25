from __future__ import annotations

import json

import httpx
import pytest

from alice.decider import Choice, Decider, DeciderError, JevEngine, LLMEngine, Noul, Score, extract_json
from alice.providers.router import Router
from tests.conftest import FakeProvider, make_config


def jev_with(handler) -> JevEngine:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return JevEngine("https://api.typesafe.ai/v1/systemone", "jev-latest", "k", client=client)


def test_jev_request_shape_and_parse():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {
            "dept": {"type": "choice", "choice": "technical", "confidence": 0.85,
                     "probabilities": {"technical": 0.85, "billing": 0.15}},
            "urgent": {"type": "noul", "value": 0.9},
            "mood": {"type": "choice", "probabilities": {"1": 0.0, "2": 0.5, "3": 0.5}},
        }})

    d = jev_with(handler).decide("my app crashes", {
        "dept": Choice("which team", {"technical": "bugs", "billing": "money"}),
        "urgent": Noul("is it urgent"),
        "mood": Score("frustration", {1: "calm", 2: "annoyed", 3: "angry"}),
    })
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["questions"]["dept"] == {"type": "choice", "instructions": "which team",
                                                 "criteria": {"technical": "bugs", "billing": "money"}}
    assert seen["body"]["questions"]["urgent"]["type"] == "noul"
    # Score Jev'e choice olarak gider
    assert seen["body"]["questions"]["mood"]["type"] == "choice"
    assert d["dept"].value == "technical" and d["dept"].confidence == 0.85
    assert d["urgent"].value == 0.9
    assert d["mood"].value == pytest.approx(2.5)


def test_jev_label_from_argmax_when_no_label_field():
    def handler(req):
        return httpx.Response(200, json={"answers": {"q": {"probabilities": {"a": 0.2, "b": 0.8}}}})

    d = jev_with(handler).decide("s", {"q": Choice("?", {"a": "A", "b": "B"})})
    assert d["q"].value == "b" and d["q"].confidence == 0.8


def test_jev_http_error_raises():
    eng = jev_with(lambda req: httpx.Response(401, text="no"))
    with pytest.raises(DeciderError):
        eng.decide("s", {"q": Noul("?")})


def _llm_router(script):
    cfg = make_config({"fast": ["a:m"]})
    prov = FakeProvider("a", script)
    return Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None), prov


def test_llm_engine_parses_json():
    router, prov = _llm_router(['```json\n{"next": {"choice": "click_3", "confidence": 0.9}, "done": {"yes_probability": 0.1}}\n```'])
    d = LLMEngine(router).decide("state", {"next": Choice("?", {"click_3": "x", "back": "y"}), "done": Noul("?")})
    assert d["next"].value == "click_3" and d["next"].confidence == 0.9
    assert d["done"].value == pytest.approx(0.1)
    assert prov.calls[0]["response_format"] == {"type": "json_object"}


def test_llm_engine_rejects_invalid_option():
    router, _ = _llm_router(['{"q": {"choice": "nonsense"}}'])
    with pytest.raises(DeciderError):
        LLMEngine(router).decide("s", {"q": Choice("?", {"a": "A", "b": "B"})})


def test_decider_chain_falls_back():
    class Broken:
        name = "jev"

        def decide(self, state, questions):
            raise DeciderError("down")

    router, _ = _llm_router(['{"q": {"choice": "a", "confidence": 0.7}}'])
    events = []
    d = Decider([Broken(), LLMEngine(router)], on_event=events.append).decide("s", {"q": Choice("?", {"a": "A", "b": "B"})})
    assert d["q"].value == "a" and events


def test_from_config_skips_jev_without_key(monkeypatch):
    cfg = make_config({"fast": ["a:m"]})
    cfg.data["decider"]["chain"] = ["jev", "llm:fast"]
    router, _ = _llm_router([])
    assert Decider.from_config(cfg, router).names == ["llm:fast"]
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    assert Decider.from_config(cfg, router).names == ["jev", "llm:fast"]


def test_question_validation():
    router, _ = _llm_router([])
    with pytest.raises(ValueError):
        LLMEngine(router).decide("s", {f"q{i}": Noul("?") for i in range(17)})  # Jev'de sayı sınırı yok; 16 makul
    with pytest.raises(ValueError):
        LLMEngine(router).decide("s", {})


def test_extract_json():
    assert extract_json('blah {"a": 1} trailing') == {"a": 1}
    assert extract_json("nope") is None


def test_jev_openrouter_engine_and_402_cooldown(monkeypatch):
    cfg = make_config({"fast": ["a:m"]})
    cfg.data["decider"]["chain"] = ["jev", "jev-openrouter", "llm:fast"]
    router, _ = _llm_router(['{"q": {"choice": "a", "confidence": 0.7}}', '{"q": {"choice": "a", "confidence": 0.7}}'])
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    dec = Decider.from_config(cfg, router)
    assert dec.names == ["jev-openrouter", "llm:fast"]
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    dec.engines[0]._client = httpx.Client(transport=httpx.MockTransport(handler))
    q = {"q": Choice("?", {"a": "A", "b": "B"})}
    assert dec.decide("s", q)["q"].value == "a"
    assert calls[0]["model"] == "~typesafe/jev-latest"
    dec.decide("s", q)
    assert len(calls) == 1  # kredi yok: 1 saat boyunca tekrar denenmez
