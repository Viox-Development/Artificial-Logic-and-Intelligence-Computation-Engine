"""make_tool: ALICE writes a tool itself when no existing tool can do the job (details: alice/selftools.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import DANGER, EXEC, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

PLUGIN_SKELETON = (
    "from alice.plugins import Plugin\n"
    "from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj\n\n"
    "class <Ad>(Tool):\n    name = '<ad>_<eylem>'\n    description = '...'\n    parameters = obj({...}, [...])\n"
    "    risk = READ\n    def run(self, ctx: ToolContext, ...) -> ToolResult: ...\n\n"
    "PLUGIN = Plugin(name='<ad>', tools=[<Ad>()], description='...', keywords=r'...')"
)


class MakeTool(Tool):
    name = "make_tool"
    description = (
        "Yapamadığın bir iş için yeni bir araç (ALICE eklentisi) yazar, testini kum havuzunda dener, kullanıcının "
        "onayıyla ekler ve aynı konuşmada hemen kullanılır hâle getirir.\n"
        "- Kullan: hiçbir aracın (bağlantılar ve load_tools grupları dahil) işi göremediğinde. Önce kullanıcıya "
        "neyin eksik olduğunu söyle (\"Steam'deki son oyunları listeleyen aracım yok, yazayım mı?\").\n"
        "- Kullanma: var olan bir araç ya da run_command tek seferde işi görüyorsa.\n"
        "Akış: action=test (kod + test + bildirge) -> geçerse kullanıcıya erişimi bir cümleyle sor -> "
        "action=install. action=list: yazdığın araçlar; action=remove: birini kaldır.\n"
        "Kod: alice.plugins.Plugin ve alice.tools.base.Tool kullanan tek dosya; araç adları '<name>_' ile başlar. "
        "Test: pytest, ağ yerine sahte veri (monkeypatch). Bildirgede olmayan internet adresi, klasör ya da "
        "uygulama kullanan kod reddedilir; eval/exec/ctypes yasak."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["test", "install", "list", "remove"],
                   "description": "test: denetle ve kum havuzunda dene. install: test edilmiş aracı kullanıcı "
                                  "onayıyla ekle. list: kendi yazdığın araçlar. remove: kaldır."},
        "name": {"type": "string", "description": "Eklenti adı: küçük harf ve _, ör. 'steam_recent'."},
        "need": {"type": "string", "description": "test: kullanıcının ihtiyacı, kendi sözleriyle."},
        "description": {"type": "string",
                        "description": "test: aracın ne yaptığı, tek cümle (onayda kullanıcıya gösterilir)."},
        "manifest": {"type": "object", "description": (
            "test: neye erişeceği. network: internet adresleri (ör. ['api.steampowered.com']), folders: "
            "okuyacağı/yazacağı klasörler, apps: çalıştıracağı programlar, asks_each_time: kod değiştiriyor, para "
            "harcıyor, mesaj gönderiyor ya da bir şey siliyorsa true (her kullanımda onay ister)."),
            "properties": {"network": {"type": "array", "items": {"type": "string"},
                                       "description": "Erişeceği alan adları, ör. ['api.steampowered.com']."},
                           "folders": {"type": "array", "items": {"type": "string"},
                                       "description": "Okuyacağı/yazacağı klasörler (mutlak yol)."},
                           "apps": {"type": "array", "items": {"type": "string"},
                                    "description": "Çalıştıracağı programlar, ör. ['git']."},
                           "asks_each_time": {"type": "boolean", "description": "true: her kullanımda onay ister "
                                              "(kod değiştiren, para harcayan, mesaj gönderen, silen araçlar)."}}},
        "code": {"type": "string", "description": "test: eklentinin Python kodu. İskelet:\n" + PLUGIN_SKELETON},
        "test": {"type": "string",
                 "description": "test: pytest testi. Eklenti modülü adıyla içe aktarılır: `import <name>`."},
    }, ["action"])
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        return {"list": READ, "install": DANGER, "remove": WRITE}.get(str(args.get("action")), EXEC)

    def summary(self, args: dict[str, Any]) -> str:
        from alice import selftools

        act, name = args.get("action", "?"), args.get("name", "")
        if act == "install":
            return selftools.approval_text(str(name))[:300]
        return f"make_tool {act} {name}".strip()

    def run(self, ctx: ToolContext, action: str, name: str = "", need: str = "", description: str = "",
            manifest: dict[str, Any] | None = None, code: str = "", test: str = "") -> ToolResult:
        from alice import selftools

        if action == "list":
            return ToolResult(selftools.listing())
        if action == "remove":
            return ToolResult(selftools.remove(name.strip().lower()))
        if action == "test":
            agent = ctx.state.get("agent")
            existing = set(getattr(agent, "tools", {}) or {})
            ok, msg = selftools.prepare(name, need, description, manifest or {}, code, test, ctx, existing=existing)
            return ToolResult(msg, is_error=not ok)
        if action == "install":
            try:
                path, tools = selftools.install(name.strip().lower())
            except Exception as e:  # noqa: BLE001 - make the install error meaningful to the model
                raise ToolError(f"eklenmedi: {type(e).__name__}: {e}") from e
            agent = ctx.state.get("agent")
            if agent is not None:  # usable right away in this conversation (visible without waiting for the group)
                for t in tools:
                    agent.tools[t.name] = t
            names = ", ".join(t.name for t in tools)
            return ToolResult(f"Eklendi: {path.name}. Araçlar: {names}. Şimdi kullanabilirsin.")
        raise ToolError("action test, install, list ya da remove olmalı")


TOOLS = [MakeTool()]
