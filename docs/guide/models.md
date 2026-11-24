# Choosing models

Written from the "Model seçimi: neden bu sıra" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and
checked against the `[providers]` and `[roles]` sections of `alice/config.py` and `alice/providers/router.py`. Free
tiers change often and sources can disagree; the table explains the reasoning as of October 2026. Check each
provider's own dashboard for exact limits.

| Provider | What it gives for free | Role in ALICE |
|---|---|---|
| **Google Gemini (AI Studio)** | Flash and Flash-Lite free, 1M context, images. Pro models left the free tier in April 2026. Free-tier data may be used for product development. | **brain #1, vision #1.** Large context and images in one model: the best free option for an agent |
| **OpenRouter** | `:free` models at 20 requests per minute / 50 per day; 1000 per day after a one-time $10 top-up. The catalog changes every few months (gpt-oss-120b, Poolside Laguna, North Mini Code...) | brain fallback, `openrouter/free` as the last-resort router |
| **NVIDIA NIM** | About 40 requests per minute (per account) with the Developer Program; big models like qwen3-coder-480b and deepseek | brain fallback, strong coding model |
| **Groq** | 1000 requests per day but 8K tokens per minute and 200K per day. Llama and Kimi were removed, gpt-oss stays. Very fast | **fast #1.** Ideal for short jobs and Jev emulation, not for large contexts |
| **Cerebras** | Time-limited credit according to recent reports; a lasting free tier is unclear | fallback |
| **Mistral (La Plateforme)** | The "Experiment" plan is free, needs phone verification, low requests per second | brain/fast fallback |
| **SambaNova** | Free tier, fast large open models | fallback |
| **Z.ai** | `glm-4.7-flash`, `glm-4.5-flash` and `glm-4.6v-flash` (images) are free. ALICE only puts these three in the chain and never moves to paid GLM models by itself | brain/vision fallback |
| **Cohere** | Free trial key (limited requests per month, no commercial use). The Command A family, with tool calling | brain fallback |
| **Cloudflare Workers AI** | 10,000 "neurons" per day for free: Kimi K2.5, GLM 4.7 Flash, Llama 3.3 70B, Gemma 4; about 150-300K tokens per day on a big model. The URL contains the account ID, so `CLOUDFLARE_ACCOUNT_ID` is needed too | brain fallback |
| **Vercel AI Gateway** | $5 of credit per account every 30 days; Claude, GPT and Gemini with one key. It spends credit, so it is **not** added to the chains automatically, only by hand: `/model brain vercel:anthropic/claude-sonnet-5.5` | strong model chosen by hand |
| **Anthropic (Claude API)** | The official API, pay as you go (console.anthropic.com): Claude Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1, through the official `anthropic` package (`pip install anthropic`). It is paid, so it is **not** added automatically: pick it from the model menu or with `/model brain anthropic:claude-opus-5-5` | strong model chosen by hand |
| **CLIProxyAPI (subscriptions)** | Claude Pro/Max, ChatGPT (Codex) and other subscriptions through a local proxy; spends your subscription quota | chosen by hand; also the ChatGPT Realtime voice backend. See [keys.md](keys.md) |
| **Ollama (local)** | Unlimited, offline, depends on your GPU | the last resort at the end of every chain |

With a key in `.env`, **Mistral** and **SambaNova** are measured and added to the chains automatically as well. In the
default `config.toml` the brain chain is `auto` (the best measured free models), with local Ollama at the end.

## Not relying on one provider

The router keeps a chain per role. On a 429 the model is cooled down and the next one is used; when a daily quota runs
out it waits until the quota resets (an hour if the reset time is unknown); a wrong key closes the provider for the
session; a model removed from the catalog is closed. If a request exceeds a provider's token limit (like Groq's 8K
tokens per minute), that provider is skipped for that request. So ALICE keeps working when a provider goes down. The
details are in [architecture.md](architecture.md).

**Gemini's "You have reached a rate limit. Set up billing..." error** means the free tier is used up. ALICE treats it
as a quota, not a 20-second rate limit: Gemini is skipped and requests go to the next provider. Live voice
conversation (Gemini Live) switches to the regular voice path (speech-to-text + model + speech) for an hour on the
same error and says so once. That is why filling in at least two keys besides Gemini (Groq, Z.ai, Mistral, Cerebras)
really makes a difference.

GitHub Models was shut down on 30 July 2026, so it isn't listed. Hugging Face Inference Providers wasn't added
either: a free account gets $0.10 of credit a month, which runs out after a few dozen requests.

When the catalogs change, `alice models --write` scans the live `/models` endpoints and updates the chains.
