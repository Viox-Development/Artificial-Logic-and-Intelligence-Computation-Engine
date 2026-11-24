"""GitHub Actions workflow auditing: finds known dangerous patterns rule-based (no model required).

Inspired by the workflow hardening check in the claude-code-action repository; the rules are not specific
to that organization, but known vulnerabilities valid in any repository:

1. Checking out a PR's code on a privileged trigger (pull_request_target / workflow_run) ("pwn request"):
   the workflow runs with repository secrets and write permissions, and the attacker's code runs with them too.
2. Embedding untrusted text directly with ${{ }} inside run:/script: (command injection): issue title,
   PR body, comment, branch name, commit message are attacker-controlled text.
3. permissions: write-all — all permissions to every job.
4. Not pinning a third-party action to a full commit SHA: the tag can be moved to different code later.

Works line-by-line (PyYAML not required); the goal is to produce definitive, explainable findings during review. Line numbers
start at 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORKFLOW_RX = re.compile(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", re.I)
_PRIVILEGED_RX = re.compile(r"^\s*-?\s*['\"]?(pull_request_target|workflow_run)['\"]?\s*(:|$|,|\])|"
                            r"\[\s*[^\]]*\b(pull_request_target|workflow_run)\b")
# Context fields that the attacker can write to (GitHub's "untrusted input" list).
_UNTRUSTED = (r"github\.event\.(?:issue\.(?:title|body)|pull_request\.(?:title|body|head\.ref|head\.label|"
              r"head\.repo\.default_branch)|comment\.body|review\.body|review_comment\.body|"
              r"discussion\.(?:title|body)|head_commit\.(?:message|author\.(?:name|email))|"
              r"commits(?:\[[^\]]*\]|\.\*)\.(?:message|author\.(?:name|email))|pages(?:\[[^\]]*\]|\.\*)\.page_name|"
              r"workflow_run\.(?:head_branch|head_commit\.message|display_title))|github\.head_ref")
_INJECT_RX = re.compile(r"\$\{\{[^}]*\b(" + _UNTRUSTED + r")\b[^}]*\}\}")
_PR_HEAD_RX = re.compile(r"\$\{\{\s*(github\.event\.pull_request\.head\.(?:sha|ref|repo\.full_name)|github\.head_ref|"
                         r"github\.event\.workflow_run\.head_(?:sha|branch)|"
                         r"format\([^)]*pull/[^)]*\))")
_USES_RX = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^@\s'\"]+)@([^\s'\"#]+)")
_SHA_RX = re.compile(r"^[0-9a-f]{40}$")
_BLOCK_KEY_RX = re.compile(r"^(\s*)(?:-\s*)?(run|script):\s*(.*)$")
_TRUSTED_OWNERS = {"actions", "github"}


@dataclass
class Issue:
    line: int
    severity: str  # critical | high | medium | low (same as review.SEVERITIES)
    title: str
    detail: str
    fix: str


def is_workflow(path: str) -> bool:
    return bool(_WORKFLOW_RX.search(path.replace("\\", "/")))


def _code(line: str) -> str:
    """Is it a comment line? Do not audit comments (example code might be in a comment)."""
    return "" if line.lstrip().startswith("#") else line


def _privileged(lines: list[str]) -> bool:
    """Are there any privileged triggers among the triggers (before the jobs: section)."""
    for raw in lines:
        line = _code(raw)
        if re.match(r"^jobs\s*:", line):
            return False
        if _PRIVILEGED_RX.search(line):
            return True
    return False


def _script_lines(lines: list[str]) -> set[int]:
    """Line numbers (0-indexed) of run: and script: (actions/github-script) contents."""
    out: set[int] = set()
    i = 0
    while i < len(lines):
        m = _BLOCK_KEY_RX.match(_code(lines[i]))
        if not m:
            i += 1
            continue
        indent, rest = len(m.group(1)), m.group(3).strip()
        if rest and not rest.startswith(("|", ">")):
            out.add(i)  # single line: run: echo ...
            i += 1
            continue
        out.add(i)
        j = i + 1
        while j < len(lines):
            body = lines[j]
            if body.strip() and len(body) - len(body.lstrip()) <= indent:
                break
            out.add(j)
            j += 1
        i = j
    return out


def audit(text: str) -> list[Issue]:
    lines = text.splitlines()
    issues: list[Issue] = []
    privileged = _privileged(lines)
    scripts = _script_lines(lines)
    for i, raw in enumerate(lines):
        line = _code(raw)
        if not line:
            continue
        n = i + 1
        if i in scripts:
            m = _INJECT_RX.search(line)
            if m:
                issues.append(Issue(
                    n, "high", f"Command injection: {m.group(1)} directly in a script",
                    (f"`{m.group(1)}` is text anyone outside can write (title, body, comment, branch name, "
                    "commit message). ${{ }} is pasted into the text before the script runs: if it holds something like "
                    "`\"; curl ... | sh`, it runs as a command on the runner and can reach the workflow's secrets."),
                    "Put the value in an environment variable (env: TITLE: ${{ ... }}) and use it quoted in the script as \"$TITLE\"."))
        if privileged and _PR_HEAD_RX.search(line) and re.search(r"\b(ref|repository)\s*:", line):
            issues.append(Issue(
                n, "critical", "PR code is checked out under a privileged trigger (pwn request)",
                ("A pull_request_target/workflow_run workflow runs with the default branch's permissions and secrets. Checking "
                "out and building/running the PR's code (npm install, make, test scripts) gives the attacker's code those "
                "secrets and the write access of GITHUB_TOKEN."),
                ("Move the jobs that run PR code to the `pull_request` trigger; if secrets are needed, pass the result as an "
                "artifact to a separate workflow_run job and don't run PR code there.")))
        if re.match(r"^\s*permissions\s*:\s*['\"]?write-all['\"]?\s*$", line):
            issues.append(Issue(
                n, "medium", "permissions: write-all",
                "Gives the token write access to everything in the repository; an injection in a job does the most damage.",
                "Write only the permissions needed (e.g. contents: read, pull-requests: write)."))
        m = _USES_RX.match(line)
        if m:
            name, ref = m.group(1), m.group(2)
            owner = name.split("/", 1)[0].lower()
            if (not name.startswith(("./", "docker://")) and "/" in name and owner not in _TRUSTED_OWNERS
                    and not _SHA_RX.match(ref)):
                issues.append(Issue(
                    n, "low", f"Action not pinned to a commit: {name}@{ref}",
                    ("A tag or branch can later be moved to another commit; the action's owner (or whoever takes over their "
                    "account) runs any code they like in the workflow."),
                    f"Use the full commit SHA: uses: {name}@<40-character sha>  # {ref}"))
    return issues
