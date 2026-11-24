# Configuration and development

Written from the "Config", "Görsel desteği nasıl çalışıyor", "Bilinen sınırlar" and "Geliştirme" sections of the
Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked against `alice/config.py`, `alice/prompts.py` and
`alice/agent.py`. Several details there are out of date (compaction at 80%, notes in `memory.md`, no UI Automation on
Linux); this page describes the current behavior.

## config.toml

`~/.alice/config.toml` (`%USERPROFILE%\.alice\config.toml` on Windows). All of ALICE's files (config, `.env`,
sessions, memory, projects) live in this folder; set the `ALICE_HOME` environment variable to use another place.
`alice init --force` rewrites the files with the defaults. The important parts:

```toml
[agent]
permission_mode = "auto"
compact_at_tokens = "auto"   # "auto": summarize at 60% of the model's context window; or a number of tokens
keep_images = 2              # screenshots kept in the history
browser_headless = false

[roles]
# "auto": free models from the providers you have keys for, measured and ranked (alice models)
brain = ["auto", "ollama:qwen3:8b"]
voice = ["auto", "ollama:qwen3:8b"]
fast  = ["auto", "ollama:qwen3:8b"]
vision = ["auto", "gemini:gemini-flash-lite-latest"]

[thinking]
default = "low"              # off | low | medium | high | default, also per role

[decider]
chain = ["jev", "jev-openrouter", "llm:fast"]
min_confidence = 0.55        # reflex decisions below this go to the brain

[models."groq:openai/gpt-oss-20b"]
extra = { reasoning_effort = "low" }   # extra parameters added to the request
```

To add a new OpenAI-compatible provider, write `base_url`, `api_key_env`, `vision_models` and `max_input_tokens` under
`[providers.<name>]`. Keys and providers can also be added from Connections → Keys in the app
([keys.md](keys.md)).

## Instructions and notes

- An `ALICE.md` (or `CLAUDE.md` / `AGENTS.md`) in the project folder is read as that project's instructions;
  `~/.alice/ALICE.md` is read in every session. `/init` creates one for the current project.
- Notes saved with the `memory` tool are markdown files in `~/.alice/memory/` (and
  `~/.alice/projects/<repo>/memory/` for one repository); see [memory.md](memory.md).

## How image support works

When a screenshot is taken and the brain model in use supports images, the image goes to it directly. If it doesn't,
the model in the `vision` role is asked to describe the screen (what is on it, where the buttons are) and the brain
sees the text. This way strong coding models without image support can still work with the screen. Old screenshots are
dropped from the context automatically (`keep_images`).

## Known limitations

- Games (DCS, War Thunder) are invisible to UI Automation; there the `computer` tool uses screenshots and key presses,
  which is slower than reflex.
- Free models are not at Claude/GPT level at tool calling. The router sends broken JSON back to the model, which
  usually fixes it on the second try, but long and complex jobs go better on the stronger models.
- Jev's API is in early access; if field names change, `JevEngine._parse` in `alice/decider.py` is tolerant but may
  need an update. If Jev fails, ALICE falls back to an LLM automatically.
- UI elements by ID use UI Automation on Windows and AT-SPI on Linux; macOS has no backend yet and uses the browser and
  the `computer` tool. See the platform table in the [README](../../README.md#platform-support).

## Development

```bash
pip install -e ".[dev]"
ruff check alice tests
python -m pytest -q
```

The tests use fake providers to cover router fallbacks, the agent loop (tool calls, permission denials, image routing,
context compaction, interruption repair, subagents), the Jev request/answer format and the reflex loop. CI runs them
on Ubuntu, Windows and macOS. See [CONTRIBUTING.md](../../CONTRIBUTING.md).
