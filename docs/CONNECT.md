# Connections: teaching ALICE a site

ALICE doesn't need code to use a service. You write a TOML file, and ALICE turns it into a tool with a schema: the
model sees the actions and their parameters, credentials are added automatically, and responses are made
readable.

```powershell
alice connect                      # connections and their status
alice connect spotify              # OAuth: grant access in the browser
alice connect steam --test library --arg steamid=7656119...
alice connect mysite --new         # create a template: ~/.alice/connect/mysite.toml
alice connect spotify --logout     # delete the stored token
```

Built-in definitions live in `alice/connect/builtin/*.toml`; yours go in `~/.alice/connect/*.toml` (if a name
exists in both, yours wins). In `config.toml`:

```toml
[connect]
disabled = ["discord"]    # turn off
paths = ["D:/alice-baglantilar"]   # extra folder
```

The tools sit in the `connect` group: they are not added to every request, and open when a service's name comes up
in a message or the model calls `load_tools(["connect"])`.

## Definition file

```toml
name = "steam"                 # tool name: lowercase letters, digits, underscores
title = "Steam"
description = "The description the model reads: what this service is for."
base_url = "https://api.steampowered.com"
keywords = "steam|oyun|kütüphane"      # when these words come up, the group opens by itself
docs = "https://steamcommunity.com/dev"
```

### Credentials: `[auth]`

| kind | what it does | required fields |
|---|---|---|
| `none` | open API without credentials | — |
| `api_key` | puts the key in the query or a header | `env`, `place` (query/header), `name`, `prefix` |
| `bearer` | `Authorization: Bearer <key>` | `env` |
| `oauth2` | consent in the browser, token storage and refresh | `client_id_env`, `authorize_url`, `token_url`, `scopes`, `redirect_port` |

Keys are read from `.env` (`env = "STEAM_API_KEY"`) and are not copied anywhere. Put where to get the key in the
`help` field; ALICE shows it when the key is missing.

The OAuth2 flow: ALICE opens a one-time server on a port on `127.0.0.1`, opens the consent page in the browser,
exchanges the returned code for a token and writes it to `~/.alice/connect.json` (chmod 600). Because PKCE is used,
no client secret is needed. If the service requires the redirect address to be registered in advance (Spotify),
set `redirect_port` and register `http://127.0.0.1:<port>/callback` in the service's dashboard.

### Actions: `[actions.<name>]`

```toml
[actions.library]
summary = "Games in the library"          # one-line description sent to the model
method = "GET"                            # GET | POST | PUT | PATCH | DELETE
path = "/IPlayerService/GetOwnedGames/v1/"   # {param} placeholders can be used
query = { format = "json" }               # fixed query sent with every request
headers = { X-Thing = "1" }
body = { fixed = true }                   # fixed body for POST/PUT
pick = "response.games"                   # which branch of the response to show
fields = ["name", "playtime_forever"]     # fields to show in lists (nested: "snippet.title")
limit = 40                                # at most this many rows
writes = true                             # the permission system counts it as a "write" (default true except GET)
sends = true                              # goes to someone else (message, post): asks for approval even in auto mode
cache_seconds = 300                       # a reading action's response is remembered this many seconds (0: never)

[actions.library.params.steamid]
type = "string"        # string | integer | number | boolean | array
required = true
description = "SteamID64"
into = "query"         # query | path | body | header (empty: query for GET, body for POST)
enum = ["a", "b"]
default = "a"
```

The model calls the tool with the `action` parameter: `steam(action="library", steamid="765...")`.

## Built-in connections

| name | credentials | what it does |
|---|---|---|
| `steam` | `STEAM_API_KEY` | library, playtime, friends, player status, game news |
| `youtube_api` | `YOUTUBE_API_KEY` | video/channel search, video statistics, comments |
| `discord` | `DISCORD_BOT_TOKEN` | servers, channels, recent messages, sending messages (with a bot account) |
| `spotify` | OAuth2 + `SPOTIFY_CLIENT_ID` | current track, play/pause/next, search, devices, playlists |
| `google_calendar` | OAuth2 + `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | upcoming events, search, adding in natural language ("Cuma 15:00 dişçi", i.e. "Friday 3 pm dentist"), deleting. When connected, the core turns events in the next two days such as exams, matches or appointments into follow-ups; the next day ALICE asks how it went |

## Sites without an official API

WhatsApp, Instagram and TikTok have no official API usable with a personal account: the WhatsApp Cloud API needs a
business number, and the Instagram and TikTok APIs depend on a business/creator account and an approval process.
For these the right path is not an API but the real browser ALICE opens with a persistent profile (the `browser`
tool) and reflex mode: you log in once and the session stays open. Ready-made recipes for these sites are in
`alice/apps.toml`, and the `app` tool runs them (see the "Uygulama tarifleri" section of the detailed guide,
[docs/README.tr.md](README.tr.md)).

## Feeds: watching a connection

You can poll a connection's action in the background and turn new items into notifications: `~/.alice/feeds.toml`
(`alice feeds --new` creates an example file). It is polled while the core service (`alice core run`) is running.

```toml
[[feed]]
name = "new_video"
connector = "youtube_api"
action = "search"
args = { q = "DCS World update", type = "video", maxResults = "5" }
every_minutes = 30
id_field = "id.videoId"
title = "New video: {snippet.title}"
body = "{snippet.channelTitle}"
```

If you add `agent = "..."`, ALICE wakes up when a new item arrives and does that job (summarizing, checking
something). The first round is silent; at most 5 notifications are sent per round.

`[feed.when]` (any/all/none/regex/fields/min/max/ask) decides which items get notified, and `[feed.escalate]` which
ones are urgent; `mode = "change"` watches a field for changes, and `quiet`, `digest_minutes` and `cooldown_minutes`
set when notifications are delivered. The full list and examples are in the "Akışlar" section of the detailed guide,
[docs/README.tr.md](README.tr.md). While setting up a condition, `alice feeds --test <name>` shows why each item
passed or was filtered out.

## Speed and resilience

* `cache_seconds`: remembers slowly changing data (Steam library, YouTube video info, Discord channel list), so
  when the agent asks the same question again step by step, the request doesn't go out to the network. Off by
  default. Actions that write or send never answer from the cache; feeds always ask for fresh data.
* Retries happen once and quickly: on 429 (the server refused the request without processing it), if Retry-After
  is under 5 s, ALICE waits and retries. On network errors and 502/503/504 only reading actions are retried:
  repeating a request that sends a message could send the same message twice.

## Tips

* Try it with `--test` first: `alice connect <name> --test <action> --arg k=v`. It prints the output the way the
  model will see it; tune your `fields` choice by looking at it.
* If you don't give `fields`, the response goes out as JSON; long JSON is both slow and eats quota. Pick the fields
  you really need.
* A broken definition does not stop ALICE: it is skipped, and the reason shows up in the `alice connect` output.
