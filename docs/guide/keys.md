# API keys and model access

This page is the detailed version of the "Install" and "API keys" parts of the [README](../../README.md). It is
translated from the Turkish guide, [docs/README.tr.md](../README.tr.md).

## Where keys live

`alice init` creates the `.env` file in the ALICE folder: `C:\Users\<user>\.alice\.env` on Windows, `~/.alice/.env` on
Linux and macOS. The file is in `.gitignore` and never goes to GitHub. When an update adds a new provider, ALICE adds
its empty line (with a comment and a link) to this file at startup and tells you; it doesn't touch keys you filled in.

Fill in at least one key. The more you fill in, the more resilient the model chain is:

| Variable | Where to get it |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey (the most important one) |
| `GROQ_API_KEY` | https://console.groq.com/keys (reflex and quick jobs) |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys |
| `NVIDIA_API_KEY` | https://build.nvidia.com |
| `CEREBRAS_API_KEY` | https://cloud.cerebras.ai |
| `MISTRAL_API_KEY` | https://console.mistral.ai/api-keys (Experiment plan) |
| `SAMBANOVA_API_KEY` | https://cloud.sambanova.ai/apis |
| `ZAI_API_KEY` | https://z.ai/manage-apikey/apikey-list |
| `COHERE_API_KEY` | https://dashboard.cohere.com/api-keys |
| `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | https://dash.cloudflare.com/profile/api-tokens (a token allowed to use "Workers AI"; the account id is on the right of the dashboard) |
| `AI_GATEWAY_API_KEY` | https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%2Fapi-keys (Vercel, $5 credit; only used when you pick it) |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys (the official Claude API, paid; only used when you pick it) |
| `TYPESAFE_API_KEY` | if you have Jev access |
| `GITHUB_TOKEN` | optional, for the git and GitHub tools |
| `BRAVE_SEARCH_API_KEY` | optional, to use Brave for web search |

With no key at all, ALICE only tries a local Ollama and says so at startup.

Then check the setup:

```powershell
alice doctor          # sends a test request to every provider, tries the decision engine, lists missing packages
alice models --write  # updates the role chains from the live free-model catalog (optional)
```

## Adding keys in the app

If you'd rather not edit the file, use **Connections → Keys** in the app. Each provider has a "get the key" link.
Paste the key and press Save: it is written to `.env` and used right away. No restart is needed; a new provider is
measured in the background and joins the model order. The full key never comes back to the interface; only its last
4 characters are shown. The background core (the voice assistant) sees a new key the next time it starts.

## Several keys for one provider (key pool)

You can add more than one key for the same provider: open the provider's card under Keys and use "paste another key",
or add `GEMINI_API_KEY_2=`, `GEMINI_API_KEY_3=`... to `.env`.

- Requests are shared among the keys in turn (`[agent] key_strategy = "round-robin"`, the default). With
  `"fill-first"` ("move to the next one when one runs out") the first key is used until it runs out.
- When a key hits 429, that key waits on that model for as long as the server says, and the request is retried at
  once with the next key.
- A key whose daily quota is used up is closed on that model until the next reset. This is remembered across restarts
  in `~/.alice/key-cooldowns.json`.
- An invalid key is skipped for the rest of the session.
- When every key is used up, the router moves on to the next model as usual.

The card shows each key's last 4 characters, how many requests it made and how long it is waiting on which model.

## Your own server or another OpenAI-compatible service

Keys → **Add an OpenAI-compatible server**: a name, an address (e.g. `http://localhost:1234/v1`), a key if it has one,
and optionally a model list. ALICE tries `/models` first; if it answers, the server is written to `config.toml` as
`[providers.<name>]`, and its models are measured and join the order automatically. Use it for LM Studio, vLLM,
LiteLLM or your own proxy. The trash icon on its card removes it.

## Subscriptions through a local proxy (CLIProxyAPI)

CLIProxyAPI is a local bridge that turns Claude Pro/Max, ChatGPT (Codex), Google Antigravity, Kimi and xAI
subscriptions into a single OpenAI-compatible endpoint on this computer. ALICE knows it as the `cliproxy` provider:
`http://127.0.0.1:8317/v1`, with the key `CLIPROXY_API_KEY` from `.env`. It is the first option in the setup wizard.

**Subscriptions come first.** Once an account is signed in, its models head every role's automatic order, picked for
the job (#459):

| Role | Subscription models |
|---|---|
| `brain` (chat, code, agents) | the strongest of each service (Opus, gpt-terra) |
| `voice` | mid-size first (Sonnet), then small; large models are slow to start speaking |
| `fast` (summaries, decisions, background jobs) | small ones only (Haiku, gpt-luna, Gemini Flash) |
| `vision` | the brain's picks that understand images |

Services are tried in `[agent] subscription_order` (Claude, ChatGPT, Antigravity); free models follow them. ChatGPT
signed in directly (without the proxy) counts as a subscription too.

- **Top-tier models are picked by hand only:** `[agent] manual_only_models = ["*fable*", "*mythos*", "*astra*"]` keeps
  them out of every automatic order. Pick them from the model menu, with `/model`, or write them in a `[roles]` chain.
- **Background work leaves quota for you:** the core, standing tasks, KAIROS, PR watch and the `fast` role skip a
  service once its least-used account has spent `[agent] background_quota_limit` (default 80) percent of its quota.
  Your own chats still use it. `100` turns this off.

Connecting an account is your choice; check the provider's terms. Without a subscription everything works with API
keys as before.

**Setup is automatic.** On first start ALICE downloads CLIProxyAPI from its GitHub release, verifies its SHA-256 with
`checksums.txt` and installs it under `~/.alice/cliproxy/` (`[cliproxy] auto_install = true`). If you don't want the
download, set `auto_install = false` and use the **Install (automatic)** button on the Keys page instead. `config.yaml`,
`keys.txt`, `auths/` and `CLIPROXY_API_KEY` are created automatically too.

- The proxy **starts by itself** when ALICE opens (`[cliproxy] auto_start = true`). It keeps running on its own
  after ALICE closes.
- To pin a version, set `[cliproxy] version = "v8.0.57"` (empty = the tested version, `"latest"` = the newest). The
  binary isn't kept in the repository (about 77 MB, platform-specific); a verified release is downloaded at install.

**Connect accounts from the app:** Connections → Keys → the **Claude Code proxy (accounts)** card. Pick the service and
press **Add account (sign in)**; the service's sign-in page opens in the browser and you sign in with your own account.
When sign-in finishes, the account shows on the card (email, provider, quota percentage), and you can remove it there.
You can add several accounts for the same service: requests are shared among them in turn
(`routing.strategy: round-robin`), and when an account hits a limit or its quota, the proxy moves to the next one
without waiting. The credentials stay on this computer, under `~/.alice/cliproxy/auths/`.

**Models and thinking:** the model menu shows all of the proxy's models (the Claude and GPT families, e.g.
`cliproxy:claude-opus-5-5`, `cliproxy:gpt-5.6-terra`). The **Thinking** slider picks `off / low / medium / high / xhigh /
max`; the proxy turns this into Claude's adaptive thinking and Codex's reasoning effort (the highest level matches the
"ultracode" setting in Codex). If you write the model into a `[roles]` chain instead of pinning it by hand, ALICE moves
on to the next model in the chain (e.g. GPT) when the Claude limit is reached.
