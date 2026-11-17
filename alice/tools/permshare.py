"""permissions: suggests "stop asking" rules for jobs the user always approves, and applies rules.
share: publish a report/page as a secret Gist on the user's GitHub account and give the link."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, READ, SEND, Tool, ToolContext, ToolError, ToolResult, obj


class PermissionsTool(Tool):
    name = "permissions"
    description = (
        "İzin kuralları. suggest: kullanıcının daha önce hep onayladığı (en az 3 kez, hiç reddetmediği) işlerden "
        "'artık sormadan yap' kuralları önerir; tehlikeli ve başkasına giden işler önerilmez. apply: kullanıcı "
        "açıkça seçtiği kuralları bu projenin izin listesine ekler. list: bu projede geçerli kurallar.\n"
        "Önerileri önce kullanıcıya göster; hangilerini uygulayacağına o karar versin, kendin apply yapma.")
    parameters = obj({
        "action": {"type": "string", "enum": ["suggest", "apply", "list"], "description": "Yapılacak iş."},
        "rules": {"type": "array", "items": {"type": "string", "description": "Kural, ör. run_command(npm test*)."},
                  "description": "apply: eklenecek kurallar (suggest çıktısından, kullanıcının seçtikleri)."},
        "everywhere": {"type": "boolean", "description": "suggest: yalnız bu projedeki değil bütün sorulanlara bak."},
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
                return ToolResult("öneri yok: henüz sık ve hep onaylanan bir iş birikmedi")
            return ToolResult("Hep onayladığın işler (kural · kaç kez onaylandı):\n"
                              + "\n".join(f"- {s['rule']} · {s['yes']} kez" for s in rows[:20])
                              + "\nHangilerini artık sormadan yapayım? (apply ile eklenir)")
        if action == "apply":
            good = [r.strip() for r in rules or [] if r.strip()]
            if not good:
                raise ToolError("apply için rules gerekli")
            for r in good:
                name, _ = perm.parse_rule(r)
                if not name or name == "*":
                    raise ToolError(f"çok geniş kural: {r}")
            where = None
            for r in good:
                where = perm.persist_allow(ctx.cwd, r)
            return ToolResult(f"{len(good)} kural eklendi ({where}): " + ", ".join(good))
        raise ToolError(f"bilinmeyen action: {action}")


class Share(Tool):
    name = "share"
    description = (
        "Bir dosyayı (rapor, sayfa, markdown, kod) bağlantıyla paylaşılabilir yapar: kullanıcının GitHub hesabında "
        "gizli bir Gist olarak yayınlar (bağlantıyı bilen görür, aramada çıkmaz). Markdown ve kod GitHub'da "
        "doğrudan görünür; HTML için ayrıca tarayıcıda açılan bir önizleme adresi verilir.\n"
        "Dışarıya içerik yayınlar: yalnız kullanıcı paylaşmak istediğinde kullan; gizli bilgi (anahtar, parola, "
        "kişisel veri) içeren dosyayı paylaşma.")
    parameters = obj({
        "paths": {"type": "array", "items": {"type": "string", "description": "Dosya yolu."},
                  "description": "Paylaşılacak dosyalar (en fazla 10, her biri 1 MB'a kadar)."},
        "title": {"type": "string", "description": "Kısa açıklama (Gist başlığı)."},
        "public": {"type": "boolean", "description": "Herkese açık Gist (aramada çıkar). Varsayılan false (gizli)."},
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
                raise ToolError(f"dosya çok büyük (1 MB üstü): {p.name}")
            text = p.read_text(encoding="utf-8", errors="replace")
            if _looks_secret(p, text):
                raise ToolError(f"{p.name} anahtar/parola gibi görünen içerik taşıyor; paylaşılmadı")
            files[p.name] = {"content": text}
        if not files:
            raise ToolError("paylaşılacak dosya yok")
        gist = _api(ctx).post("/gists", {"description": title or ", ".join(files), "public": bool(public),
                                         "files": files})
        url = str(gist.get("html_url") or "")
        lines = [f"Paylaşıldı ({'herkese açık' if public else 'gizli, bağlantıyı bilen görür'}): {url}"]
        for name, f in (gist.get("files") or {}).items():
            raw = str(f.get("raw_url") or "")
            if Path(name).suffix.lower() in (".html", ".htm") and raw:
                lines.append(f"{name} sayfa olarak: {raw.replace('gist.githubusercontent.com', 'gistcdn.githack.com')}")
        lines.append("Silmek için GitHub'da Gist sayfasından Delete.")
        return ToolResult("\n".join(lines))


def _looks_secret(p: Path, text: str) -> bool:
    from alice.tools.github import SECRET_FILES, SECRET_RX

    return bool(SECRET_FILES.search(p.name) or SECRET_RX.search(text))


TOOLS: list[Tool] = [PermissionsTool(), Share()]
