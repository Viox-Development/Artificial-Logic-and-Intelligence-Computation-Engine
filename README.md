# ALICE

[![CI](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/ci.yml) [![CodeQL](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/codeql.yml/badge.svg)](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/actions/workflows/codeql.yml) [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.10%20%7C%203.12%20%7C%203.13-blue) ![Platform](https://img.shields.io/badge/platform-Windows%20(full)%20%7C%20Linux%20(partial)-lightgrey) [![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**Artificial Logic and Intelligence Computation Engine.** An AI agent that lives on your computer, in the spirit of
Claude Code and Codex, but not tied to one paid model. It chains free API tiers (and your own keys or subscriptions)
with automatic failover, talks to you by voice, uses your mouse, keyboard and browser, remembers what matters, and
can run whole projects with a team of autonomous agents.

https://github.com/user-attachments/assets/4b67b5c5-4148-49cb-9195-05c788c51b02

*ALICE fixes a bug and runs the tests, then keeps going on another model when the first one hits its rate limit.*

> ALICE started as a Turkish-speaking assistant. The interface and prompts are being translated to English;
> the full, detailed guide is currently in Turkish: [docs/README.tr.md](docs/README.tr.md).

## What it can do

ALICE ships with about 90 tools. The main groups:

**Coding agent**
- Read, write, and edit files, multi-file edits and patches, `grep`/`glob`, code intelligence, notebooks, and test runs.
- Shell commands with background processes you can watch, wait on, and stop.
- Git and GitHub: commits, branches, pull requests, CI status, and review comments. Watched pull requests are fixed
  automatically when CI turns red or a trusted review comment arrives.
- Plan mode, todo lists, sub-agents, custom agent definitions, skills, hooks, and MCP servers, in the style of
  Claude Code.

**Projects with agent teams**
- Big jobs run as projects: a manager plans the work, parallel agents build it in their own git worktrees, a review
  council (critic, advocate, checker, experts) votes on plans and results, and the project ends in a pull request.
- A preview tab serves and screenshots web projects so the council can check them like a user would.

**Using your computer**
- Screenshots, mouse, and keyboard, plus Windows UI Automation to click controls by ID instead of guessing pixels.
- A real browser with a persistent profile, and a browser extension for working in your own Chrome.
- **Reflex mode:** small UI goals ("open Settings, go to Privacy, tick this box") are handed to a fast decision model
  that acts in a few hundred milliseconds per step and gives control back to the main model when unsure.
- Windows, processes, clipboard, Wi-Fi, media, smart TVs, and Android phones over ADB.

**Voice**
- Wake word ("Hey Alice"), speaker recognition (it ignores other voices), and live conversation over Gemini Live or
  ChatGPT realtime, with a classic speech-to-text → model → text-to-speech fallback.
- Barge-in, and "stop" or "I wasn't talking to you" silences it immediately.
- Local Whisper and local text-to-speech options, so not every sentence goes to the cloud.

**Always on: the background core**
- Watches CPU, GPU, RAM, disks, and running apps, and warns about overheating, full disks, or memory leaks.
- Rules in plain language ("tell me if the GPU stays above 80°C for 5 minutes") and scheduled tasks
  ("every Sunday night back up D:\Unity").
- Modes (focus, gaming, quiet hours), feeds that watch Discord, YouTube and other services, a daily brief, and a
  daily self health report.
- **KAIROS:** a background agent that wakes itself up when it decides to, checks the situation, and acts.
- **Night cycle:** ALICE reads its own logs at night and fixes repeated mistakes, noisy announcements, and false
  wake-ups.

**Memory and awareness**
- Notes, past conversations, and follow-ups across sessions, with a page to review, edit, or delete what it knows.
- **Time machine** (off by default): text seen on screen goes into a local, encrypted, searchable timeline
  ("find the link someone shared on Discord last week").
- An on-screen HUD that draws arrows, cards, and highlights over games, the desktop, and the browser.

**Phone and remote access**
- Android app to talk to ALICE, get notifications, share permission prompts, and place calls.
- Smartwatch support (heart rate) and remote access from outside your home network through a relay.

**Connections**
- Gmail, GitHub, Spotify, Steam, Discord, YouTube, Blender, and any HTTP API you describe in TOML.
- Plugins that add their own tools.

**Flight sims**
- Live threat and missile callouts, checklists, kneeboard, debriefs, and coaching from your death patterns for
  War Thunder and DCS, plus voice commands for DCS wingmen.

**Self-updating**
- Installs new releases on its own, with a smoke test and automatic rollback if the new version fails to start.

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
| ChatGPT account | Sign in with your ChatGPT plan (free plans included) through the Codex login; no API key | only when you pick it |
| Anthropic, Vercel AI Gateway | Paid or credit-based | only when you pick them |
| Your own subscriptions (via CLIProxyAPI) | Claude, ChatGPT, and others through a local proxy | only when you pick them |
| Any OpenAI-compatible server | LM Studio, vLLM, LiteLLM, your own | measured and added automatically |
| Ollama (local) | Unlimited, offline | last resort in every chain |

By default the brain chain uses the best measured free models from the providers you have keys for, then local
Ollama. Free tiers change often; `alice models --write` scans the live catalogs and updates the chains.

## Install (Windows)

You need Python 3.10 or newer ("Add to PATH" checked).

```powershell
git clone https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine
cd Artificial-Logic-and-Intelligence-Computation-Engine
powershell -ExecutionPolicy Bypass -File install.ps1
```

The script installs Python 3.11 and Git with winget if they are missing, creates `.venv`, installs the `computer`,
`browser`, `app`, `core`, and `voice` extras plus local Whisper, downloads Chromium only if neither Chrome nor Edge is
installed, then runs `alice init` and `alice shortcut`. A prebuilt
`ALICE-Setup.exe` and the Android APK are attached to the [latest release](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/releases/latest).

Manual install (also Linux and macOS):

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .\.venv\Scripts\activate
pip install -e ".[all]"
alice init
```

Optional extras (`pip install -e ".[extra1,extra2]"`):

| Extra | Adds |
|---|---|
| `computer` | screenshots, mouse and keyboard, clipboard, windows, UI Automation |
| `browser` | Playwright browser control |
| `app` | native desktop window (pywebview); without it the app opens in Edge or Chrome app mode |
| `core` | tray icon for the background core |
| `voice` | microphone, push-to-talk, voice activity detection, Edge voices, live conversation |
| `voice-local` | local Whisper and openWakeWord ("Hey Alice") |
| `voice-xtts` | local XTTS v2 voice |
| `home` | smart home and TV connections |
| `claude` | official Anthropic SDK |
| `all` | `computer`, `browser`, `app`, `core`, `voice`, `home`, `claude` |
| `dev` | pytest, pytest-xdist, ruff |

If a command needs a missing extra, ALICE tells you which one to install instead of printing a traceback.

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

Other commands: `alice voice`, `alice phone`, `alice watch`, `alice connect`, `alice feeds`, `alice notes`,
`alice brief`, `alice report`, `alice log`, `alice hud`, `alice timeline`, `alice night`, `alice relay`,
`alice plugins`, `alice reflex`, `alice dcs`, `alice eval`. Every command accepts `--help`.

In the REPL: `/help`, `/plan`, `/mode plan|ask|auto|yolo` (or Shift+Tab), `/model`, `/think`, `/memory`, `/mcp`,
`/tools`, `/usage`, `/sessions`, `/resume`, `/rewind`, `/compact`, `/update`.

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
# "auto": the best measured free models from providers you have keys for
brain  = ["auto", "ollama:qwen3:8b"]
voice  = ["auto", "ollama:qwen3:8b"]
fast   = ["auto", "ollama:qwen3:8b"]
vision = ["auto", "gemini:gemini-flash-lite-latest"]

[thinking]
default = "low"   # off | low | medium | high
```

More: [connections](docs/CONNECT.md), [plugins](docs/PLUGINS.md), [tool style](docs/TOOL-STYLE.md), and the
[full guide (Turkish)](docs/README.tr.md).

## Platform support

ALICE is developed and used daily on **Windows**, where every feature works.

| | Windows | Linux | macOS |
|---|---|---|---|
| Agent, coding tools, shell, browser, web, memory, projects, providers | ✅ | ✅ (tested in CI) | untested |
| Voice, phone app chat and notifications, system monitoring and rules | ✅ | ✅ | untested |
| Command sandbox | low integrity | bubblewrap | sandbox-exec |
| UI elements by ID, reflex desktop observer, screen OCR | ✅ | ❌ (screenshots only) | ❌ |
| On-screen HUD, global shortcuts / quick bar | ✅ | ❌ | ❌ |
| PC control (lock, sleep, brightness), Wi-Fi, foreground app modes, now playing | ✅ | ❌ | ❌ |
| Time machine, Steam and flight-sim integration, installer | ✅ | ❌ | ❌ |

Bringing Linux and macOS closer to Windows is tracked in
[#239](https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/issues/239); most items
have their own issue and are good places to start contributing.

## Known limitations

- Games such as DCS and War Thunder are invisible to UI Automation; ALICE uses screenshots and key presses there.
- Free models are weaker at tool calling than top paid models; long, complex tasks work best with Gemini Flash or a
  model you choose.
- Several "control the computer" features are Windows-only for now (see Platform support).
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
