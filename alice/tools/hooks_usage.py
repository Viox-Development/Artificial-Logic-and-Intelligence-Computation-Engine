"""hooks tool (add/remove/test hooks from chat) and usage tool (where tokens went in this session)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import DANGER, EXEC, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class HooksTool(Tool):
    name = "hooks"
    description = (
        "Kullanıcının hook'larını yönetir: belli anlarda kendiliğinden çalışan kabuk komutları (Claude Code hook'ları "
        "gibi). 'her dosya düzenlemesinden sonra ruff çalıştır', 'iş bitince testleri koş, kırıksa devam et', "
        "'her mesajda git durumunu ekle' gibi istekler için.\n"
        "Olaylar: PostToolUse (araçtan sonra; çıkış kodu 2 = stderr sana geri bildirim), PreToolUse (araçtan önce; 2 = "
        "engelle), Stop (cevap bitince; 2 = devam et), UserPromptSubmit (her mesajda; stdout mesaja eklenir), "
        "SessionStart (ilk mesajda; stdout bağlama eklenir). matcher: araç adı regex'i (ör. "
        "'edit_file|write_file|multi_edit'). Komutta $ALICE_TOOL_PATH (aracın yol argümanı), $ALICE_TOOL, $ALICE_CWD "
        "kullanılabilir; olay JSON'u stdin'den gelir.\n"
        "action: list | add (event, command, matcher, timeout, on_failure) | remove (index) | test (index: hook'u örnek olayla "
        "bir kez çalıştırıp sonucu gösterir). Hook kalıcıdır ve her sohbette çalışır: eklemeden önce kullanıcıya ne "
        "yapacağını söyle.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "test"], "description": "Yapılacak işlem."},
        "event": {"type": "string", "enum": ["PostToolUse", "PreToolUse", "Stop", "UserPromptSubmit",
                                              "SessionStart"], "description": "add: hook'un çalışacağı an."},
        "command": {"type": "string", "description": "Çalışacak kabuk komutu. PostToolUse komutu stdout'a "
                                                     "{\"updatedToolOutput\": \"...\"} yazarsa araç çıktısı onunla "
                                                     "değişir (ör. sırları gizleme)."},
        "matcher": {"type": "string", "description": "PreToolUse/PostToolUse: araç adı regex'i; boş = her araç."},
        "timeout": {"type": "number", "description": "saniye (varsayılan 30)"},
        "on_failure": {"type": "string", "enum": ["ignore", "block"],
                       "description": "add: hook'un kendisi bozulursa (zaman aşımı/çalışmazsa) ne olsun. Denetim "
                                      "yapan PreToolUse hook'larında 'block' (bozulunca araç geçmesin)."},
        "index": {"type": "integer", "description": "remove/test: list'teki sıra numarası."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        # An added hook runs commands automatically in every chat: it always asks first.
        return {"add": DANGER, "remove": WRITE, "test": EXEC}.get(str(args.get("action")), READ)

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action", "list")
        if a == "add":
            return f"hook ekle: {args.get('event')} {args.get('matcher') or '*'} → {str(args.get('command'))[:60]}"
        return f"hook {a}" + (f" #{args['index']}" if "index" in args else "")

    def run(self, ctx: ToolContext, action: str = "list", event: str = "", command: str = "", matcher: str = "",
            timeout: float = 30.0, index: int = -1, on_failure: str = "ignore") -> ToolResult:
        from alice import hooks as hk

        try:
            if action == "add":
                h = hk.add_hook(event, command, matcher, timeout, on_failure)
                return ToolResult(f"eklendi ({hk.hooks_path()}): {h.event} [{h.matcher or 'her araç'}] {h.command}. "
                                  "Açık sohbetler dahil hemen geçerli.")
            if action == "remove":
                h = hk.remove_hook(int(index))
                return ToolResult(f"silindi: {h.event} {h.command}")
            if action == "test":
                rows = hk.file_hooks()
                if not 0 <= int(index) < len(rows):
                    raise ToolError(f"böyle bir hook yok: {index}")
                h = rows[int(index)]
                r = hk.Hooks([h], ctx.cwd)._run_one(h, {"args": {"path": "ornek.py"}, "result": "", "prompt": "deneme"},
                                                    "edit_file")
                return ToolResult(f"çıkış kodu {r.code}" + (" (engeller/geri bildirim verir)" if r.blocked else "")
                                  + (f"\nstdout: {r.stdout}" if r.stdout else "") + (f"\nstderr: {r.stderr}" if r.stderr
                                                                                     else ""))
        except ValueError as e:
            raise ToolError(str(e)) from e
        return ToolResult(describe_hooks(ctx))


def describe_hooks(ctx: ToolContext) -> str:
    from alice import hooks as hk

    rows = hk.file_hooks()
    cfg_rows = hk._parse(dict(((getattr(ctx.config, "data", None) or {}).get("hooks")) or {}))
    out = [f"{i}. {h.event} [{h.matcher or 'her araç'}] {h.command} (zaman aşımı {h.timeout:g} sn)"
           for i, h in enumerate(rows)]
    out += [f"-. {h.event} [{h.matcher or 'her araç'}] {h.command} (config.toml'da; buradan silinmez)"
            for h in cfg_rows]
    return "\n".join(out) if out else "hook yok"


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


class Usage(Tool):
    name = "usage"
    description = ("Bu oturumda tokenların nereye gittiğini döker: model başına çağrı, girdi (önbellekten gelen "
                   "dahil), çıktı ve süre; araç başına bağlama eklenen çıktı; bağlamın şu anki dağılımı (sistem istemi, "
                   "araç tanımları, mesajlar). 'tokenlarım nereye gitti', 'neden bu kadar harcadı', 'kullanımı göster' "
                   "deyince kullan.")
    parameters = obj({}, [])
    risk = READ

    def run(self, ctx: ToolContext) -> ToolResult:
        agent = ctx.state.get("agent")
        if agent is None or not hasattr(agent, "usage_breakdown"):
            raise ToolError("bu bağlamda oturum istatistiği yok")
        u = agent.usage_breakdown()
        total_in = u["input"] or 1
        lines = [f"{u['turns']} tur, {u['steps']} model çağrısı · model {u['llm_s']} sn, araç {u['tool_s']} sn",
                 f"Girdi {_k(u['input'])} (önbellekten {_k(u['cached'])}, %{100 * u['cached'] // total_in}), "
                 f"çıktı {_k(u['output'])}", "", "Modeller:"]
        lines += [f"  {m['model']}: {m['calls']} çağrı, girdi {_k(m['input'])} (önbellek {_k(m['cached'])}), "
                  f"çıktı {_k(m['output'])}, {m['seconds']} sn" for m in u["models"]] or ["  (henüz çağrı yok)"]
        lines += ["", "Araç çıktıları (bağlama eklenen ~token):"]
        lines += [f"  {t['tool']}: {t['calls']} kez, ~{_k(t['tokens'])}" + (f", {t['errors']} hata" if t["errors"]
                                                                              else "") for t in u["tools"][:12]] \
            or ["  (araç kullanılmadı)"]
        c = u["ctx"]
        lines += ["", f"Bağlam şu an ~{_k(c['total'])}: sistem istemi {_k(c['system'])}, araç tanımları "
                      f"{_k(c['tools'])}, mesajlar {_k(c['messages'])}"]
        return ToolResult("\n".join(lines), card={"kind": "usage", **u})


TOOLS: list[Tool] = [HooksTool(), Usage()]
