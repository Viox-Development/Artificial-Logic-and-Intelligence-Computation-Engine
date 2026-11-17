# Security Policy

ALICE runs commands, edits files, controls the browser, and handles account tokens on the user's computer, so
security bugs matter more here than in most projects.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report them privately with GitHub's
**"Report a vulnerability"** button on this repository's Security tab.

Include what you can:

- the affected file or feature and the ALICE version (commit),
- steps to reproduce or a proof of concept,
- what an attacker could do with it (for example: run commands without approval, read secrets, escape the
  sandbox).

You will get a first reply within a week. Please give us a reasonable time to ship a fix before publishing
details.

## What counts

Examples of issues we want to hear about:

- running a tool or command without the permission the user's mode requires,
- escaping the sandbox or reading files it hides (keys, tokens, `~/.alice` secrets),
- prompt injection from web pages, emails, files, or repositories that leads to real actions,
- secrets leaking into logs, commits, pull requests, or messages,
- the browser extension, phone app, or local server accepting requests from untrusted origins.

## Supported versions

Only the latest commit on `main` receives security fixes.
