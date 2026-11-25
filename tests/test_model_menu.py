"""The model menu shows the newest models a user can chat with, and the effort levels include ultracode."""

from __future__ import annotations

import time

import pytest

from alice.providers import menu, ranking, thinking


def _entry(name, *models, hidden=(), native=False, quality=None):
    return {"name": name, "enabled": True, "hidden": False, "native": native, "models": [
        {"ref": f"{name}:{m}", "model": m, "ok": True, "hidden": m in hidden, "quality": (quality or {}).get(m, 50)}
        for m in models]}


def _main(entry):
    return {r["model"] for r in entry["models"] if r["main"]}


def test_newest_model_of_each_family():
    e = _entry("cliproxy-claude", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-20250514",
               "claude-opus-4-6-thinking", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5",
               "claude-3-7-sonnet-20250219", "claude-haiku-5-5", "claude-haiku-4-5-20251001", native=True)
    menu.mark([e])
    assert _main(e) == {"claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5", "claude-haiku-5-5"}


def test_variants_are_the_same_family():
    assert menu.family("kimi-k3.1-pro-highspeed")[0] == menu.family("kimi-k3.1-pro")[0]
    assert menu.family("gemini-3.8-flash-high")[:2] == ("gemini flash", (3.8,))
    assert menu.family("z-ai/glm-5.3:free")[0] == "glm"
    assert menu.family("gpt-oss-120b")[0] != menu.family("gpt-oss-20b")[0]
    e = _entry("cliproxy:kimi-*", "kimi-k3", "kimi-k3-256k", "kimi-k2.8")
    menu.mark([e])
    assert _main(e) == {"kimi-k3"}


@pytest.mark.parametrize("model", ["grok-imagine-video", "gemini-3.1-flash-image", "nvidia/nvclip", "fastino/gliner2-pii",
                                   "nvidia/riva-translate-4b", "gemini-3.5-live-translate-preview", "gpt-image-2"])
def test_models_that_cannot_chat(model):
    assert not menu.chat_model(model)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "gpt-6-chat-thinking", "deepseek-v4.1-flash", "olive-2", "grok-4.7"])
def test_chat_models_stay(model):
    assert menu.chat_model(model) and not menu.disguised(model)


def test_proxy_models_under_a_made_up_claude_name():
    assert menu.disguised("claude-fable-5-dd-7.4-korg") and menu.disguised("claude-fable-5-dd-egami-enigami-korg")
    assert not menu.disguised("claude-fable-5-1")


def test_same_claude_model_is_shown_once():
    native = _entry("cliproxy-claude", "claude-opus-5-5", native=True)
    compat = _entry("cliproxy:claude-*", "claude-opus-5-5", "claude-sonnet-5-5")
    menu.mark([compat, native])
    assert _main(native) == {"claude-opus-5-5"} and _main(compat) == {"claude-sonnet-5-5"}
    native["enabled"] = False
    menu.mark([compat, native])
    assert _main(compat) == {"claude-opus-5-5", "claude-sonnet-5-5"}


def test_few_per_provider_hidden_left_out_pinned_always_there():
    names = [f"m{c}-1" for c in "abcdef"]
    e = _entry("nvidia", *names, hidden=("ma-1",), quality={n: 90 - i for i, n in enumerate(names)})
    menu.mark([e], keep={"nvidia:mf-1"})
    assert _main(e) == {"mb-1", "mc-1", "md-1", "mf-1"}


# ------------------------------------------------------------------------------------------------ ultracode
def test_ultracode_is_xhigh_where_the_model_has_it():
    assert thinking.ULTRACODE in thinking.levels_for("anthropic", "claude-opus-5-5")[0]
    assert thinking.ULTRACODE in thinking.levels_for("cliproxy", "gpt-6.1-terra")[0]
    assert thinking.params("anthropic", "claude-opus-5-5", "ultracode") == {"reasoning_effort": "xhigh"}
    for provider, model in (("anthropic", "claude-haiku-5-5"), ("gemini", "gemini-3.8-flash"), ("openrouter", "x/y")):
        levels = thinking.levels_for(provider, model)[0]
        assert thinking.ULTRACODE not in levels
        assert thinking.fit("ultracode", levels) in ("high", "max")
    assert thinking.fit("ultracode", ["low", "ultracode"]) == "ultracode"


def test_ultracode_note_goes_to_the_model_not_to_the_screen(workdir):
    from tests.test_agent import make_agent

    agent, _, _ = make_agent(["tamam", "tamam", "tamam"], workdir)
    agent.run_turn("testleri düzelt")
    assert "[ultracode:" not in agent.messages[-2]["content"]
    agent.run_turn("ultracode testleri düzelt")
    msg = agent.messages[-2]
    assert "[ultracode:" in msg["content"] and msg["_display"] == "ultracode testleri düzelt"
    agent.router.thinking_overrides["brain"] = "ultracode"
    agent.run_turn("testleri düzelt")
    assert "[ultracode:" in agent.messages[-2]["content"]


# ------------------------------------------------------------------------------------------------ measurement file
def test_background_measurement_writes_where_it_was_started(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ranking, "refresh", lambda router, path=None: seen.append(path) or {"chains": {}})
    monkeypatch.setenv("ALICE_HOME", str(tmp_path / "first"))

    class R:
        class config:  # noqa: N801
            roles = {"brain": ["auto"]}
            agent: dict = {}

        def reload_auto(self, cache):
            pass

    assert ranking.refresh_in_background(R(), max_age=0)
    monkeypatch.setenv("ALICE_HOME", str(tmp_path / "second"))
    end = time.time() + 5
    while not seen and time.time() < end:
        time.sleep(0.02)
    assert seen == [tmp_path / "first" / "models.json"]
