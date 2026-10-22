from __future__ import annotations

import sys

import pytest

from alice import guard, sandbox
from alice.permissions import Permissions
from alice.tools import ToolContext, builtin_tools
from alice.tools.base import ToolError
from tests.conftest import make_config

TOOLS = {t.name: t for t in builtin_tools()}


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    monkeypatch.setattr(sandbox, "_probe", {})


def ctx(workdir, **agent):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, **agent))


def fake_backend(monkeypatch, name="bwrap", problem=""):
    monkeypatch.setattr(sandbox, "backend", lambda: (name, problem))


def test_modes(workdir, monkeypatch):
    fake_backend(monkeypatch)
    c = ctx(workdir, sandbox="auto")
    assert not sandbox.applies(c, {"command": "ls"})  # temiz tur: normal çalışır
    guard.mark(c, "web_fetch")
    assert sandbox.applies(c, {"command": "ls"})  # dış içerik okundu: sandbox
    assert not sandbox.applies(c, {"command": "ls", "unsandboxed": True})
    assert sandbox.applies(ctx(workdir, sandbox="on"), {"command": "ls"})
    assert not sandbox.applies(ctx(workdir, sandbox="off"), {"command": "ls"})
    fake_backend(monkeypatch, problem="bwrap yok")
    assert not sandbox.applies(c, {"command": "ls"})  # auto: kurulamıyorsa onay akışına döner
    assert sandbox.applies(ctx(workdir, sandbox="on"), {"command": "ls"})  # on: hata verecek


def test_bwrap_argv(workdir, monkeypatch):
    fake_backend(monkeypatch)
    monkeypatch.setattr(sandbox, "IS_WIN", False)
    argv, env = sandbox.wrap(["bash", "-lc", "ls"], workdir, ctx(workdir, sandbox_network=False))
    s = " ".join(argv)
    assert "--ro-bind / /" in s and f"--bind {workdir.resolve()} {workdir.resolve()}" in s and "--unshare-net" in s
    assert argv[-3:] == ["bash", "-lc", "ls"] and env == {}


def test_mac_profile():
    from pathlib import PurePosixPath

    p = sandbox.mac_profile(PurePosixPath("/Users/hayat/proje"), net=False)
    assert "(deny file-write*)" in p and '(subpath "/Users/hayat/proje")' in p and "(deny network*)" in p
    assert "(deny network*)" not in sandbox.mac_profile(PurePosixPath("/x"), net=True)


def test_permissions_use_sandbox_instead_of_asking(workdir, monkeypatch):
    rc = TOOLS["run_command"]
    fake_backend(monkeypatch)
    c = ctx(workdir, sandbox="auto")
    guard.mark(c, "web_search")
    assert Permissions("auto").decide(rc, {"command": "pytest -q"}, c) == "allow"  # sandbox'ta çalışacak
    assert Permissions("auto").decide(rc, {"command": "rm -rf build"}, c) == "ask"  # tehlikeli: yine sorar
    assert rc.risk_for({"command": "ls", "unsandboxed": True}) == "danger"
    fake_backend(monkeypatch, problem="bwrap yok")
    assert Permissions("auto").decide(rc, {"command": "pytest -q"}, c) == "ask"


def test_home_and_root_are_never_sandbox_areas(workdir, monkeypatch):
    from pathlib import Path

    assert sandbox.boxable(workdir) == ""
    assert "olamaz" in sandbox.boxable(Path.home())
    assert "olamaz" in sandbox.boxable(Path(Path.home().anchor))
    fake_backend(monkeypatch)
    home_ctx = ToolContext(cwd=Path.home(), config=make_config({"brain": ["a:m"]}, sandbox="auto"))
    guard.mark(home_ctx, "web_fetch")
    assert not sandbox.usable(home_ctx, {"command": "ls"})  # ev klasöründe: sandbox yok, onay sorulur
    assert Permissions("auto").decide(TOOLS["run_command"], {"command": "ls"}, home_ctx) == "ask"
    assert sandbox.usable(home_ctx, {"command": "ls", "cwd": str(workdir)})


def test_on_mode_without_backend_refuses(workdir, monkeypatch):
    fake_backend(monkeypatch, problem="bubblewrap (bwrap) kurulu değil")
    with pytest.raises(ToolError, match="sandbox kurulamadı"):
        TOOLS["run_command"].run(ctx(workdir, sandbox="on"), "echo hi")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows düşük bütünlük seviyesi")
def test_windows_low_integrity_blocks_writes_outside_project(tmp_path):
    proj = tmp_path / "proje"
    proj.mkdir()
    outside = tmp_path / "disari.txt"
    c = ToolContext(cwd=proj, config=make_config({"brain": ["a:m"]}, sandbox="on"))
    res = TOOLS["run_command"].run(c, f"Set-Content -Path icerde.txt -Value ok; "
                                      f"Set-Content -Path '{outside}' -Value kotu")
    assert (proj / "icerde.txt").exists(), res.text
    assert not outside.exists(), res.text
    assert "[sandbox:" in res.text
