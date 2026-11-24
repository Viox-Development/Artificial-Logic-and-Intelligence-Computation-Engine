"""git and GitHub: repo cloning, branches, commit, push, opening/merging PRs.

Key priority: first the user's `gh auth token` session (keyring), otherwise GITHUB_TOKEN / GH_TOKEN (.env),
otherwise the key of the GitHub MCP server in the config, and last GITHUB_MODELS_TOKEN. The key is given to git only in
that command's environment (GIT_CONFIG_*) and is never written to any file.

Rules (as in Claude Code / Codex), config.toml [github]:
  protected_branches = ["main", "master"]  # pushing to these branches and the repo's default branch asks for approval in every permission mode
  merge_method = "squash"                  # merge | squash | rebase
  require_checks = true                    # if CI is red or still running, merging is refused (force=true passes with approval)
  clone_dir = ""                           # empty: ~/GitHub/<owner>/<repo>

- Reads (status, diff, log, PR/issue lists, CI status) never ask; they also work in plan mode.
- commit, switching branches, pushing to an unprotected branch, opening PR/issue, comments: local or reversible; not asked in auto mode.
- Merging/closing PRs, pushing to a protected branch, force push: irreversible; always asks for approval except in yolo mode.
- A commit containing a .env or something that looks like a key is refused.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj, truncate
from alice.tools.shell import hidden_startupinfo

API = "https://api.github.com"
TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_PAT")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DEFAULTS: dict[str, Any] = {"protected_branches": ["main", "master"], "merge_method": "squash",
                            "require_checks": True, "clone_dir": ""}

# Key patterns that must not end up in a commit (searched in added lines).
REPO_RX = re.compile(r"[\w-][\w.-]*/[\w-][\w.-]*")  # "owner/repo"; ".." cannot escape the folder
SECRET_RX = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{32,}|AIza[0-9A-Za-z_-]{35}|"
    r"gsk_[A-Za-z0-9]{40,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_FILES = re.compile(r"(^|/)(\.env(\.[^/]*)?|id_rsa|id_ed25519|.*\.pem|.*\.key)$", re.I)


def settings(cfg: Any) -> dict[str, Any]:
    data = getattr(cfg, "data", None) or {}
    return {**DEFAULTS, **(data.get("github") or {})}


def _token_candidates(cfg: Any = None):
    """Tokens, in priority order (lazy: gh only runs when needed):

    1. `gh auth token` (keyring — the user's login on this computer),
    2. GITHUB_TOKEN / GH_TOKEN (.env),
    3. the key of the GitHub MCP server in the config,
    4. GITHUB_MODELS_TOKEN.
    """
    # Priority: gh CLI session (user's keyring preference)
    gh = shutil.which("gh")
    if gh:
        try:
            gh_env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10,
                               env=gh_env, creationflags=NO_WINDOW, startupinfo=hidden_startupinfo())
            if r.returncode == 0 and r.stdout.strip():
                yield r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass  # no gh CLI: try the next token source

    for k in TOKEN_ENVS:
        if os.environ.get(k):
            yield os.environ[k]
    # If the GitHub MCP server is already defined with a key, use it.
    data = getattr(cfg, "data", None) or {}
    for name, spec in ((data.get("mcp") or {}).get("servers") or {}).items():
        if "github" not in name.lower():
            continue
        for k, v in (spec.get("env") or {}).items():
            val = os.path.expandvars(str(v))
            if "TOKEN" in k.upper() and val and "$" not in val:
                yield val
        for h in (spec.get("headers") or {}).values():
            m = re.match(r"(?i)bearer\s+(\S+)", os.path.expandvars(str(h)))
            if m and "$" not in m.group(1):
                yield m.group(1)
    if os.environ.get("GITHUB_MODELS_TOKEN"):
        yield os.environ["GITHUB_MODELS_TOKEN"]


_private_cache: dict[str, bool | None] = {}


def _private_access(token: str) -> bool | None:
    """Can this key see private repos? (None: could not be asked). Asked once per process."""
    import hashlib

    import httpx

    key = hashlib.sha256(token.encode()).hexdigest()
    if key not in _private_cache:
        try:
            r = httpx.get("https://api.github.com/user/repos", timeout=8,
                          params={"visibility": "private", "per_page": 1},
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            _private_cache[key] = bool(r.json()) if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            _private_cache[key] = None
    return _private_cache[key]


def github_token(cfg: Any = None) -> str | None:
    """The GitHub key to use. If the first candidate (gh keyring) is a fine-grained token (github_pat_) that cannot see
    private repos, the next key that can access private repos is chosen: the .env key was authorized only for public
    repos, so ALICE could never see the user's private repos (7 Nov: 7 repos, 0 private; with gh, 16 private)."""
    tokens = _token_candidates(cfg)
    first = next(tokens, None)
    if first is None or not first.startswith("github_pat_") or _private_access(first) is not False:
        return first
    for other in tokens:
        if other != first and _private_access(other):
            return other
    return first


def token_source(cfg: Any, token: str | None) -> str:
    """Find where the chosen key came from, in the same order as _token_candidates (gh, environment, MCP, Models).
    So the audit report doesn't say 'Auth: bilinmiyor' (unknown): a key from the MCP server shows up by name too."""
    if not token:
        return "none"
    gh = shutil.which("gh")
    if gh:
        try:
            gh_env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10,
                               env=gh_env, creationflags=NO_WINDOW, startupinfo=hidden_startupinfo())
            if r.returncode == 0 and r.stdout.strip() == token:
                return "gh CLI (keyring)"
        except (OSError, subprocess.SubprocessError):
            pass  # no gh CLI: the token came from elsewhere
    for k in TOKEN_ENVS:
        if os.environ.get(k) == token:
            return f"Environment ({k})"
    data = getattr(cfg, "data", None) or {}
    for name, spec in ((data.get("mcp") or {}).get("servers") or {}).items():
        if "github" not in name.lower():
            continue
        for k, v in (spec.get("env") or {}).items():
            if "TOKEN" in k.upper() and os.path.expandvars(str(v)) == token:
                return f"MCP sunucusu ({name})"
        for h in (spec.get("headers") or {}).values():
            m = re.match(r"(?i)bearer\s+(\S+)", os.path.expandvars(str(h)))
            if m and m.group(1) == token:
                return f"MCP sunucusu ({name})"
    if os.environ.get("GITHUB_MODELS_TOKEN") == token:
        return "Environment (GITHUB_MODELS_TOKEN)"
    return "bilinmiyor"


