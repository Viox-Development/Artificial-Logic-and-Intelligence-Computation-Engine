"""Güncelleyici: gerçek git depolarıyla (uzak taraf yerel bir klasör, ağ yok)."""

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
    """(yerel klon, uzak depo). Uzak tarafa commit atınca yerelde 'güncelleme var' görünür."""
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
    """GitHub API'sine hiç gidilmesin: commit listesi boş, CI 'unknown'."""
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
    # --force bunu geçer
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
    """notify: açılışta ağ turu bile yok (arka plan thread'i bakar), commit kesinlikle çekilmez."""
    clone, origin = repos
    point_at(monkeypatch, clone)
    sha = push(origin, "yeni")
    real_check = updater.check
    monkeypatch.setattr(updater, "restart", lambda *a, **k: pytest.fail("notify modunda yeniden başlatma yok"))
    monkeypatch.setattr(updater, "check", lambda *a, **k: pytest.fail("notify modunda açılış bekletilmemeli"))
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "notify"}
    assert updater.startup_update(cfg) is None
    assert git(clone, "rev-parse", "HEAD") != sha  # çekilmedi
    # Arka plandaki kontrol (check) yeni sürümü görür ama kendiliğinden uygulamaz.
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
    assert restarts[0][1] == {"ALICE_RESTARTED": "1"}  # bayrak sadece çocuğun ortamında


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
    """CI daha bitmemişse otomatik çekilmez; sonraki kontrolde yeşilse kendiliğinden geçer."""
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


def test_acilista_arka_arkaya_ag_turu_harcanmaz(repos, monkeypatch, offline):
    """Az önce 'güncelsin' dendiyse ikinci açılış ls-remote bile yapmaz (hızlı açılış)."""
    clone, _origin = repos
    point_at(monkeypatch, clone)
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["update"] = {"mode": "auto"}
    real_check = updater.check
    assert updater.startup_update(cfg) is not None  # ilk açılış: baktı, güncel
    monkeypatch.setattr(updater, "check", lambda *a, **k: pytest.fail("yakın zamanda bakılmıştı"))
    assert updater.startup_update(cfg) is None
    # Süre dolunca yine bakar.
    updater.write_state(last_check=0)
    monkeypatch.setattr(updater, "check", real_check)
    assert updater.startup_update(cfg) is not None


# ------------------------------------------------------------------------------------------- geri alma
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


def test_crashing_boots_roll_back(repos, monkeypatch, offline):
    clone, origin = repos
    point_at(monkeypatch, clone)
    old = git(clone, "rev-parse", "HEAD")
    new = push(origin, "çalışıyor gibi ama açılınca çöküyor")
    monkeypatch.setattr(updater, "SMOKE_MODULES", ())
    cfg = make_config({"brain": ["a:m"]})
    ok, _ = updater.apply_update(updater.check(cfg), cfg)
    assert ok and git(clone, "rev-parse", "HEAD") == new
    assert updater.boot_check() == "" and updater.boot_check() == ""  # iki açılış: sayılıyor
    assert updater.boot_check() == "rolled"  # ikisi de sağlam sayılmadan bitti: geri dön
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
        assert updater.boot_check() == ""  # sağlam sürüm bir daha sorgulanmaz
