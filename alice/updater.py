"""ALICE kendi kendini günceller.

Depo `git clone` ile kurulduğu için güncelleme ölçüsü basit: yerel HEAD ile GitHub'daki dalın
(varsayılan `main`) ucu aynı mı? Kontrol `git ls-remote` ile yapılır; bu tek bir ağ turu, fetch
yapmadığı için açılışta saniyenin altında biter.

Akış:

* `alice update --check`  : yeni commit var mı, neler değişmiş, CI yeşil mi
* `alice update`          : fast-forward çek, gerekiyorsa bağımlılıkları kur
* `alice app`             : config [update] mode="auto" ise açılışta sessizce günceller ve
                            güncelleme varsa kendini yeniden başlatıp en son sürümü açar
* uygulama açıkken        : arka planda periyodik kontrol, arayüzde "Güncelle ve yeniden başlat"
* arka plan çekirdeği     : uygulama kapalıyken kendisi çeker; boştayken (çalışan görev yokken)
                            sessizce yeniden başlar. Uygulama açıksa çekmez, işi arayüzü olana bırakır.
* bayat süreç             : diskteki kod çalışan süreçten yeniyse (başka bir süreç ya da elle
                            `git pull` çekti) süreç bunu fark eder: uygulama "yeniden başlat" der,
                            çekirdek boşta kalınca kendini yeniden başlatır. Eski süreç yeni dosyaları
                            yarı yarıya yükleyip karışık sürümle çalışmasın diye.

Kurallar:

* Çalışma ağacı kirliyse (kendi değişikliklerin varsa) otomatik güncelleme yapılmaz, sadece haber verilir.
* `require_green_ci = true` iken commit'in CI'ı kırmızıysa otomatik güncelleme yapılmaz; elle
  `alice update --force` bunu geçer. (CI durumu anahtarsız GitHub API'siyle okunur, ulaşılamazsa
  "bilinmiyor" sayılır ve güncelleme yapılır.)
* `pyproject.toml` değiştiyse güncellemeden sonra `pip install -e .` otomatik çalışır; hangi
  opsiyonel paketlerin kurulu olduğu tespit edilip aynı extra'larla kurulur.

Geri alma (bozuk sürüm ALICE'i açılmaz bırakmasın):

* Duman testi: çekilen sürümün ana modülleri ayrı bir Python sürecinde içe aktarılır. Hata verirse (sözdizimi,
  eksik paket) çalışma ağacı hemen önceki commit'e döner, o commit "bozuk" diye işaretlenir.
* Açılış sağlığı: güncellemeden sonraki açılışlar sayılır. Yeni sürüm HEALTHY_AFTER saniye ayakta kalınca sağlam
  sayılır. Üst üste MAX_BAD_BOOTS açılış sağlam sayılmadan biterse (açılıp çöktü) önceki commit'e dönülür.
* Bozuk işaretli commit otomatik olarak bir daha çekilmez (yeni bir commit gelince o denenir); `alice update
  --force` yine de çeker. Geri alma bir kez bildirilir.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

REPO = "hayatexe/Artificial-Logic-and-Intelligence-Computation-Engine"
API = "https://api.github.com"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DEFAULTS: dict[str, Any] = {
    "mode": "auto",              # auto | notify | off
    "branch": "main",
    "check_every_minutes": 60,
    "require_green_ci": True,
    "startup_timeout": 12,
    "startup_check": True,
}
# extra adı -> o extra kurulduysa içe aktarılabilen bir modül
SMOKE_MODULES = ("alice.cli", "alice.agent", "alice.memory", "alice.app.server", "alice.core.service",
                 "alice.voice.live", "alice.tools")
HEALTHY_AFTER = 90.0  # saniye: yeni sürüm bu kadar ayakta kalınca sağlam sayılır
MAX_BAD_BOOTS = 2     # bu kadar açılış sağlam sayılmadan biterse geri dön
EXTRA_PROBE = {"computer": "mss", "browser": "playwright", "app": "webview", "core": "pystray",
               "voice": "sounddevice", "home": "websockets"}


def settings(cfg: Any = None) -> dict[str, Any]:
    data = getattr(cfg, "data", None) or {}
    out = {**DEFAULTS, **(data.get("update") or {})}
    env = os.environ.get("ALICE_UPDATE_MODE")
    if env in ("auto", "notify", "off"):
        out["mode"] = env
    return out


# --------------------------------------------------------------------------------- git yardımcıları
def _git(args: list[str], cwd: Path, timeout: float = 30.0) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never"}
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return 127, "git bulunamadı"
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} zaman aşımına uğradı"
    except OSError as e:  # noqa: BLE001
        return 1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def repo_dir() -> Path | None:
    """ALICE'in çalıştığı git deposu (editable kurulumda klonladığın klasör). Değilse None."""
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return None
    code, out = _git(["rev-parse", "--show-toplevel"], root, timeout=10)
    if code != 0:
        return None
    top = Path(out.strip().splitlines()[-1]) if out.strip() else root
    return top if (top / "alice" / "__init__.py").exists() else None


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
    """(sha, hata). Ağ turu bir tane: ls-remote fetch yapmaz."""
    code, out = _git(["ls-remote", "--heads", "origin", branch], repo, timeout=timeout)
    if code != 0:
        return "", out.strip().splitlines()[-1] if out.strip() else "ls-remote başarısız"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith(f"/{branch}"):
            return parts[0], ""
    return "", f"uzak dal bulunamadı: {branch}"


