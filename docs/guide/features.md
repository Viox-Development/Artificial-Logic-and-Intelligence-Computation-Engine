# Claude Code-style features

Written from the "Claude Code'daki özellikler" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)),
including its GitHub part, and checked against `alice/tools/`, `alice/hooks.py`, `alice/skills.py`,
`alice/commands.py`, `alice/mcp.py`, `alice/evals.py` and `alice/tools/github.py`. The Turkish GitHub part predates
the current token order and undercover mode; this page describes both.

| Feature | In ALICE |
|---|---|
| Plan mode | `/plan` or the mode menu: only reads and researches, tools that change things are refused, it writes a plan |
| Rewind (checkpoints) | Rewind / Edit under a message, or `/rewind`. Changes made by the file tools are undone |
| `@file` mentions | The file's content or the folder listing is added to the message; the app autocompletes |
| Slash commands | Built-ins + `~/.alice/commands/*.md` and `.alice/commands/*.md` in the project (`.claude/commands` is read too) |
| `/init` | Studies the project and writes an `ALICE.md` instruction file |
| MCP servers | `[mcp.servers.<name>]` in `config.toml`; tools are added as `mcp__<server>__<tool>` |
| Project instructions | `ALICE.md`, `CLAUDE.md`, `AGENTS.md` and `~/.alice/ALICE.md` are read automatically |
| Subagents, todos, memory, compaction, resuming sessions | yes |
| Message queue, pasting images, showing thinking | yes (in the app) |
| Multiple edits (MultiEdit) | `multi_edit`: several changes to one file in one call; if any of them doesn't match, nothing is written |
| Multi-file patch (Codex `apply_patch`) | `apply_patch`: add / delete / edit / move across files in the `*** Begin Patch` format, all or nothing |
| File management | `file_ops`: move, copy, create folder, delete (deleted files go to `~/.alice/trash` and can be restored) |
| Grep modes | `grep`: `output_mode` (content, files_with_matches, count), `context` lines, `multiline`, paging with `offset` |
| Reading PDF and Jupyter | `read_file` reads a PDF as text (with `pages`) and an `.ipynb` with its cells and outputs |
| WebFetch | `web_fetch`: smart trimming (menus, ads and footers dropped), Markdown, numbered links, JSON/PDF; a hidden browser fallback when a site blocks or needs JS; `prompt` gives a BM25-focused summary from a small model |
| WebCrawl | `web_crawl` (net group): research across many pages of a site, opening the most relevant link first, stopping at saturation, respecting robots.txt |
| WebSearch | `web_search`: Google first (Gemini grounding, `GEMINI_API_KEY`) for a current summary with sources. On quota or errors, DuckDuckGo and Bing; Brave with `BRAVE_SEARCH_API_KEY`; `allowed_domains` / `blocked_domains` |
| HTTP requests | `http_request` (net group): REST APIs, webhooks, file downloads; with `${env:NAME}` in a header the key is never visible to the model |
| Parallel tool calls | When the model asks for several reads or searches in one reply (`read_file`, `grep`, `glob`, `list_dir`, `web_search`, `web_fetch`, read-only MCP tools), they run at once; results return to the model in order |
| Tool grouping | Consecutive reads become one collapsible line in the app ("read 2 files, ran 1 search"); one line each in the terminal |
| Hooks | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`; the command runs in a shell, the event comes as JSON on stdin, exit code 2 blocks or gives the model feedback. See below |
| Permission rules | `[permissions] allow / ask / deny` patterns (`run_command(git status*)`), `.alice/permissions.toml` in the project; deny applies in every mode |
| Skills | `~/.alice/skills/<name>/SKILL.md` (and `.alice/skills`, `.claude/skills` in the project): only the description is in the prompt; the whole skill and its files are read with the `skill` tool when needed |
| Custom subagents | `~/.alice/agents/<name>.md` (and in the project): their own prompt, tool list, model and step limit; `agent=<name>` in the `task` tool |
| Workflows, ultracode | `workflow`: ALICE writes a script that runs many subagents, has them verify each other and returns data; see below |
| Multiple-choice questions | `ask_user`: a clickable card in the app (keys 1-6), a numbered list in the terminal, buttons in the quick bar; a plain question in voice |
| Headless | `alice -p "..."` (also reads stdin), `--output-format json` or `stream-json` |
| Notebook editing | `notebook_edit`: replace, insert or delete a cell in an .ipynb (other cells and outputs are untouched) |
| `/review`, `/security-review` | Reviews the change (or a given PR/branch) for bugs or security without changing anything; your own command with the same name wins |
| Eval | `alice eval`: a test set from real conversations (below) |
| Review council | `/kurul <topic>` and in projects: critic, advocate, checker and experts debate and decide with a reasoned, weighted vote ([council.md](council.md)) |
| git and GitHub | `git` (status, diff, log, branches, commit, pull, push) and `github` (repositories, cloning, open/review/merge PRs, comments, issues, CI status) tools; see below |

## Hooks

Format after every edit, block `git push --force`, check the tests when the job ends (in `config.toml` or
`~/.alice/hooks.toml`):

```toml
[[hooks.PostToolUse]]
matcher = "edit_file|write_file|multi_edit"
command = "ruff format \"$ALICE_TOOL_PATH\""

