"""code_review tool: an independent, clean-context reviewer (not this chat) reviews the change (alice/review.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class CodeReview(Tool):
    name = "code_review"
    description = (
        "Has the change reviewed by a reviewer INDEPENDENT of this conversation: a separate agent that cannot see the "
        "conversation and has only read tools reads the diff and the related code, and finds real bugs with a "
        "concrete failure scenario; a second agent verifies each finding in the code, and refuted ones are dropped. "
        "Use it when you finish code you wrote, before opening a PR, or when the user says 'review this'.\n"
        "target=local: changes in the working folder (since it branched off the main branch, uncommitted ones "
        "included). target=pr: a GitHub PR (number). post=true: write the findings to the PR as a review with line "
        "comments.\n"
        "Show the findings to the user before fixing them yourself; don't apply a finding you are not sure is real.")
    parameters = obj({
        "target": {"type": "string", "enum": ["local", "pr"], "description": "What to review. If number is given, pr."},
        "number": {"type": "integer", "description": "target=pr: PR number."},
        "repo": {"type": "string", "description": "target=pr: 'owner/repo'. Empty: the working folder's origin."},
        "base": {"type": "string", "description": "target=local: branch to compare against (e.g. origin/main). Empty: "
                                                  "the repo's main branch."},
        "focus": {"type": "string", "enum": ["bugs", "security", "performance", "tests", "docs", "all", "deep"],
                  "description": "bugs (default): correctness bugs. security: security. performance: measurable "
                                 "slowness/resource leaks. tests: untested paths, tests that check nothing. docs: "
                                 "comments/docs that are now wrong. all: bugs+security in one agent. deep: bugs, "
                                 "security, performance and tests with separate agents at the same time (more "
                                 "expensive; for big changes or before a PR). If .github/workflows changes, the "
                                 "workflow security rules are checked automatically with every focus."},
        "post": {"type": "boolean", "description": "target=pr: write the findings to the PR as a review."},
        "verify": {"type": "boolean", "description": "Verify the findings with a separate agent. Default true."},
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
