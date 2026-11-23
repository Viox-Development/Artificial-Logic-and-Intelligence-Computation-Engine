# Automatic updates

The detailed version of the "Updates" part of the [README](../../README.md), written from the "Otomatik güncelleme"
section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked against `alice/updater.py`. The
Turkish section predates update channels; this page describes the current behavior.

ALICE updates itself from its GitHub repository. Updates work only in an ALICE installed with `git clone` (the
installers do this); a zip download has no git repository, and `alice update` says so.

## Channels

- `channel = "stable"` (default): ALICE follows **releases**, the highest `vX.Y.Z` tag. A merged pull request reaches
  users only after a maintainer publishes a release.
- `channel = "main"`: every commit on the `branch` (default `main`). For developers.

The check is a single `git ls-remote` round trip with no fetch, so it doesn't hold up startup.

## When ALICE updates

- **At startup:** `alice app` checks before it opens. If there is a new version, it pulls it with a fast-forward,
  installs dependencies if `pyproject.toml` (or another dependency file) changed, restarts itself and opens the new
  version. The startup check waits at most `startup_timeout` seconds.
- **While the app is open:** it checks in the background every `check_every_minutes`. When a new version is out, a
  "Yeni sürüm hazır · Güncelle ve yeniden başlat" (new version ready, update and restart) bar appears above the chat.
  If the agent is in the middle of a job, stop it first so nothing is left half done.
- **In the terminal:** `alice update --check` shows what changed and whether CI is green, `alice update` pulls, and
  `alice update --restart` pulls and opens the app. In the REPL, `/update` and `/update apply` do the same.
- **The background core stays current too.** It can run for days, so it also checks every hour. If the app is open,
  the core leaves the update to the app; if the app is closed, the core pulls by itself and restarts quietly when no
  task is running.

Dependencies are installed with the same optional extras that are already installed (detected from the installed
packages), so an update doesn't drop features.

## Safety rules

- **Your own changes come first.** If the working tree is dirty, nothing is pulled automatically; you are only told.
  `alice update --force` pulls anyway; the fast-forward keeps your uncommitted changes, and a rollback keeps them too.
- **Red CI blocks automatic updates.** With `require_green_ci = true`, a version whose CI is red or still running isn't
  pulled automatically. CI status is read with the keyless GitHub API; if the API can't be reached, the status counts
  as unknown and the update goes ahead. To skip the wait: `alice update --force`, or "Yine de güncelle" (update
  anyway) on the bar.
- **One process updates at a time.** `~/.alice/update.lock` keeps the app and the core from running git at the same
  time; a process that waited for the lock doesn't pull again if the other already did.
- **A stale process notices.** If the code on disk is newer than the running process (the core pulled it, or you ran
  `git pull` by hand), the app shows "Yeni sürüm indirildi · yeniden başlatınca açılır" (downloaded, opens on
  restart), and an idle core restarts itself, so an old process doesn't run a mix of old and new files.
- **What changed is told once.** The first start after an update shows a note with the commit titles at the top of the
  chat; if the app is closed, the core sends a desktop notification.

## Rollback

A broken version must not leave ALICE unable to start:

- **Smoke test:** after a pull, the main modules of the new version are imported in a separate Python process. If
  that fails (a syntax error, a missing package), the working tree goes back to the previous commit, and the new
  commit is marked broken.
- **Boot health:** a new version counts as healthy once it stays up for 90 seconds. If 2 boots in a row end before
  that, ALICE goes back to the previous commit (unless you changed files by hand).
- A commit marked broken isn't pulled again automatically; the next new commit is tried as usual, and `alice update
  --force` still pulls it. The rollback is reported once.

## Settings

Settings → General → Update in the app, or `config.toml`:

```toml
[update]
mode = "auto"               # auto: update at startup | notify: only tell me | off: never check
channel = "stable"          # stable: releases only | main: every commit on branch
branch = "main"
check_every_minutes = 60    # background check while the app is open
require_green_ci = true     # no automatic update while CI is red or running
startup_timeout = 12        # the startup check waits at most this many seconds
startup_check = true        # false: check only while the app is open, or by hand
```

For a single run, set `ALICE_UPDATE_MODE=off` (or `notify`/`auto`), `ALICE_UPDATE_CHANNEL=main`, or `ALICE_NO_UPDATE=1`.
