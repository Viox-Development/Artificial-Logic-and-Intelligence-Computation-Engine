"""Updating the core and the app together: lock, stale process, announcement, restart when idle."""

from __future__ import annotations

import os

import pytest

from alice import updater
from tests.conftest import make_config
from tests.test_updater import git, main_channel, push, repos  # noqa: F401 - fixture (main_channel: autouse)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(updater, "_api", lambda *a, **k: None)


@pytest.fixture
def at_clone(repos, monkeypatch):  # noqa: F811
    clone, origin = repos
    monkeypatch.setattr(updater, "repo_dir", lambda: clone)
    monkeypatch.setattr(updater, "_RUNNING", None)
    return clone, origin


def cfg(**update):
    c = make_config({"brain": ["a:m"]})
    c.data["update"] = {"mode": "auto", **update}
    return c


# ------------------------------------------------------------------------------ lock
def test_ayni_anda_tek_surec_gunceller():
    with updater.update_lock():
        with pytest.raises(updater.UpdateBusy):
            with updater.update_lock():
                pass
    with updater.update_lock():  # can be acquired again once released
        pass


def test_cokmus_surecten_kalan_kilit_temizlenir():
    p = updater._lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("12345", encoding="utf-8")
    old = p.stat().st_mtime - updater.LOCK_STALE - 10
    os.utime(p, (old, old))
    with updater.update_lock():
        pass


def test_uzun_suren_guncellemenin_kilidi_bayat_sayilmaz_olu_surecinki_sayilir(monkeypatch):
    import psutil

    p = updater._lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("424242", encoding="utf-8")
    old = p.stat().st_mtime - 1000  # fetch + pip + smoke test may exceed 900 s
    os.utime(p, (old, old))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    with pytest.raises(updater.UpdateBusy):
        with updater.update_lock():
            pass
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)  # the holding process crashed: acquire immediately
    with updater.update_lock():
        pass


def test_kilit_doluyken_apply_beklemeden_soyler(at_clone, offline):
    clone, origin = at_clone
    push(origin, "yeni")
    info = updater.check(cfg())
    with updater.update_lock():
        ok, msg = updater.apply_update(info, cfg())
    assert not ok and "başka bir ALICE süreci" in msg


def test_baskasi_zaten_cektiyse_tekrar_cekmez(at_clone, offline):
    clone, origin = at_clone
    sha = push(origin, "yeni")
    info = updater.check(cfg())
    git(clone, "pull", "-q", "--ff-only")       # another process (or the user) pulled
    ok, msg = updater.apply_update(info, cfg())
    assert ok and "zaten güncellendi" in msg and git(clone, "rev-parse", "HEAD") == sha


# ------------------------------------------------------------------------------ stale process
def test_diskteki_kod_yeniyse_surec_bayat(at_clone, offline):
    clone, origin = at_clone
    updater.mark_running()
    assert not updater.check(cfg()).stale
    push(origin, "yeni")
    git(clone, "pull", "-q", "--ff-only")       # code changed while the process was running
    info = updater.check(cfg())
    assert info.stale and not info.available    # nothing to pull, a restart is enough
    assert info.as_dict()["stale"] is True


def test_mark_running_cagrilmadiysa_bayat_sayilmaz(at_clone, offline):
    clone, origin = at_clone
    push(origin, "yeni")
    git(clone, "pull", "-q", "--ff-only")
    assert not updater.check(cfg()).stale


# ------------------------------------------------------------------------------ announcement
def test_guncelleme_bir_kez_duyurulur_her_yuzeyde(at_clone, offline):
    clone, origin = at_clone
    push(origin, "Spotify akışları eklendi")
    push(origin, "Akış koşulları", path="alice/b.py")
    ok, _ = updater.apply_update(updater.check(cfg()), cfg())
    assert ok
    a = updater.consume_announcement("app")
    assert a["changes"] == ["Akış koşulları", "Spotify akışları eklendi"]   # from local git, without the API
    assert updater.consume_announcement("app") is None                      # not announced a second time
    assert updater.consume_announcement("core")["changes"]                  # the core gets its own share


def test_eski_guncelleme_duyurulmaz():
    import time

    updater.write_state(last_update=time.time() - 8 * 86400, changes=["eski"])
    assert updater.consume_announcement("app") is None


def test_local_log(at_clone):
    clone, origin = at_clone
    a = git(clone, "rev-parse", "HEAD")
    push(origin, "bir")
    git(clone, "pull", "-q", "--ff-only")
    assert updater.local_log(clone, a, git(clone, "rev-parse", "HEAD")) == ["bir"]


# ------------------------------------------------------------------------------ app marker
def test_uygulama_isareti(monkeypatch):
    assert not updater.app_running()            # no file
    updater.mark_app()
    # This is pytest: no "alice app" on the command line; act as if the pid now belongs to another program
    assert not updater.app_running()

    class P:
        def __init__(self, pid):
            pass

        def cmdline(self):
            return ["pythonw.exe", "-m", "alice", "app"]

    import psutil

    monkeypatch.setattr(psutil, "Process", P)
    assert updater.app_running()
    updater.clear_app()
    assert not updater.app_running()


def test_olu_pid_uygulama_sayilmaz():
    updater._app_pid_path().parent.mkdir(parents=True, exist_ok=True)
    updater._app_pid_path().write_text("999999999", encoding="utf-8")
    assert not updater.app_running()


