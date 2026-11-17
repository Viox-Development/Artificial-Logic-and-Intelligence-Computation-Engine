from __future__ import annotations

import json
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
    assert not sandbox.applies(c, {"command": "ls"})  # clean turn: runs normally
    guard.mark(c, "web_fetch")
    assert sandbox.applies(c, {"command": "ls"})  # external content was read: sandbox
    assert not sandbox.applies(c, {"command": "ls", "unsandboxed": True})
    assert sandbox.applies(ctx(workdir, sandbox="on"), {"command": "ls"})
    assert not sandbox.applies(ctx(workdir, sandbox="off"), {"command": "ls"})
    fake_backend(monkeypatch, problem="bwrap yok")
    assert not sandbox.applies(c, {"command": "ls"})  # auto: if it cannot be set up, falls back to the approval flow
    assert sandbox.applies(ctx(workdir, sandbox="on"), {"command": "ls"})  # on: will raise an error


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
    assert Permissions("auto").decide(rc, {"command": "pytest -q"}, c) == "allow"  # will run in the sandbox
    assert Permissions("auto").decide(rc, {"command": "rm -rf build"}, c) == "ask"  # dangerous: still asks
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
    assert not sandbox.usable(home_ctx, {"command": "ls"})  # in the home folder: no sandbox, approval is asked
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows düşük bütünlük seviyesi")
def test_sandbox_cannot_read_hidden_secrets(tmp_path):
    """The Low level could read every file: a tricked sandbox command could read ~/.alice/phone-token and then
    control the computer with full privileges through the phone server on 127.0.0.1."""
    import subprocess

    secret, plain = tmp_path / "phone-token", tmp_path / "acik.txt"
    secret.write_text("gizli-anahtar", encoding="utf-8")
    plain.write_text("acik", encoding="utf-8")
    assert sandbox.hide_from_sandbox(secret)
    assert secret.read_text(encoding="utf-8") == "gizli-anahtar"  # ALICE itself keeps reading it

    def low_read(p):
        return subprocess.run([sys.executable, "-m", "alice.sandbox_run", "--", sys.executable, "-c",
                               f"print(open(r'{p}').read())"], capture_output=True, text=True, timeout=60)

    assert "acik" in low_read(plain).stdout  # check: the sandbox really works
    r = low_read(secret)
    assert r.returncode != 0 and "gizli-anahtar" not in r.stdout


def test_phone_token_is_hidden_from_sandbox(monkeypatch):
    from alice.app import phone

    seen = []
    monkeypatch.setattr(sandbox, "hide_from_sandbox", lambda p: seen.append(p.name) or True)
    t = phone.token(new=True)
    assert seen == ["phone-token"] and phone.token() == t and seen == ["phone-token", "phone-token"]


def _secret_home():
    from alice.config import alice_home

    home = alice_home()
    (home / "cliproxy" / "auths").mkdir(parents=True)
    (home / ".env").write_text("OZEL_SERVIS=gizli-env-degeri\nGEMINI_API_KEY=gizli-gemini\n", encoding="utf-8")
    (home / "cliproxy" / "keys.txt").write_text("api_key=gizli-proxy\n", encoding="utf-8")
    (home / "cliproxy" / "auths" / "claude.json").write_text('{"t": "gizli-oturum"}', encoding="utf-8")
    return home


def test_secret_env_is_not_passed_to_sandbox(monkeypatch):
    """The API keys in .env are loaded into ALICE's environment; a command in the sandbox could read them from there."""
    _secret_home()
    env = {"PATH": "x", "OZEL_SERVIS": "gizli-env-degeri", "GEMINI_API_KEY": "g", "GH_TOKEN": "t",
           "CLIPROXY_API_KEY": "c", "AWS_SECRET_ACCESS_KEY": "a", "HF_TOKEN": "h", "PYTHONUTF8": "1"}
    assert sandbox.scrub_env(env) == {"PATH": "x", "PYTHONUTF8": "1"}
    c = ToolContext(cwd=".", config=make_config({"brain": ["a:m"]}, sandbox_env_allow=["HF_TOKEN"]))
    assert sandbox.scrub_env(env, c) == {"PATH": "x", "PYTHONUTF8": "1", "HF_TOKEN": "h"}


def test_bwrap_and_mac_hide_secret_files(workdir, monkeypatch):
    home = _secret_home()
    fake_backend(monkeypatch)
    monkeypatch.setattr(sandbox, "IS_WIN", False)
    s = " ".join(sandbox.wrap(["ls"], workdir, ctx(workdir))[0])
    assert f"--ro-bind /dev/null {home / '.env'}" in s and f"--tmpfs {home / 'cliproxy' / 'auths'}" in s
    assert f"--ro-bind /dev/null {home / 'cliproxy' / 'keys.txt'}" in s
    p = sandbox.mac_profile(workdir, True, sandbox.secret_paths())
    assert "(deny file-read*" in p and json.dumps(str(home / "cliproxy" / "keys.txt")) in p


@pytest.mark.skipif(sys.platform != "win32", reason="Windows düşük bütünlük seviyesi")
def test_windows_sandbox_cannot_read_api_keys(tmp_path, monkeypatch):
    """Live audit: ~/.alice/.env and ~/.alice/cliproxy/keys.txt were readable from the sandbox,
    and the network was open."""
    home = _secret_home()
    monkeypatch.setenv("OZEL_SERVIS", "gizli-env-degeri")
    from alice import config as alice_config

    monkeypatch.setattr(alice_config, "LOADED_ENV_KEYS", {"OZEL_SERVIS"})
    proj = tmp_path / "proje"
    proj.mkdir()
    c = ToolContext(cwd=proj, config=make_config({"brain": ["a:m"]}, sandbox="on"))
    files = [home / ".env", home / "cliproxy" / "keys.txt", home / "cliproxy" / "auths" / "claude.json"]
    cmd = "; ".join(f"Get-Content -Raw '{f}'" for f in files) + "; Write-Output \"env=$env:OZEL_SERVIS\""
    res = TOOLS["run_command"].run(c, cmd)
    assert "env=" in res.text, res.text  # check: the command really ran
    assert "gizli" not in res.text, res.text
    assert (home / ".env").read_text(encoding="utf-8").startswith("OZEL_SERVIS")  # ALICE keeps reading it
    assert "gizli-oturum" in files[2].read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows düşük bütünlük seviyesi")
