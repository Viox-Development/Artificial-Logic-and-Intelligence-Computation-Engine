# Contributing to ALICE

Thanks for your interest in ALICE! Bug fixes, tests, documentation, and new tools are all welcome.

## Before you start

- Look for issues labeled **good first issue** or **help wanted**. Comment on the issue so others know you are
  working on it.
- For larger changes, open an issue first and describe what you plan to do. This avoids work that cannot be merged.
- Please write issues, pull requests, commit messages, and new code comments in English.

## License and CLA

ALICE is licensed under the [GNU Affero General Public License v3.0](LICENSE).

Before your first pull request can be merged, you need to sign the
[Contributor License Agreement](CLA.md). You keep the copyright to your work; the agreement lets the maintainer
manage the project's license over time. The CLA check on your pull request explains how to sign with a single
comment. You only sign once.

## Development setup

```bash
pip install -e ".[dev]"
pre-commit install
ruff check alice tests
python -m pytest -q
```

CI runs the same lint and tests on Windows and Linux with Python 3.10, 3.12, and 3.13.

The tests use a fake model. To see how well ALICE actually solves tasks with real models, and whether the review
council helps, run the agent evals: `python -m evals --models <provider:model,...>`. They need API keys, use model
quota and are not part of CI. See [evals/README.md](evals/README.md).

## Pull request checklist

- The change is focused on one thing. Unrelated cleanups go in a separate pull request.
- New behavior and bug fixes come with a test that fails without the change.
- `ruff check alice tests` and the related tests pass locally.
- The pull request description says what changed, why, and how you tested it.
- No secrets: never commit API keys, tokens, `.env` files, cookies, or personal data, not even in tests. Use
  obviously fake values such as `sk-test-0000`.

## Security-sensitive areas

Changes to these parts get a stricter review, because ALICE runs commands and handles credentials on the user's
computer:

- `alice/permissions.py`, `alice/sandbox.py`, `alice/sandbox_run.py`, `alice/guard.py`
- `alice/gitsafe.py`, `alice/tools/shell.py`, `alice/tools/github.py`
- `alice/tools/browser_ext.py`, `alice/extension/`, `alice/connect/`
- anything that reads, stores, or sends tokens, passwords, or cookies

If you find a security vulnerability, please do **not** open a public issue. Report it privately through
GitHub's "Report a vulnerability" button on the Security tab.