# ---------------------------------------------------------------------------------------------- git
def git_env(token: str | None) -> dict[str, str]:
    """Give git the key only through this process's environment; it is never written to a file, a command line,
    or a remote URL."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env[f"GIT_CONFIG_KEY_{n}"] = "http.https://github.com/.extraheader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"AUTHORIZATION: basic {basic}"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


def run_git(args: list[str], cwd: Path, token: str | None = None, timeout: int = 120,
            check: bool = True) -> tuple[int, str]:
    git = shutil.which("git")
    if not git:
        raise ToolError("git is not installed (https://git-scm.com/download/win)")
    try:
        from alice.gitsafe import harden

        # Hooks and fsmonitor are off: a program that a sandboxed command left in .git must not run with ALICE's git.
        r = subprocess.run([git, *harden(args, cwd)], cwd=cwd, capture_output=True, timeout=timeout, env=git_env(token),
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, startupinfo=hidden_startupinfo())
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"git {args[0]} {timeout} sn'de bitmedi") from e
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    if token:
        out = out.replace(token, "***")
    if check and r.returncode != 0:
        raise ToolError(f"git {' '.join(args[:2])} failed (code {r.returncode}):\n{out}")
    return r.returncode, out


def parse_remote(url: str) -> str | None:
    """'https://github.com/o/r(.git)' or 'git@github.com:o/r.git' -> 'o/r'."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                 r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def repo_root(cwd: Path) -> Path:
    _, out = run_git(["rev-parse", "--show-toplevel"], cwd, check=False)
    p = Path(out.splitlines()[-1]) if out else None
    if p is None or not p.is_dir():
        raise ToolError(f"{cwd} is not a git repository. To open one from GitHub use github action=clone.")
    return p


def list_branches(cwd: Path, fetch: bool = False, token: str | None = None) -> list[dict[str, Any]]:
    """Local and remote branches (by last commit, newest first). A branch that is remote but not local gets remote=True.
    fetch: first refresh origin's branches (so a branch opened on GitHub after the clone also appears in the list). If
    there is no network or it is slow, the list at hand is used."""
    root = repo_root(cwd)
    if fetch:
        try:
            run_git(["fetch", "--prune", "origin"], root, token, timeout=20, check=False)
        except ToolError:  # timed out: continue with the known branches
            pass
    cur = current_branch(root)
    fmt = "%(refname)\t%(committerdate:unix)\t%(subject)"
    _, out = run_git(["for-each-ref", f"--format={fmt}", "--sort=-committerdate", "refs/heads", "refs/remotes"],
                     root, check=False)
    seen: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        ref, _, rest = line.partition("\t")
        ts, _, subject = rest.partition("\t")
        if ref.startswith("refs/heads/"):
            name, remote = ref[len("refs/heads/"):], False
        elif ref.startswith("refs/remotes/"):
            name = ref[len("refs/remotes/"):].partition("/")[2]
            remote = True
            if not name or name == "HEAD":
                continue
        else:
            continue
        row = seen.get(name)
        if row is None:
            seen[name] = {"name": name, "current": name == cur, "remote": remote, "subject": subject[:100],
                          "ts": int(ts or 0)}
        elif not remote:
            row["remote"] = False  # also exists locally
    return list(seen.values())


def switch_branch(cwd: Path, branch: str, create: bool = False, token: str | None = None) -> str:
    """Switch branch. If it is not local, a tracking branch is created from the remote one (fetching first if needed).
    If unsaved changes block the switch, say what to do instead of showing the raw git error."""
    root = repo_root(cwd)
    branch = branch.strip()
    if branch.startswith("origin/"):
        branch = branch[len("origin/"):]
    if not branch or branch.startswith("-") or ".." in branch:
        raise ToolError(f"invalid branch name: {branch!r}")
    if branch == current_branch(root):
        return f"already on '{branch}'"

    def has(ref: str) -> bool:
        return run_git(["rev-parse", "--verify", "--quiet", ref], root, check=False)[0] == 0

    if create:
        args = ["switch", "-c", branch]
    elif has(f"refs/heads/{branch}"):
        args = ["switch", branch]
    else:
        if not has(f"refs/remotes/origin/{branch}"):
            run_git(["fetch", "--prune", "origin"], root, token, timeout=300, check=False)
        if not has(f"refs/remotes/origin/{branch}"):
            raise ToolError(f"there is no branch '{branch}' locally or on origin. To create a new branch: create=true.")
        args = ["switch", "--track", f"origin/{branch}"]
    code, out = run_git(args, root, check=False)
    if code != 0:
        if "would be overwritten" in out or "commit your changes or stash" in out.lower():
            raise ToolError(f"could not switch to '{branch}': uncommitted changes would be overwritten on that branch. "
                            f"Commit first or set them aside with `git stash`.\n{out}")
        raise ToolError(f"could not switch to '{branch}' (code {code}):\n{out}")
    return out or f"branch: {branch}"


def current_branch(cwd: Path) -> str:
    _, out = run_git(["branch", "--show-current"], cwd, check=False)
    return out.strip()


def origin_repo(cwd: Path) -> str | None:
    code, out = run_git(["remote", "get-url", "origin"], cwd, check=False)
    return parse_remote(out) if code == 0 else None


def protected(branch: str, cfg: Any, default_branch: str = "") -> bool:
    names = {b.lower() for b in settings(cfg)["protected_branches"]}
    if default_branch:
        names.add(default_branch.lower())
    return branch.lower() in names


def staged_secrets(cwd: Path) -> list[str]:
    """Are there keys or a .env among the files that will go into the commit?"""
    return diff_secrets(cwd, ["--cached"])


def diff_secrets(cwd: Path, spec: list[str]) -> list[str]:
    """Are there keys or a .env in the files/lines added by `git diff <spec>`? (["--cached"] or ["base..HEAD"])"""
    hits = []
    _, names = run_git(["diff", *spec, "--name-only", "--diff-filter=AMR"], cwd, check=False)
    hits += [n for n in names.splitlines() if SECRET_FILES.search(n.strip())]
    _, diff = run_git(["diff", *spec, "-U0", "--no-color"], cwd, check=False)
    fname = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            fname = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and SECRET_RX.search(line):
            hits.append(f"{fname}: line that looks like a key")
    return list(dict.fromkeys(hits))


def clone_root(cfg: Any) -> Path:
    d = settings(cfg).get("clone_dir") or ""
    return Path(d).expanduser() if d else Path.home() / "GitHub"


def clone_or_update(cfg: Any, repo: str, branch: str = "", token: str | None = None) -> tuple[Path, str]:
    """Clone the repo under ~/GitHub/<owner>/<repo>; update it if it already exists. Returns (folder, what was done)."""
    repo = parse_remote(repo) or repo.strip().strip("/")
    if not REPO_RX.fullmatch(repo):
        raise ToolError(f"the repo name must look like 'owner/repo': {repo}")
    token = token if token is not None else github_token(cfg)
    owner, name = repo.split("/")
    target = clone_root(cfg) / owner / name
    if (target / ".git").exists():
        run_git(["fetch", "--prune", "origin"], target, token, timeout=300)
        note = "updated (fetch)"
        if branch and branch != current_branch(target):
            switch_branch(target, branch, token=token)
            note += f", branch: {branch}"
        _, dirty = run_git(["status", "--porcelain"], target, check=False)
        if not dirty.strip():
            code, out = run_git(["pull", "--ff-only"], target, token, timeout=300, check=False)
            if code == 0:
                note = "updated (pull)" + (f", branch: {branch}" if branch else "")
        else:
            note += "; pull skipped because there are local changes"
        return target, note
    if target.exists() and any(target.iterdir()):
        raise ToolError(f"{target} is not empty and not a git repository; pick another place with config [github] clone_dir")
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", f"https://github.com/{repo}.git", str(target)]
    if branch:
        args[1:1] = ["--branch", branch]
    run_git(args, target.parent, token, timeout=900)
    return target, "cloned"


