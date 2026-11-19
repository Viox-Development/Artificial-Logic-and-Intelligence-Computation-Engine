"""The project's result is a PR: it runs on its own branch in a separate worktree, not in the user's copy.
When it is done, the branch is pushed, a PR is opened and handed to PR tracking (alice/prwatch.py). Red CI and review
comments reach the chat that started the project this way.

At project start, branch `alice/proje-<id>` is created from the user's current HEAD, in ~/.alice/worktrees/proje-<id>.
All of the project's agents work there (worktree agents also branch from there and are merged there once approved).
The user's uncommitted changes do not enter the project; the working folder is not touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.tools.base import ToolError

IDENT = ["-c", "user.name=ALICE", "-c", "user.email=alice@localhost"]


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    from alice.gitsafe import harden
    from alice.tools.github import NO_WINDOW
    from alice.tools.shell import hidden_startupinfo

    return subprocess.run(["git", *harden(args, cwd)], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                          startupinfo=hidden_startupinfo())


def prepare(team: Any) -> str:
    """Open the project's branch and worktree, and move team.cwd there. Returns a note line."""
    from alice.tools.github import current_branch, origin_repo

    top = team._repo_top()
    repo = origin_repo(top)
    if not repo:
        raise ToolError("PR için proje klasörü GitHub'a bağlı (origin) bir git deposu olmalı")
    base = current_branch(top) or ""
    branch = f"alice/proje-{team.id}"
    wt = alice_home() / "worktrees" / f"proje-{team.id}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    if not (wt / ".git").exists():
        _git(top, "worktree", "prune")
        r = _git(top, "worktree", "add", "-b", branch, str(wt), "HEAD", timeout=300)
        if r.returncode != 0:
            raise ToolError(f"projenin dalı açılamadı: {(r.stderr or r.stdout).strip()[:300]}")
    rel = Path(team.cwd).resolve().relative_to(top.resolve()) if Path(team.cwd).resolve() != top.resolve() else Path()
    team.pr_repo, team.pr_base, team.pr_branch, team.pr_top = repo, base, branch, str(top)
    team.pr_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    # Capture the starting chat now: by the end, parent_ctx may point to a different chat.
    team.pr_session = str(team.parent_ctx.state.get("session_id") or "")
    team.cwd = wt / rel
    dirty = _git(top, "status", "--porcelain").stdout.strip()
    return (f"proje kendi dalında çalışıyor: {branch} ({wt})"
            + ("; çalışma klasöründeki commit'lenmemiş değişiklikler projeye girmedi" if dirty else ""))


def open_pr(team: Any) -> str:
    """Commit the project's branch, push it, open a PR and track it. Returns a result line (errors as text too)."""
    from alice import prwatch
    from alice.tools.github import GitHubAPI, diff_secrets, github_token, repo_root, run_git

    try:
        wt = repo_root(Path(team.cwd))
        if _git(wt, "status", "--porcelain").stdout.strip():
            _git(wt, "add", "-A", "--", ".", ":(exclude).alice-team")  # keep agents' worktrees out of the PR
            r = _git(wt, *IDENT, "commit", "-q", "-m", f"ALICE projesi: {team.title[:60]}")
            if r.returncode != 0:
                return f"PR açılamadı: commit başarısız ({(r.stderr or r.stdout).strip()[:200]})"
        ahead = _git(wt, "rev-list", "--count", f"{team.pr_sha}..HEAD").stdout.strip()
        if ahead in ("", "0"):
            return "PR açılmadı: projede değişiklik yok"
        # Agents commit with `add -A`: a key or .env added to the project is stopped before it reaches GitHub.
        # The whole range is scanned (commits the agents made earlier on their own branches also go into the push).
        leaks = diff_secrets(wt, [f"{team.pr_sha}..HEAD"])
        if leaks:
            return ("PR açılmadı: projede anahtar ya da gizli dosya var, GitHub'a gönderilmedi: " + ", ".join(leaks[:8])
                    + ". Dosyayı çıkarıp .gitignore'a ekle, commit geçmişinden de temizle.")
        token = github_token(team.parent_ctx.config)
        run_git(["push", "-u", "origin", team.pr_branch], wt, token, timeout=300)
        api = GitHubAPI(token)
        body = (f"{team.summary or team.goal}\n\n---\nALICE projesi `{team.id}` ({len(team.members)} ajan). "
                "Bu PR takip ediliyor: kırmızı CI ve inceleme yorumları projeyi başlatan sohbete gelir.")
        data = {"title": team.title[:200], "head": team.pr_branch, "body": body[:60000]}
        try:
            p = api.post(f"/repos/{team.pr_repo}/pulls", {**data, "base": team.pr_base or "main"})
        except ToolError:
            default = api.get(f"/repos/{team.pr_repo}").get("default_branch", "main")
            if default == team.pr_base:
                raise
            team.pr_base = default  # the starting branch is not on GitHub: open against the default branch
            p = api.post(f"/repos/{team.pr_repo}/pulls", {**data, "base": default})
        team.pr_url, team.pr_number = str(p.get("html_url") or ""), int(p.get("number") or 0)
        session = str(getattr(team, "pr_session", "") or team.parent_ctx.state.get("session_id") or "")
        prwatch.watch(team.pr_repo, team.pr_number, team.pr_top, session=session)
        return f"PR #{team.pr_number} açıldı ve takibe alındı: {team.pr_url}"
    except (ToolError, OSError, subprocess.SubprocessError) as e:
        return f"PR açılamadı: {str(e)[:300]}"