def test_failed_read_is_not_blamed_on_sandbox(tmp_path):
    """Live log: once a sandbox note was added to the error of a Get-Item on a missing file, the model took it for
    "access to the downloads folder was blocked" and went the wrong way."""
    proj = tmp_path / "proje"
    proj.mkdir()
    c = ToolContext(cwd=proj, config=make_config({"brain": ["a:m"]}, sandbox="on"))
    res = TOOLS["run_command"].run(c, f"Get-Item '{tmp_path / 'yok.exe'}'")
    assert res.is_error and "[sandbox:" not in res.text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows düşük bütünlük seviyesi")
def test_sandboxed_command_output_reaches_the_model(tmp_path):
    """Live log: the output of commands in the sandbox was lost. When a tool ALICE had written failed its test, the
    reason was not visible, so the tool could not be fixed."""
    proj = tmp_path / "proje"
    proj.mkdir()
    c = ToolContext(cwd=proj, config=make_config({"brain": ["a:m"]}, sandbox="on"))
    res = TOOLS["run_command"].run(c, "Write-Output merhaba-kum-havuzu; Write-Error hata-satiri")
    assert "merhaba-kum-havuzu" in res.text and "hata-satiri" in res.text


def test_git_snapshot_sees_planted_hooks_and_config(tmp_path):
    import subprocess
    from pathlib import Path

    from alice import gitsafe

    subprocess.run(["git", "init", "-q", str(tmp_path / "r")], check=True, capture_output=True)
    sub = tmp_path / "r" / "alt"
    sub.mkdir()
    before = gitsafe.snapshot(sub)
    assert str((tmp_path / "r" / ".git" / "config").resolve()) in before
    assert gitsafe.changes(before, gitsafe.snapshot(sub)) == [] and gitsafe.warning([]) == ""
    (tmp_path / "r" / ".git" / "hooks" / "pre-commit").write_text("kotu", encoding="utf-8")
    with open(tmp_path / "r" / ".git" / "config", "a", encoding="utf-8") as f:
        f.write("[core]\n\tfsmonitor = kotu\n")
    changed = gitsafe.changes(before, gitsafe.snapshot(sub))
    assert [Path(c).name for c in changed] == ["config", "pre-commit"]
    assert "GÜVENLİK" in gitsafe.warning(changed)
    assert gitsafe.snapshot(tmp_path) == {}  # not a repo


@pytest.mark.skipif(sys.platform != "win32", reason="Windows düşük bütünlük seviyesi")
def test_windows_sandbox_hook_write_is_reported(tmp_path):
    """A command in the sandbox can write to .git/hooks (the folder lock was bypassed by renaming): at the very least,
    a warning must show in the result."""
    import subprocess

    proj = tmp_path / "proje"
    subprocess.run(["git", "init", "-q", str(proj)], check=True, capture_output=True)
    c = ToolContext(cwd=proj, config=make_config({"brain": ["a:m"]}, sandbox="on"))
    res = TOOLS["run_command"].run(c, "Write-Output temiz")
    assert "GÜVENLİK" not in res.text
    res = TOOLS["run_command"].run(c, "Set-Content -Path .git/hooks/pre-commit -Value 'echo kotu'")
    assert "GÜVENLİK UYARISI" in res.text and "pre-commit" in res.text, res.text


def test_personal_folders_are_never_sandbox_areas(workdir):
    from pathlib import Path

    for name in ("Downloads", "Desktop", "Documents"):
        assert "kişisel klasör" in sandbox.boxable(Path.home() / name)
        assert "kişisel klasör" in sandbox.boxable(Path.home() / name / "bir-proje")
    from alice.config import alice_home

    assert "kişisel klasör" in sandbox.boxable(alice_home() / "agents")
    assert sandbox.boxable(workdir) == ""


def test_label_low_restores_exes_and_repairs_old_labels(tmp_path, monkeypatch):
    calls = []

    class R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(sandbox, "IS_WIN", True)
    monkeypatch.setattr(sandbox, "_labels_path", lambda: tmp_path / "sandbox_dirs.json")
    monkeypatch.setattr(sandbox, "_EXE_FIXED", set())
    monkeypatch.setattr(sandbox.subprocess, "run", lambda argv, **k: calls.append(argv) or R())
    proj = tmp_path / "proje"
    sandbox.label_low(proj)
    assert calls[0][1:3] == [str(proj), "/setintegritylevel"] and calls[0][3] == "(OI)(CI)low"
    assert calls[1][1] == str(proj / "*.exe") and calls[1][3] == "M"  # .venv python'u düşük açılmasın
    sandbox.label_low(proj)
    assert len(calls) == 2  # aynı süreçte tekrar dokunulmaz

    calls.clear()
    monkeypatch.setattr(sandbox, "_EXE_FIXED", set())  # eski sürümün etiketlediği klasör, yeni süreç
    sandbox.label_low(proj)
    assert calls == [["icacls", str(proj / "*.exe"), "/setintegritylevel", "M", "/T", "/C", "/Q"]]
