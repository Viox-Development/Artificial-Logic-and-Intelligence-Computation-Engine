# Permission modes and safety

Written from the "İzin modları ve güvenlik" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and
checked against `alice/permissions.py`, `alice/actionlog.py` and `alice/cli.py`.

- **plan:** only reads and researches. Everything that changes files, runs commands or touches the screen is refused;
  ALICE writes a plan and waits for your approval.
- **ask:** everything except reading asks for approval.
- **auto** (default): editing files, running commands and mouse/keyboard are allowed. Dangerous patterns such as
  `rm -r`, `Remove-Item -Recurse`, format, the registry, shutdown, `git push --force` and `curl | sh`, and writing to
  system folders such as `C:\Windows` or `Program Files`, ask for approval.
- **yolo:** asks nothing.

Switch with `--mode`, `/mode`, Shift+Tab, or `permission_mode` in `config.toml`.

## Messages to other people are always asked about (except in yolo)

Sending a Discord message, writing on WhatsApp, an Instagram DM or a Gmail email are in their own risk class
(`send`). Unlike a file edit, a sent message can't be taken back, so these ask for approval in `auto` mode too, are
never done in `plan` mode, and are refused in background jobs (when you are not at the computer). The approval card
says the message will go to someone else. Actions that control your own devices, like play/pause on Spotify, are not
in this class. A connection marks an action with `sends = true`, and an app recipe lists the flows that send
(`sends = ["send"]`); to turn the extra question off everywhere, set `[agent] confirm_outward = false`.

## A log of everything sent on your behalf

Every action that reaches someone else is written to `~/.alice/actions.jsonl` with the message itself: sent, rejected
by you, blocked in plan mode or failed. `alice log` shows it in the terminal, and "Senin adına gönderilenler" (sent on
your behalf) under Connections shows it in the app; in a chat you can ask "who did you write to today?". The log never
leaves your computer.

## Approvals in the terminal

Answering `a` to an approval question allows that tool for the rest of the session (except dangerous patterns). The
desktop app's approval card also offers "always in this project", which writes a rule to `.alice/permissions.toml`.

## Why auto is the default

Full access is convenient, but a free model that misunderstands and deletes the wrong folder can't be undone; `auto`
almost never asks during normal work. For no questions at all, use `--yolo` or `permission_mode = "yolo"`.

## Emergency stop

`Ctrl+C` stops the current job. Moving the mouse to the **top-left corner** of the screen stops mouse/keyboard
control at once (pyautogui failsafe).
