# Notes for AI coding agents

This file is for coding agents (Claude Code, Codex, Copilot, Cursor, Devin, OpenHands, ...) working on ALICE. People
can read [CONTRIBUTING.md](CONTRIBUTING.md); everything here applies to them too.

## Pick a task

- Issues labeled **good first issue** and **help wanted** are written to be picked up as they are: each names the
  files, what to change and what "done" means. Start there.
- Comment on the issue before you start, so two people (or agents) don't do the same work.
- One issue per pull request. Anything you notice on the way goes in a new issue, not in the same pull request.

## Set up and check

Python 3.10+. Linux, macOS and Windows all work for development; nothing needs a GPU, an API key or the network.

```bash
pip install -e ".[dev]" pytest-cov
ruff check alice tests                        # lint (CI fails on any finding)
pyright                                       # type check (alice/core only, see [tool.pyright])
python -m pytest -q -n auto                   # the whole suite, ~2 minutes in parallel
python -m pytest tests/test_router.py -q      # or the tests for the part you changed
```

Coverage of one module: `python -m pytest tests/test_x.py --cov=alice.x --cov-report=term-missing`.

## Rules the tests and CI enforce

- **Tests never touch the real machine.** `tests/conftest.py` points `ALICE_HOME` to a temp folder, blocks real
  browsers and project scans. Use `tmp_path` and `monkeypatch`; fake network calls, subprocesses, models
  (`FakeProvider`, `make_config` in `conftest.py`) and OS APIs. A test must pass offline on all three platforms.
- **User-facing text is translated.** Write it in English wrapped in `_()` (`from alice.i18n import _`), and add the
  Turkish translation to `alice/locales/tr.json` with the same `{placeholders}`. Keep the file's key order sorted and
  its format (`json.dumps(..., ensure_ascii=False, indent=1)`), or the diff touches every line.
  `tests/test_i18n.py` checks this, also for strings in `alice/app/static/*.js`.
- **Model-facing text** (prompts, tool descriptions, tool results) is English and not translated.
- **Config defaults** live in `DEFAULT_CONFIG_TOML` in `alice/config.py`, with a comment for each new setting.
- **Security-sensitive files** (see [CONTRIBUTING.md](CONTRIBUTING.md#security-sensitive-areas)) get a stricter
  review: keep changes small and add tests for the failure paths.
- No secrets anywhere, not even in tests: use obviously fake values like `sk-test-0000`.

## Style

- English for code, comments, commit messages, issues and pull requests.
- Comments say why, not what. Match the surrounding code; ruff's line length is 120.
- Fix the cause, not the symptom: never skip or loosen a test to make CI green.

## Pull request

- Fill in the template (`.github/pull_request_template.md`): what and why, how you tested it, the checklist.
- Link the issue (`Fixes #123`).
- The first pull request of every author is asked to sign the [CLA](CLA.md) with a one-line comment. If an agent
  opened the pull request for you, you (the person) sign it.
