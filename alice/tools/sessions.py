"""session: start a task in a separate chat, or hand an unfinished task off to one (equivalent of
start_session and hand_off in Claude Code).

A separate chat runs on its own (shown in the sidebar, the user can steer it). When its turn ends, it reports
the result here as an "[Oturum bitti]" message. fork=true also gives the new chat this conversation's history,
so work based on what was learned here is not researched again. Runs only inside the app (ctx.state["sessions"]).
"""

from __future__ import annotations

from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class Session(Tool):
    name = "session"
    description = (
        "Starts a job in a SEPARATE conversation; it runs on its own and reports its result to this one when done. "
        "Use it when the user says 'do this separately / in the background / start it in parallel', or to hand off "
        "unfinished work when the conversation moves on to another topic (action=handoff). Not for small jobs whose "
        "result you need this turn (use task for those).\n"
        "action=start: prompt (the complete task: what, where, what should be true when done), title. fork=true: the "
        "new conversation also gets this conversation's history (if the job builds on what was learned here). "
        "action=handoff: hand off the ongoing work with the remaining plan (forked), then go back to the user's new "
        "request. action=list: the sessions you started and their status. action=message: send extra instructions to "
        "the session with id.")
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "handoff", "list", "message"], "description": "What to do."},
        "prompt": {"type": "string", "description": "start/handoff/message: the task or instruction text."},
        "title": {"type": "string", "description": "start/handoff: short title (shown in the sidebar)."},
        "fork": {"type": "boolean", "description": "start: give this conversation's history to the new conversation. "
                                                   "Default false."},
        "id": {"type": "string", "description": "message: session id (from the list output)."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "list" else WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('title') or args.get('id') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, prompt: str = "", title: str = "", fork: bool = False,
            id: str = "") -> ToolResult:  # noqa: A002
        api = ctx.state.get("sessions")
        if api is None:
            raise ToolError("ayrı oturum yalnız ALICE uygulamasında başlatılabilir")
        if action == "list":
            rows = api.linked()
            if not rows:
                return ToolResult("bu sohbetten başlatılmış oturum yok")
            return ToolResult("\n".join(f"{r['id']}  {r['title']}  [{r['state']}]" for r in rows))
        if action == "message":
            if not id or not prompt:
                raise ToolError("message için id ve prompt gerekli")
            err = api.message(id, prompt)
            if err:
                raise ToolError(err)
            return ToolResult(f"{id} oturumuna iletildi")
        if action not in ("start", "handoff"):
            raise ToolError(f"bilinmeyen action: {action}")
        if not prompt.strip():
            raise ToolError("prompt gerekli: yeni oturumun eksiksiz görevi")
        handoff = action == "handoff"
        sid, err = api.start(prompt.strip(), (title or prompt.strip().splitlines()[0])[:80], fork or handoff)
        if err:
            raise ToolError(err)
        tail = (" Bu işi burada bırak; kullanıcının yeni isteğine dön." if handoff else
                " Bitince sonucu bu sohbete '[Oturum bitti]' mesajıyla gelecek; beklemeden devam edebilirsin.")
        return ToolResult(f"Ayrı oturum başladı: {sid} ('{title or prompt[:40]}'). Kenar çubuğunda görünür.{tail}")


TOOLS: list[Tool] = [Session()]