# ------------------------------------------------------------------------------ core
@pytest.fixture
def core(tmp_path):
    from alice.core.service import Core
    from alice.core.store import Store

    restarts = []
    c = Core(cfg(), store=Store(tmp_path / "s.db"), restart=lambda: restarts.append(1))
    c.restarts = restarts
    yield c
    c.store.close()


def test_cekirdek_uygulama_kapaliyken_ceker(core, at_clone, offline, monkeypatch):
    clone, origin = at_clone
    sha = push(origin, "yeni")
    monkeypatch.setattr(updater, "app_running", lambda: False)
    core.update_job()
    assert git(clone, "rev-parse", "HEAD") == sha and core.restart_pending


def test_cekirdek_uygulama_aciksa_cekmez(core, at_clone, offline, monkeypatch):
    clone, origin = at_clone
    sha = push(origin, "yeni")
    monkeypatch.setattr(updater, "app_running", lambda: True)
    core.update_job()
    assert git(clone, "rev-parse", "HEAD") != sha and not core.restart_pending


def test_cekirdek_bayatsa_cekmeden_yeniden_baslamayi_isaretler(core, at_clone, offline, monkeypatch):
    clone, origin = at_clone
    updater.mark_running()
    push(origin, "yeni")
    git(clone, "pull", "-q", "--ff-only")   # the app pulled it
    monkeypatch.setattr(updater, "apply_update", lambda *a, **k: pytest.fail("çekecek bir şey yok"))
    core.update_job()
    assert core.restart_pending


def test_cekirdek_kirli_agacta_cekmez(core, at_clone, offline, monkeypatch):
    clone, origin = at_clone
    push(origin, "yeni")
    (clone / "alice" / "__init__.py").write_text("# elle\n", encoding="utf-8")
    monkeypatch.setattr(updater, "app_running", lambda: False)
    core.update_job()
    assert not core.restart_pending


def test_cekirdek_notify_modunda_cekmez(tmp_path, at_clone, offline, monkeypatch):
    from alice.core.service import Core
    from alice.core.store import Store

    clone, origin = at_clone
    sha = push(origin, "yeni")
    monkeypatch.setattr(updater, "app_running", lambda: False)
    c = Core(cfg(mode="notify"), store=Store(tmp_path / "n.db"), restart=lambda: None)
    c.update_job()
    assert git(clone, "rev-parse", "HEAD") != sha
    c.store.close()


def test_yeniden_baslama_calisan_gorev_bitince(core):
    core.restart_pending = True
    core.running_tasks.add("yedekleme")
    core._maybe_update(0)
    assert core.restarts == []                 # don't leave the task half-finished
    core.running_tasks.clear()
    core._maybe_update(0)
    assert core.restarts == [1]


def test_kontrol_araligi_ve_tek_thread(core, monkeypatch):
    import time

    started = []
    monkeypatch.setattr(core, "update_job", lambda: started.append(1))
    now = time.time()
    core._maybe_update(now)                     # doesn't check right at startup (the app already checks)
    assert started == []
    core._maybe_update(now + 3601)
    core._update_thread.join(2)
    assert started == [1]
    core._maybe_update(now + 3700)              # time has not elapsed
    core._update_thread.join(2)
    assert started == [1]


def test_mode_off_cekirdek_hic_bakmaz(tmp_path, monkeypatch):
    from alice.core.service import Core
    from alice.core.store import Store

    c = Core(cfg(mode="off"), store=Store(tmp_path / "o.db"), restart=lambda: pytest.fail("yeniden başlamamalı"))
    c.restart_pending = True
    monkeypatch.setattr(c, "update_job", lambda: pytest.fail("bakmamalı"))
    c._maybe_update(10**12)
    c.store.close()


def test_cekirdek_yeniden_baslarken_kilidi_birakir(core, monkeypatch):
    from alice.core import service

    service.acquire_lock()
    seen = {}
    monkeypatch.setattr(updater, "restart", lambda argv, env=None, delay=0.3: seen.update(
        lock_free=not (service.alice_home() / "core.pid").exists(), env=env, argv=argv))
    core._restart_process()
    assert seen["lock_free"] and seen["env"] == {"ALICE_RESTARTED": "1"}
    assert seen["argv"][1:3] == ["-m", "alice"]


def test_duyuru_uygulama_aciksa_cekirdekte_susar(core, monkeypatch):
    import time

    from alice.core.cli import announce_update

    sent = []
    monkeypatch.setattr(core.notifier, "send", lambda n: sent.append(n))
    updater.write_state(last_update=time.time(), changes=["a", "b", "c", "d"], update_to="abcdef1",
                        announced_core=False)
    monkeypatch.setattr(updater, "app_running", lambda: True)
    announce_update(core, updater)
    assert sent == []
    monkeypatch.setattr(updater, "app_running", lambda: False)
    announce_update(core, updater)
    assert sent[0].title == "ALICE güncellendi" and "1 değişiklik daha" in sent[0].body
    announce_update(core, updater)
    assert len(sent) == 1


def test_cekirdek_ci_surerken_birkac_dakika_sonra_tekrar_bakar(core, at_clone, offline, monkeypatch):
    import time as _t

    clone, origin = at_clone
    sha = push(origin, "yeni")
    monkeypatch.setattr(updater, "app_running", lambda: False)
    monkeypatch.setattr(updater, "ci_state", lambda sha, timeout=5.0: "pending")
    core.update_job()
    assert git(clone, "rev-parse", "HEAD") != sha and not core.restart_pending
    every = 60 * 60
    wait = core._last_update_check + every - _t.time()  # time left until the next check
    assert 0 < wait <= updater.CI_RECHECK + 1  # a few minutes, not an hour
