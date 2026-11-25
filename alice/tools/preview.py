"""preview: run the app, open it in the built-in browser and verify the change (Claude Code's "run / verify"
flow). It gives the model the screenshot and separately lists the page's console errors and failed network
requests: "page opened" and "page works correctly" are different things."""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from typing import Any

from alice.tools.base import EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - the user's local server
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (OSError, ValueError):
        return False


def format_logs(rows: list[dict[str, Any]], limit: int = 40) -> str:
    if not rows:
        return "No console errors/warnings, no failed network requests."
    kinds = {"console": "konsol", "exception": "uncaught error", "log": "browser", "network": "network"}
    out = [f"- [{kinds.get(r['kind'], r['kind'])} {r['level']}] {' '.join(str(r['text']).split())[:400]}"
           for r in rows[-limit:]]
    errors = sum(1 for r in rows if r["level"] == "error")
    return f"{errors} errors, {len(rows) - errors} warnings:\n" + "\n".join(out)


class Preview(Tool):
    name = "preview"
    description = (
        "Really verifies a web UI change: starts the dev server (if needed), opens the page in ALICE's built-in "
        "browser (the user watches it live in the panel), and returns a screenshot plus the page's console errors / "
        "failed network requests. Use it after changing something in the UI, before saying 'it works'.\n"
        "action=open: url (e.g. http://localhost:5173); if command is given it is started first in the background "
        "(e.g. npm run dev) and the tool waits until the server answers. action=check: reload the current page (or "
        "url) and get the screenshot and errors. action=logs: only the latest errors.\n"
        "Use the browser tool to click/type on the page; it is the same browser.")
    parameters = obj({
        "action": {"type": "string", "enum": ["open", "check", "logs"], "description": "What to do."},
        "url": {"type": "string", "description": "open: address to open. check: empty means the current page."},
        "command": {"type": "string", "description": "open: server command to start first (in the background)."},
        "wait": {"type": "integer", "description": "open: at most this many seconds for the server to answer. Default "
                                                   "60."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        return EXEC if args.get("command") else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('url') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, url: str = "", command: str = "", wait: int = 60) -> ToolResult:
        factory = ctx.state.get("builtin_browser")
        if not callable(factory):
            raise ToolError("the built-in browser exists only in the ALICE app; use the browser tool")
        from alice.app.builtin_browser import BrowserError

        bb = factory()
        try:
            if action == "logs":
                return ToolResult(format_logs(bb.logs()))
            started = time.time()
            note = ""
            if action == "open":
                if not url:
                    raise ToolError("open needs url")
                if command:
                    note = self._serve(ctx, command, url, max(5, min(int(wait or 60), 300)))
                bb.navigate(url)
            elif action == "check":
                if url:
                    bb.navigate(url)
                else:
                    bb.history("reload")
            else:
                raise ToolError(f"unknown action: {action}")
            loaded = bb.wait_load(20)
            time.sleep(1.2)  # first paint and late-arriving requests
            png = bb.screenshot_png()
            tab = bb.current()
            logs = bb.logs(since=started)
        except BrowserError as e:
            raise ToolError(f"built-in browser: {e}") from e
        head = f"Sayfa: {tab.get('title') or '(untitled)'} — {tab.get('url')}" + ("" if loaded else " (not finished loading)")
        return ToolResult("\n".join(x for x in (note, head, format_logs(logs),
                                                "The screenshot is attached; the user sees it in the panel too.") if x),
                          images=[png] if png else [])

    @staticmethod
    def _serve(ctx: ToolContext, command: str, url: str, wait: int) -> str:
        from alice.tools.shell import RunCommand

        if _reachable(url):
            return "The server was already running; the command was not run."
        res = RunCommand().run(ctx, command, background=True)
        m = re.search(r"pid=(\d+)", res.text)
        proc = ctx.state.get("bg_procs", {}).get(int(m.group(1))) if m else None
        if proc is not None:
            proc.notify = False  # a preview server that closes is no news for the chat
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if _reachable(url):
                return f"Server started (pid={proc.pid if proc else '?'})."
            if proc is not None and proc.popen.poll() is not None:
                tail = proc.log.read_bytes().decode("utf-8", errors="replace")[-1500:] if proc.log.exists() else ""
                raise ToolError(f"the server command ended (code {proc.popen.returncode}); the page didn't open:\n{tail}")
            if ctx.cancelled():
                raise ToolError("durduruldu")
            time.sleep(0.5)
        raise ToolError(f"{url} did not answer within {wait} s (server pid={proc.pid if proc else '?'} keeps running in the background)")


TOOLS: list[Tool] = [Preview()]
