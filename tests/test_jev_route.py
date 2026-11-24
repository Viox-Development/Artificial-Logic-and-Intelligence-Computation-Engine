"""Jev model routing: truth table, failure handling, tier chains, and hookup to the agent."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from alice.decider import Answer, Decision, DeciderError
from alice.providers import jev_route
from alice.providers.jev_route import decide_tier


@pytest.mark.parametrize("code,thinking,steps,chat,tier", [
    (0.95, 2.0, 1.5, 0.05, "strong"),   # "bu fonksiyondaki hatayı düzelt" (fix the bug in this function)
    (0.05, 3.6, 1.2, 0.02, "strong"),   # "mikroservis mi monolit mi, gerekçeyle" (microservices or monolith, with reasons)
    (0.10, 2.5, 3.7, 0.01, "strong"),   # "projeyi kur, testleri çalıştır, raporla" (set up the project, run the tests, report)
    (0.02, 1.1, 1.0, 0.96, "light"),    # "naber" (hey, what's up)
    (0.03, 1.6, 1.9, 0.20, "light"),    # "YouTube'da bir şarkı aç" (open a song on YouTube)
    (0.04, 2.6, 2.0, 0.10, "normal"),   # "React ile Vue farkı ne" (what's the difference between React and Vue)
    (0.50, 2.0, 1.5, 0.10, "normal"),   # unclear whether it's code: the table falls back to "normal"
])
def test_truth_table(code, thinking, steps, chat, tier):
    assert decide_tier(code, thinking, steps, chat).tier == tier


class FakeEngine:
    def __init__(self, values=None, error=None):
        self.values, self.error, self.calls = values or {}, error, []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return Decision({k: Answer("noul" if k in ("code", "chat") else "score", v, 1.0)
                         for k, v in self.values.items()}, "fake", 0.1)


def test_route_sends_request_and_previous_reply():
    eng = FakeEngine({"code": 0.9, "thinking": 2.0, "steps": 1.5, "chat": 0.1})
    r = jev_route.route(NS(), "şu scripti düzelt", "Hangi script?", engine=eng)
    state, questions = eng.calls[0]
    assert r.tier == "strong" and state["request"] == "şu scripti düzelt" and state["previous_reply"] == "Hangi script?"
    assert set(questions) == {"code", "thinking", "steps", "chat"}


def test_jev_failure_is_not_an_answer():
    logs = []
    r = jev_route.route(NS(), "selam", engine=FakeEngine(error=DeciderError("HTTP 500")), log=logs.append)
    assert r.tier == "normal" and r.reason == "jev error" and "failed" in logs[0]


def make_router(chains):
    return NS(chain=lambda role: list(chains[role]), role_overrides={}, on_event=lambda s: None)


def test_tier_chains_keep_brain_as_fallback():
    router = make_router({"brain": ["chatgpt:terra", "gemini:flash"], "fast": ["groq:qwen", "gemini:flash"]})
    cfg = NS(data={"routing": {"strong": ["cliproxy:claude-opus-5-5", "chatgpt:terra"], "light": []}})
    assert jev_route.tier_chain(router, cfg, "strong") == ["cliproxy:claude-opus-5-5", "chatgpt:terra", "gemini:flash"]
    assert jev_route.tier_chain(router, cfg, "light") == ["groq:qwen", "gemini:flash", "chatgpt:terra"]
    assert jev_route.tier_chain(router, cfg, "normal") == ["chatgpt:terra", "gemini:flash"]


def test_voice_chain_only_promotes_heavy_work(monkeypatch):
    fast = ["gemini:flash", "cerebras:qwen"]
    cfg = NS(data={"routing": {"enabled": True, "voice_strong": ["cliproxy:claude-sonnet-5-5"]}})
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: jev_route.Route("strong", "derin düşünme"))
    assert jev_route.voice_chain(None, cfg, "tatil planı yap", fast)[0] == ["cliproxy:claude-sonnet-5-5", *fast]
    for tier in ("light", "normal"):
        monkeypatch.setattr(jev_route, "route", lambda *a, t=tier, **k: jev_route.Route(t, "x"))
        assert jev_route.voice_chain(None, cfg, "spotify aç", fast)[0] == fast  # seste hız önce
    off = NS(data={"routing": {"enabled": True, "voice": False, "voice_strong": ["cliproxy:claude-sonnet-5-5"]}})
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: jev_route.Route("strong", "x"))
    assert jev_route.voice_chain(None, off, "tatil planı yap", fast)[0] == fast


def agent_stub(enabled=True, pinned=False, child=False, role="brain"):
    from alice.agent import Agent

    a = Agent.__new__(Agent)
    a.config = NS(data={"routing": {"enabled": enabled, "strong": ["cliproxy:claude-opus-5-5"]}})
    a.router = make_router({"brain": ["chatgpt:terra"], "fast": ["groq:qwen"]})
    if pinned:
        a.router.role_overrides["brain"] = ["gemini:flash"]
    a.is_child, a.role = child, role
    a.messages = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "Önceki cevap"}]
    return a


def test_agent_routes_strong_turn(monkeypatch):
    seen = {}

    def fake_route(config, text, prev, log=None):
        seen["prev"] = prev
        return jev_route.Route("strong", "kod işi", {"code": 0.9})

    monkeypatch.setattr(jev_route, "route", fake_route)
    a = agent_stub()
    assert a._route_role("bu kodu düzelt") == "route-strong"  # noqa: SLF001
    assert a.router.role_overrides["route-strong"] == ["cliproxy:claude-opus-5-5", "chatgpt:terra"]
    assert seen["prev"] == "Önceki cevap"


def test_agent_leaves_pinned_child_voice_and_disabled_alone(monkeypatch):
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: jev_route.Route("strong", "kod işi"))
    for stub in (agent_stub(pinned=True), agent_stub(child=True), agent_stub(role="voice"), agent_stub(enabled=False)):
        assert stub._route_role("bu kodu düzelt") is None  # noqa: SLF001


def test_agent_normal_tier_keeps_brain(monkeypatch):
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: jev_route.Route("normal", "orta"))
    assert agent_stub()._route_role("React ile Vue farkı") is None  # noqa: SLF001


def test_hidden_models_are_skipped_by_routing():
    router = make_router({"brain": ["chatgpt:terra"], "fast": []})
    router.is_hidden = lambda ref: ref.startswith("cliproxy:claude-")
    cfg = NS(data={"routing": {"strong": ["cliproxy:claude-opus-5-5", "chatgpt:terra"]}})
    assert jev_route.tier_chain(router, cfg, "strong") == ["chatgpt:terra"]


def warm_stub(prev_role="brain", tokens=20000, ago=30.0, **routing):
    import time

    a = agent_stub()
    a.config.data["routing"].update(routing)
    a._last_turn_role = prev_role  # noqa: SLF001
    a._last_call_at = time.monotonic() - ago  # noqa: SLF001
    a.context_tokens = lambda: tokens
    return a


def test_warm_cache_keeps_easy_message_on_heavy_model(monkeypatch):
    calls = []
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: calls.append(1) or jev_route.Route("light", "sohbet"))
    assert warm_stub()._route_role("teşekkürler") is None  # noqa: SLF001 - stayed on brain
    assert calls  # strong tier starts on a different model: Jev was still asked
    assert agent_stub()._route_role("teşekkürler") == "route-light"  # noqa: SLF001 - first turn: to the light model


def test_cold_short_or_disabled_cache_routes_light(monkeypatch):
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: jev_route.Route("light", "sohbet"))
    assert warm_stub(tokens=1000)._route_role("selam") == "route-light"  # noqa: SLF001 - short context
    assert warm_stub(ago=600)._route_role("selam") == "route-light"  # noqa: SLF001 - cache went cold (5 min)
    assert warm_stub(cache_sticky=False)._route_role("selam") == "route-light"  # noqa: SLF001
    assert warm_stub(prev_role="route-light")._route_role("selam") == "route-light"  # noqa: SLF001


def test_warm_cache_still_upgrades_and_skips_jev_when_pointless(monkeypatch):
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: jev_route.Route("strong", "kod işi"))
    a = warm_stub()
    assert a._route_role("bu kodu düzelt") == "route-strong"  # noqa: SLF001 - upgrading is allowed
    a._last_turn_role = "route-strong"  # noqa: SLF001
    monkeypatch.setattr(jev_route, "route", lambda *a, **k: (_ for _ in ()).throw(AssertionError("jev çağrıldı")))
    assert a._route_role("tamam devam") == "route-strong"  # noqa: SLF001 - same model: Jev was not asked
