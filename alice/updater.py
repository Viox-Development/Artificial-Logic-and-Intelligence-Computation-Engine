"""ALICE updates itself.

Since the repo was installed with `git clone`, the update check is simple: is the local HEAD the same as
the tip of the branch on GitHub (default `main`)? The check uses `git ls-remote`, a single network round trip;
since it does not fetch, it finishes in under a second at startup.

Flow:

* `alice update --check`  : is there a new commit, what changed, is CI green
* `alice update`          : fast-forward pull, install dependencies if needed
* `alice app`             : if config [update] mode="auto", it updates silently at startup and
                            if there is an update, restarts itself and opens the latest version
* while the app is open   : periodic check in the background, UI shows "Güncelle ve yeniden başlat" (Update and restart)
* background core        : pulls by itself while the app is closed; when idle (no task running)
                            it restarts silently. If the app is open it does not pull; it leaves the job to the UI.
* stale process        : if the code on disk is newer than the running process (a manual `git pull` or another
                            process pulled it), the process notices: the app says "yeniden başlat" (restart),
                            and an idle core restarts itself, so the old process does not run a mix of
                            old and new files.

Rules:

* If the working tree is dirty (you have your own changes), no automatic update is done; you are only notified.
* When `require_green_ci = true` and the commit's CI is red, no automatic update is done; `alice update --force`
  gets past this manually. (CI status is read with the keyless GitHub API; if it cannot be reached it counts as
  "bilinmiyor" (unknown) and the update goes ahead.)
* If `pyproject.toml` changed, `pip install -e .` runs automatically after the update; which optional packages
  are installed is detected and they are installed with the same extras.

Rollback (a broken version must not leave ALICE unable to start):

* Smoke test: the main modules of the pulled version are imported in a separate Python process. If it fails
  (syntax error, missing package), the working tree returns to the previous commit and it is marked "bozuk" (broken).
* Boot health: boots after an update are counted. The new version is healthy once it stays up for HEALTHY_AFTER
  seconds. If MAX_BAD_BOOTS boots in a row end before that (open and crash), it rolls back to the previous commit.
* A commit marked broken is not pulled again automatically (it is tried when a new commit arrives); `alice update
  --force` still pulls it. The rollback is reported once.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

REPO = "Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine"
API = "https://api.github.com"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
WINDOWS = sys.platform == "win32"
REMOTE_URL = f"https://github.com/{REPO}.git"
# The commit the Windows installer (ALICE-Setup.exe) was built from. If there is no .git in the install, the repo is
# re-created from this commit (without touching the files), so an ALICE installed via the exe can update itself too.
BUILD_FILE = ".alice-build"
GIT_INSTALL_RETRY = 86400.0  # seconds: if Git could not be installed with winget, it is retried no sooner than this
DEFAULTS: dict[str, Any] = {
    "mode": "auto",              # auto | notify | off
    # stable: yalnız yayınlanmış sürüm etiketlerine (vX.Y.Z) güncellenir. main'e birleşen her PR kullanıcıların
    # bilgisayarında hemen çalışmasın diye varsayılan bu. main: dalın en son commit'ine (geliştirici için).
    "channel": "stable",
    "branch": "main",
    "check_every_minutes": 60,
    "require_green_ci": True,
    "startup_timeout": 12,
    "startup_check": True,
}
# extra name -> a module that can be imported if that extra is installed
SMOKE_MODULES = ("alice.cli", "alice.agent", "alice.memory", "alice.app.server", "alice.core.service",
                 "alice.voice.live", "alice.tools")
HEALTHY_AFTER = 90.0  # seconds: the new version counts as healthy once it stays up this long
MAX_BAD_BOOTS = 2     # if this many boots end without counting as healthy, roll back
EXTRA_PROBE = {"computer": "mss", "browser": "playwright", "app": "webview", "core": "pystray",
               "voice": "sounddevice", "home": "websockets", "timeline": "keyring"}


def settings(cfg: Any = None) -> dict[str, Any]:
    data = getattr(cfg, "data", None) or {}
    out = {**DEFAULTS, **(data.get("update") or {})}
    env = os.environ.get("ALICE_UPDATE_MODE")
    if env in ("auto", "notify", "off"):
        out["mode"] = env
    channel = os.environ.get("ALICE_UPDATE_CHANNEL")
    if channel in ("stable", "main"):
        out["channel"] = channel
    return out


# --------------------------------------------------------------------------------- git helpers
_NET = ("ls-remote", "fetch", "pull")
_token_cache: list[Any] = []


def _auth_env() -> dict[str, str]:
    """Private repo: for network operations, ALICE's GitHub key (.env or `gh auth`) is given to git. On a machine
    set up with the installer wizard, git may not have its own saved login; the update should still work.
    The key only enters this command's environment (alice.tools.github.git_env)."""
    try:
        from alice.tools.github import git_env, github_token

        if not _token_cache or time.monotonic() - _token_cache[1] > 3600:
            # Pass the config file too: the key that can reach the repo may be in its GitHub connection.
            # Without it, the key from .env (not authorized for this private repo) gets picked, and since git puts it
            # before every saved login, the update failed with 403 (7 Kasım / November 7).
            from alice.config import load_config

            try:
                cfg = load_config()
            except Exception:  # noqa: BLE001 - if the config cannot be read, try anyway
                cfg = None
            _token_cache[:] = [github_token(cfg), time.monotonic()]
        token = _token_cache[0]
        return {k: v for k, v in git_env(token).items() if k.startswith("GIT_CONFIG_")} if token else {}
    except Exception:  # noqa: BLE001 - if there is no key, try with git's own login
        return {}


