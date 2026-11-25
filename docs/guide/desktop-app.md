# Desktop app

Written from the "Masaüstü uygulaması" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked
against `alice/cli.py` and `alice/app/static/`. The interface itself is still in Turkish (#214), so the names of
buttons and sections are given in Turkish with their meaning.

## Opening it

`install.ps1` puts an **ALICE** shortcut on the desktop and in the Start menu (you can also press the Windows key and
type "ALICE"). Double-clicking it opens the app without a console window.

- `alice app` opens it by hand, `alice shortcut` recreates the shortcut.
- `alice app --cwd <folder>` picks the starting folder, `--mode` the permission mode, and `--window none` only prints
  the address without opening a window.
- Without a folder, ALICE opens `[app] workspace` from `config.toml`, else the last folder used, else
  `Documents\ALICE` (created if missing).

![Start screen](../app-baslangic.png)

## Look

The "Grafit" (graphite) theme: neutral gray surfaces, a single blue accent and the Geist typeface everywhere (code and
paths in Geist Mono). Colors, corners, spacing and font sizes come from the variables at the top of `app.css`; the
phone interface, the quick bar and the HUD use the same palette. In a new chat the greeting and the input box sit in
the middle. The theme follows Windows, or can be fixed to light or dark in Settings.

## Getting started

If no key has been entered or the core is not running, an empty chat shows a three-step card: connect a model,
measure the models, start the core. Each step is read from the real state, and the card disappears when all three
are done.

## Choosing a folder

Click the folder label in the top bar. Home, desktop, documents, downloads, GitHub and the drives are on the left, with
recently used folders below them. Click through folders and press "Burada çalış" (work here); no typing of paths. The
same window can create a new folder, and its GitHub tab can clone a repository. The Folder field of the new project
window has a "Seç" (choose) button too.

## Left rail

Sohbetler (chats), Projeler (projects), Görevler (standing tasks and the approvals inbox), Otomasyonlar (rules,
scheduled jobs, notes, logs), Sistem (health, core, assistant, voice, setup, usage), Bağlantılar (services, API keys)
and Ayarlar (settings) at the bottom. Each section opens as a full page with its tabs in the sidebar.

## Input box

- Enter sends, Shift+Enter starts a new line.
- `/` opens the command list, `@` opens file and folder suggestions. With `@src/main.py` the file's content goes to
  the model together with the message.
- Paste an image (Ctrl+V), drag and drop it, or add it with `+`.
- A message written while ALICE is working is **queued** and sent automatically when the job is done.
- The box holds the permission mode (Shift+Tab cycles it) and the model picker.

## Chat

- Answers stream in. If the model sends reasoning text, it appears as a collapsible "düşündü" (thought) section.
- Tools that run one after another are grouped in a single "adımlar" (steps) card with a summary in its title
  ("5 adım · 2 dosya · 1,2 sn"). When the job ends, cards with three or more steps and no errors collapse; click to
  open them. Each row shows a spinner and a live timer while it runs, then a green or red dot, the duration and the
  first lines of the result.
- **Editing a message:** press Düzenle (edit) under a message you sent, or double-click it. The message turns back
  into an input box; Enter or Send rewinds the conversation to before that message and sends the new text. If ALICE
  is working, it is stopped first. Changes made by the file tools after that message are undone. Esc cancels.
- File edits open as colored diffs with line numbers. Commands are shown in a terminal view with their exit code.
- Code blocks are syntax highlighted (Python, JS/TS, PowerShell, bash, JSON, CSS, Lua, SQL, the C family...).
- When ALICE keeps a plan, the task list is drawn with a progress bar, and the current task sits above the input box
  while the job runs.
- An action that needs approval shows a card in the chat: "İzin ver" (allow), "Bu oturumda hep izin ver" (always in
  this session), "Bu projede hep izin ver" (always in this project) and "Reddet" (deny); keys 1-4 answer it too.
  "Always in this project" writes a lasting rule (`.alice/permissions.toml`, e.g. `run_command(git status*)`); it is
  not offered for dangerous actions or ones that reach other people.
- Every job ends with a one-line summary: duration, number of tools and changed files.
- Code blocks and answers have a copy button.

## Side panel (Ctrl+J)

- **Plan:** the current task list.
- **Değişiklikler (changes):** every file changed in this session with its +/− counts. Each file's diff can be opened
  and undone file by file. The `+12 −3` button in the top bar opens this tab.
- **Bağlam (context):** context usage, model, mode, folder, git branch and session.

## Command palette (Ctrl+K)

Actions, permission modes, thinking levels, models, slash commands and past chats in one search box.

## Settings (Ctrl+,)

Genel (general: theme, font size, tool details, side panel, permission mode, folder, updates), Modeller (models),
Araçlar (tools), Hafıza (memory) and Kısayollar (shortcuts).

## Rewinding

- Every message has **Düzenle** (edit) and **Geri sar** (rewind) under it. The conversation goes back to before that
  message.
- Files ALICE changed or created with its file tools after that message return to their earlier state. Changes made
  by shell commands can't be tracked.

## Sidebar

In the chats section: new chat, search, and the history grouped as Bugün / Dün / Bu hafta (today / yesterday / this
week). Each row's "⋯" menu can rename (F2 or double-click), pin (pinned chats stay on top under "Sabitlenenler") and
delete. The open chat's name is also in the top bar and can be changed there. In the other sections the sidebar holds
that section's tabs. Ctrl+B hides and shows it.

A running command shows up in the status bar: a click (or Ctrl+Shift+B) moves it to the background, and ALICE goes on
working. Stop, a message you send and the command's timeout do the same instead of killing it; the chat is told when the
command ends. Commands in the background are stopped from the same place. `[agent] background_commands = false` turns
this off.

## Status line

The model that answered, context usage and today's request count. Hovering shows the decision engine, the git branch
and the session time as well.

## Window and security

The window opens with the first of these that works: a real app window with pywebview (Edge WebView2 on Windows),
else Edge or Chrome in app mode without an address bar, else the default browser. The interface runs only on
`127.0.0.1` and requires a random token that changes on every start, so another site open in the browser can't send
ALICE commands. Model output is cleaned with DOMPurify before it is shown.

![Chat](../app-acik.png)

![Command palette](../app-palet.png)

![Automations](../app-ayarlar.png)
