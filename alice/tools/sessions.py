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
        "Bir işi AYRI bir sohbette başlatır; o sohbet kendi başına çalışır, bitince sonucu bu sohbete bildirir. "
        "Kullanıcı 'bunu ayrı yap / arkada yap / paralel başlat' derse ya da konuşma başka konuya geçerken yarım "
        "kalan işi devretmek için (action=handoff). Bu turda sonucuna ihtiyacın olan küçük işler için kullanma "
        "(onlar için task).\n"
        "action=start: prompt (eksiksiz görev: ne, nerede, bitince ne olmalı), title. fork=true: yeni sohbet bu "
        "konuşmanın geçmişini de alır (iş burada öğrenilenlere dayanıyorsa). action=handoff: süren işi kalan "
        "planla devret (fork'lu), sonra kullanıcının yeni isteğine dön. action=list: başlattığın oturumlar ve "
        "durumları. action=message: id'li oturuma ek talimat gönder.")
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "handoff", "list", "message"], "description": "Yapılacak iş."},
        "prompt": {"type": "string", "description": "start/handoff/message: görev ya da talimat metni."},
        "title": {"type": "string", "description": "start/handoff: kısa başlık (kenar çubuğunda görünür)."},
        "fork": {"type": "boolean", "description": "start: bu konuşmanın geçmişini yeni sohbete ver. Varsayılan false."},
        "id": {"type": "string", "description": "message: oturum kimliği (list çıktısından)."},
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