def _git(args: list[str], cwd: Path, timeout: float = 30.0) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never"}
    if args and args[0] in _NET:
        auth = _auth_env()
        if auth:
            code, out = _git_run(args, cwd, timeout, {**env, **auth})
            if code == 0 or not any(s in out for s in ("403", "not granted", "Authentication failed")):
                return code, out
            # ALICE's key is not authorized for this repo: try once more with git's own saved login.
    return _git_run(args, cwd, timeout, env)


def git_exe() -> str | None:
    """Path to git. In the exe install, if Git was just installed with winget it is not yet in PATH (ALICE started right
    after the install runs with the old PATH): then Git's known install folders are checked."""
    found = shutil.which("git")
    if found or not WINDOWS:
        return found
    for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if not base:
            continue
        root = Path(base, "Programs", "Git") if env == "LOCALAPPDATA" else Path(base, "Git")
        for exe in (root / "cmd" / "git.exe", root / "bin" / "git.exe"):
            if exe.is_file():
                return str(exe)
    return None


_git_install: threading.Thread | None = None
_git_install_lock = threading.Lock()


def git_installing() -> bool:
    """Is Git currently being installed in the background with winget?"""
    t = _git_install
    return t is not None and t.is_alive()


def _install_git(winget: str) -> None:
    try:
        subprocess.run([winget, "install", "-e", "--id", "Git.Git", "--scope", "user", "--silent",
                        "--accept-package-agreements", "--accept-source-agreements"], capture_output=True,
                       timeout=900, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def ensure_git(wait: bool = False) -> str | None:
    """If Git is missing (Windows, winget available), install it for the user; at most one try per day. The install
    runs in the background and is not awaited (wait=False): winget can take up to 15 minutes, and the `alice app`
    startup used to freeze during that time. Once the install finishes, the next check finds Git."""
    global _git_install
    exe = git_exe()
    if exe or not WINDOWS:
        return exe
    with _git_install_lock:
        t = _git_install
        if t is None or not t.is_alive():
            winget = shutil.which("winget")
            if not winget or time.time() - float(read_state().get("git_install_tried") or 0) < GIT_INSTALL_RETRY:
                return None
            write_state(git_install_tried=time.time())
            t = _git_install = threading.Thread(target=_install_git, args=(winget,), daemon=True,
                                                name="alice-git-install")
            t.start()
    if not wait:
        return None
    t.join(910)
    return git_exe()


def _git_run(args: list[str], cwd: Path, timeout: float, env: dict[str, str]) -> tuple[int, str]:
    try:
        r = subprocess.run([git_exe() or "git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return 127, "git bulunamadı"
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} zaman aşımına uğradı"
    except OSError as e:  # noqa: BLE001
        return 1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def repo_dir() -> Path | None:
    """The git repo ALICE runs from (the folder you cloned in an editable install). None if it is not one."""
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return None
    code, out = _git(["rev-parse", "--show-toplevel"], root, timeout=10)
    if code != 0:
        return None
    top = Path(out.strip().splitlines()[-1]) if out.strip() else root
    return top if (top / "alice" / "__init__.py").exists() else None


def install_root() -> Path | None:
    """ALICE's source folder (editable install: the clone, or the folder the exe installed)."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / "alice" / "__init__.py").exists() and (root / "pyproject.toml").exists() else None


def bootstrap_repo(root: Path, branch: str = "main") -> tuple[Path | None, str]:
    """If there is no .git in the install, re-create the repo from the commit the install was built from (BUILD_FILE).
    The working files are not touched: only the history is downloaded and the folder is attached to that commit;
    later updates are pulled the normal way."""
    try:
        sha = (root / BUILD_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        sha = ""
    if not sha:
        return None, ""
    # Attach HEAD to the configured branch; else auto-update stops with "depo 'main' dalında" (repo is on 'main').
    steps = [["init", "-q"], ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], ["remote", "add", "origin", REMOTE_URL],
             ["fetch", "--depth", "1", "origin", sha], ["reset", "-q", "--mixed", "FETCH_HEAD"]]
    if WINDOWS:
        steps.insert(1, ["config", "core.autocrlf", "true"])  # install files are CRLF: so they don't look "changed"
    for args in steps:
        code, out = _git(args, root, timeout=300 if args[0] == "fetch" else 30)
        if code != 0:
            shutil.rmtree(root / ".git", ignore_errors=True)  # no half-made repo left; the next check retries
            return None, f"git {args[0]} başarısız: {out.strip()[-200:]}"
    return repo_dir(), ""


def _find_repo(branch: str = "main") -> tuple[Path | None, str]:
    """If there is no repo, find the reason and fix it if possible: find/install Git, re-create a missing .git."""
    repo = repo_dir()
    if repo is not None:
        return repo, ""
    root = install_root()
    if root is None:
        return None, f"ALICE git deposundan kurulmamış; güncelleme için depoyu klonla (git clone https://github.com/{REPO})"
    if ensure_git() is None:
        if git_installing():
            return None, "Git arka planda kuruluyor; kurulum bitince güncelleme kendiliğinden denenecek"
        return None, ("Git bulunamadı; otomatik güncelleme için Git gerekiyor. Git'i kur (winget install Git.Git "
                      "ya da git-scm.com) ve ALICE'i yeniden aç")
    # The app and the core may both get here at once: one installing .git must not let the other delete it half-made.
    try:
        with update_lock():
            repo = repo_dir()  # another process may have set it up while we waited for the lock
            if repo is not None:
                return repo, ""
            if (root / ".git").exists():
                return None, f"{root} bir git deposu ama git onu okuyamadı"
            repo, err = bootstrap_repo(root, branch)
    except UpdateBusy as e:
        return None, f"{e}; birazdan tekrar denenecek"
    if repo is not None:
        return repo, ""
    if err:
        return None, f"güncelleme geçmişi indirilemedi ({err}); internet bağlantını kontrol et"
    return None, (f"ALICE git deposundan kurulmamış; güncelleme için depoyu klonla (git clone https://github.com/{REPO}) "
                  "ya da en son ALICE-Setup.exe'yi kur")


def local_commit(repo: Path) -> str:
    code, out = _git(["rev-parse", "HEAD"], repo, timeout=10)
    return out.strip() if code == 0 else ""


def current_branch(repo: Path) -> str:
    code, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo, timeout=10)
    return out.strip() if code == 0 else ""


def is_dirty(repo: Path) -> bool:
    code, out = _git(["status", "--porcelain", "--untracked-files=no"], repo, timeout=20)
    return code == 0 and bool(out.strip())


def remote_head(repo: Path, branch: str, timeout: float = 20.0) -> tuple[str, str]:
    """(sha, error). One network round trip: ls-remote does not fetch."""
    code, out = _git(["ls-remote", "--heads", "origin", branch], repo, timeout=timeout)
    if code != 0:
        return "", out.strip().splitlines()[-1] if out.strip() else "ls-remote başarısız"
    # ls-remote matches the pattern from the end: "main" also returns "refs/heads/x/main", and that branch (sorted
    # first) was taken, so the wrong commit was treated as a "new version". The full name is searched.
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
            return parts[0], ""
    return "", f"uzak dal bulunamadı: {branch}"


TAG_RX = re.compile(r"^refs/tags/v(\d+)\.(\d+)\.(\d+)$")


def latest_release(repo: Path, timeout: float = 20.0) -> tuple[str, str, str]:
    """En yüksek sürüm etiketi: (commit sha, etiket, hata). Açıklamalı etiketlerde commit'in kendisi (^{}) alınır."""
    code, out = _git(["ls-remote", "--tags", "origin", "refs/tags/v*"], repo, timeout=timeout)
    if code != 0:
        return "", "", out.strip().splitlines()[-1] if out.strip() else "ls-remote başarısız"
    shas: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        peeled = ref.endswith("^{}")
        ref = ref[:-3] if peeled else ref
        if TAG_RX.match(ref) and (peeled or ref not in shas):
            shas[ref] = sha
    if not shas:
        return "", "", "henüz yayınlanmış bir sürüm yok"
    best = max(shas, key=lambda r: tuple(int(x) for x in TAG_RX.match(r).groups()))  # type: ignore[union-attr]
    return shas[best], best.removeprefix("refs/tags/"), ""


def contains(repo: Path, sha: str, head: str = "HEAD") -> bool:
    """Is `sha` already in the local branch (an ancestor of head, or head itself)? False if the commit is not local."""
    code, _ = _git(["merge-base", "--is-ancestor", sha, head], repo, timeout=20)
    return code == 0


# --------------------------------------------------------------------------------- GitHub (keyless)
def _api(path: str, timeout: float = 8.0) -> Any:
    """Keyless GitHub API call. None if it cannot be reached (the update does not get stuck on it)."""
    import httpx

    try:
        r = httpx.get(f"{API}{path}", timeout=timeout, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "alice-updater"})
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 - on a network error the update check quietly falls back to "bilinmiyor" (unknown)
        return None


def commit_log(local: str, remote: str, timeout: float = 8.0) -> list[str]:
    """Commit headlines between two commits (newest first). An empty list if they cannot be reached."""
    data = _api(f"/repos/{REPO}/compare/{local}...{remote}", timeout)
    if not isinstance(data, dict):
        return []
    out = []
    for c in reversed(data.get("commits") or []):
        msg = str((c.get("commit") or {}).get("message", "")).strip().splitlines()
        if msg:
            out.append(msg[0][:120])
    return out[:20]


def ci_state(sha: str, timeout: float = 8.0) -> str:
    """"green" | "red" | "pending" | "unknown". Checks both classic statuses and check-runs."""
    if not sha:
        return "unknown"
    seen = False
    st = _api(f"/repos/{REPO}/commits/{sha}/status", timeout)
    if isinstance(st, dict) and st.get("statuses"):
        seen = True
        state = str(st.get("state", ""))
        if state == "failure" or state == "error":
            return "red"
        if state == "pending":
            return "pending"
    runs = _api(f"/repos/{REPO}/commits/{sha}/check-runs", timeout)
    if isinstance(runs, dict) and runs.get("check_runs"):
        seen = True
        for run in runs["check_runs"]:
            if run.get("status") != "completed":
                return "pending"
            if run.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required"):
                return "red"
    return "green" if seen else "unknown"


# --------------------------------------------------------------------------------- status
@dataclass
class UpdateInfo:
    repo: Path | None = None
    branch: str = "main"
    local: str = ""
    remote: str = ""
    dirty: bool = False
    commits: list[str] = field(default_factory=list)
    ci: str = "unknown"
    error: str = ""
    running: str = ""       # bu sürecin başladığı commit (mark_running çağrıldıysa)
    bad: bool = False       # uzaktaki commit daha önce açılamadığı için geri alındı
    ahead: bool = False     # uzaktaki commit yerelde zaten var (yerel dal önde): çekecek bir şey yok
    on: str = ""            # yerelde açık dal; ayarlardaki daldan farklıysa otomatik çekilmez
    tag: str = ""           # stable kanalında hedef sürüm etiketi (vX.Y.Z)

    @property
    def available(self) -> bool:
        return bool(self.local and self.remote and self.local != self.remote and not self.ahead)

    @property
    def stale(self) -> bool:
        """The code on disk is newer than this process: a restart is enough, there is nothing to pull."""
        return bool(self.running and self.local and self.running != self.local)

    @property
    def hand_pulled(self) -> bool:
        """Stale because someone moved the checkout by hand (git pull while developing), not because ALICE's own
        updater pulled it: apply_update records the commit it moved to as update_to."""
        return self.stale and read_state().get("update_to") != self.local

    @property
    def can_auto(self) -> bool:
        """Can it be applied without asking?"""
        return self.available and not self.blocked

    @property
    def blocked(self) -> str:
        """The reason that stops the automatic update (empty means no block). A manual `--force` gets past these."""
        if not self.available:
            return ""
        if self.bad:
            return "bu sürüm daha önce açılamadı ve geri alındı; yeni bir düzeltme bekleniyor"
        if self.on and self.on not in ("HEAD", self.branch):
            return f"depo '{self.on}' dalında; otomatik güncelleme yalnız {self.branch} dalında yapılır"
        if self.dirty:
            return "depoda kaydedilmemiş değişiklikler var"
        if self.ci == "red":
            return "yeni commit'in CI'ı kırmızı"
        if self.ci == "pending":
            return "CI hâlâ sürüyor"  # if it turns green at a check a few minutes later, it passes by itself
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {"available": self.available, "branch": self.branch, "local": self.local[:7],
                "remote": self.remote[:7], "commits": self.commits, "ci": self.ci, "dirty": self.dirty,
                "blocked": self.blocked, "error": self.error, "count": len(self.commits), "stale": self.stale,
                # On another branch on purpose (development): the UI shows it only when checked by hand.
                "off_branch": bool(self.available and self.on and self.on not in ("HEAD", self.branch)),
                # Restart needed only because of a manual git pull: the bar stays quiet (dot only) for developers;
                # an update ALICE pulled itself still shows the "Yeniden başlat" bar to everyone.
                "hand_pulled": self.hand_pulled,
                "running": self.running[:7], "tag": self.tag}


def _state_path() -> Path:
    return alice_home() / "update.json"


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(**kw: Any) -> None:
    """Writes to a temp file and then replaces the target. The app and the core write the same file: during a direct
    write_text, the other process reads the file half-written or empty, takes it as {}, and in its own write wipes out
    the "bad" list and the pending health check (a rolled-back broken version could be pulled again)."""
    import threading

    data = {**read_state(), **kw}
    p = _state_path()
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for i in range(5):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:  # Windows: the other process is reading it at that moment
                if i == 4:
                    raise
                time.sleep(0.05)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------------- between processes
_RUNNING: str | None = None
# seconds: a lock this old was left by a crashed process. A legitimate update can take up to fetch 300 + pip 900 +
# smoke test 180 sec. At 900 sec, a lock held by a running update was seen as stale and a second process entered git.
LOCK_STALE = 1800.0


def mark_running() -> str:
    """Remember which commit this process started with (the app and the core call this at startup)."""
    global _RUNNING
    if _RUNNING is None:
        repo = repo_dir()
        _RUNNING = local_commit(repo) if repo else ""
    return _RUNNING


def running_commit() -> str:
    return _RUNNING or ""


def _lock_path() -> Path:
    return alice_home() / "update.lock"


class UpdateBusy(RuntimeError):
    pass


def _holder_alive(p: Path) -> bool:
    """Is the process holding the lock still alive? If it cannot be read (it is being written) or psutil is missing,
    it counts as alive; only its age decides."""
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    if pid == os.getpid():
        return True
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001
        return True


class update_lock:  # noqa: N801 - used as a context manager: with update_lock():
    """Only one process updates at a time (if the app and the core enter git at once, index.lock conflicts)."""

    def __enter__(self) -> "update_lock":
        p = _lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - p.stat().st_mtime > LOCK_STALE or not _holder_alive(p):
                p.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise UpdateBusy("başka bir ALICE süreci şu an güncelliyor") from e
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            _lock_path().unlink()
        except OSError:
            pass


def _app_pid_path() -> Path:
    return alice_home() / "app.pid"


def mark_app() -> None:
    """The app is open: the core leaves the update to it."""
    try:
        _app_pid_path().parent.mkdir(parents=True, exist_ok=True)
        _app_pid_path().write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def clear_app() -> None:
    try:
        if _app_pid_path().read_text(encoding="utf-8").strip() == str(os.getpid()):
            _app_pid_path().unlink()
    except (OSError, ValueError):
        pass


def app_running() -> bool:
    try:
        pid = int(_app_pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        cmd = " ".join(psutil.Process(pid).cmdline()).lower()
    except Exception:  # noqa: BLE001 - if unsure, count it as open: the core should not pull for nothing
        return True
    return "alice" in cmd and "app" in cmd  # the pid may have been reassigned to another program


def local_log(repo: Path, a: str, b: str, limit: int = 20) -> list[str]:
    """Headlines between two commits, from local git (after fetch; no API needed)."""
    code, out = _git(["log", "--format=%s", f"-n{limit}", f"{a}..{b}"], repo, timeout=20)
    return [line.strip()[:120] for line in out.splitlines() if line.strip()] if code == 0 else []


def consume_announcement(who: str) -> dict[str, Any] | None:
    """If this consumer (app | core) has not yet announced the last update, return it and mark it as announced."""
    st = read_state()
    if not st.get("last_update") or st.get(f"announced_{who}"):
        return None
    if time.time() - float(st.get("last_update") or 0) > 7 * 86400:
        return None  # don't announce an update older than a week
    write_state(**{f"announced_{who}": True})
    return {"from": st.get("update_from", ""), "to": st.get("update_to", ""), "changes": st.get("changes") or []}


def check(cfg: Any = None, timeout: float | None = None, details: bool = True) -> UpdateInfo:
    """Is there a new commit? Network round trips: 1 (ls-remote), plus 2 keyless API calls if details are requested."""
    s = settings(cfg)
    branch = str(s.get("branch") or "main")
    repo, why = _find_repo(branch)
    if repo is None:
        return UpdateInfo(error=why)
    info = UpdateInfo(repo=repo, branch=branch, local=local_commit(repo), running=_RUNNING or "")
    wait = float(timeout or s.get("startup_timeout") or 12)
    if str(s.get("channel") or "stable") == "main":
        info.remote, info.error = remote_head(repo, branch, wait)
    else:
        info.remote, info.tag, info.error = latest_release(repo, wait)
    info.bad = bool(info.remote) and info.remote in (read_state().get("bad") or [])
    # If the local branch is ahead of the remote (e.g. a feature branch cut from main), the SHAs differ but there is
    # nothing to pull. Without this check, every startup said "new version available" and did an empty pull and restart.
    info.ahead = info.local != info.remote and bool(info.remote) and contains(repo, info.remote)
    info.on = current_branch(repo)
    if info.available:
        info.dirty = is_dirty(repo)
        if details:
            info.commits = commit_log(info.local, info.remote)
            info.ci = ci_state(info.remote) if s.get("require_green_ci", True) else "unknown"
    write_state(last_check=time.time(), remote=info.remote, local=info.local)
    return info


# --------------------------------------------------------------------------------- application
def installed_extras() -> list[str]:
    """Check the installed optional packages and guess which extras they were installed with."""
    import importlib.util

    out = []
    for extra, module in EXTRA_PROBE.items():
        try:
            if importlib.util.find_spec(module) is not None:
                out.append(extra)
        except (ImportError, ValueError):
            pass
    return out


def _pip_needed(repo: Path, local: str, remote: str) -> bool:
    code, out = _git(["diff", "--name-only", f"{local}..{remote}"], repo, timeout=30)
    if code != 0:
        return True  # if unsure, install: better than starting with missing dependencies
    names = set(out.split())
    return bool(names & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"})


def install_deps(repo: Path, emit: Callable[[str], None] | None = None) -> bool:
    extras = installed_extras()
    target = f".[{','.join(extras)}]" if extras else "."
    if emit:
        emit(f"bağımlılıklar kuruluyor: pip install -e {target}")
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", target], cwd=str(repo),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        if emit:
            emit(f"pip çalıştırılamadı: {e}")
        return False
    if r.returncode != 0 and emit:
        emit(f"pip hatası: {(r.stderr or r.stdout or '').strip()[-400:]}")
    return r.returncode == 0


def apply_update(info: UpdateInfo | None = None, cfg: Any = None, force: bool = False,
                 emit: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """Pull with fast-forward and install dependencies if needed. (success, message)."""
    info = info or check(cfg)
    say = emit or (lambda _m: None)
    if info.error and not info.available:
        return False, info.error
    if not info.available:
        return False, "zaten en son sürümdesin"
    if info.blocked and not force:
        return False, f"güncelleme yapılmadı: {info.blocked}"
    repo = info.repo
    assert repo is not None
    try:
        with update_lock():
            return _apply_locked(info, repo, say)
    except UpdateBusy as e:
        return False, str(e)


def _apply_locked(info: UpdateInfo, repo: Path, say: Callable[[str], None]) -> tuple[bool, str]:
    if local_commit(repo) != info.local:
        # Another process already pulled while we waited for the lock: don't pull again, the result is the same.
        return True, f"zaten güncellendi: {local_commit(repo)[:7]}"
    say(f"güncelleme çekiliyor: {info.local[:7]} → {info.tag or info.remote[:7]}")
    ref = ["tag", info.tag] if info.tag else [info.branch]
    code, out = _git(["fetch", "--prune", "origin", *ref], repo, timeout=300)
    if code != 0:
        return False, f"fetch başarısız: {out.strip()[-300:]}"
    # Etikette açıklamalı etiket nesnesi değil commit'in kendisi birleştirilir (yoksa ff yerine birleştirme ister).
    code, out = _git(["merge", "--ff-only", info.remote if info.tag else "FETCH_HEAD"], repo, timeout=120)
    if code != 0:
        return False, ("fast-forward yapılamadı (yerel commit'lerin olabilir): " + out.strip()[-300:])
    new = local_commit(repo)
    if new == info.local:
        # Nothing was pulled: restarting after "güncellendi" (updated) would loop at startup.
        write_state(local=new, remote=info.remote)
        return False, "zaten en son sürümdesin"
    if _pip_needed(repo, info.local, new):
        install_deps(repo, say)
    problem = smoke_test(repo)
    if problem:
        say(f"yeni sürüm açılamıyor, geri alınıyor: {problem}")
        rolled = rollback(repo, info.local, new, f"duman testi: {problem}")
        return False, ("yeni sürüm açılamadı, önceki sürüme dönüldü" if rolled else
                       "yeni sürüm açılamadı ve geri alınamadı") + f": {problem}"
    changes = local_log(repo, info.local, new) or info.commits
    write_state(last_update=time.time(), local=new, remote=new, update_from=info.local, update_to=new,
                changes=changes, announced_app=False, announced_core=False, pending_health=new, bad_boots=0)
    n = len(changes)
    return True, f"güncellendi: {info.local[:7]} → {new[:7]}" + (f" ({n} commit)" if n else "")


# --------------------------------------------------------------------------------- rollback
def smoke_test(repo: Path, timeout: float = 180.0) -> str:
    """Can the main modules of the new code be imported in a clean process? "" if fine, otherwise a short error."""
    code = "import importlib\nfor m in %r:\n    importlib.import_module(m)\n" % (SMOKE_MODULES,)
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(repo), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                           env={**os.environ, "PYTHONPATH": str(repo), "ALICE_NO_UPDATE": "1"})
    except subprocess.TimeoutExpired:
        return "içe aktarma zaman aşımına uğradı"
    except OSError as e:
        return f"python çalıştırılamadı: {e}"
    if r.returncode == 0:
        return ""
    lines = [ln for ln in (r.stderr or r.stdout or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else f"çıkış kodu {r.returncode}")[:300]


def rollback(repo: Path, good: str, bad: str, reason: str) -> bool:
    """Reset the working tree to the good commit, and mark the bad one so it is not pulled again automatically.

    --keep: when `alice update --force` pulled into a dirty tree and the smoke test failed, --hard deleted the user's
    unsaved changes. --keep preserves them (the fast-forward had not touched conflicting files anyway)."""
    code, _ = _git(["reset", "--keep", good], repo, timeout=60)
    if code != 0 and not is_dirty(repo):
        code, _ = _git(["reset", "--hard", good], repo, timeout=60)
    st = read_state()
    bads = [b for b in (st.get("bad") or []) if b != bad][-20:] + [bad]
    write_state(bad=bads, local=good, pending_health="", bad_boots=0,
                rollback={"from": bad, "to": good, "reason": reason, "ts": time.time(), "ok": code == 0,
                          "announced_app": False, "announced_core": False})
    return code == 0


def boot_check(repo: Path | None = None) -> str:
    """Called at startup. If MAX_BAD_BOOTS boots in a row after an update end without counting as healthy, it goes
    back to the previous commit and returns "rolled" (the caller must restart); otherwise ""."""
    st = read_state()
    pending = st.get("pending_health") or ""
    repo = repo or repo_dir()
    if not pending or repo is None or local_commit(repo) != pending:
        return ""
    boots = int(st.get("bad_boots") or 0)
    if boots >= MAX_BAD_BOOTS and st.get("update_from"):
        if is_dirty(repo):
            return ""  # manual changes were made: don't touch
        ok = rollback(repo, str(st["update_from"]), pending, f"{boots} açılış üst üste çöktü")
        return "rolled" if ok else ""
    write_state(bad_boots=boots + 1)
    return ""


def boot_healthy() -> None:
    """The process stayed up for HEALTHY_AFTER seconds: the new version is healthy."""
    if read_state().get("pending_health"):
        write_state(pending_health="", bad_boots=0)


def watch_health(delay: float = HEALTHY_AFTER) -> None:
    """Wait in the background, then count the process as healthy if it is still alive (app and core call this)."""
    import threading

    if not read_state().get("pending_health"):
        return
    t = threading.Timer(delay, boot_healthy)
    t.daemon = True
    t.start()


def consume_rollback(who: str) -> dict[str, Any] | None:
    """If a rollback happened, return it once for this consumer (app | core)."""
    rb = read_state().get("rollback")
    if not isinstance(rb, dict) or rb.get(f"announced_{who}") or time.time() - float(rb.get("ts") or 0) > 7 * 86400:
        return None
    write_state(rollback={**rb, f"announced_{who}": True})
    return rb


def restart(argv: list[str] | None = None, delay: float = 0.3, env: dict[str, str] | None = None) -> None:
    """Start a new process with the same command and end this one. Does not return."""
    args = argv if argv is not None else [sys.executable, "-m", "alice", *sys.argv[1:]]
    time.sleep(delay)
    try:
        kw: dict[str, Any] = {"close_fds": True, "env": {**os.environ, **(env or {})}}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kw["start_new_session"] = True
        subprocess.Popen(args, cwd=str(Path.cwd()), **kw)
    except (OSError, subprocess.SubprocessError):
        pass
    _before_exit()
    os._exit(0)


def _before_exit() -> None:
    """os._exit does not run atexit: an orphaned Playwright driver must not dump an EPIPE trace into core.log."""
    try:
        from alice.tools.browser import kill_playwright_drivers

        kill_playwright_drivers()
    except Exception:  # noqa: BLE001 - nothing should block the exit
        pass


def prepare(info: UpdateInfo, cfg: Any = None, ci_timeout: float = 5.0) -> UpdateInfo:
    """Add what automatic apply needs on top of the basic check (a single ls-remote): dirty tree, CI."""
    if not info.available:
        return info
    info.dirty = is_dirty(info.repo) if info.repo else False
    if settings(cfg).get("require_green_ci", True) and not info.dirty:
        info.ci = ci_state(info.remote, timeout=ci_timeout)
    return info


def startup_update(cfg: Any = None, emit: Callable[[str], None] | None = None,
                   restart_argv: list[str] | None = None) -> UpdateInfo | None:
    """Called at startup. If mode="auto", it applies the update and restarts the process (does not return).

    Return value: the check result (so the UI can report it if it was not applied), or None if disabled.
    """
    s = settings(cfg)
    if s.get("mode") != "auto" or not s.get("startup_check", True):
        return None  # notify/off: startup is never held up, the background thread does the check
    if os.environ.get("ALICE_NO_UPDATE") or os.environ.get("ALICE_RESTARTED"):
        return None  # a restarted process must not check again and loop
    say = emit or (lambda _m: None)
    # Checked on every startup (a single ls-remote, under a second). Before, a check within the last 10 minutes
    # was skipped: an ALICE opened right after a merge did not see the new version.
    # To avoid holding up startup, only one ls-remote runs here; details like the commit list (keyless GitHub
    # API) are skipped, and CI is checked only when there is an update, with a short timeout.
    info = check(cfg, timeout=float(s.get("startup_timeout") or 12), details=False)
    if not info.available:
        return info
    prepare(info, cfg)
    if not info.can_auto:
        if info.blocked:
            say(f"yeni sürüm var ama {info.blocked}; `alice update --force` ile yine de geçebilirsin")
        return info
    ok, msg = apply_update(info, cfg, emit=say)
    say(msg)
    if not ok:
        return info
    say("en son sürümle yeniden başlatılıyor…")
    args = restart_argv if restart_argv is not None else [sys.executable, "-m", "alice", *sys.argv[1:]]
    # The new process must not check again at startup and loop: the flag is set only in the child's environment.
    restart(args, env={"ALICE_RESTARTED": "1"})
    return None  # pragma: no cover - restart does not return


CI_RECHECK = 180.0  # seconds: while the new version's CI is pending, check again at this interval (not hourly)


def due(cfg: Any = None) -> bool:
    """Is it time for the periodic check (while the app is open)?"""
    s = settings(cfg)
    if s.get("mode") == "off":
        return False
    every = max(5.0, float(s.get("check_every_minutes") or 60)) * 60
    return time.time() - float(read_state().get("last_check") or 0) >= every