[[hooks.PreToolUse]]
matcher = "run_command"
command = "python C:/Users/you/.alice/no_force_push.py"   # reads the command from the JSON on stdin, exit 2 if forbidden

[[hooks.Stop]]
command = "pytest -q -x || (echo tests are broken 1>&2 && exit 2)"
```

With exit code 2, `PreToolUse` blocks the tool, and `PostToolUse` and `Stop` give stderr to the model as feedback (on
Stop the agent keeps working, at most 3 times). The stdout of `UserPromptSubmit` and `SessionStart` is added to the
message as context. Hooks are read only from your own config; a file in a repository you cloned can't run commands.

## Permission rules

```toml
[permissions]
allow = ["run_command(git status*)", "run_command(git diff*)", "run_command(npm test*)", "edit_file(src/*)"]
ask = ["run_command(git push*)"]
deny = ["run_command(*rm -rf*)", "write_file(*.env)", "mcp__github__delete_*"]
```

A pattern looks at the tool's main argument (the command, path, URL...). `allow` doesn't break plan mode, and only a
rule with a pattern releases a dangerous command (`"run_command"` alone doesn't allow `rm -r`).

## Skills

`~/.alice/skills/unity-build/SKILL.md` (you can put files like `build.ps1` next to it):

```markdown
---
name: unity-build
description: Make a Windows build of a Unity project without opening the editor
---
1. Find the project's path and Unity version with the projects tool.
2. Run Unity.exe with `-batchmode -quit -projectPath <path> -buildWindows64Player <output>`...
```

## Custom subagents

`~/.alice/agents/reviewer.md`:

```markdown
---
name: reviewer
description: Reviews code changes for bugs and risks without changing anything
tools: read_file, grep, glob, list_dir, git
model: brain
max_steps: 30
---
You are a meticulous code reviewer. Report only real bugs...
```

## Workflows and ultracode

For work that one agent can't hold at once (a review of a whole project, a migration over 200 files, research that has
to be checked), ALICE writes a **workflow**: a short Python script that fans the work out to subagents, waits where it
must, has other agents try to refute what was found, and returns the result. It runs when you ask for a workflow or for
many agents, and by itself with the **ultracode** switch in the effort menu or when the word *ultracode* is in your message
(only for that message). It costs tokens: a `+500k` or `+2m` in the message is a ceiling for the workflows of that turn.

```python
META = {"name": "review", "description": "Review the changed files and verify each finding",
        "phases": [{"title": "Review"}, {"title": "Verify"}]}

BUGS = {"type": "object", "required": ["bugs"], "properties": {"bugs": {"type": "array", "items": {"type": "string"}}}}
VERDICT = {"type": "object", "required": ["real"], "properties": {"real": {"type": "boolean"}}}

async def review(path):
    return await agent(f"Review {path} for bugs.", label=path, phase="Review", schema=BUGS)

async def verify(found, path):
    votes = await parallel([lambda b=b: agent(f"Try to refute this bug in {path}: {b}", phase="Verify", schema=VERDICT)
                            for b in found["bugs"]])
    return [b for b, v in zip(found["bugs"], votes) if v and v["real"]]

