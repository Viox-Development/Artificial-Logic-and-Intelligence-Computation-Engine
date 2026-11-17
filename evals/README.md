# Agent evals

The unit tests in `tests/` check that ALICE's code is correct, using a fake model. This folder measures something
different: **how well ALICE actually gets work done with real models.** Each task is a tiny project, a goal and an
automatic check. The runner gives the goal to ALICE, lets it work with its normal tools, and scores the result.

It also answers one specific question: **does the review council help?** With `--council both` (the default)
every task runs twice. The first run is plain. In the second, the council (critic, verifier and, when relevant,
experts) reviews the finished work, and if it finds a blocking problem ALICE gets one turn to fix it, the same way
team projects use the council.

This is not part of CI. It needs API keys and spends model quota.

## Run it

```bash
pip install -e ".[dev]"
python -m evals --models gemini:gemini-3.5-flash,gemini:gemini-flash-latest,groq:openai/gpt-oss-120b
```

One command runs every task with the council off and on, then writes:

- `evals/results/<timestamp>/scoreboard.md`: summary, per-task table, the council on/off comparison and the failures;
- `evals/results/<timestamp>/results.json`: every run with its checks, steps, tool calls, tokens, time, models used,
  model switches and the council verdict;
- `evals/results/<timestamp>/transcripts/`: the full conversation of each run, for finding out why a task failed.

Results are written after every run, so a crash or Ctrl+C keeps what finished. `evals/results/` is git-ignored.

Useful options:

| Option | What it does |
|---|---|
| `--models a:x,b:y` | Model chain for **every** role, the agent and the council members alike. Without it your own `[roles]` from `~/.alice/config.toml` are used, which may include paid or subscription models. |
| `--role council=a:x` | Chain for one role (`brain`, `fast`, `vision`, `council`); overrides `--models` for that role. |
| `--council off\|on\|both` | Plain only, council only, or both (default). |
| `--only log` | Only tasks whose id or category contains the text (repeatable). |
| `--max-steps N`, `--timeout S` | Override the per-task limits (default 40 steps and 600 s). |
| `--keep` | Keep the temporary task folders so you can look at what ALICE left behind. |
| `--list` | List the tasks. |
| `--render results.json` | Rebuild the scoreboard from a saved result. |

### What a run does

1. Copies `tasks/<id>/repo/` to a fresh temporary folder.
2. Builds an ALICE agent there: no long-term memory, no MCP servers, your permission rules ignored, permission mode
   `auto`. Permission questions are answered "yes", except for sending messages. Tools that reach outside the
   folder are removed: desktop control, the browser window, mail, phone, memory, GitHub, schedules, and the council
   tool itself. File, shell, search, web and planning tools stay.
3. Sends the goal and stops the agent at the step or time limit.
4. With the council on: convenes a `work` council over the goal, the agent's reply and the diff. If the council's
   result is blocking (a confirmed or unclear critical/major finding), the agent gets one fix turn with the
   council's fix list.
5. Copies `tasks/<id>/hidden/` over the folder (the original tests and check scripts the agent never saw, so
   editing or deleting tests does not help) and runs the checks.

The agent's shell finds the runner's Python (with pytest) as `python`. `git config --global` inside a task writes
to a throwaway file, not your real git config.

### What it measures

| Field | Meaning |
|---|---|
| passed | All checks passed and the run did not crash. |
| steps | Successful model calls by the agent (each step of the agent loop is one call), including the fix turn. |
| tool_calls | Tool calls the agent made. |
| tokens | Prompt + completion tokens the agent used. Council members are counted separately (`council_tokens`). |
| model_switches | How often the serving model changed between consecutive agent calls (failover or chain order). |
| failed_calls | Model requests that failed (rate limit, error) and were retried on another model. |
| council_review | Decision, score, number of live findings, whether it was blocking and whether a fix turn ran. |

## What it costs

There is no money cost on free tiers, but it uses quota. The first run of the full set (14 tasks, council off and
on) is in the table below; use it to estimate whether your daily free limits are enough.

COST_TABLE

Free-tier limits change often. If a provider runs out mid-run, ALICE fails over to the next model in the chain, and
the scoreboard shows that as model switches and failed calls. Long chains of different models make results noisier.
For a fair comparison, use the same chain for both modes, which is what one `--council both` run does.

