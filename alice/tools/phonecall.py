"""phone_call: ALICE calls the user by voice from the ALICE app on their phone (alice/app/call.py)."""

from __future__ import annotations

from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj


class PhoneCall(Tool):
    name = "phone_call"
    description = (
        "Kullanıcıyı telefonundaki ALICE uygulamasından sesli ara: telefonda gelen arama ekranı açılır, açarsa "
        "seninle konuşur (ayrı bir sesli görüşmede, araçların açık).\n"
        "- Kullan: kullanıcı \"beni ara\", \"X'te beni ara\", \"bitince ara\" dediğinde (zamanlıysa önce bir görev/"
        "hatırlatma kur, vakti gelince bu aracı çağır) ya da bildirimle geçiştirilemeyecek kadar önemli bir şey "
        "olduğunda.\n"
        "- message: kullanıcı açınca söyleyeceğin ilk cümle (kısa, doğal, neden aradığın). reason: bildirimde "
        "görünen kısa neden.\n"
        "- Araç aramayı başlatır ve hemen döner; açılmazsa 45 sn sonra cevapsız arama olarak kalır."
    )
    parameters = obj({
        "message": {"type": "string", "description": "Açınca ilk söyleyeceğin cümle"},
        "reason": {"type": "string", "description": "Bildirimde görünen kısa neden (ör. 'Hatırlatma: ilaç')"},
    }, ["message"])
    risk = READ  # calling the user themselves sends nothing outside

    def run(self, ctx: ToolContext, message: str, reason: str = "") -> ToolResult:
        from alice.app import call

        mgr = call.current()
        if mgr is None:
            raise ToolError("telefon köprüsü kapalı: Ayarlar → Kurulum → Telefondan ALICE açık olmalı")
        r = mgr.ring(reason or message[:60], message)
        if not r.get("ok"):
            raise ToolError(r.get("error") or "aranamadı")
        return ToolResult("Telefon çalıyor. Kullanıcı açarsa görüşme ayrı bir sesli konuşmada sürecek; burada "
                          "ayrıca bir şey söylemene gerek yok.")


TOOLS = [PhoneCall()]