# --------------------------------------------------------------------------------------------- API
class GitHubAPI:
    def __init__(self, token: str | None, client: Any = None):
        if not token:
            raise ToolError("No GitHub key found: add GITHUB_TOKEN=... to ~/.alice/.env (a token with repo access) "
                            "or run `gh auth login`.")
        self.token = token
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(base_url=API, timeout=30, headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "alice"})
        return self._client

    def call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.client.request(method, path, **kw)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"could not reach GitHub: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message", "")
                errs = "; ".join(str(x.get("message") or x) for x in body.get("errors") or [])
            except ValueError:
                msg, errs = r.text[:300], ""
            hint = {401: " (the token is invalid or expired)",
                    403: " (the token isn't allowed to do this: it needs repo / pull request write access)",
                    404: " (the repo doesn't exist or the token can't see it)"}.get(r.status_code, "")
            raise ToolError(f"GitHub {r.status_code}: {msg}{hint}" + (f" [{errs}]" if errs else ""))
        return r.json() if r.content else {}

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params=params or None)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.call("POST", path, json=body)

    def user_repos(self, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        rows = self.get("/user/repos", per_page=100, sort="updated",
                        affiliation="owner,collaborator,organization_member")
        q = query.lower().strip()
        out = [{"full_name": r["full_name"], "private": r.get("private", False), "description": r.get("description") or "",
                "default_branch": r.get("default_branch", "main"), "updated": r.get("pushed_at") or r.get("updated_at")}
               for r in rows if not q or q in r["full_name"].lower() or q in (r.get("description") or "").lower()]
        return out[:limit]

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        data = self.call("POST", "/graphql", json={"query": query, "variables": variables})
        if data.get("errors"):
            raise ToolError("GitHub GraphQL: " + "; ".join(str(e.get("message") or e) for e in data["errors"])[:400])
        return data.get("data") or {}

    def text(self, path: str) -> str:
        """Plain-text endpoint (job logs): GitHub redirects to a temporary download address."""
        try:
            r = self.client.get(path, follow_redirects=True)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"could not reach GitHub: {e}") from e
        if r.status_code >= 400:
            raise ToolError(f"GitHub {r.status_code}: could not get the log")
        return r.text

    def checks(self, repo: str, ref: str) -> dict[str, Any]:
        """CI summary: state = success | failure | pending | none."""
        runs = self.get(f"/repos/{repo}/commits/{ref}/check-runs", per_page=100).get("check_runs", [])
        status = self.get(f"/repos/{repo}/commits/{ref}/status")
        items = [(c["name"], c.get("conclusion") or c.get("status")) for c in runs]
        items += [(s["context"], s["state"]) for s in status.get("statuses", [])]
        bad = [n for n, s in items if s in ("failure", "error", "timed_out", "cancelled", "action_required")]
        waiting = [n for n, s in items if s in ("queued", "in_progress", "pending", "waiting", "requested")]
        state = "none" if not items else "failure" if bad else "pending" if waiting else "success"
        return {"state": state, "failed": bad, "pending": waiting, "total": len(items)}


# ----------------------------------------------------------------------------------- CI and review helpers
ALICE_MARK = "— ALICE"  # signature of ALICE's replies: PR tracking must not mistake its own replies for new comments

# Authors whose comments may be applied automatically in the background. A stranger's comment is only reported:
# anyone who can comment on a PR must not be able to make ALICE change code.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
TRUSTED_BOTS = ("copilot-pull-request-reviewer", "copilot", "github-actions")
_LOG_HIT = re.compile(r"(?i)(^|\s)(FAILED|ERROR|error[:\[]|E\s{2,}|Traceback|AssertionError|Exception:|"
                      r"##\[error\]|exit code [1-9]|npm ERR!|✗|FAIL\b)")
_LOG_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")


def excerpt_log(text: str, limit: int = 4000) -> str:
    """The useful part of a long CI log: error lines (with context) and the last lines. Timestamps are dropped."""
    lines = [_LOG_STAMP.sub("", ln) for ln in text.splitlines()]
    keep: list[int] = []
    for i, ln in enumerate(lines):
        if _LOG_HIT.search(ln):
            keep += range(max(0, i - 2), min(len(lines), i + 3))
    keep += range(max(0, len(lines) - 25), len(lines))
    out, last = [], -2
    for i in sorted(set(keep)):
        if i != last + 1:
            out.append("…")
        out.append(lines[i][:400])
        last = i
    body = "\n".join(out)
    return body if len(body) <= limit else "…\n" + body[-limit:]


def failed_jobs(api: GitHubAPI, repo: str, sha: str, with_logs: bool = True) -> list[dict[str, Any]]:
    """GitHub Actions jobs that failed on this commit: name, address, run ID and an error summary from the log."""
    runs = api.get(f"/repos/{repo}/actions/runs", head_sha=sha, per_page=30).get("workflow_runs", [])
    out: list[dict[str, Any]] = []
    for run in runs:
        if run.get("conclusion") not in ("failure", "timed_out", "cancelled", "startup_failure"):
            continue
        jobs = api.get(f"/repos/{repo}/actions/runs/{run['id']}/jobs", per_page=50).get("jobs", [])
        for job in jobs:
            if job.get("conclusion") not in ("failure", "timed_out", "cancelled"):
                continue
            row = {"name": job.get("name", ""), "workflow": run.get("name", ""), "run_id": run["id"],
                   "job_id": job["id"], "url": job.get("html_url", ""), "conclusion": job.get("conclusion"),
                   "log": ""}
            if with_logs:
                try:
                    row["log"] = excerpt_log(api.text(f"/repos/{repo}/actions/jobs/{job['id']}/logs"))
                except ToolError as e:
                    row["log"] = f"(could not get the log: {e})"
            out.append(row)
    return out


def rerun_failed(api: GitHubAPI, repo: str, sha: str) -> list[str]:
    """Re-run the failed jobs (only the failed ones). Names of the workflows that were restarted."""
    runs = api.get(f"/repos/{repo}/actions/runs", head_sha=sha, per_page=30).get("workflow_runs", [])
    done = []
    for run in runs:
        if run.get("status") == "completed" and run.get("conclusion") not in ("success", "skipped", "neutral"):
            api.call("POST", f"/repos/{repo}/actions/runs/{run['id']}/rerun-failed-jobs")
            done.append(run.get("name") or str(run["id"]))
    return done