# --------------------------------------------------------------------------------- GitHub (anahtarsız)
def _api(path: str, timeout: float = 8.0) -> Any:
    """Anahtarsız GitHub API çağrısı. Ulaşılamazsa None (güncelleme buna takılmaz)."""
    import httpx

    try:
        r = httpx.get(f"{API}{path}", timeout=timeout, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "alice-updater"})
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 - güncelleme kontrolü ağ hatasında sessizce "bilinmiyor"a düşer
        return None


def commit_log(local: str, remote: str, timeout: float = 8.0) -> list[str]:
    """İki commit arasındaki commit başlıkları (yeniden eskiye). Ulaşılamazsa boş liste."""
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
    """"green" | "red" | "pending" | "unknown". Hem klasik status'lara hem check-run'lara bakar."""
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


# --------------------------------------------------------------------------------- durum
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

    @property
    def available(self) -> bool:
        return bool(self.local and self.remote and self.local != self.remote)

    @property
    def stale(self) -> bool:
        """Diskteki kod bu süreçten yeni: yeniden başlamak yeter, çekecek bir şey yok."""
        return bool(self.running and self.local and self.running != self.local)

    @property
    def can_auto(self) -> bool:
        """Soru sormadan uygulanabilir mi?"""
        return self.available and not self.blocked

    @property
    def blocked(self) -> str:
        """Otomatik güncellemeyi durduran sebep (boşsa engel yok). Elle `--force` bunları geçer."""
        if not self.available:
            return ""
        if self.bad:
            return "bu sürüm daha önce açılamadı ve geri alındı; yeni bir düzeltme bekleniyor"
        if self.dirty:
            return "depoda kaydedilmemiş değişiklikler var"
        if self.ci == "red":
            return "yeni commit'in CI'ı kırmızı"
        if self.ci == "pending":
            return "CI hâlâ sürüyor"  # birkaç dakika sonraki kontrolde yeşilse kendiliğinden geçer
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {"available": self.available, "branch": self.branch, "local": self.local[:7],
                "remote": self.remote[:7], "commits": self.commits, "ci": self.ci, "dirty": self.dirty,
                "blocked": self.blocked, "error": self.error, "count": len(self.commits), "stale": self.stale,
                "running": self.running[:7]}


def _state_path() -> Path:
    return alice_home() / "update.json"


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(**kw: Any) -> None:
    data = {**read_state(), **kw}
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------------- süreçler arası
_RUNNING: str | None = None
LOCK_STALE = 900.0  # saniye: bu kadar eski kilit, çökmüş bir süreçten kalmıştır


def mark_running() -> str:
    """Bu sürecin hangi commit'le başladığını hatırla (uygulama ve çekirdek açılışta çağırır)."""
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


class update_lock:  # noqa: N801 - bağlam yöneticisi olarak okunur: with update_lock():
    """Aynı anda tek süreç güncellesin (uygulama ve çekirdek aynı anda git'e girerse index.lock çakışır)."""

    def __enter__(self) -> "update_lock":
        p = _lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - p.stat().st_mtime > LOCK_STALE:
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
    """Uygulama açık: çekirdek güncellemeyi ona bıraksın."""
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
    except Exception:  # noqa: BLE001 - emin değilsek açık say: çekirdek gereksiz yere çekmesin
        return True
    return "alice" in cmd and "app" in cmd  # pid başka bir programa geçmiş olabilir


def local_log(repo: Path, a: str, b: str, limit: int = 20) -> list[str]:
    """İki commit arası başlıklar, yerel git'ten (fetch sonrası; API gerekmez)."""
    code, out = _git(["log", "--format=%s", f"-n{limit}", f"{a}..{b}"], repo, timeout=20)
    return [line.strip()[:120] for line in out.splitlines() if line.strip()] if code == 0 else []


