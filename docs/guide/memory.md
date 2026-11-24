# Memory, projects and follow-ups

Written from the "Hafıza, projeler ve takip" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and
checked against `alice/memory.py`, `alice/memdir.py`, `alice/projects.py` and `alice/config.py`. The Turkish section
predates note files: lasting information used to be rows in `memory.db`, and it is now a folder of markdown notes.
This page describes the current layout.

ALICE's memory stays on your computer.

## Notes

Lasting information about you and your work is kept as markdown notes, one file per subject, like a personal wiki:

- `~/.alice/memory/*.md` for everything about you (the general scope), and
  `~/.alice/projects/<repo>/memory/` for notes that only apply inside one git repository.
- Categories: **you** (`profile`, `preferences`), **people** (one note per person in your life, with nicknames as
  aliases), **areas** (ongoing parts of your life: a long project, school, an internship) and **topics** (interests
  and the things you own or use).
- `MEMORY.md` in each folder is the index: one line per note. It goes into every conversation's system prompt, and
  the notes most relevant to your message (at most 5) are attached to it in full.
- "Bunu hatırla: ..." (remember this: ...) saves a note at once. When you talk about yourself, a fast model extracts
  lasting notes in the background and updates outdated ones.
- **Dream:** once a day (`[memory] dream_hours`, default 24) and only after enough conversation, a background pass
  merges duplicate notes, fixes outdated ones, turns relative dates into real ones and tidies the index. A backup is
  taken first (`.backups`) and deleted notes go to `.trash`.
- Settings → Memory in the app lists the notes; you can read, edit and delete them there.

## Conversation log, summaries and follow-ups

`~/.alice/memory.db` (SQLite) keeps every turn, the summaries of finished conversations and follow-ups.

- **Conversation summaries:** when a conversation has been quiet for 20 minutes, the whole conversation is
  summarized in one call. The same call extracts lasting information said without a signal word ("Unity'de FPS
  yapıyorum": I'm making an FPS in Unity) and follow-ups. Recent summaries go into the system prompt as background,
  so ALICE knows what you talked about yesterday, without taking your new message as a continuation of it.
- **Follow-ups:** say "yarın sınavım var" (I have an exam tomorrow) and the next day ALICE asks how it went by itself;
  it brings up unfinished work at a fitting moment. Reminders like "pay the bill on Friday" also arrive as a
  notification from the core on the day. In the `memory` tool: `followup`, `followups`, `done`.
- **Past chats:** ALICE looks up older conversations when you refer to them (`memory` actions `search_chats` by topic
  and `recent_chats` by time). Turning off "chat history" in Settings → Memory (`chat_history = false`) stops storing
  turns at all.
- **Semantic search:** with `GEMINI_API_KEY`, notes, summaries and conversations are turned into vectors with the free
  `gemini-embedding-001`, so "the thing I made in a game engine" matches "Unity FPS project". Without a key, or when
  the provider fails, it works with keyword search.

```toml
[memory]
digest = true                  # conversation summaries and follow-ups
session_gap_minutes = 20       # this much silence = the conversation ended
chat_history = true            # keep turns and look up past chats
embed_provider = "gemini"      # any provider with an OpenAI-compatible /embeddings endpoint; "off" = disabled
embed_model = "gemini-embedding-001"
keep_days = 365                # how long turns are kept (notes are not deleted); 0 = forever
```

## Projects

ALICE finds the projects on your computer by itself. It shallowly scans places in your home folder such as Desktop,
Documents, GitHub, source/repos and Unity, and on Windows the top folders of the other drives. It recognizes a project
from markers like `.git`, Unity (Assets + ProjectSettings, with its version), Godot, pyproject, package.json or .sln.
For each project it records the path, languages, GitHub address, branch, last commit and a README summary (tokens in
URLs are not written). The eight most recently worked-on projects go into the system prompt, so "open my Unity
project" knows which one. Details come from the `projects` tool (`list`, `find`, `info`, `rescan`). The list is in
`~/.alice/projects.json` and is refreshed in the background every 12 hours.

```toml
[projects]
paths = ["D:/Games/Unity"]     # add a folder it doesn't find
depth = 3
```

## Not leaving a job half done

If the agent stops while working with tools after saying "now I'll run the tests", or leaves unfinished steps on its
todo list, ALICE reminds it to carry on at most twice. It doesn't when the agent is asking a question, is in plan mode
or is in a voice conversation (`[agent] finish_nudges`, 0 = off).

## Experience on screen

`computer_task` writes the steps of each finished job to `~/.alice/computer_recipes.json`. When a similar job comes
up, the path that worked is given as a hint, and a failed attempt warns "don't go this way". Opening the same setting
a second time doesn't start the search from scratch.
