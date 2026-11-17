"""code_review tool: an independent, clean-context reviewer (not this chat) reviews the change (alice/review.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class CodeReview(Tool):
    name = "code_review"
    description = (
        "Değişikliği bu konuşmadan BAĞIMSIZ bir inceleyiciye inceletir: konuşmayı görmeyen, yalnız okuma araçları "
        "olan ayrı bir ajan farkı ve ilgili kodu okur, gerçek hataları somut bozulma senaryosuyla bulur; ikinci bir "
        "ajan her bulguyu kodda doğrular, çürütülenler atılır. Kendi yazdığın kodu bitirince, PR açmadan önce ya da "
        "kullanıcı 'incele/review et' deyince kullan.\n"
        "target=local: çalışma klasöründeki değişiklikler (ana daldan ayrıldığı noktadan beri, commit'lenmemişler "
        "dahil). target=pr: GitHub PR'ı (number). post=true: bulguları PR'a satır yorumlarıyla inceleme olarak yaz.\n"
        "Bulguları kendin düzeltmeden önce kullanıcıya göster; gerçek olduğundan emin olmadığın bulguyu uygulama.")
    parameters = obj({
        "target": {"type": "string", "enum": ["local", "pr"], "description": "Ne incelenecek. number verilirse pr."},
        "number": {"type": "integer", "description": "target=pr: PR numarası."},
        "repo": {"type": "string", "description": "target=pr: 'sahip/depo'. Boşsa çalışma klasörünün origin'i."},
        "base": {"type": "string", "description": "target=local: karşılaştırılacak dal (ör. origin/main). Boşsa "
                 "deponun ana dalı."},
        "focus": {"type": "string", "enum": ["bugs", "security", "performance", "tests", "docs", "all", "deep"],
                  "description": "bugs (varsayılan): doğruluk hataları. security: güvenlik. performance: ölçülebilir "
                                 "yavaşlık/kaynak sızıntısı. tests: test edilmeyen yollar, hiçbir şeyi sınamayan "
                                 "testler. docs: artık yanlış olan yorum/doküman. all: bugs+security tek ajanda. "
                                 "deep: bugs, security, performance, tests ayrı ajanlarla aynı anda (daha pahalı; "
                                 "büyük değişiklik ya da PR öncesi). .github/workflows değişirse iş akışı güvenlik "
                                 "kuralları her odakta kendiliğinden denetlenir."},
        "post": {"type": "boolean", "description": "target=pr: bulguları PR'a inceleme olarak yaz."},
        "verify": {"type": "boolean", "description": "Bulguları ayrı bir ajanla doğrula. Varsayılan true."},
    }, [])
    risk = READ
    parallel_safe = False

    def risk_for(self, args: dict[str, Any]) -> str:
        return WRITE if args.get("post") else READ

    def summary(self, args: dict[str, Any]) -> str:
        what = f"PR #{args['number']}" if args.get("number") else "yerel değişiklikler"
        return f"{what} ({args.get('focus') or 'bugs'})" + (", PR'a yaz" if args.get("post") else "")

    def run(self, ctx: ToolContext, target: str = "", number: int = 0, repo: str = "", base: str = "",
            focus: str = "bugs", post: bool = False, verify: bool = True) -> ToolResult:
        from alice import review
        from alice.tools.github import GitHub, _api

        if ctx.router is None:
            raise ToolError("model yönlendiricisi yok: inceleme bu bağlamda çalışmaz")
        target = target or ("pr" if number else "local")
        sha = ""
        if target == "pr":
            if not number:
                raise ToolError("target=pr için number gerekli")
            api = _api(ctx)
            repo = GitHub()._repo(ctx, repo)
            title, diff, sha = review.pr_diff(api, repo, int(number))
        else:
            if post:
                raise ToolError("post yalnız target=pr ile")
            title, diff = review.local_diff(ctx.cwd, base)
        ctx.emit(f"bağımsız inceleme başladı: {title}")
        findings = review.review(ctx.config, ctx.router, ctx.cwd, title, diff, focus, verify)
        text = review.report(findings)
        if post:
            url = review.post(_api(ctx), repo, int(number), sha, findings, diff)
            text += f"\n\nPR'a inceleme olarak yazıldı: {url}"
        ctx.emit("bağımsız inceleme bitti")
        return ToolResult(text)


TOOLS: list[Tool] = [CodeReview()]
