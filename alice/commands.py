"""Slash commands: built-ins + custom commands written by the user.

A custom command is a Markdown file. ~/.alice/commands/ (valid everywhere) or
the project folder's .alice/commands/ (.claude/commands/ is also read, for Claude Code compatibility).

  ~/.alice/commands/review.md:
      ---
      description: Review changes
      ---
      Review the recent changes in this project and list the bugs. Focus: $ARGUMENTS

Typing `/review performans` sends the file body to ALICE as a normal message, with $ARGUMENTS replaced by
"performans" (Turkish for "performance"); the words after the name can be in any language.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alice.config import alice_home
from alice.i18n import _

# English; catalog() shows them in the interface language.
BUILTINS: dict[str, str] = {
    "help": "Show the commands",
    "clear": "Reset the conversation (new chat)",
    "compact": "Summarize the context now",
    "btw": "Ask a side question while a job runs: it doesn't interrupt the job and isn't written into the conversation (/btw <question>)",
    "mode": "Permission mode: plan | ask | auto | yolo",
    "plan": "Switch to plan mode (only reads, writes a plan)",
    "model": "Role chains / pin the model",
    "think": "Thinking level: off | low | medium | high | default (a role can be given: /think brain off)",
    "rewind": "Rewind to the previous message (files included)",
    "usage": "Today's requests and token usage",
    "cwd": "Show/change the working folder",
    "tools": "List of tools",
    "mcp": "Connected MCP servers and their tools",
    "init": "Create an ALICE.md instruction file for this project",
    "memory": "Show the lasting memory",
    "kurul": "Ask the review council about something: the critic, advocate, checker and experts debate and vote",
    "review": "Review the changes (git diff, or a given PR/branch) for bugs and risks; /review deep: separate reviewer "
              "agents (performance, tests and docs are possible too)",
    "security-review": "Look for security holes in the changes (leaked secrets, injection, authorization, unsafe commands)",
}

# If the user has a custom command with the same name, theirs wins (old ~/.alice/commands/review.md files keep working).
OVERRIDABLE = {"review", "security-review"}

REVIEW_PROMPT = (
    "Do a code review. Scope: {target}. First find the change: look at status and diff with the git tool (if the scope "
    "is a PR or a branch, follow that); read the relevant parts of the changed files. Report only REAL problems: wrong "
    "logic, edge cases, race conditions, broken calls, missing error handling, behavior the tests miss. For each "
    "finding: file:line, what is wrong, which input breaks it, the suggested fix. Order by severity (critical, major, "
    "minor). Put style tastes and guesses you're unsure of under a separate 'maybe' heading. Do NOT change anything; "
    "only report. Write the report in the user's language."
)
# /review deep|derin|performans|test ...: run the code_review tool with that focus, using independent agent(s).
DEEP_WORDS = {"deep": "deep", "derin": "deep", "performance": "performance", "performans": "performance",
              "tests": "tests", "test": "tests", "docs": "docs", "doküman": "docs", "dokuman": "docs"}
TOOL_REVIEW_PROMPT = (
    "Run the code_review tool with focus={focus}. Scope: {target} (if a PR number is given, pass target=pr and "
    "number). Show me the findings in order of severity; do NOT change anything, fix things when I ask. Answer in "
    "the user's language."
)
SECURITY_PROMPT = (
    "Do a security review. Scope: {target}. Find the change with the git tool and read the changed code. Look for: "
    "keys and passwords that got into the code/commit, command/SQL/path injection, untrusted input reaching a shell or "
    "eval, missing authorization checks, unsafe deserialization, turning off TLS verification, logging sensitive "
    "data, content from outside being treated as instructions. For each finding: file:line, the attack scenario "
    "(who, with what input, what they get), severity (critical/high/medium/low), the fix. If there are no findings, say "
    "'no findings' plainly. Do NOT change anything. Write the report in the user's language."
)


def builtin_prompt(text: str) -> str | None:
    """Built-in commands sent to the model as a message (/init, /kurul, /review, /security-review); else None."""
    name, args = _split(text.strip())
    name = name.lower()
    if name == "init":
        return INIT_PROMPT
    if name == "kurul":
        return council_prompt(args)
    if name == "review":
        words = args.split()
        focus = next((DEEP_WORDS[w.lower()] for w in words if w.lower() in DEEP_WORDS), "")
        if focus:
            rest = " ".join(w for w in words if w.lower() not in DEEP_WORDS)
            return TOOL_REVIEW_PROMPT.format(focus=focus, target=rest or "local changes (target=local)")
    if name in ("review", "security-review"):
        target = args or "the uncommitted changes in the working folder, or the last commit if there are none"
        return (REVIEW_PROMPT if name == "review" else SECURITY_PROMPT).format(target=target)
    return None

INIT_PROMPT = (
    "Study the project in this folder (structure, languages, build/test commands, important files, code style) and "
    "create a short, dense instruction file named ALICE.md at the project root. Write everything an agent that works "
    "on this project in the future will need: how to run it, how to test it, the folder structure, rules to watch. "
    "Build on the README and on existing ALICE.md/CLAUDE.md/AGENTS.md files if there are any; don't repeat them."
)


def council_prompt(args: str) -> str:
    """/kurul <topic>: the message that asks the review council about the topic."""
    topic = args.strip() or "the topic or change we last talked about in this conversation"
    return (f"Ask the review council about this (council tool; critical=true if the topic is critical): {topic}\n\n"
            "Summarize briefly for me the council's decision, the verified findings and who defended what and why. "
            "Answer in the user's language.")


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    path: Path

    def expand(self, args: str) -> str:
        if "$ARGUMENTS" in self.body:
            return self.body.replace("$ARGUMENTS", args)
        return self.body + (f"\n\n{args}" if args else "")


def _parse(path: Path) -> CustomCommand:
    text = path.read_text(encoding="utf-8", errors="replace")
    desc = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                k, _sep, v = line.partition(":")
                if k.strip() == "description":
                    desc = v.strip()
            text = text[end + 4:]
    body = text.strip()
    if not desc:
        desc = body.splitlines()[0][:80] if body else ""
    return CustomCommand(path.stem.lower(), desc, body, path)


def load_custom(cwd: Path) -> dict[str, CustomCommand]:
    out: dict[str, CustomCommand] = {}
    # Later entries overwrite earlier ones: project commands take priority over personal commands.
    for d in (alice_home() / "commands", cwd / ".claude" / "commands", cwd / ".alice" / "commands"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                cmd = _parse(f)
            except OSError:
                continue
            if cmd.name not in BUILTINS or cmd.name in OVERRIDABLE:
                out[cmd.name] = cmd
    return out


def catalog(cwd: Path) -> list[dict[str, str]]:
    custom = load_custom(cwd)
    items = [{"name": n, "description": _(d), "kind": "builtin"} for n, d in BUILTINS.items() if n not in custom]
    items += [{"name": c.name, "description": c.description, "kind": "custom"} for c in custom.values()]
    return items


def expand_custom(text: str, cwd: Path) -> str | None:
    """If '/name args' is a custom command, return the expanded message; otherwise None."""
    if not text.startswith("/"):
        return None
    name, args = _split(text)
    cmd = load_custom(cwd).get(name.lower())
    return cmd.expand(args) if cmd else None


def _split(text: str) -> tuple[str, str]:
    """'/name args' -> (name, args). The name ends at the first space or at the end of the line:
    "/kurul\\nkonu" (Shift+Enter). It used to split only at a space, so the name became "kurul\\nkonu" and
    "bilinmeyen komut" ("unknown command") was reported."""
    parts = text[1:].split(None, 1)
    return (parts[0] if parts else ""), (parts[1].strip() if len(parts) > 1 else "")