THREADS_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100) { nodes { id isResolved isOutdated path line originalLine
      comments(first: 30) { nodes { databaseId body createdAt authorAssociation author { __typename login } } } } }
    reviews(first: 50) { nodes { databaseId state body submittedAt authorAssociation author { __typename login } } }
    comments(first: 50) { nodes { databaseId body createdAt authorAssociation author { __typename login } } } } } }"""


def by_alice(body: str, cid: Any = None) -> bool:
    """Did ALICE write the comment? The signature must be at the END of the body; quoted lines ('> ') are not counted.
    When the user quotes ALICE's reply and writes below it, that comment must not be taken as ALICE's and skipped.
    Unsigned ones (undercover mode, general comments) are recognized by their IDs."""
    from alice import undercover

    if cid and undercover.is_own(cid):
        return True
    own = [ln for ln in (body or "").splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(own).rstrip().endswith(ALICE_MARK)


def trusted(author: str, association: str, kind: str = "") -> bool:
    """An authorized user in the repo, or a known bot. The bot name must match EXACTLY and the account must really be
    a bot (kind: GraphQL __typename; in REST, the '[bot]' suffix): a user account such as 'copilot-helper' must not be
    trusted."""
    if association in TRUSTED_ASSOCIATIONS:
        return True
    low = (author or "").lower()
    is_bot = kind == "Bot" or low.endswith("[bot]")
    return is_bot and low.removesuffix("[bot]") in TRUSTED_BOTS


def review_threads(api: GitHubAPI, repo: str, number: int) -> dict[str, list[dict[str, Any]]]:
    """The review conversations in the PR (line comments, whether resolved), review summaries and general comments."""
    owner, name = repo.split("/", 1)
    pr = api.graphql(THREADS_QUERY, owner=owner, name=name, number=int(number))["repository"]["pullRequest"]

    def who(node: dict[str, Any]) -> str:
        return ((node.get("author") or {}).get("login") or "?")

    def ok(node: dict[str, Any]) -> bool:
        return trusted(who(node), node.get("authorAssociation", ""), (node.get("author") or {}).get("__typename", ""))

    threads = []
    for t in pr["reviewThreads"]["nodes"]:
        comments = [{"id": c["databaseId"], "author": who(c), "body": c.get("body") or "",
                     "trusted": ok(c), "at": c.get("createdAt", "")}
                    for c in t["comments"]["nodes"]]
        threads.append({"id": t["id"], "resolved": t["isResolved"], "outdated": t["isOutdated"], "path": t["path"],
                        "line": t.get("line") or t.get("originalLine"), "comments": comments})
    reviews = [{"id": r["databaseId"], "state": r["state"], "author": who(r), "body": r.get("body") or "",
                "trusted": ok(r), "at": r.get("submittedAt", "")}
               for r in pr["reviews"]["nodes"]]
    comments = [{"id": c["databaseId"], "author": who(c), "body": c.get("body") or "",
                 "trusted": ok(c), "at": c.get("createdAt", "")}
                for c in pr["comments"]["nodes"]]
    return {"threads": threads, "reviews": reviews, "comments": comments}


def format_threads(data: dict[str, list[dict[str, Any]]], only_open: bool = True) -> str:
    rows = []
    for t in data["threads"]:
        if only_open and t["resolved"]:
            continue
        rows.append(f"[konu {t['id']}] {t['path']}:{t['line'] or '?'}{' (outdated)' if t['outdated'] else ''}"
                    f"{' (resolved)' if t['resolved'] else ''}")
        for c in t["comments"]:
            rows.append(f"  #{c['id']} {c['author']}{'' if c['trusted'] else ' (outside)'}: "
                        + " ".join(c["body"].split())[:1200])
    for r in data["reviews"]:
        if r["body"].strip() and r["state"] in ("CHANGES_REQUESTED", "COMMENTED"):
            rows.append(f"[inceleme #{r['id']}] {r['author']} {r['state']}: " + " ".join(r["body"].split())[:800])
    for c in data["comments"]:
        if not by_alice(c["body"]):
            rows.append(f"[yorum #{c['id']}] {c['author']}: " + " ".join(c["body"].split())[:800])
    return "\n".join(rows) or ("no open review threads" if only_open else "no comments")


def reply_to_comment(api: GitHubAPI, repo: str, number: int, comment_id: int, body: str,
                     sign: bool = True) -> dict[str, Any]:
    """sign=False (undercover mode): unsigned; recognized by ALICE's own ID."""
    from alice import undercover

    text = body.rstrip() + ("" if not sign or by_alice(body) else f"\n\n{ALICE_MARK}")
    c = api.post(f"/repos/{repo}/pulls/{int(number)}/comments/{int(comment_id)}/replies", {"body": text})
    undercover.remember_own(c.get("id"))
    return c


def resolve_thread(api: GitHubAPI, thread_id: str) -> bool:
    data = api.graphql("mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }",
                       id=thread_id)
    return bool(((data.get("resolveReviewThread") or {}).get("thread") or {}).get("isResolved"))


def _api(ctx: ToolContext) -> GitHubAPI:
    api = ctx.state.get("github_api")
    if api is None:
        api = GitHubAPI(github_token(ctx.config))
        ctx.state["github_api"] = api
    return api


def _token(ctx: ToolContext) -> str | None:
    api = ctx.state.get("github_api")
    return api.token if api is not None else github_token(ctx.config)


# --------------------------------------------------------------------------------------------- git tool
GIT_READ = {"status", "diff", "log", "branches", "audit", "worktrees"}


