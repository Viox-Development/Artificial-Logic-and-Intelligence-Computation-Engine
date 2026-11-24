# Architecture

Written from the "Mimari" and "Jev hakkında net bilgi" sections of the Turkish guide
([docs/README.tr.md](../README.tr.md)) and checked against `alice/reflex/`, `alice/decider.py` and
`alice/providers/router.py`. The reflex steps here follow the current screen agent (`alice/reflex/agent.py`), which
asks Jev one question at a time instead of a single combined question.

```
                 ┌──────────────────────── ALICE ────────────────────────┐
  you ─► CLI ───►│ Agent (System 2)                                       │
   or app        │   brain model ⇄ tools (files, shell, screen, UIA,      │
                 │                 browser, web, system, subagents)       │
                 │        │                                               │
                 │        └─► reflex tool ──► screen agent (System 1)     │
                 │              observe → item list → Decider → act       │
                 │                                   │                    │
                 │                    Jev ──(if not)──► fast LLM (JSON)   │
                 │                                                        │
                 │ Router: a provider chain per role + automatic fallback │
                 └────────────────────────────────────────────────────────┘
```

## Two systems

The big model (brain) plans, writes code and decides. But for jobs like "go to Settings, click Privacy, tick that
box", asking the big model about every click is slow and burns free-tier quota. So the brain hands such sub-goals to
the `reflex` tool:

1. The screen is read into a numbered list of items with IDs and positions (the DOM in a browser, UI Automation on
   Windows, AT-SPI on Linux).
2. A System One model is asked short typed questions, one per request: what kind of action, which item, which key,
   whether the goal is done, and whether what to work on is missing from the list.
3. The chosen action is applied; if text has to be typed, it comes from the given inputs or the fast LLM.
4. After every action ALICE checks whether the screen changed; an action that did nothing is not repeated.
5. On low confidence, a repeated action or being stuck, it first consults the brain briefly with only the goal and
   the screen; if that doesn't help, control returns to the brain.

A goal with several parts is first split into short steps, each verified on its own. Every step is written to
`~/.alice/reflex-trace.jsonl`. This is the same idea as Cua's `jev-use` recipe: Jev is not an agent but a decision
component.

## The router

Each role (brain, fast, vision, voice) has an ordered chain of `provider:model` entries, and a request goes to the
first suitable one. Depending on the error: a rate limit cools the model down briefly (retry-after or 20 s); a daily
quota closes it until the provider's quota resets (saved to `~/.alice/cooldowns.json`, otherwise one hour); a wrong
key closes the provider for the session; a model removed from the catalog is closed; a request too large for a
provider (like Groq's 8K tokens per minute) skips that provider for that request only; server and network errors get
30 seconds, doubling on repeated errors up to 6 hours. So ALICE keeps working when a provider goes down. See
[models.md](models.md) for the providers.

## About Jev

Jev (TypeSafe AI, September 2026) is a System One model that makes typed decisions instead of generating text. **It
has no free tier**: $0.042 per million input tokens, output free, currently with an early access list. In practice
that is very cheap (a reflex step is about 1-2K tokens, so millions of decisions cost a few dollars), but ALICE is set
up to work without paying:

- With `TYPESAFE_API_KEY`, the decision engine is Jev (REST `POST /v1/systemone`, Choice / Noul / Score / Box
  questions).
- Because TypeSafe paused new sign-ups, Jev is also reached **through OpenRouter** (`~typesafe/jev-latest`, the same
  `OPENROUTER_API_KEY`). There is no `:free` version there either, so the account needs credit; without credit
  OpenRouter returns 402, and ALICE skips Jev for an hour and uses the fallback.
- Without Jev, or if it fails, the same question format goes as JSON to the free fast model in the `fast` role (Groq
  gpt-oss-20b with `reasoning_effort=low`).

So reflex mode works without any key, just slower: Jev is reported at about 100-350 ms, the Groq fallback at a few
hundred ms to 1-2 s per step (`alice doctor` prints the decision time). If you get Jev access, one key line is enough.
Score questions are sent to Jev as Choice questions, because that is the format the official documentation describes
most clearly.

The decision chain is set in `[decider] chain` (default `["jev", "jev-openrouter", "llm:fast"]`).