return await pipeline(args["files"], review, verify)
```

- The script runs in its own process without files, network, clock or randomness; only its agents touch anything, and
  their tool calls ask for permission like always, under the name of the run. Starting a workflow is asked about like a
  command (in plan mode it does not run). Agents can't start other agents or workflows.
- The run goes on in the background: its phases and agents are in the Agents tab, and the result comes back to the chat.
  Stop in the chat stops its workflows too.
- Every run is kept in `~/.alice/workflows/<id>/` (script, journal of finished agents, result). Resume replays the agents
  that are already done and runs the rest.
- An agent can have its own model (`fast`, `provider:model`), effort, custom definition, or git worktree
  (`isolation="worktree"`: removed if nothing changed, kept with its branch otherwise).
- Workflows worth keeping go to `<project>/.alice/workflows/*.py` or `~/.alice/workflows/saved/*.py`; ALICE starts them by
  the name in `META`.
- Limits: `[agent] workflow_max_agents` (200) and `workflow_concurrency` (0 = 2-6, depending on the computer).

## Eval

Turn the sentences ALICE got wrong into test cases and replay them after every update:

```powershell
alice eval --from-log 20          # add the last 20 sentences from voice.log to ~/.alice/evals.toml as drafts
alice eval --add "what is the difference between the F-16 and the F-15"
alice eval                        # run them all: pass/fail, and what got fixed or broke since last time
```

Each case can have `expect` (must appear in the answer), `forbid`, `expect_tools`, `forbid_tools` and `role`. Cases run
with approval questions answered "no": ALICE reads and researches, but tools that change things or touch the screen
don't run; which tool it chose is still recorded. Test conversations are not written to memory.

## Automation

`alice -p "summarize the latest commits" --output-format json` prints a single JSON object at the end (`result`,
`is_error`, `models`, `tools`, `duration_s`); `stream-json` prints every event (text, tool call, result) on its own
line. Tools that need approval are refused without asking (except with `--yolo`).

## Custom commands

`~/.alice/commands/review-changes.md`:

```markdown
---
description: Review the changes
---
Review the latest changes in this project and list bugs and risky spots. Focus: $ARGUMENTS
```

Then `/review-changes performance` sends this text with "performance" in place of `$ARGUMENTS`.

## MCP

In `config.toml`:

```toml
[mcp.servers.files]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/you/Projects"]

[mcp.servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_PERSONAL_ACCESS_TOKEN = "..." }
# trust_annotations = true
```

The "read-only" label (`readOnlyHint`) a server puts on its tools isn't trusted by default: those tools count as writes
(they run without asking in auto mode, ask in ask mode, and are closed in plan mode). If you trust the server,
`trust_annotations = true` makes labeled tools count as reads.

If a server fails to start, the error shows as a note in the app, with details in `~/.alice/mcp-<name>.log`.

## GitHub

The window behind the working-folder button has an **Open from GitHub** list: clicking a repository clones it to
`~/GitHub/<owner>/<repo>` (or updates it if it is already there) and makes it the working folder (`[github]
clone_dir` changes the place). Saying "open the alice repository" in the chat does the same.

The token is taken in this order: `gh auth token` (your login on this computer), `GITHUB_TOKEN` (or `GH_TOKEN`) in
`.env`, the key of `[mcp.servers.github]`, and last `GITHUB_MODELS_TOKEN`. It is given to git only in that command's
environment and is never written to the remote URL or a file. Cloning, pushing and pull requests need write
permission for repository contents and pull requests.

Rules (`[github]` in `config.toml`):

- Reads (status, diff, log, PR and issue lists, CI status) never ask and work in plan mode too.
- Commits, new branches, pushes to unprotected branches, opening PRs and issues, and comments don't ask in auto mode;
  they are refused in plan mode.
- A direct push to `protected_branches` (default `main`, `master`) or the repository's default branch is refused; ALICE
  opens a new branch and a PR instead. If you explicitly ask for it, it does it but asks first. A force push asks too.
- Merging and closing a PR always ask ("always allow" doesn't cover them). It won't merge while CI is red or running,
  the PR is a draft or there is a conflict (`require_checks = false` turns that off). The method is `merge_method`
  (default `squash`).
- A commit containing `.env`, a private key file or a line that looks like a key is refused.
- Merge / delete / close tools of MCP servers (`merge_*`, `delete_*`...) ask in auto mode too.
- **Undercover mode** (`undercover = "off" | "on" | "auto"`, auto = public repositories only): commits, PRs and
  comments go out unsigned and without mentioning ALICE or AI; `undercover_words` lists extra words that must not
  appear.
