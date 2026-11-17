"""Updater: runs against real git repos (the remote side is a local folder, no network)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from alice import updater
from tests.conftest import make_config


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """(local clone, remote repo). A commit pushed to the remote makes 'update available' show locally."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "alice").mkdir()
    (origin / "alice" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (origin / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "ilk")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.email", "t@t")
    git(clone, "config", "user.name", "t")
    return clone, origin


def push(origin: Path, msg: str, path: str = "alice/new.py") -> str:
    f = origin / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"# {msg}\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", msg)
    return git(origin, "rev-parse", "HEAD")


@pytest.fixture
def offline(monkeypatch):
    """Never hit the GitHub API: the commit list is empty and CI is 'unknown'."""
    monkeypatch.setattr(updater, "_api", lambda *a, **k: None)


def point_at(monkeypatch, clone: Path):
    monkeypatch.setattr(updater, "repo_dir", lambda: clone)


def test_guncel_depoda_guncelleme_yok(repos, monkeypatch, offline):
    clone, _ = repos
    point_at(monkeypatch, clone)
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert not info.available
    assert info.local == git(clone, "rev-parse", "HEAD")
    assert not info.error


def test_yeni_commit_gorulur_ve_cekilir(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    sha = push(origin, "yeni özellik")
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert info.available and info.remote == sha and info.can_auto
    ok, msg = updater.apply_update(info, make_config({"brain": ["a:m"]}))
    assert ok, msg
    assert git(clone, "rev-parse", "HEAD") == sha
    assert (clone / "alice" / "new.py").exists()
    assert not updater.check(make_config({"brain": ["a:m"]})).available


def test_one_gecmis_dalda_guncelleme_yok(repos, monkeypatch, offline):
    # A branch cut from main: the SHA differs, but main is already in it. Previously every launch said
    # "new version available" and got stuck in an empty-pull + restart loop.
    clone, _ = repos
    point_at(monkeypatch, clone)
    git(clone, "checkout", "-qb", "ozellik")
    (clone / "alice" / "x.py").write_text("# yerel\n", encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "yerel commit")
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert info.ahead and not info.available
    assert updater.apply_update(info) == (False, "zaten en son sürümdesin")


def test_baska_dalda_otomatik_cekilmez(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    git(clone, "checkout", "-qb", "ozellik")
    push(origin, "main'e yeni")
    git(clone, "fetch", "-q")
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert info.available and not info.can_auto and "'ozellik' dalında" in info.blocked


def test_kirli_agac_otomatik_guncellenmez(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    push(origin, "yeni")
    (clone / "alice" / "__init__.py").write_text("# elle değiştirdim\n", encoding="utf-8")
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert info.available and info.dirty and not info.can_auto
    assert info.blocked == "depoda kaydedilmemiş değişiklikler var"
    ok, msg = updater.apply_update(info, make_config({"brain": ["a:m"]}))
    assert not ok and "kaydedilmemiş" in msg


def test_kirmizi_ci_otomatik_guncellenmez(repos, monkeypatch):
    clone, origin = repos
    point_at(monkeypatch, clone)
    push(origin, "bozuk commit")
    monkeypatch.setattr(updater, "commit_log", lambda *a, **k: ["bozuk commit"])
    monkeypatch.setattr(updater, "ci_state", lambda *a, **k: "red")
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert info.available and info.ci == "red" and not info.can_auto
    ok, _ = updater.apply_update(info, make_config({"brain": ["a:m"]}))
    assert not ok
    # --force skips this
    ok, msg = updater.apply_update(info, make_config({"brain": ["a:m"]}), force=True)
    assert ok, msg


def test_pip_sadece_bagimlilik_degisince_calisir(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    calls = []
    monkeypatch.setattr(updater, "install_deps", lambda repo, emit=None: calls.append(repo) or True)

    push(origin, "sadece kod")
    updater.apply_update(updater.check(make_config({"brain": ["a:m"]})), make_config({"brain": ["a:m"]}))
    assert calls == []

    (origin / "pyproject.toml").write_text("[project]\nname='x'\nversion='2'\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "bağımlılık eklendi")
    updater.apply_update(updater.check(make_config({"brain": ["a:m"]})), make_config({"brain": ["a:m"]}))
    assert calls == [clone]


def test_git_deposu_degilse_anlasilir_hata(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "repo_dir", lambda: None)
    monkeypatch.setattr(updater, "install_root", lambda: None)
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert not info.available and "git deposundan kurulmamış" in info.error


def test_mode_off_acilista_hic_bakmaz(repos, monkeypatch):
    clone, origin = repos
    point_at(monkeypatch, clone)
    push(origin, "yeni")
    monkeypatch.setattr(updater, "check", lambda *a, **k: pytest.fail("mode=off iken kontrol edilmemeli"))
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "off"}
    assert updater.startup_update(cfg) is None


def test_notify_modunda_acilis_bekletilmez_ve_cekilmez(repos, monkeypatch, offline):
    """notify: no network round at startup either (a background thread checks); the commit is never pulled."""
    clone, origin = repos
    point_at(monkeypatch, clone)
    sha = push(origin, "yeni")
    real_check = updater.check
    monkeypatch.setattr(updater, "restart", lambda *a, **k: pytest.fail("notify modunda yeniden başlatma yok"))
    monkeypatch.setattr(updater, "check", lambda *a, **k: pytest.fail("notify modunda açılış bekletilmemeli"))
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "notify"}
    assert updater.startup_update(cfg) is None
    assert git(clone, "rev-parse", "HEAD") != sha  # not pulled
    # The background check (check) sees the new version but does not apply it on its own.
    found = real_check(cfg)
    assert found.available and found.remote == sha
    assert git(clone, "rev-parse", "HEAD") != sha


def test_auto_modunda_ceker_ve_yeniden_baslatir(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    sha = push(origin, "yeni")
    restarts = []
    monkeypatch.setattr(updater, "restart", lambda argv=None, delay=0, env=None: restarts.append((argv, env)))
    monkeypatch.delenv("ALICE_RESTARTED", raising=False)
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "auto"}
    updater.startup_update(cfg)
    assert git(clone, "rev-parse", "HEAD") == sha
    assert restarts and restarts[0][0][0] == sys.executable
    assert restarts[0][1] == {"ALICE_RESTARTED": "1"}  # the flag is only in the child's environment


def test_yeniden_baslatilan_surec_tekrar_kontrol_etmez(repos, monkeypatch):
    clone, origin = repos
    point_at(monkeypatch, clone)
    push(origin, "yeni")
    monkeypatch.setenv("ALICE_RESTARTED", "1")
    monkeypatch.setattr(updater, "check", lambda *a, **k: pytest.fail("döngü: tekrar kontrol edilmemeli"))
    assert updater.startup_update(make_config({"brain": ["a:m"]})) is None


def test_ci_durumu_check_runs_ve_status_birlikte(monkeypatch):
    responses = {
        "/status": {"state": "success", "statuses": [{"state": "success"}]},
        "/check-runs": {"check_runs": [{"status": "completed", "conclusion": "success"}]},
    }
    monkeypatch.setattr(updater, "_api", lambda path, timeout=8.0: responses["/status" if path.endswith("status") else "/check-runs"])
    assert updater.ci_state("abc") == "green"
    responses["/check-runs"] = {"check_runs": [{"status": "completed", "conclusion": "failure"}]}
    assert updater.ci_state("abc") == "red"
    responses["/check-runs"] = {"check_runs": [{"status": "in_progress"}]}
    assert updater.ci_state("abc") == "pending"
    monkeypatch.setattr(updater, "_api", lambda *a, **k: None)
    assert updater.ci_state("abc") == "unknown"
    assert updater.ci_state("") == "unknown"


def test_due_periyodu(monkeypatch, tmp_path):
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "auto", "check_every_minutes": 60}
    updater.write_state(last_check=0)
    assert updater.due(cfg)
    import time as _t

    updater.write_state(last_check=_t.time())
    assert not updater.due(cfg)
    cfg.data["update"]["mode"] = "off"
    assert not updater.due(cfg)


def test_env_degiskeni_modu_ezer(monkeypatch):
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "auto"}
    monkeypatch.setenv("ALICE_UPDATE_MODE", "off")
    assert updater.settings(cfg)["mode"] == "off"


def test_ci_surerken_beklenir(repos, monkeypatch):
    """If CI is still running, no automatic pull; once it is green at a later check, it goes through by itself."""
    clone, origin = repos
    point_at(monkeypatch, clone)
    sha = push(origin, "yeni")
    monkeypatch.setattr(updater, "commit_log", lambda *a, **k: [])
    state = {"ci": "pending"}
    monkeypatch.setattr(updater, "ci_state", lambda *a, **k: state["ci"])
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert info.blocked == "CI hâlâ sürüyor" and not info.can_auto
    state["ci"] = "green"
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert info.can_auto
    ok, _ = updater.apply_update(info, make_config({"brain": ["a:m"]}))
    assert ok and git(clone, "rev-parse", "HEAD") == sha


def test_merge_sonrasi_hemen_acilista_yeni_surum_gorulur(repos, monkeypatch, offline):
    """User: 'it says there's no update, but I opened it'. Previously, if 'up to date' had been reported in the
    last 10 minutes, startup never checked; an ALICE opened right after a merge did not see the new version."""
    clone, origin = repos
    point_at(monkeypatch, clone)
    monkeypatch.delenv("ALICE_RESTARTED", raising=False)
    restarts = []
    monkeypatch.setattr(updater, "restart", lambda argv=None, delay=0, env=None: restarts.append(argv))
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "auto"}
    assert not updater.startup_update(cfg).available  # opened: up to date
    sha = push(origin, "az önce merge edildi")
    updater.startup_update(cfg)  # reopened a minute later
    assert git(clone, "rev-parse", "HEAD") == sha and restarts