A single run per task and mode is noisy: one flipped task can just be model variance. Run the set a few times, or
compare totals, before drawing conclusions.

## Run it on GitHub

`.github/workflows/evals.yml` is a manual (`workflow_dispatch`) job. Open Actions → Agent evals → Run workflow,
choose the models, the council mode and optionally a task filter. It uses the repository secrets `GEMINI_API_KEY`,
`GROQ_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `NVIDIA_API_KEY` and `MISTRAL_API_KEY` when they exist,
and skips with a warning when none is set. The scoreboard is shown on the run's summary page and the full results
are uploaded as an artifact. It never runs on push or pull requests.

## The tasks

| Task | Category | What ALICE has to do | Checked by |
|---|---|---|---|
| fix-pagination | bugfix | Two off-by-one bugs make the tests fail | original tests + one extra |
| fix-mutable-default | bugfix | A bug report about state shared between objects; no tests given | hidden tests |
| fix-csv-quotes | bugfix | A report script splits CSV on commas and crashes on quoted fields | script output + hidden tests |
| implement-roman | feature | Implement two functions from their docstrings, including strict validation | hidden tests |
| add-tests-slugify | tests | Write tests for every rule of a function | tests must pass, and catch 4 of 5 broken copies |
| refactor-extract-helper | refactor | Extract duplicated code into a helper without changing behaviour | hidden tests + duplicate count |
| rename-across-files | refactor | Rename a function in a package, its callers and tests | old name gone + hidden tests |
| add-field-multifile | multi-file | Add a field through model, storage (backward compatible) and CLI | hidden tests |
| cli-json-flag | multi-file | Add `--json` to a CLI with the formatting in another module | check script |
| log-top-error | log-reading | Count ERROR lines per user and per code in a 420-line log | answer in the reply |
| log-root-cause | log-reading | Find the first error among cascading failures and decoys | answer in the reply |
| shell-git-init | shell | Create a repo with local identity, commit and tag | git output |
| shell-checksums | shell | Write a `sha256sum`-format file for binary data | check script |
| web-rfc-lookup | web | Look up which RFC replaced RFC 4122 and when | answer + a web tool was used |

## Add a task

Create `evals/tasks/<id>/` with:

```
task.toml        goal and checks
repo/            the project ALICE starts from (put an empty conftest.py at the root for pytest imports)
hidden/          optional: copied over the result before checking (original tests, check scripts)
reference/       a correct solution, used by tests/test_agent_evals.py:
  files/           files copied over repo/
  solve.py         optional script run in the folder (for shell tasks)
  reply.txt        optional final reply (for reply checks)
  tools.txt        optional tool names the agent would call (for tool_used checks)
```

`task.toml`:

```toml
category = "bugfix"                # bugfix, feature, tests, refactor, multi-file, log-reading, shell, web, ...
needs = ["shell"]                  # optional, informational (the git task is skipped in tests without git)
max_steps = 40                     # optional
timeout = 600                      # optional, seconds
goal = """
What ALICE should do, written the way a user would ask.
"""

[[check]]
type = "command"                   # exit code 0 (or `exit = N`), optional regex on stdout
run = "python -m pytest -q tests"  # no shell; `python` is the runner's interpreter
output = '^ok$'

[[check]]
type = "file_contains"             # regex in a file or glob ("**/*.py")
path = "app.py"
pattern = 'def main\('
# absent = true                    # must NOT match anywhere
# max_count = 1                    # at most this many matches

[[check]]
type = "reply"                     # regex (case-insensitive) in the final reply
pattern = '\b42\b'

[[check]]
type = "tool_used"                 # at least one of these tools was called
any = ["web_search", "web_fetch"]
```

Write regexes as TOML literal strings (single quotes) so backslashes need no escaping.

Then run `python -m pytest -q tests/test_agent_evals.py`. It checks that the task **fails** on the untouched
`repo/` and **passes** with `reference/`. A task that passes without any work, or that a correct solution cannot
pass, measures nothing.

Good tasks are small (a few minutes for a strong model), have one clearly right outcome, and are checked by
something the agent cannot game: hidden tests, a script, or an exact answer it can only get by doing the work.
