# Feeds: ALICE speaking up on its own

Translated from the "Akışlar" section of the Turkish guide, [docs/README.tr.md](../README.tr.md). The short version
is in [docs/CONNECT.md](../CONNECT.md#feeds-watching-a-connection).

An assistant answers when asked; ALICE should also say what is happening. The core service already watched the
computer (rules such as "tell me if the GPU stays hot for long"); feeds let it watch connected services too. But an
assistant that reads out every new message gets muted within five minutes, so the real work is deciding **what is
worth saying**. Feeds live in `~/.alice/feeds.toml` and are polled while the core (`alice core run`) is running:

```toml
[[feed]]
name = "discord_genel"
connector = "discord"
action = "messages"
args = { channel_id = "123456" }
every_minutes = 5
title = "Discord · {author.username}"
body = "{content}"
quiet = "23:00-08:00"          # hold at night, deliver a digest in the morning
agent = "Önemli bir şey varsa bana kısaca söyle"   # optional: ALICE wakes up and does this job

[feed.when]                     # which messages are worth a notification
any  = ["alice", "@everyone"]   # at least one must appear
none = ["bot"]                  # none of these may appear
ask  = "Bu mesaj bana bir şey soruyor ya da acil bir şey mi?"   # decided by meaning

[feed.escalate]                 # if this matches, the item is critical: told at once, even in quiet hours
regex = '\b(acil|hemen)\b'
```

## The condition language

`[feed.when]` and `[feed.escalate]` are checked from the cheapest to the most expensive, and the first test that fails
drops the item:

| field | what it does |
|---|---|
| `any` / `all` / `none` | at least one / all / none of the words must appear (in the title, the body and every text field of the item; Turkish case is handled, so "ACİL" = "acil") |
| `regex` | a regular expression |
| `fields` | field equality: `{ "author.username" = ["ahmet", "ayşe"] }`; `"*"` means not empty |
| `min` / `max` | numeric limits: `{ "statistics.viewCount" = 1000 }` |
| `ask` | a yes/no question about the meaning, answered by Jev or a fast model. Because it is a model call, it runs last and only for items that passed everything else |

## Two ways of watching

- `mode = "new"` (default): an item new to the list (a new message, a new video).
- `mode = "change"` with `watch = ["field"]`: a field of the same item changes. For example, watch the Steam friend
  list and say "Ahmet joined DCS World" when `gameextrainfo` changes to something non-empty. In the template,
  `{prev.field}` is the old value and `{changed}` lists the changed fields.

## When notifications are delivered

- `quiet = "23:00-08:00"`: items are held during these hours and one digest arrives when the window ends. Windows that
  cross midnight work.
- `digest_minutes = 60`: an hourly digest instead of one notification per item.
- `cooldown_minutes = 10`: after a notification, items arriving within 10 minutes are collected and sent later as a
  digest, so a busy chat doesn't keep the phone buzzing.
- An item that matches `[feed.escalate]` becomes `critical` and waits for none of these.

## Commands

```powershell
alice feeds                         # the feeds and what they do, in plain words
alice feeds --new                   # an example file (with Discord and Steam "joined a game" examples)
alice feeds --test discord_genel    # poll now: ✓/✗ for each item and why ("unwanted word: bot")
```

`--test` is the most useful tool while you set up conditions: it notifies nothing and shows why each item passed or
was dropped.

## Setting up a feed from the chat

When you say "tell me when Ahmet starts a game on Steam, don't bother me at night", ALICE uses the `feeds` tool: it
first checks which connection is ready and looks at the field names of sample items, then sets up the feed and writes
it to `feeds.toml`. It doesn't rewrite the file: your comments and hand-made entries stay as they are, and the new
feed is appended. A feed is validated before it is saved (a wrong action name, a missing parameter or a broken
condition goes back to the model, which fixes it). The core notices the file changed by itself; no restart is needed.
A feed that wakes ALICE up when it notifies (`agent`) runs unattended, so ALICE asks for approval when setting it up.

## Behavior

- The first round notifies nothing: the current items are marked as seen. Otherwise the last 15 messages would arrive
  one after another the moment you set the feed up.
- At most 5 notifications are sent per round; the rest are summed up as "3 more new items".
- Notifications go through the mode policy: in flight or quiet mode they are muted and logged. If voice mode is on,
  they are also spoken.
- The Connections page in the app lists the feeds and how many items are waiting for a digest.
