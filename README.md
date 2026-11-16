# ALICE

[![CI](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/ci.yml) [![CodeQL](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/codeql.yml/badge.svg)](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/codeql.yml) [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.10%20%7C%203.12%20%7C%203.13-blue) ![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey) [![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**Artificial Logic and Intelligence Computation Engine.** An AI agent that lives on your computer, in the spirit of
Claude Code and Codex, but not tied to one paid model. It chains free API tiers (and your own keys or subscriptions)
with automatic failover, talks to you by voice, uses your mouse, keyboard and browser, remembers what matters, and
can run whole projects with a team of autonomous agents.

![ALICE desktop app](docs/app-koyu.png)

> ALICE started as a Turkish-speaking assistant. The interface and prompts are being translated to English;
> the full, detailed guide is currently in Turkish: [docs/README.tr.md](docs/README.tr.md).

## What it can do

- **Code and files:** read, write, edit, and search code bases (`grep`, `glob`), apply patches, show colored diffs.
- **Shell:** run PowerShell and bash commands, start servers in the background and watch their output.
- **Computer use:** take screenshots, move the mouse, type (including non-ASCII text), and click Windows controls by
  their UI Automation IDs instead of guessing pixels.
- **Browser:** drive a real browser with a persistent profile, so sites you logged into once stay logged in.
- **Reflex mode:** hand small UI goals ("open Settings, go to Privacy, tick this box") to a fast decision model that
  acts in ~100-500 ms per step, and fall back to the main model when unsure.
- **Projects:** split big jobs across autonomous agent teams with their own manager, parallel workers, messaging,
  review councils, and a pull request at the end.
- **Voice:** wake word ("Hey Alice"), speaker recognition, live conversation, barge-in and "stop" that really stops.
- **Background core:** watches your PC (CPU, GPU, RAM, disk, apps), runs rules and scheduled tasks, switches modes
  (focus, gaming, quiet hours), and notifies you on desktop or phone.
- **Memory:** notes, past conversations, and follow-ups across sessions, all editable from the app.
- **Connections:** Spotify, Steam, Discord, YouTube, Gmail, GitHub, MCP servers, and any API you describe in TOML.
- **Phone app:** talk to ALICE, get notifications, and approve actions from Android.
- **Flight sims:** live threat callouts, checklists, and debriefs for DCS and War Thunder.
- **Self-updating:** installs new releases on its own, with a smoke test and automatic rollback.

Two interfaces share the same agent: a desktop app and a terminal REPL, both with streaming answers, diffs, a status
bar, and multi-line input.

## How it works

```
                 ┌──────────────────────── ALICE ────────────────────────┐
  you ──► CLI ──►│ Agent (System 2)                                       │
                 │   brain model ⇄ tools (files, shell, screen, UIA,      │
                 │                 browser, web, system, sub-agents)      │
                 │        │                                               │
                 │        └─► reflex tool ──► ReflexLoop (System 1)       │
                 │              observe → candidates → Decider → act      │
                 │                                   │                    │
                 │                    Jev ──(or)──► fast LLM (JSON)       │
                 │                                                        │
                 │ Router: a provider chain per role + automatic failover │
                 └────────────────────────────────────────────────────────┘
```

**Two systems.** A large "brain" model plans, writes code, and makes decisions. Clicking through a settings page step
by step with that model is slow and burns free-tier quota, so the brain delegates UI sub-goals to the `reflex` tool:
the screen becomes a list of candidates with IDs (DOM in the browser, UI Automation on Windows), one fast decision call
picks the next action, and low confidence or repeated actions hand control back to the brain.

**No single provider.** The router keeps a chain of models for each role (`brain`, `fast`, `voice`, `vision`). A 429
puts that model on cooldown and the request moves to the next one; an exhausted daily quota skips the model for an
hour; a bad key skips the provider for the session. If one provider goes down, ALICE keeps working.

| Provider | What is free | Role in ALICE |
|---|---|---|
| Google Gemini (AI Studio) | Flash and Flash-Lite, 1M context, vision | brain and vision |
| OpenRouter | `:free` models with daily limits | brain fallback |
| NVIDIA NIM | Developer program, large open models | brain fallback, coding |
| Groq | Very fast, small token limits | fast role, reflex decisions |
| Mistral, SambaNova, Z.ai, Cohere, Cerebras, Cloudflare Workers AI | Free tiers of varying size | fallbacks |
| Anthropic, Vercel AI Gateway | Paid or credit-based | only when you pick them |
| Ollama (local) | Unlimited, offline | last resort in every chain |

Free tiers change often; `alice models --write` scans the live catalogs and updates the chains.

## Install (Windows)

You need Python 3.10 or newer ("Add to PATH" checked).

```powershell
git clone https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine
cd Artificial-Logic-and-Intelligence-Computation-Engine
powershell -ExecutionPolicy Bypass -File install.ps1
```

The script installs Python 3.11 with winget if needed, creates `.venv`, installs the `computer`, `browser`, `app`,
`core`, and `voice` extras plus local Whisper, then runs `alice init` and `alice shortcut`. A prebuilt
`ALICE-Setup.exe` and the Android APK are on the [Releases](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/releases) page.

Manual install (also Linux and macOS):

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .\.venv\Scripts\activate
pip install -e ".[all]"
alice init
```

Optional extras (`pip install -e ".[extra1,extra2]"`): `computer`, `browser`, `app`, `core`, `voice`,
`voice-local`, `voice-xtts`, `all`, `dev`. If a command needs a missing extra, ALICE tells you which one to install.

### API keys

`alice init` creates `~/.alice/.env`. Fill in at least one key; the more you add, the more resilient the chain:

| Variable | Where to get it |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey (most important) |
| `GROQ_API_KEY` | https://console.groq.com/keys (reflex and quick tasks) |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys |
| `NVIDIA_API_KEY` | https://build.nvidia.com |
| `MISTRAL_API_KEY`, `SAMBANOVA_API_KEY`, `ZAI_API_KEY`, `COHERE_API_KEY`, `CEREBRAS_API_KEY` | each provider's console |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com (paid, used only when selected) |
| `GITHUB_TOKEN` | optional, for git and GitHub tools |

You can also paste keys in the app under **Connections → Keys**; they are saved to `.env` and used immediately.
Several keys per provider are supported and rotated automatically. Any OpenAI-compatible server (LM Studio, vLLM,
LiteLLM) can be added from the same page.

**Subscriptions through a local proxy (optional).** ALICE can use [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
to route your own Claude, ChatGPT, or other subscriptions through an OpenAI-compatible endpoint on your machine.
It is off the automatic chain and only used when you pick its models. Using subscriptions this way may violate the
providers' terms of service and can get accounts restricted; that choice and its risk are yours.

### First run

```bash
alice doctor          # test every provider, the decision engine, and missing packages
alice app             # open the desktop app
alice                 # or start the terminal REPL
```

## Using it

```bash
alice "find and fix the jump bug in PlayerController"
alice -p "list the 5 processes using the most RAM"    # one-shot, prints and exits
alice --mode plan --cwd ~/projects/game                # permission mode and working folder
alice --resume                                         # continue the last session
alice core install                                     # start the background core with Windows
alice rules add "tell me if the GPU stays above 80°C for 5 minutes"
alice tasks add "every Sunday night zip D:\Unity into E:\Backup"
alice update --check                                   # is a new release available?
```

In the REPL: `/help`, `/plan`, `/mode plan|ask|auto|yolo` (or Shift+Tab), `/model`, `/think`, `/update`.

## Safety

- **Permission modes:** `plan` only reads; `ask` confirms everything except reading; `auto` (default) allows edits,
  commands, and input but still asks for dangerous patterns (`rm -r`, registry, shutdown, `git push --force`,
  `curl | sh`, system folders); `yolo` asks nothing.
- **Messages to other people** (Discord, WhatsApp, email, DMs) always need approval outside `yolo`, and everything
  sent on your behalf is logged to `~/.alice/actions.jsonl` (`alice log`).
- **Sandbox:** commands can run with a low-integrity token on Windows, bubblewrap on Linux, or sandbox-exec on macOS.
  Secrets in `~/.alice` are hidden from it.
- **Git:** ALICE's own git calls ignore hooks, filters, and credential helpers planted in a repository's config, and
  it refuses to commit or push files that look like keys or `.env` files.
- **Emergency stop:** `Ctrl+C` stops the current task; moving the mouse to the top-left corner stops input control.

## Updates

ALICE updates itself from GitHub. By default it follows **releases** (`vX.Y.Z` tags), not every commit on `main`,
so a merged pull request only reaches users after a maintainer publishes a release. Each update runs a smoke test and
rolls back automatically if the new version fails to start.

```toml
[update]
mode = "auto"         # auto | notify | off
channel = "stable"    # stable: releases only | main: every commit (for developers)
require_green_ci = true
```

## Configuration

Everything lives in `~/.alice/` (`config.toml`, `.env`, sessions, memory, projects); set `ALICE_HOME` to move it.
A project's `ALICE.md` (or `CLAUDE.md` / `AGENTS.md`) is read as project instructions.

```toml
[agent]
permission_mode = "auto"

[roles]
brain  = ["auto", "ollama:qwen3:8b"]   # "auto": measured free models from providers you have keys for
fast   = ["auto", "ollama:qwen3:8b"]
vision = ["auto", "gemini:gemini-flash-lite-latest"]

[thinking]
default = "low"   # off | low | medium | high
```

More: [connections](docs/CONNECT.md), [plugins](docs/PLUGINS.md), [tool style](docs/TOOL-STYLE.md), and the
[full guide (Turkish)](docs/README.tr.md).

## Known limitations

- Games such as DCS and War Thunder are invisible to UI Automation; ALICE uses screenshots and key presses there.
- Free models are weaker at tool calling than top paid models; long, complex tasks work best with Gemini Flash or a
  model you choose.
- UI Automation is Windows only; on Linux and macOS ALICE uses the browser and the `computer` tool.
- Parts of the interface and prompts are still in Turkish.

## Contributing

Contributions are welcome! Read [CONTRIBUTING.md](CONTRIBUTING.md), look for
[good first issues](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/labels/good%20first%20issue),
and report security problems privately as described in [SECURITY.md](SECURITY.md).

```bash
pip install -e ".[dev]"
ruff check alice tests
python -m pytest -q
```

## License

ALICE is licensed under the [GNU Affero General Public License v3.0](LICENSE). Contributions are accepted under
the [Contributor License Agreement](CLA.md).