def consume_announcement(who: str) -> dict[str, Any] | None:
    """Son güncellemeyi bu tüketici (app | core) henüz duyurmadıysa döndür ve duyurulmuş say."""
    st = read_state()
    if not st.get("last_update") or st.get(f"announced_{who}"):
        return None
    if time.time() - float(st.get("last_update") or 0) > 7 * 86400:
        return None  # bir haftadan eski güncellemeyi artık duyurma
    write_state(**{f"announced_{who}": True})
    return {"from": st.get("update_from", ""), "to": st.get("update_to", ""), "changes": st.get("changes") or []}


def check(cfg: Any = None, timeout: float | None = None, details: bool = True) -> UpdateInfo:
    """Yeni commit var mı? Ağ turu: 1 (ls-remote) + ayrıntı istenirse 2 anahtarsız API çağrısı."""
    s = settings(cfg)
    repo = repo_dir()
    if repo is None:
        return UpdateInfo(error="ALICE git deposundan kurulmamış; güncelleme için depoyu klonla "
                                f"(git clone https://github.com/{REPO})")
    branch = str(s.get("branch") or "main")
    info = UpdateInfo(repo=repo, branch=branch, local=local_commit(repo), running=_RUNNING or "")
    info.remote, info.error = remote_head(repo, branch, float(timeout or s.get("startup_timeout") or 12))
    info.bad = bool(info.remote) and info.remote in (read_state().get("bad") or [])
    if info.available:
        info.dirty = is_dirty(repo)
        if details:
            info.commits = commit_log(info.local, info.remote)
            info.ci = ci_state(info.remote) if s.get("require_green_ci", True) else "unknown"
    write_state(last_check=time.time(), remote=info.remote, local=info.local)
    return info


# --------------------------------------------------------------------------------- uygulama
def installed_extras() -> list[str]:
    """Kurulu opsiyonel paketlere bakıp hangi extra'larla kurulduğunu tahmin et."""
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
        return True  # emin değilsek kur: eksik bağımlılıkla açılmaktan iyidir
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
    """Fast-forward çek ve gerekirse bağımlılıkları kur. (başarılı mı, mesaj)."""
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
        # Kilidi beklerken başka bir süreç zaten çekti: tekrar çekme, sonuç aynı.
        return True, f"zaten güncellendi: {local_commit(repo)[:7]}"
    say(f"güncelleme çekiliyor: {info.local[:7]} → {info.remote[:7]}")
    code, out = _git(["fetch", "--prune", "origin", info.branch], repo, timeout=300)
    if code != 0:
        return False, f"fetch başarısız: {out.strip()[-300:]}"
    code, out = _git(["merge", "--ff-only", "FETCH_HEAD"], repo, timeout=120)
    if code != 0:
        return False, ("fast-forward yapılamadı (yerel commit'lerin olabilir): " + out.strip()[-300:])
    new = local_commit(repo)
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


# --------------------------------------------------------------------------------- geri alma
def smoke_test(repo: Path, timeout: float = 180.0) -> str:
    """Yeni kodun ana modülleri temiz bir süreçte içe aktarılabiliyor mu? Sorun yoksa "", varsa kısa hata."""
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
    """Çalışma ağacını iyi commit'e döndür, kötüyü bir daha otomatik çekilmesin diye işaretle."""
    code, _ = _git(["reset", "--hard", good], repo, timeout=60)
    st = read_state()
    bads = [b for b in (st.get("bad") or []) if b != bad][-20:] + [bad]
    write_state(bad=bads, local=good, pending_health="", bad_boots=0,
                rollback={"from": bad, "to": good, "reason": reason, "ts": time.time(), "ok": code == 0,
                          "announced_app": False, "announced_core": False})
    return code == 0


def boot_check(repo: Path | None = None) -> str:
    """Açılışta çağrılır. Güncellemeden sonra üst üste MAX_BAD_BOOTS açılış sağlam sayılmadan bittiyse önceki
    commit'e döner ve "rolled" döndürür (çağıran yeniden başlatmalı); aksi halde ""."""
    st = read_state()
    pending = st.get("pending_health") or ""
    repo = repo or repo_dir()
    if not pending or repo is None or local_commit(repo) != pending:
        return ""
    boots = int(st.get("bad_boots") or 0)
    if boots >= MAX_BAD_BOOTS and st.get("update_from"):
        if is_dirty(repo):
            return ""  # elle değişiklik yapılmış: dokunma
        ok = rollback(repo, str(st["update_from"]), pending, f"{boots} açılış üst üste çöktü")
        return "rolled" if ok else ""
    write_state(bad_boots=boots + 1)
    return ""


