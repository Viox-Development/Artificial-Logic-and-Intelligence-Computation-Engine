# App recipes: which way to do which job

Translated from the "Uygulama tarifleri" section of the Turkish guide, [docs/README.tr.md](../README.tr.md).

ALICE can do the same job three ways: an official API (fast and reliable), the app's deep link
(`steam://rungameid/...`), or browser/interface automation (works everywhere but is slow). Instead of letting the
model guess which is right each time, the choice is written down in `alice/apps.toml`, and the `app` tool uses it:

```
app(action="list")                                  # defined apps and flows, and whether there is an official API
app(action="open", name="wp")                       # open WhatsApp Web in the user's own browser
app(action="flow", name="whatsapp", flow="send",
    args={"kime": "Ahmet", "mesaj": "geliyorum"})   # run the recipe's flow with reflex
app(action="open", name="steam_game", target="238960")   # start a game directly
```

`name` matches an app's name or its `keywords` (so `wp` finds WhatsApp). The flow arguments are the recipe's own
placeholders; the built-in recipes use Turkish names such as `kime` (to whom) and `mesaj` (message).

Built-in recipes: WhatsApp (send/read/search), Instagram (DMs, profile, feed), TikTok (search, profile), YouTube (open
and play a video), Gmail (write, inbox), Discord, Spotify, Steam, and starting a Steam game.

## Desktop apps

Desktop apps have flows too. Spotify (play, pause, next, playlist), Discord (message a server channel, DM, join a voice
channel, mute the microphone) and Steam (library, store search, friends) run in the app's own window: if there is no
window, ALICE starts the app, waits for its window, brings it to the front, and then reflex sees the buttons by their
IDs through Windows UI Automation and clicks them. In your own recipe, `target = "desktop"` and
`window = "part of the window title"` are enough.

## Your own recipes

Put them in `~/.alice/apps.toml`. If you use the same name as a built-in recipe, only the fields you change are
overridden.

Sites without an official API for personal accounts (WhatsApp, Instagram, TikTok) are used this way, through the real
browser ALICE opens with a persistent profile: you sign in once and the session stays open. See
[docs/CONNECT.md](../CONNECT.md#sites-without-an-official-api).
