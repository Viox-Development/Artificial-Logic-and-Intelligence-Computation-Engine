"""permissions: suggests "stop asking" rules for jobs the user always approves, and applies rules.
share: publish a report/page as a secret Gist on the user's GitHub account and give the link."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, READ, SEND, Tool, ToolContext, ToolError, ToolResult, obj


class PermissionsTool(Tool):
    name = "permissions"
    description = (
        "Permission rules. suggest: proposes 'do it without asking from now on' rules from actions the user always "
        "approved before (at least 3 times, never refused); dangerous and outward actions are not suggested. apply: "
        "adds the rules the user explicitly chose to this project's allow list. list: the rules in effect in this "
        "project.\n"
        "Show the suggestions to the user first; let them decide which to apply, don't apply them yourself.")
    parameters = obj({
        "action": {"type": "string", "enum": ["suggest", "apply", "list"], "description": "What to do."},
        "rules": {"type": "array", "items": {"type": "string", "description": "Rule, e.g. run_command(npm test*)."},
                  "description": "apply: rules to add (from the suggest output, the ones the user chose)."},
        "everywhere": {"type": "boolean", "description": "suggest: look at everything asked, not only in this "
                                                         "project."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        # Adding a permission rule removes all later approvals, so it is always asked of the user (in a turn
        # where external content was read, the model must not grant itself permission).
        return DANGER if args.get("action") == "apply" else READ

    def run(self, ctx: ToolContext, action: str, rules: list[str] | None = None,
            everywhere: bool = False) -> ToolResult:
        from alice import permissions as perm

        if action == "list":
            r = perm.project_rules(ctx.cwd)
            text = "\n".join(f"{k}: {', '.join(r.get(k) or []) or '-'}" for k in ("allow", "ask", "deny"))
            return ToolResult(f"Bu proje ({ctx.cwd}):\n{text}")
        if action == "suggest":
            rows = perm.suggestions(None if everywhere else ctx.cwd)
            if not rows:
                return ToolResult("no suggestions: no frequent, always-approved job has built up yet")
            return ToolResult("Jobs you always approve (rule · times approved):\n"
                              + "\n".join(f"- {s['rule']} · {s['yes']} kez" for s in rows[:20])
                              + "\nWhich of them should I do without asking from now on? (added with apply)")
        if action == "apply":
            good = [r.strip() for r in rules or [] if r.strip()]
            if not good:
                raise ToolError("apply needs rules")
            for r in good:
                name, _ = perm.parse_rule(r)
                if not name or name == "*":
                    raise ToolError(f"rule too broad: {r}")
            where = None
            for r in good:
                where = perm.persist_allow(ctx.cwd, r)
            return ToolResult(f"{len(good)} kural eklendi ({where}): " + ", ".join(good))
        raise ToolError(f"unknown action: {action}")


class Share(Tool):
    name = "share"
    description = (
        "Makes a file (report, page, markdown, code) shareable by link: publishes it as a secret Gist on the user's "
        "GitHub account (anyone with the link can see it; it doesn't show up in search). Markdown and code show "
        "directly on GitHub; for HTML a preview address that opens in the browser is also given.\n"
        "It publishes content outside: use it only when the user wants to share; don't share a file with secrets "
        "(keys, passwords, personal data).")
    parameters = obj({
        "paths": {"type": "array", "items": {"type": "string", "description": "Dosya yolu."},
                  "description": "Files to share (at most 10, up to 1 MB each)."},
        "title": {"type": "string", "description": "Short description (Gist title)."},
        "public": {"type": "boolean", "description": "Public Gist (shows up in search). Default false (secret)."},
    }, ["paths"])
    risk = SEND

    def summary(self, args: dict[str, Any]) -> str:
        return ", ".join(str(p) for p in (args.get("paths") or [])[:3])

    def run(self, ctx: ToolContext, paths: list[str], title: str = "", public: bool = False) -> ToolResult:
        from alice.tools.github import _api

        files: dict[str, dict[str, str]] = {}
        for raw in (paths or [])[:10]:
            p = ctx.resolve(str(raw))
            if not p.is_file():
                raise ToolError(f"dosya yok: {p}")
            if p.stat().st_size > 1_000_000:
                raise ToolError(f"file too large (over 1 MB): {p.name}")
            text = p.read_text(encoding="utf-8", errors="replace")
            if _looks_secret(p, text):
                raise ToolError(f"{p.name} contains what looks like a key/password; not shared")
            files[p.name] = {"content": text}
        if not files:
            raise ToolError("no files to share")
        gist = _api(ctx).post("/gists", {"description": title or ", ".join(files), "public": bool(public),
                                         "files": files})
        url = str(gist.get("html_url") or "")
        lines = [f"Shared ({'public' if public else 'secret, anyone with the link can see it'}): {url}"]
        for name, f in (gist.get("files") or {}).items():
            raw = str(f.get("raw_url") or "")
            if Path(name).suffix.lower() in (".html", ".htm") and raw:
                lines.append(f"{name} sayfa olarak: {raw.replace('gist.githubusercontent.com', 'gistcdn.githack.com')}")
        lines.append("To delete it: Delete on the Gist page on GitHub.")
        return ToolResult("\n".join(lines))


def _looks_secret(p: Path, text: str) -> bool:
    from alice.tools.github import SECRET_FILES, SECRET_RX

    return bool(SECRET_FILES.search(p.name) or SECRET_RX.search(text))


TOOLS: list[Tool] = [PermissionsTool(), Share()]