def boot_healthy() -> None:
    """Süreç HEALTHY_AFTER saniye ayakta kaldı: yeni sürüm sağlam."""
    if read_state().get("pending_health"):
        write_state(pending_health="", bad_boots=0)


def watch_health(delay: float = HEALTHY_AFTER) -> None:
    """Arka planda bekle, süreç hâlâ yaşıyorsa sağlam say (uygulama ve çekirdek açılışta çağırır)."""
    import threading

    if not read_state().get("pending_health"):
        return
    t = threading.Timer(delay, boot_healthy)
    t.daemon = True
    t.start()


def consume_rollback(who: str) -> dict[str, Any] | None:
    """Geri alma olduysa bu tüketici (app | core) için bir kez döndür."""
    rb = read_state().get("rollback")
    if not isinstance(rb, dict) or rb.get(f"announced_{who}") or time.time() - float(rb.get("ts") or 0) > 7 * 86400:
        return None
    write_state(rollback={**rb, f"announced_{who}": True})
    return rb


def restart(argv: list[str] | None = None, delay: float = 0.3, env: dict[str, str] | None = None) -> None:
    """Aynı komutla yeni bir süreç başlat ve bu süreci bitir. Dönmez."""
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
    """os._exit atexit'i çalıştırmaz: sahipsiz kalacak Playwright sürücüsü core.log'a EPIPE yığını dökmesin."""
    try:
        from alice.tools.browser import kill_playwright_drivers

        kill_playwright_drivers()
    except Exception:  # noqa: BLE001 - çıkışı hiçbir şey engellemesin
        pass


def prepare(info: UpdateInfo, cfg: Any = None, ci_timeout: float = 5.0) -> UpdateInfo:
    """Ayrıntısız kontrolün (tek ls-remote) üstüne otomatik uygulama için gerekenleri ekle: kirli ağaç, CI."""
    if not info.available:
        return info
    info.dirty = is_dirty(info.repo) if info.repo else False
    if settings(cfg).get("require_green_ci", True) and not info.dirty:
        info.ci = ci_state(info.remote, timeout=ci_timeout)
    return info


def startup_update(cfg: Any = None, emit: Callable[[str], None] | None = None,
                   restart_argv: list[str] | None = None) -> UpdateInfo | None:
    """Açılışta çağrılır. mode="auto" ise güncellemeyi uygular ve süreci yeniden başlatır (dönmez).

    Geri dönen değer: kontrol sonucu (uygulanmadıysa arayüzde haber vermek için), ya da kapalıysa None.
    """
    s = settings(cfg)
    if s.get("mode") != "auto" or not s.get("startup_check", True):
        return None  # notify/off: açılış hiç bekletilmez, kontrolü arka plan thread'i yapar
    if os.environ.get("ALICE_NO_UPDATE") or os.environ.get("ALICE_RESTARTED"):
        return None  # yeniden başlatılan süreç tekrar kontrol edip döngüye girmesin
    say = emit or (lambda _m: None)
    # Her açılışta bakılır (tek ls-remote, saniyenin altında). Eskiden son 10 dakikada "güncel" denmişse hiç
    # bakılmıyordu: merge'den hemen sonra açılan ALICE yeni sürümü görmüyordu.
    # Açılışı bekletmemek için burada sadece tek ls-remote turu var; commit listesi gibi ayrıntılar
    # (anahtarsız GitHub API) atlanır, CI'a da yalnız güncelleme varken ve kısa zaman aşımıyla bakılır.
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
    # Yeni süreç açılışta tekrar kontrol edip döngüye girmesin: bayrak yalnız çocuğun ortamına konur.
    restart(args, env={"ALICE_RESTARTED": "1"})
    return None  # pragma: no cover - restart dönmez


CI_RECHECK = 180.0  # saniye: yeni sürüm CI'ı bitmeyi bekliyorsa bu aralıkla tekrar bakılır (saatte bir değil)


def due(cfg: Any = None) -> bool:
    """Periyodik kontrolün zamanı geldi mi (uygulama açıkken)."""
    s = settings(cfg)
    if s.get("mode") == "off":
        return False
    every = max(5.0, float(s.get("check_every_minutes") or 60)) * 60
    return time.time() - float(read_state().get("last_check") or 0) >= every
