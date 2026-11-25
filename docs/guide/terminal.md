# Terminal

Written from the "Terminal" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked against
`alice/cli.py` and `alice/commands.py`. The Turkish section's line for `alice update --check` predates release
channels; this page describes the current behavior.

```powershell
alice                                   # interactive session
alice "find and fix the jump bug in PlayerController in this Unity project"
alice -p "list the 5 processes using the most RAM"   # one-shot: prints the result and exits
alice --yolo                            # full access without asking anything (same as --mode yolo)
alice --resume                          # continue the last session (or --resume <id>)
alice -v                                # show which model answered
alice --mode plan --cwd D:\Projects\game  # permission mode and working folder
alice --model gemini:gemini-flash-latest # pin the brain role to one model
alice --version                         # version
alice <command> --help                  # usage of every subcommand (alice voice --help, alice core --help...)
alice update --check                    # is a new version available on your update channel?
alice update --restart                  # update and open the app
alice connect                           # service connections (Spotify, Steam, Discord, YouTube)
```

If you mistype a subcommand (`alice doctr`), ALICE doesn't send it to the model; it suggests the right one. To send a
single word as a message, use `alice -p "word"`.

## Trying reflex on its own

```powershell
alice reflex "open the Settings page and switch to the Privacy tab" --target browser --url https://github.com
alice reflex "press the Save button" --target desktop --title "Notepad"
alice reflex "search for the weather" --target browser --url https://duckduckgo.com --input "search=ankara weather"
```

`--target browser` first connects to your open browser (it must be running with `--remote-debugging-port=9222`).
Opening a separate window when that fails is off by default; to al| `/think [role] off|low|medium|high|xhigh|max|ultracode|default` | it, add `browser_allow_separate = true` to the
`[agent]` section of `config.toml`. `--max-steps` sets the step limit (default 25), `--close` closes the browser when
done, and `--input field=text` can be repeated.

## In the session

Answers stream in, file edits show as colored diffs, and a status bar at the bottom shows the model, the permission
mode, context usage and today's request count. For multi-line input press **Alt+Enter**, or end the line with `\` and
press Enter (works in every terminal); Enter alone sends.

REPL commands:

| Command | What it does |
|---|---|
| `/help` | list commands |
| `/plan`, `/mode plan\|ask\|auto\|yolo` | permission mode (Shift+Tab cycles it) |
| `/model` | chain status; `/model brain groq:openai/gpt-oss-120b` pins a model, `/model brain reset` unpins |
| `/think [role] off|low|medium|high|xhigh|max|ultracode|default` |\|low\|medium\|high\|default` | thinking level |
| `/kurul <topic>` | ask the review council (critic, advocate, checker and experts debate and vote) |
| `/rewind` | go back to before a message |
| `/init` | create an `ALICE.md` instruction file for this project |
| `/usage`, `/compact`, `/clear` | usage, summarize the conversation, start over |
| `/sessions`, `/resume` | past sessions |
| `/cwd`, `/tools`, `/mcp`, `/memory` | working folder, tools, MCP servers, memory |
| `/update`, `/exit` | update, quit |

Custom commands and `@file` work in the terminal too.
