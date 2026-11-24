# Connections

Written from the "Bağlantılar: servisleri ALICE'e öğretmek" section of the Turkish guide
([docs/README.tr.md](../README.tr.md)) and checked against `alice/connect/`. The Turkish section predates the
Google Workspace connection; this page lists it. How to write a connection yourself is in
[docs/CONNECT.md](../CONNECT.md).

Connecting a site to ALICE needs no code: you write a TOML definition, and ALICE turns it into a tool with a schema
(actions, parameters, credentials, a readable form of the response).

```powershell
alice connect                       # connections and their status
alice connect spotify               # OAuth: allow it in the browser, the token is saved
alice connect steam --test library --arg steamid=7656119...
alice connect mysite --new          # template at ~/.alice/connect/mysite.toml
```

Built in:

| name | credentials | what it does |
|---|---|---|
| `steam` | `STEAM_API_KEY` | library, play time, friends, who is playing what, game news |
| `youtube_api` | `YOUTUBE_API_KEY` | video/channel search, views and likes, comments |
| `discord` | `DISCORD_BOT_TOKEN` | servers, channels, recent messages, sending messages (as a bot) |
| `spotify` | OAuth2 + `SPOTIFY_CLIENT_ID` | the current track, play/pause/next, search, choosing a device, playlists |
| `google_calendar` | OAuth2 + `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | upcoming events, search, adding in plain language ("Friday 15:00 dentist"), deleting. When connected, the core turns events like exams, matches and appointments into follow-ups: the next day ALICE asks how it went |
| `google_workspace` | the same Google client | Google Drive, Docs and Sheets with the same Google sign-in |

**Connections** in the app shows each service's status, Connect / Sign out buttons for OAuth services, which `.env`
variable is missing for key-based ones, and the feeds defined below them. In the terminal the same summary is one
line of `alice doctor`.

Slowly changing data (a Steam library, YouTube video details) is remembered for a few minutes, so when the agent asks
for the same thing again the request doesn't go out; on a rate limit (429) or a gateway error it waits briefly and
retries once. Requests that send messages are never retried after a network error, so the same message can't go out
twice.

Credentials: API keys are read from `.env`; OAuth2 tokens are kept in `~/.alice/connect.json` (readable only by you)
and refreshed automatically when they expire. The OAuth flow uses PKCE, so Spotify needs no client secret.

The tools are in the `connect` group: they aren't added to every request, and open when a service's name comes up in
the message or the model calls `load_tools`. That keeps requests small even with every service connected.

**WhatsApp, Instagram, TikTok:** these have no official API usable with a personal account (the WhatsApp Cloud API
needs a business number; the Instagram and TikTok APIs need a business account and a review). They are used through
the real browser ALICE opens with a persistent profile, and reflex mode, instead of a connection: you sign in once
and the session stays open. See [apps.md](apps.md).