# ------------------------------------------------------------------------------------------- rollback
def test_smoke_test_catches_broken_code(tmp_path, monkeypatch):
    (tmp_path / "kirik_mod.py").write_text("def (:\n", encoding="utf-8")
    (tmp_path / "saglam_mod.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(updater, "SMOKE_MODULES", ("saglam_mod",))
    assert updater.smoke_test(tmp_path) == ""
    monkeypatch.setattr(updater, "SMOKE_MODULES", ("saglam_mod", "kirik_mod"))
    assert "SyntaxError" in updater.smoke_test(tmp_path)


def test_broken_update_rolls_back_and_is_skipped(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    old = git(clone, "rev-parse", "HEAD")
    bad = push(origin, "kırık sürüm", "kirik_mod.py")
    (origin / "kirik_mod.py").write_text("def (:\n", encoding="utf-8")
    git(origin, "commit", "-qam", "gerçekten kırık")
    bad = git(origin, "rev-parse", "HEAD")
    monkeypatch.setattr(updater, "SMOKE_MODULES", ("kirik_mod",))
    cfg = make_config({"brain": ["a:m"]})
    ok, msg = updater.apply_update(updater.check(cfg), cfg)
    assert not ok and "önceki sürüme dönüldü" in msg
    assert git(clone, "rev-parse", "HEAD") == old
    info = updater.check(cfg)
    assert info.bad and "geri alındı" in info.blocked and not info.can_auto
    rb = updater.consume_rollback("app")
    assert rb["from"] == bad and rb["to"] == old and updater.consume_rollback("app") is None


def test_write_state_never_exposes_partial_file(monkeypatch):
    """If another process reads the file mid-write, it must see the previous complete state (not empty or partial)."""
    import os

    updater.write_state(bad=["abc"], pending_health="def")
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append(updater.read_state())
        return real(src, dst)

    monkeypatch.setattr(updater.os, "replace", spy)
    updater.write_state(last_check=1.0)
    assert seen == [{"bad": ["abc"], "pending_health": "def"}]
    assert updater.read_state() == {"bad": ["abc"], "pending_health": "def", "last_check": 1.0}
    assert [p.name for p in updater._state_path().parent.iterdir() if p.suffix == ".tmp"] == []


def test_remote_head_ignores_branches_ending_with_same_name(repos, monkeypatch, offline):
    """The ls-remote "main" pattern also matches "refs/heads/feature/main"; since it sorts first alphabetically,
    that branch's commit must not be mistaken for the new version."""
    clone, origin = repos
    main = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "-qb", "feature/main")
    push(origin, "başka dal")
    git(origin, "checkout", "-q", "main")
    assert updater.remote_head(clone, "main") == (main, "")


def test_forced_update_rollback_keeps_uncommitted_changes(repos, monkeypatch, offline):
    """--force on a dirty tree: if the smoke test fails after pulling, rollback must not wipe uncommitted work."""
    clone, origin = repos
    point_at(monkeypatch, clone)
    old = git(clone, "rev-parse", "HEAD")
    (origin / "kirik_mod.py").write_text("def (:\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "kırık")
    (clone / "alice" / "__init__.py").write_text("# elle değiştirdim\n", encoding="utf-8")
    monkeypatch.setattr(updater, "SMOKE_MODULES", ("kirik_mod",))
    cfg = make_config({"brain": ["a:m"]})
    ok, msg = updater.apply_update(updater.check(cfg), cfg, force=True)
    assert not ok and "önceki sürüme dönüldü" in msg
    assert git(clone, "rev-parse", "HEAD") == old
    assert (clone / "alice" / "__init__.py").read_text(encoding="utf-8") == "# elle değiştirdim\n"


def test_crashing_boots_roll_back(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    old = git(clone, "rev-parse", "HEAD")
    new = push(origin, "çalışıyor gibi ama açılınca çöküyor")
    monkeypatch.setattr(updater, "SMOKE_MODULES", ())
    cfg = make_config({"brain": ["a:m"]})
    ok, _ = updater.apply_update(updater.check(cfg), cfg)
    assert ok and git(clone, "rev-parse", "HEAD") == new
    assert updater.boot_check() == "" and updater.boot_check() == ""  # two boots: counted
    assert updater.boot_check() == "rolled"  # both ended without being counted healthy: roll back
    assert git(clone, "rev-parse", "HEAD") == old
    assert new in updater.read_state()["bad"]


def test_healthy_boot_clears_pending(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    push(origin, "sağlam")
    monkeypatch.setattr(updater, "SMOKE_MODULES", ())
    cfg = make_config({"brain": ["a:m"]})
    updater.apply_update(updater.check(cfg), cfg)
    updater.boot_check()
    updater.boot_healthy()
    assert not updater.read_state().get("pending_health")
    for _ in range(4):
        assert updater.boot_check() == ""  # a healthy version is never checked again


def test_unauthorized_alice_token_falls_back_to_git_login(monkeypatch, tmp_path):
    """If ALICE's key is not authorised for this private repo (403), retry with git's own saved login;
    previously the update failed with "The requested URL returned error: 403"."""
    runs = []

    def fake_run(args, cwd, timeout, env):
        authed = any(k.startswith("GIT_CONFIG_KEY") for k in env)
        runs.append(authed)
        if authed:
            return 128, "remote: Write access to repository not granted.\nfatal: ... returned error: 403"
        return 0, "abc\trefs/heads/main"

    monkeypatch.setattr(updater, "_auth_env", lambda: {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.x",
                                                        "GIT_CONFIG_VALUE_0": "y"})
    monkeypatch.setattr(updater, "_git_run", fake_run)
    assert updater._git(["fetch", "origin"], tmp_path) == (0, "abc\trefs/heads/main") and runs == [True, False]
    runs.clear()
    assert updater._git(["status"], tmp_path)[0] == 0 and runs == [False]  # no key is passed to local commands


def _exe_install(clone: Path, dest: Path, sha: str) -> Path:
    """Like a folder installed by ALICE-Setup.exe: source files present, no .git, build commit recorded in a file."""
    import shutil

    shutil.copytree(clone, dest, ignore=shutil.ignore_patterns(".git"))
    (dest / updater.BUILD_FILE).write_text(sha, encoding="utf-8")
    return dest


def test_exe_kurulumunda_git_gecmisi_yoksa_yeniden_kurulur_ve_guncellenir(repos, monkeypatch, offline, tmp_path):
    """Live log (10 Nov): an ALICE installed from the exe said "not installed from a git repo" and never updated."""
    clone, origin = repos
    app = _exe_install(clone, tmp_path / "ALICE", git(clone, "rev-parse", "HEAD"))
    (app / ".venv").mkdir()  # the installer's own files are left untouched
    monkeypatch.setattr(updater, "REMOTE_URL", origin.as_uri())
    monkeypatch.setattr(updater, "install_root", lambda: app)
    monkeypatch.setattr(updater, "repo_dir", lambda: app if (app / ".git").exists() else None)
    cfg = make_config({"brain": ["a:m"]})
    info = updater.check(cfg)
    assert info.error == "" and not info.available and not updater.is_dirty(app)
    new = push(origin, "yeni özellik")
    info = updater.check(cfg)
    assert info.available and info.can_auto
    ok, msg = updater.apply_update(info, cfg)
    assert ok, msg
    assert (app / "alice" / "new.py").exists() and updater.local_commit(app) == new and (app / ".venv").is_dir()


def test_gecmis_indirilemezse_yarim_depo_kalmaz(repos, monkeypatch, tmp_path):
    clone, _origin = repos
    app = _exe_install(clone, tmp_path / "ALICE", "0" * 40)
    monkeypatch.setattr(updater, "REMOTE_URL", (tmp_path / "yok").as_uri())
    monkeypatch.setattr(updater, "install_root", lambda: app)
    monkeypatch.setattr(updater, "repo_dir", lambda: app if (app / ".git").exists() else None)
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert "indirilemedi" in info.error and not (app / ".git").exists()


def test_git_yoksa_dogru_sebep_soylenir(repos, monkeypatch, tmp_path):
    clone, _origin = repos
    monkeypatch.setattr(updater, "install_root", lambda: clone)
    monkeypatch.setattr(updater, "repo_dir", lambda: None)
    monkeypatch.setattr(updater, "ensure_git", lambda: None)
    assert "Git bulunamadı" in updater.check(make_config({"brain": ["a:m"]})).error


def test_git_kurulumu_acilisi_bekletmez(repos, monkeypatch):
    """While winget installed Git (up to 15 min), `alice app` froze at startup.
    The install now runs in the background and is not awaited."""
    import threading
    import time as _t

    clone, _origin = repos
    gate, started = threading.Event(), []

    def slow_run(cmd, **kw):
        started.append(cmd)
        gate.wait(10)

    monkeypatch.setattr(updater, "WINDOWS", True)
    monkeypatch.setattr(updater, "git_exe", lambda: None)
    monkeypatch.setattr(updater.shutil, "which", lambda name: "winget" if name == "winget" else None)
    monkeypatch.setattr(updater.subprocess, "run", slow_run)
    monkeypatch.setattr(updater, "install_root", lambda: clone)
    monkeypatch.setattr(updater, "repo_dir", lambda: None)
    t0 = _t.monotonic()
    info = updater.check(make_config({"brain": ["a:m"]}))
    assert _t.monotonic() - t0 < 3 and "arka planda kuruluyor" in info.error
    assert updater.ensure_git() is None and len(started) == 1  # a second install is not started
    gate.set()
    updater._git_install.join(5)
    assert not updater.git_installing()


def test_path_disindaki_git_bulunur(monkeypatch, tmp_path):
    """If winget just installed Git, an ALICE started right after the install does not have it on PATH yet."""
    exe = tmp_path / "Programs" / "Git" / "cmd" / "git.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(updater.shutil, "which", lambda name: None)
    monkeypatch.setattr(updater, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(env, raising=False)
    assert updater.git_exe() == str(exe)


def test_yeniden_kurulan_depo_ayarlardaki_dalda(repos, tmp_path, monkeypatch):
    """Review: HEAD was always attached to main; if another branch was selected in settings, auto-update stopped."""
    clone, origin = repos
    app = _exe_install(clone, tmp_path / "ALICE", git(clone, "rev-parse", "HEAD"))
    monkeypatch.setattr(updater, "REMOTE_URL", origin.as_uri())
    monkeypatch.setattr(updater, "repo_dir", lambda: app if (app / ".git").exists() else None)
    repo, err = updater.bootstrap_repo(app, "master")
    assert repo == app and err == "" and updater.current_branch(app) == "master" and not updater.is_dirty(app)


def test_baska_surec_guncellerken_depo_kurulmaz(repos, monkeypatch, tmp_path):
    """Review: if the app and the core installed at the same time, one could delete the other's half-written .git."""
    clone, origin = repos
    app = _exe_install(clone, tmp_path / "ALICE", git(clone, "rev-parse", "HEAD"))
    monkeypatch.setattr(updater, "REMOTE_URL", origin.as_uri())
    monkeypatch.setattr(updater, "install_root", lambda: app)
    monkeypatch.setattr(updater, "repo_dir", lambda: app if (app / ".git").exists() else None)
    with updater.update_lock():
        info = updater.check(make_config({"brain": ["a:m"]}))
    assert "başka bir ALICE süreci" in info.error and not (app / ".git").exists()
    assert updater.check(make_config({"brain": ["a:m"]})).error == ""  # it installs once the lock is released