class Git(Tool):
    name = "git"
    description = (
        "Works in the git repository in the working folder; uses the GitHub key itself.\n"
        "status/diff/log/branches/audit: reading. switch: switch branch (create=true creates a new one). commit: "
        "stages files or all=true and commits (changes with .env or keys are refused). pull. push: sends the branch "
        "to origin; a push to a protected branch such as main/master, or with force, asks for approval.\n"
        "Rule: don't work on a protected branch; first switch create=true to open a branch, commit, push, then github "
        "pr_create.\n"
        "worktree: open a separate working copy of the same repository (branch; created with create=true if missing) "
        "and move there: to work on another branch without touching the current changes. worktrees: list. "
        "worktree_remove: remove the copy at path (refused if it has uncommitted changes)."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "diff", "log", "branches", "switch", "commit", "pull", "push",
                                              "audit", "worktree", "worktrees", "worktree_remove"],
                   "description": "What to do."},
        "branch": {"type": "string", "description": "For switch/push: branch name. Empty in push: the current branch."},
        "create": {"type": "boolean", "description": "switch: create the branch if it doesn't exist. Default false."},
        "message": {"type": "string", "description": "Commit message (first line a short summary)."},
        "files": {"type": "array", "items": {"type": "string", "description": "Dosya yolu."},
                  "description": "commit: files to stage. If empty and all=false, only what is already staged."},
        "all": {"type": "boolean", "description": "commit: stage all changes (new files included)."},
        "staged": {"type": "boolean", "description": "diff: only changes staged for commit."},
        "path": {"type": "string", "description": "diff/log: only this file or folder. worktree_remove: the working "
                                                  "copy to remove."},
        "count": {"type": "integer", "description": "log: how many commits. Default 15."},
        "force": {"type": "boolean", "description": "push: --force-with-lease. Her zaman onay ister."},
        "protected": {"type": "boolean", "description": "push: push straight to a protected branch (main/master/the "
                                                        "default branch); only if the user clearly asks. Asks for "
                                                        "approval."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        action = args.get("action")
        if action in GIT_READ:
            return READ
        if action == "push" and (args.get("force") or args.get("protected")):
            return DANGER
        return WRITE

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action", "")
        extra = args.get("message") or args.get("branch") or args.get("path") or ""
        return f"git {a} {extra}".strip()[:200]

    def run(self, ctx: ToolContext, action: str, branch: str = "", create: bool = False, message: str = "",
            files: list[str] | None = None, all: bool = False, staged: bool = False, path: str = "",  # noqa: A002
            count: int = 15, force: bool = False, protected: bool = False) -> ToolResult:
        root = repo_root(ctx.cwd)
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        if action == "status":
            _, out = run_git(["status", "--short", "--branch"], root)
            return ToolResult(out or "temiz")
        if action == "diff":
            args = ["diff", "--no-color", "--stat", "--patch"] + (["--cached"] if staged else [])
            _, out = run_git(args + (["--", path] if path else []), root)
            return ToolResult(truncate(out, limit) if out else "no changes")
        if action == "log":
            n = max(1, min(int(count or 15), 100))
            _, out = run_git(["log", f"-{n}", "--date=short", "--pretty=format:%h %ad %an  %s"]
                             + (["--", path] if path else []), root)
            return ToolResult(out or "no commits")
        if action == "branches":
            _, out = run_git(["branch", "-a", "-vv", "--sort=-committerdate"], root)
            return ToolResult(out)
        if action == "audit":
            return self._audit(ctx, root)
        if action == "worktrees":
            _, out = run_git(["worktree", "list"], root)
            return ToolResult(out)
        if action == "worktree":
            return self._worktree(ctx, root, branch, create)
        if action == "worktree_remove":
            if not path:
                raise ToolError("worktree_remove needs path")
            target = ctx.resolve(path)
            if target == root:
                raise ToolError("the main working copy can't be removed")
            code, out = run_git(["worktree", "remove", str(target)], root, check=False)
            if code != 0:
                raise ToolError(f"could not remove it (there may be uncommitted changes):\n{out}")
            return ToolResult(f"removed: {target}")
        if action == "switch":
            if not branch:
                raise ToolError("switch needs branch")
            return ToolResult(switch_branch(root, branch, create, github_token(ctx.config)))
        if action == "commit":
            from alice import undercover

            if undercover.active(ctx.config, origin_repo(root), ctx.state.get("github_api")):
                message = undercover.clean(ctx.config, message, "commit message")
            return self._commit(root, message, files or [], all)
        if action == "pull":
            _, out = run_git(["pull", "--ff-only"], root, _token(ctx), timeout=300)
            return ToolResult(out)
        if action == "push":
            return self._push(ctx, root, branch, force, protected)
        raise ToolError(f"bilinmeyen action: {action}")

    @staticmethod
    def _worktree(ctx: ToolContext, root: Path, branch: str, create: bool) -> ToolResult:
        """Separate working copy: ~/.alice/worktrees/<repo>-<branch>.
        Once it is opened, the working folder switches to it."""
        from alice.config import alice_home

        branch = branch.strip()
        if not branch or branch.startswith("-") or ".." in branch:
            raise ToolError("worktree needs a valid branch")
        target = alice_home() / "worktrees" / f"{root.name}-{re.sub(r'[^A-Za-z0-9_.-]+', '-', branch)}"
        if not (target / ".git").exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            have = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root, check=False)[0] == 0
            remote = run_git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], root,
                             check=False)[0] == 0
            if have:
                args = ["worktree", "add", str(target), branch]
            elif remote:
                args = ["worktree", "add", "--track", "-b", branch, str(target), f"origin/{branch}"]
            elif create:
                args = ["worktree", "add", "-b", branch, str(target)]
            else:
                raise ToolError(f"there is no branch '{branch}'; create=true for a new branch")
            code, out = run_git(args, root, check=False)
            if code != 0:
                raise ToolError(f"could not open the working copy:\n{out}")
        switch = ctx.state.get("set_cwd")
        if callable(switch):
            switch(target)
        else:
            ctx.cwd = target
        return ToolResult(f"separate working copy of '{branch}': {target}\nThis is now the working folder; changes in the main copy "
                          f"are not touched. Remove it with worktree_remove when done.")

    @staticmethod
    def _commit(root: Path, message: str, files: list[str], add_all: bool) -> ToolResult:
        if not message.strip():
            raise ToolError("commit needs message")
        if add_all:
            run_git(["add", "-A"], root)
        elif files:
            run_git(["add", "--", *files], root)
        secrets_ = staged_secrets(root)
        if secrets_:
            raise ToolError("commit cancelled: a key or secret file seems to be staged: " + ", ".join(secrets_[:8])
                            + ". Unstage them with `git restore --staged <file>` and add them to .gitignore.")
        code, _ = run_git(["diff", "--cached", "--quiet"], root, check=False)
        if code == 0:
            raise ToolError("nothing to commit (give files or all=true)")
        _, out = run_git(["commit", "-m", message], root)
        return ToolResult(out.splitlines()[0] if out else "commit'lendi")

    def _audit(self, ctx: ToolContext, root: Path) -> ToolResult:
        """Audits the repo and auth status (all read-only)."""
        issues: list[str] = []

        # 1. Remote info and reachability (fetch health)
        # The report writes the user:token part of URLs MASKED; the raw value is kept only for detection
        # (otherwise the audit itself was leaking the key to the agent/log).
        remotes_out = run_git(["remote", "-v"], root)[1]
        remotes_shown = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", remotes_out)
        lines = [f"Remotes:\n{remotes_shown or 'none'}"]
        if not remotes_out.strip():
            issues.append("no remote is set: can't push/pull")
        else:
            # Remote reachability check (ls-remote); capped at 15 s so the audit doesn't hang when there is no network.
            fetch_check = run_git(["ls-remote", "--exit-code", "--refs", "origin", "HEAD"], root, timeout=15, check=False)
            if fetch_check[0] != 0:
                issues.append("the remote (origin) can't be reached, or there is a network/permission problem")
            else:
                lines.append("Remote reachability: OK (origin connection works)")
        # If the remote URL has key info embedded in it (masked in the report)
        if re.search(r"(https?://)[^/@\s]+:[^/@\s]+@github\.com", remotes_out):
            issues.append("user:token is embedded in the remote URL (`.git/config` should be cleaned)")
        bad_hosts = set(re.findall(r"git@([^:]+):", remotes_out)) - {"github.com"}
        for host in sorted(bad_hosts):
            issues.append(f"unknown remote host: {host}")

        # 2. Branch and upstream status
        branch = current_branch(root)
        if branch:
            upstream = run_git(["rev-parse", "--abbrev-ref", f"{branch}@{{u}}"], root, check=False)[1].strip()
            lines.append(f"\nDal: {branch}")
            if upstream:
                lines.append(f"Upstream: {upstream}")
                # Ahead/Behind
                ab_out = run_git(["rev-list", "--left-right", "--count", f"{upstream}...{branch}"], root, check=False)[1]
                if ab_out and "\t" in ab_out:
                    behind, ahead = ab_out.strip().split("\t")
                    lines.append(f"Durum: {ahead} ahead, {behind} behind")
                
                # Local-only commits
                local_only = run_git(["log", f"{upstream}..{branch}", "--oneline", "-n", "5"], root, check=False)[1]
                if local_only.strip():
                    lines.append(f"Unpushed commits (last 5):\n{local_only}")
            else:
                lines.append("No upstream set.")
                issues.append(f"'{branch}' has no upstream: it doesn't count as pushed")
        else:
            lines.append("\nDetached HEAD.")
            issues.append("detached HEAD: no branch to commit to")

        # 2b. Branches without an upstream (may not have been pushed)
        no_up = run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], root, check=False)[1].splitlines()
        orphans = [b.strip() for b in no_up if b.strip()
                   and not run_git(["rev-parse", "--abbrev-ref", f"{b.strip()}@{{u}}"], root, check=False)[1].strip()]
        if len(orphans) > 1 or (orphans and orphans != [branch]):
            lines.append("Other branches without an upstream: " + ", ".join(orphans))

        # 3. Auth status
        token = _token(ctx)
        if token:
            source = token_source(ctx.config, token)
            mask = token[:4] + "..." + token[-4:] if len(token) > 8 else "****"
            lines.append(f"\nAuth: {source} (token: {mask})")
        else:
            lines.append("\nAuth: no token found.")

        # 5. State that contradicts the remote (the upstream may have been deleted)
        branches_vv = run_git(["branch", "-vv"], root, check=False)[1]
        if ": gone]" in branches_vv:
            issues.append("a branch's upstream was deleted (': gone]'); it should be published again")

        if issues:
            lines.append("\nSorunlar:" + "".join(f"\n- {i}" for i in issues))
        else:
            lines.append("\nNo problems found.")
        return ToolResult("\n".join(lines))

    def _push(self, ctx: ToolContext, root: Path, branch: str, force: bool, allow_protected: bool) -> ToolResult:
        local = current_branch(root)
        branch = branch or local
        if not branch:
            raise ToolError("detached HEAD: give branch to push")
        default = ""
        repo = origin_repo(root)
        if repo:
            try:
                default = _api(ctx).get(f"/repos/{repo}").get("default_branch", "")
            except ToolError:
                default = ""
        # Pushing to a protected branch happens only with protected=true: risk_for then asks for approval.
        if protected(branch, ctx.config, default) and not (allow_protected or force):
            raise ToolError(f"'{branch}' is a protected branch (the repo's main branch). Instead of pushing to it directly, open a new branch "
                            f"(git switch create=true), push it and use github pr_create. If the user asked for it explicitly, "
                            f"call again with protected=true; approval will be asked.")
        # Detached HEAD (e.g. the PR tracker's worktree): no local branch, so HEAD is pushed to the remote branch.
        ref = branch if local else f"HEAD:refs/heads/{branch}"
        args = ["push"] + (["-u"] if local else []) + ["origin", ref] + (["--force-with-lease"] if force else [])
        _, out = run_git(args, root, _token(ctx), timeout=300)
        return ToolResult(out or f"{branch} pushed")


