"""Automatic model routing with Jev: which model tier for each message?

The measured ranking produced a single "best model" list; both heavy work and "naber" (Turkish for "what's up")
messages tried the same models, and strong models that come with the subscription (Opus 5.5 on the Claude Code proxy)
were never queued, so their quota would not be spent. Here Jev (TypeSafe System One) gives a few small judgments about
the message; the code decides which tier to use (truth table below):

  strong  writing/fixing code, deep thinking or multi-step computer work -> [routing] strong + brain chain
  light   chat or a short question, at most one task on the computer      -> fast chain + brain chain
  normal  everything else, and every case where Jev cannot be reached      -> brain chain (today's behavior)

The questions, criteria and thresholds are only in this file. If the Jev call fails, that does not count as an answer:
it falls back to "normal" and is written to the event log.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from alice.decider import Decision, JevEngine, Noul, Score

ASSISTANT = ("ALICE: the user's personal assistant on their Windows PC. It chats in Turkish and can use tools "
             "on the computer (open apps and websites, play music, read and edit files, run commands).")

QUESTIONS: dict[str, Any] = {
    "code": Noul(
        "Does `request` ask the assistant to write, change, fix or review program code?",
        {"true": "The user wants code written, a script changed, or a bug in a program found and fixed.",
         "false": "The user wants something that is not programming, such as opening a website, chatting, or a "
                  "general question."}),
    "thinking": Score(
        "How much careful thinking does a good answer to `request` need?",
        {1: "A greeting, thanks, or small talk.",
         2: "A quick fact or a short everyday answer.",
         3: "Explaining or comparing a known topic, such as how a tool works or how two products differ.",
         4: "Making a plan or a decision that depends on the user's own situation, deep analysis, hard math, "
            "or working out a tricky problem."}),
    "steps": Score(
        "How many separate actions on the computer does the assistant need to finish `request`?",
        {1: "No computer action; an answer in words is enough.",
         2: "One simple action, such as opening one app, one website, or one song.",
         3: "A few actions in a row, such as finding a file and then editing it.",
         4: "A long task with many actions, such as setting up a project or researching and writing a report."}),
    "chat": Noul(
        "Is `request` small talk with the assistant?",
        {"true": "A greeting, thanks, a joke, or asking how the assistant is.",
         "false": "The user asks for information or for something to be done."}),
}

# Thresholds of the truth table (Noul: above 0.7 is yes, below 0.3 is no; Score: expected level 1-4).
YES, NO = 0.7, 0.3
STRONG_THINKING = 3.2   # strong model if it needs more thinking than this
STRONG_STEPS = 3.2      # strong model if it has more steps than this
# Level 2 ("short fact / everyday answer") and 2 ("one simple task") fit the light model:
# thresholds sit between 2 and 3.
# (The first try used 1.8/2.3; single-step tasks such as "Spotify'ı aç" and "sesi kıs" were
# needlessly sent to the brain.)
LIGHT_THINKING = 2.5    # light model if it needs less thinking than this
LIGHT_STEPS = 2.5       # at most one simple task

TIMEOUT = 4.0  # sec: routing must not delay the reply; if exceeded, "normal"


@dataclass
class Route:
    tier: str  # strong | light | normal
    reason: str
    signals: dict[str, float] = field(default_factory=dict)
    latency: float = 0.0


def build_state(request: str, previous_reply: str = "") -> dict[str, Any]:
    state: dict[str, Any] = {"assistant": ASSISTANT, "request": request[:4000]}
    if previous_reply.strip():
        # Short replies like "Evet, yap" (Yes, do it) only make sense together with the previous reply.
        state["previous_reply"] = previous_reply.strip()[:800]
    return state


def decide_tier(code: float, thinking: float, steps: float, chat: float) -> Route:
    """Tier from Jev's judgments. Every combination has a result; anything in between is "normal"."""
    signals = {"code": round(code, 2), "thinking": round(thinking, 2), "steps": round(steps, 2),
               "chat": round(chat, 2)}
    if code >= YES:
        return Route("strong", "code job", signals)
    if thinking >= STRONG_THINKING:
        return Route("strong", "deep thinking", signals)
    if steps >= STRONG_STEPS:
        return Route("strong", "multi-step job", signals)
    if code <= NO and steps <= LIGHT_STEPS and (chat >= YES or thinking <= LIGHT_THINKING):
        return Route("light", "chat" if chat >= YES else "short question", signals)
    return Route("normal", "medium", signals)