# --------------------------------------------------------------------------------------------- github tool
GH_READ = {"repos", "prs", "pr_view", "issues", "checks", "ci_wait", "ci_logs", "reviews", "watching"}
WAIT_POLL = 15.0  # s: interval at which ci_wait checks CI
GH_DANGER = {"pr_merge", "pr_close"}


class GitHub(Tool):
    name = "github"
    description = (
        "GitHub, with the user's key; an empty repo means the working folder's origin.\n"
        "repos, clone (clones into ~/GitHub and moves there), prs, pr_view, checks, issues. pr_create: pushes the "
        "branch and opens a PR (not from a protected branch). pr_comment, issue_create. pr_merge/pr_close: ask for "
        "approval; doesn't merge while CI is red/running.\n"
        "CI: ci_wait (wait until it finishes; after a push), ci_logs (error summary of failed jobs; for 'why is CI "
        "red' use this first), ci_rerun (re-run the failed ones), ci_run (trigger a workflow; empty lists them).\n"
        "Review: reviews (open threads, with their ids), review_reply, review_resolve, request_review. update_branch: "
        "updates the PR branch from the base branch.\n"
        "watch/unwatch/watching: watch a PR in the background; on red CI, a comment or a conflict it fixes it itself "
        "in a separate working copy, and if asked merges when green (auto_merge)."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["repos", "clone", "prs", "pr_view", "checks", "issues", "pr_create",
                                              "pr_comment", "pr_merge", "pr_close", "issue_create", "ci_wait", "ci_logs",
                                              "ci_rerun", "ci_run", "reviews", "review_reply", "review_resolve",
                                              "request_review", "update_branch", "watch", "unwatch", "watching"],
                   "description": "What to do."},
        "comment_id": {"type": "integer", "description": "review_reply: id of the line comment to answer (the #number "
                                                         "in the reviews output)."},
        "thread": {"type": "string", "description": "review_resolve: thread id (the 'konu ...' in the reviews "
                                                    "output)."},
        "workflow": {"type": "string", "description": "ci_run: workflow file name or id (e.g. ci.yml)."},
        "minutes": {"type": "integer", "description": "ci_wait: wait at most this many minutes. Default 15, at most "
                                                      "45."},
        "inputs": {"type": "object", "description": "ci_run: workflow_dispatch girdileri."},
        "reviewers": {"type": "array", "items": {"type": "string", "description": "GitHub user name."},
                      "description": "request_review: inceleyiciler."},
        "auto_fix": {"type": "boolean", "description": "watch: fix it itself on red CI or a conflict. Default true."},
        "address_comments": {"type": "boolean", "description": "watch: apply and answer trusted review comments. "
                                                               "Default true."},
        "auto_merge": {"type": "boolean", "description": "watch: merge by itself when CI is green and no thread is "
                                                         "open. Default false; only if the user clearly asks."},
        "auto_review": {"type": "boolean", "description": "watch: have an independent reviewer review each new commit "
                                                          "and post the findings to the PR. Default true."},
        "repo": {"type": "string", "description": "'owner/repo'. Empty: the working folder's origin."},
        "query": {"type": "string", "description": "repos: filter by name/description."},
        "number": {"type": "integer", "description": "PR or issue number."},
        "title": {"type": "string", "description": "Title for pr_create/issue_create."},
        "body": {"type": "string", "description": "PR/issue description or comment text."},
        "base": {"type": "string", "description": "pr_create: target branch. Default: the repository's default "
                                                  "branch."},
        "head": {"type": "string", "description": "pr_create: source branch. Default: the current branch."},
        "branch": {"type": "string", "description": "clone: branch to switch to. checks: branch/commit to check."},
        "draft": {"type": "boolean", "description": "pr_create: taslak PR."},
        "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Filter for prs/issues. Default "
                                                                                      "open."},
        "method": {"type": "string", "enum": ["merge", "squash", "rebase"],
                   "description": "pr_merge method. Default: config [github] merge_method."},
        "force": {"type": "boolean", "description": "pr_merge: merge even if CI is red/running (if the user clearly "
                                                    "asks)."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in GH_READ:
            return READ
        if a in GH_DANGER or (a == "watch" and args.get("auto_merge")):
            return DANGER  # will merge later without asking: the user must approve
        return WRITE

    def summary(self, args: dict[str, Any]) -> str:
        bits = [args.get("action", ""), args.get("repo") or ""]
        if args.get("number"):
            bits.append(f"#{args['number']}")
        bits.append(args.get("title") or args.get("query") or "")
        return " ".join(b for b in bits if b)[:200]

    def _repo(self, ctx: ToolContext, repo: str) -> str:
        if repo:
            r = parse_remote(repo) or repo.strip().strip("/")
            if not REPO_RX.fullmatch(r):
                raise ToolError(f"repo must look like 'owner/repo': {repo}")
            return r
        r = origin_repo(ctx.cwd)
        if not r:
            raise ToolError("the working folder is not a GitHub repository: give repo='owner/repo' or use action=clone")
        return r

    def run(self, ctx: ToolContext, action: str, repo: str = "", query: str = "", number: int = 0, title: str = "",
            body: str = "", base: str = "", head: str = "", branch: str = "", draft: bool = False,
            state: str = "open", method: str = "", force: bool = False, comment_id: int = 0, thread: str = "",
            workflow: str = "", inputs: dict[str, Any] | None = None, reviewers: list[str] | None = None,
            auto_fix: bool = True, address_comments: bool = True, auto_merge: bool = False,
            auto_review: bool = True, minutes: int = 15) -> ToolResult:
        if action == "watching":
            from alice import prwatch

            return ToolResult(prwatch.describe())
        api = _api(ctx)
        if action == "repos":
            rows = api.user_repos(query)
            if not rows:
                return ToolResult("repo not found")
            return ToolResult("\n".join(f"{r['full_name']}{' (gizli)' if r['private'] else ''}  {r['description'][:80]}"
                                        for r in rows))
        if action == "clone":
            if not repo:
                raise ToolError("clone needs repo='owner/repo'")
            path, note = clone_or_update(ctx.config, repo, branch, api.token)
            switch = ctx.state.get("set_cwd")
            if callable(switch):
                switch(path)
            else:
                ctx.cwd = path
            return ToolResult(f"{repo} {note}: {path}\nThis is now the working folder.")
        repo = self._repo(ctx, repo)
        sign = True
        if action in ("pr_create", "pr_comment", "issue_create", "review_reply"):
            from alice import undercover

            if undercover.active(ctx.config, repo, api):
                sign = False
                title = undercover.clean(ctx.config, title, "title") if title else title
                body = undercover.clean(ctx.config, body, "text") if body else body
        if action == "prs":
            rows = api.get(f"/repos/{repo}/pulls", state=state or "open", per_page=30)
            if not rows:
                return ToolResult("no PRs")
            return ToolResult("\n".join(f"#{p['number']} {p['title']}  ({p['head']['ref']} -> {p['base']['ref']}"
                                        f"{', taslak' if p.get('draft') else ''}, {p['user']['login']})" for p in rows))
        if action == "issues":
            rows = [i for i in api.get(f"/repos/{repo}/issues", state=state or "open", per_page=30)
                    if "pull_request" not in i]
            return ToolResult("\n".join(f"#{i['number']} {i['title']}" for i in rows) or "no issues")
        if action == "checks":
            ref = branch or (current_branch(ctx.cwd) if not number else "")
            if number:
                ref = api.get(f"/repos/{repo}/pulls/{number}")["head"]["sha"]
            if not ref:
                raise ToolError("checks needs branch or number")
            return ToolResult(_fmt_checks(api.checks(repo, ref)))
        if action == "pr_view":
            return ToolResult(self._view(api, repo, self._need(number)))
        if action == "pr_create":
            return self._create(ctx, api, repo, title, body, base, head, draft)
        if action == "pr_comment":
            if not body:
                raise ToolError("a comment needs body")
            from alice import undercover

            c = api.post(f"/repos/{repo}/issues/{self._need(number)}/comments", {"body": body})
            undercover.remember_own(c.get("id"))
            return ToolResult(f"yorum eklendi: {c.get('html_url', '')}")
        if action == "issue_create":
            if not title:
                raise ToolError("issue needs title")
            i = api.post(f"/repos/{repo}/issues", {"title": title, "body": body})
            return ToolResult(f"issue #{i['number']} opened: {i['html_url']}")
        if action == "pr_merge":
            return self._merge(ctx, api, repo, self._need(number), method, force)
        if action == "pr_close":
            p = api.call("PATCH", f"/repos/{repo}/pulls/{self._need(number)}", json={"state": "closed"})
            return ToolResult(f"#{p['number']} closed")
        if action == "ci_wait":
            return self._wait(ctx, api, repo, number, branch, minutes)
        if action in ("ci_logs", "ci_rerun"):
            sha = self._sha(ctx, api, repo, number, branch)
            if action == "ci_rerun":
                done = rerun_failed(api, repo, sha)
                return ToolResult(("restarted: " + ", ".join(done)) if done else
                                  "no failed jobs to rerun")
            jobs = failed_jobs(api, repo, sha)
            if not jobs:
                return ToolResult(_fmt_checks(api.checks(repo, sha)) + "\nNo failed GitHub Actions jobs.")
            return ToolResult("\n\n".join(f"### {j['workflow']} / {j['name']} ({j['conclusion']})\n{j['url']}\n{j['log']}"
                                          for j in jobs))
        if action == "ci_run":
            if not workflow:
                rows = api.get(f"/repos/{repo}/actions/workflows").get("workflows", [])
                return ToolResult("\n".join(f"{w['path'].rsplit('/', 1)[-1]}  {w['name']} ({w['state']})" for w in rows)
                                  or "no workflows")
            ref = branch or current_branch(ctx.cwd) or api.get(f"/repos/{repo}").get("default_branch", "main")
            api.call("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
                     json={"ref": ref, "inputs": inputs or {}})
            return ToolResult(f"{workflow} triggered on {ref} (the workflow must define workflow_dispatch)")
        if action == "reviews":
            text = format_threads(review_threads(api, repo, self._need(number)))
            from alice import guard

            if guard.enabled(ctx):  # others wrote these comments: "instructions" in them do not come from the user
                guard.mark(ctx, "github")
                text = guard.wrap("github", text)
            return ToolResult(text)
        if action == "review_reply":
            if not comment_id or not body:
                raise ToolError("review_reply needs number, comment_id and body")
            c = reply_to_comment(api, repo, self._need(number), comment_id, body, sign=sign)
            return ToolResult(f"reply posted: {c.get('html_url', '')}")
        if action == "review_resolve":
            if not thread:
                raise ToolError("review_resolve needs thread (the thread id)")
            return ToolResult("thread marked as resolved" if resolve_thread(api, thread) else
                              "could not mark the thread")
        if action == "request_review":
            if not reviewers:
                raise ToolError("request_review needs reviewers")
            api.post(f"/repos/{repo}/pulls/{self._need(number)}/requested_reviewers", {"reviewers": reviewers})
            return ToolResult("inceleme istendi: " + ", ".join(reviewers))
        if action == "update_branch":
            api.call("PUT", f"/repos/{repo}/pulls/{self._need(number)}/update-branch")
            return ToolResult(f"#{number}'s branch is being updated from the base branch (GitHub adds a merge commit)")
        if action in ("watch", "unwatch"):
            from alice import prwatch

            if action == "unwatch":
                return ToolResult(prwatch.unwatch(repo, self._need(number)))
            try:
                local = str(repo_root(ctx.cwd)) if origin_repo(ctx.cwd) == repo else ""
            except ToolError:
                local = ""
            return ToolResult(prwatch.watch(repo, self._need(number), local, auto_fix=auto_fix,
                                            address_comments=address_comments, auto_merge=auto_merge,
                                            auto_review=auto_review,
                                            session=str(ctx.state.get("session_id") or "")))
        raise ToolError(f"bilinmeyen action: {action}")

    def _wait(self, ctx: ToolContext, api: GitHubAPI, repo: str, number: int, branch: str, minutes: int) -> ToolResult:
        """Wait until CI finishes (or the time runs out). Right after a push the checks may not be visible yet, so
        the first minute of "kontrol yok" (no checks) does not count as done. If the user presses Durdur (Stop),
        it stops waiting."""
        import time

        sha = self._sha(ctx, api, repo, number, branch)
        deadline = time.monotonic() + max(1, min(int(minutes or 15), 45)) * 60
        grace = time.monotonic() + 60
        ch = api.checks(repo, sha)
        while time.monotonic() < deadline and not ctx.cancelled():
            if ch["state"] in ("success", "failure") or (ch["state"] == "none" and time.monotonic() > grace):
                break
            time.sleep(WAIT_POLL)
            ch = api.checks(repo, sha)
        text = _fmt_checks(ch)
        if ch["state"] == "pending":
            text += "\nTime ran out; CI is still running."
        elif ch["state"] == "failure":
            text += "\nFor the cause: action=ci_logs."
        return ToolResult(f"{sha[:7]}: {text}")

    def _sha(self, ctx: ToolContext, api: GitHubAPI, repo: str, number: int, branch: str) -> str:
        if number:
            return api.get(f"/repos/{repo}/pulls/{number}")["head"]["sha"]
        ref = branch or current_branch(ctx.cwd)
        if not ref:
            raise ToolError("give the PR number (number) or branch")
        return api.get(f"/repos/{repo}/commits/{ref}")["sha"]

    @staticmethod
    def _need(number: int) -> int:
        if not number:
            raise ToolError("PR/issue number (number) is required")
        return int(number)

    @staticmethod
    def _view(api: GitHubAPI, repo: str, number: int) -> str:
        p = api.get(f"/repos/{repo}/pulls/{number}")
        ch = api.checks(repo, p["head"]["sha"])
        merge = {True: "no conflict", False: "CONFLICT", None: "computing"}.get(p.get("mergeable"), "?")
        lines = [f"#{number} {p['title']}  [{p['state']}{', taslak' if p.get('draft') else ''}"
                 f"{', merged' if p.get('merged') else ''}]",
                 f"{p['head']['ref']} -> {p['base']['ref']}, {p.get('commits', '?')} commit, "
                 f"+{p.get('additions', 0)} -{p.get('deletions', 0)}, {p.get('changed_files', 0)} dosya, {merge}",
                 _fmt_checks(ch), p["html_url"]]
        if p.get("body"):
            lines.append("\n" + p["body"][:1500])
        return "\n".join(lines)

    def _create(self, ctx: ToolContext, api: GitHubAPI, repo: str, title: str, body: str, base: str, head: str,
                draft: bool) -> ToolResult:
        if not title:
            raise ToolError("pr_create needs title")
        info = api.get(f"/repos/{repo}")
        base = base or info.get("default_branch", "main")
        root = repo_root(ctx.cwd)
        head = head or current_branch(root)
        if not head or head == base or protected(head, ctx.config, info.get("default_branch", "")):
            raise ToolError(f"a PR can't be opened from a protected branch ({head or 'detached HEAD'}): first open a new branch with "
                            f"git switch create=true, commit, then try again.")
        _, dirty = run_git(["status", "--porcelain"], root, check=False)
        note = "\nWarning: there are uncommitted changes; they are not in the PR." if dirty.strip() else ""
        if origin_repo(root) == repo:
            run_git(["push", "-u", "origin", head], root, api.token, timeout=300)
        p = api.post(f"/repos/{repo}/pulls", {"title": title, "body": body, "base": base, "head": head, "draft": draft})
        return ToolResult(f"PR #{p['number']} opened ({head} -> {base}): {p['html_url']}{note}")

    def _merge(self, ctx: ToolContext, api: GitHubAPI, repo: str, number: int, method: str, force: bool) -> ToolResult:
        cfg = settings(ctx.config)
        p = api.get(f"/repos/{repo}/pulls/{number}")
        if p.get("merged"):
            return ToolResult(f"#{number} is already merged")
        if p["state"] != "open":
            raise ToolError(f"#{number} is closed")
        if p.get("draft"):
            raise ToolError(f"#{number} is a draft; it must be marked ready first")
        if p.get("mergeable") is False:
            raise ToolError(f"#{number} conflicts with the base branch; resolve the conflict first (git pull origin {p['base']['ref']})")
        ch = api.checks(repo, p["head"]["sha"])
        if cfg.get("require_checks", True) and ch["state"] in ("failure", "pending") and not force:
            raise ToolError(f"not merged, CI {('red: ' + ', '.join(ch['failed'])) if ch['failed'] else 'still running'}"
                            ". Wait or fix it; force=true if the user asks explicitly.")
        m = method or cfg.get("merge_method", "squash")
        res = api.call("PUT", f"/repos/{repo}/pulls/{number}/merge",
                       json={"merge_method": m, "sha": p["head"]["sha"]})
        return ToolResult(f"#{number} merged ({m}): {res.get('sha', '')[:7]}")


def _fmt_checks(ch: dict[str, Any]) -> str:
    if ch["state"] == "none":
        return "CI: no checks"
    label = {"success": "green", "failure": "red", "pending": "running"}[ch["state"]]
    out = f"CI: {label} ({ch['total']} kontrol)"
    if ch["failed"]:
        out += " failed: " + ", ".join(ch["failed"][:10])
    if ch["pending"]:
        out += " running: " + ", ".join(ch["pending"][:10])
    return out


TOOLS: list[Tool] = [Git(), GitHub()]