def _signals(d: Decision) -> tuple[float, float, float, float]:
    return (float(d["code"].value), float(d["thinking"].value), float(d["steps"].value), float(d["chat"].value))


_engine: JevEngine | None = None
_broken_until = 0.0


def _get_engine(config) -> JevEngine | None:
    global _engine
    if _engine is None:
        dc = config.decider
        key = os.environ.get(dc.get("jev_api_key_env", "TYPESAFE_API_KEY"))
        if not key:
            return None
        import httpx

        _engine = JevEngine(dc.get("jev_url", "https://api.typesafe.ai/v1/systemone"),
                            dc.get("jev_model", "jev-latest"), key, name="jev-route",
                            client=httpx.Client(timeout=httpx.Timeout(TIMEOUT, connect=2.0)))
    return _engine


def route(config, request: str, previous_reply: str = "", engine: Any = None,
          log: Callable[[str], None] = lambda s: None) -> Route:
    """The message's tier. If Jev is missing or fails, "normal" (today's brain chain)."""
    global _broken_until
    if not request.strip():
        return Route("normal", "empty message")
    eng = engine or _get_engine(config)
    if eng is None:
        return Route("normal", "no TypeSafe key")
    if engine is None and time.monotonic() < _broken_until:
        return Route("normal", "jev temporarily off")
    t0 = time.monotonic()
    try:
        d = eng.decide(build_state(request, previous_reply), QUESTIONS)
        r = decide_tier(*_signals(d))
    except Exception as e:  # noqa: BLE001 - an error is not an answer: fall back to today's behavior and leave a trace
        if engine is None:
            _broken_until = time.monotonic() + float(getattr(e, "cooldown", 60.0) or 60.0)
        log(f"routing: jev failed ({str(e)[:160]}), normal chain")
        return Route("normal", "jev error")
    r.latency = time.monotonic() - t0
    return r


def _dedupe(refs: list[str]) -> list[str]:
    out: list[str] = []
    for ref in refs:
        if ref and ref != "auto" and ref not in out:
            out.append(ref)
    return out


def voice_chain(router, config, request: str, fast: list[str],
                log: Callable[[str], None] = lambda s: None) -> tuple[list[str], Route]:
    """Model order for think in voice conversations. In voice, speed comes first: only the "strong" tier changes,
    and there [routing] voice_strong (default Sonnet 5.5) goes ahead of the fast models. Everything else uses the
    `fast` order. If routing is off or Jev cannot be reached, `fast` is returned as is."""
    rc = config.data.get("routing", {}) or {}
    if not rc.get("enabled") or not rc.get("voice", True):
        return fast, Route("normal", "routing off")
    r = route(config, request, log=log)
    if r.tier != "strong":
        return fast, r
    head = [str(x) for x in rc.get("voice_strong") or [] if not _hidden(router, str(x))]
    return _dedupe(head + fast) or fast, r


def tier_chain(router, config, tier: str) -> list[str]:
    """Model order for a tier: its own models first, then the brain chain (no task is left without a fallback)."""
    rc = config.data.get("routing", {}) or {}
    brain = router.chain("brain")
    if tier == "strong":
        head = [str(r) for r in rc.get("strong") or []]
    elif tier == "light":
        head = [str(r) for r in rc.get("light") or []] or router.chain("fast")
    else:
        return brain
    # Models or groups hidden from the UI (e.g. "cliproxy:claude-*") are not used for routing either.
    return [r for r in _dedupe(head + brain) if not _hidden(router, r)]


def _hidden(router, ref: str) -> bool:
    check = getattr(router, "is_hidden", None)
    try:
        return bool(check(ref)) if callable(check) else False
    except Exception:  # noqa: BLE001
        return False
