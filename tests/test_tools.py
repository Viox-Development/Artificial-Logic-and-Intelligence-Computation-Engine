from __future__ import annotations

import sys
from pathlib import Path

import pytest

from alice.config import ProviderConfig, init_home, load_config
from alice.permissions import Permissions, is_system_path
from alice.tools import ToolContext
from alice.tools.base import ToolError
from alice.tools.files import EditFile, Glob, Grep, ListDir, ReadFile, WriteFile
from alice.tools.shell import ProcessOutput, RunCommand, cleanup
from alice.tools.web import html_to_text
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_read_with_offset_and_cp1254(ctx, workdir):
    (workdir / "t.txt").write_bytes("şğü\n".encode("cp1254") + b"line2\nline3\n")
    out = ReadFile().run(ctx, path="t.txt", offset=2, limit=1).text
    assert "line2" in out and "line3" not in out and "devam et" in out
    assert "şğü" in ReadFile().run(ctx, path="t.txt").text


def test_edit_requires_unique(ctx, workdir):
    (workdir / "d.txt").write_text("x\nx\n", encoding="utf-8")
    ReadFile().run(ctx, path="d.txt")
    with pytest.raises(ToolError):
        EditFile().run(ctx, path="d.txt", old_string="x", new_string="y")
    EditFile().run(ctx, path="d.txt", old_string="x", new_string="y", replace_all=True)
    assert (workdir / "d.txt").read_text(encoding="utf-8") == "y\ny\n"


def test_edit_handles_crlf(ctx, workdir):
    (workdir / "w.cs").write_bytes(b"class A {\r\n  int x;\r\n}\r\n")
    ReadFile().run(ctx, path="w.cs")
    EditFile().run(ctx, path="w.cs", old_string="{\n  int x;", new_string="{\n  int y;")
    assert (workdir / "w.cs").read_bytes() == b"class A {\r\n  int y;\r\n}\r\n"


def test_write_list_glob_grep(ctx, workdir):
    WriteFile().run(ctx, path="src/a.py", content="def foo():\n    return 1\n")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "x.py").write_text("def foo(): pass", encoding="utf-8")
    assert "src/" in ListDir().run(ctx).text
    g = Glob().run(ctx, pattern="**/*.py").text
    assert "a.py" in g and "node_modules" not in g
    assert "def foo" in Grep().run(ctx, pattern=r"def \w+", glob="*.py").text


def test_run_command_and_errors(ctx):
    r = RunCommand().run(ctx, command="echo merhaba")
    assert "merhaba" in r.text and not r.is_error
    r = RunCommand().run(ctx, command="exit 3")
    assert r.is_error and "çıkış kodu 3" in r.text


@pytest.mark.skipif(sys.platform == "win32", reason="posix sleep")
def test_background_process(ctx):
    r = RunCommand().run(ctx, command="echo started; sleep 30", background=True)
    pid = int(r.text.split("pid=")[1].split(",")[0])
    assert "started" in ProcessOutput().run(ctx, pid=pid).text
    cleanup(ctx)


def test_dangerous_classification():
    rc = RunCommand()
    assert rc.risk_for({"command": "rm -rf /"}) == "danger"
    assert rc.risk_for({"command": "Remove-Item C:\\x -Recurse"}) == "danger"
    assert rc.risk_for({"command": "git push --force origin main"}) == "danger"
    assert rc.risk_for({"command": "pip install requests"}) == "exec"


def test_permissions_modes(ctx):
    p = Permissions("auto")
    assert p.decide(ReadFile(), {"path": "x"}, ctx) == "allow"
    assert p.decide(WriteFile(), {"path": "x"}, ctx) == "allow"
    assert p.decide(RunCommand(), {"command": "rm -rf x"}, ctx) == "ask"
    sys_path = "C:\\Windows\\x.dll" if sys.platform == "win32" else "/etc/hosts"
    assert p.decide(WriteFile(), {"path": sys_path}, ctx) == "ask"
    assert Permissions("ask").decide(WriteFile(), {"path": "x"}, ctx) == "ask"
    assert Permissions("yolo").decide(RunCommand(), {"command": "rm -rf x"}, ctx) == "allow"
    p2 = Permissions("ask")
    p2.always_allow.add("write_file")
    assert p2.decide(WriteFile(), {"path": "x"}, ctx) == "allow"


def test_is_system_path():
    if sys.platform == "win32":
        assert is_system_path(Path(r"C:\Windows\System32"))
    else:
        assert is_system_path(Path("/usr/bin/python"))
        assert not is_system_path(Path("/home/user/x"))


def test_html_to_text():
    t = html_to_text("<html><head><title>x</title><script>bad()</script></head><body><h1>Başlık</h1><p>para</p>"
                     "<ul><li>bir</li></ul></body></html>")
    assert "# Başlık" in t and "* bir" in t and "bad()" not in t


def test_config_init_and_load(isolated_home, monkeypatch, tmp_path):
    written = init_home()
    assert (isolated_home / "config.toml") in written and (isolated_home / ".env").exists()
    (isolated_home / ".env").write_text("GROQ_API_KEY=abc\n", encoding="utf-8")
    (isolated_home / "config.toml").write_text('[agent]\npermission_mode = "yolo"\n[roles]\nbrain = ["groq:x"]\n', encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.agent["permission_mode"] == "yolo" and cfg.agent["max_steps"] == 80
    assert cfg.roles["brain"] == ["groq:x"] and cfg.roles["fast"]  # other roles come from the defaults
    assert cfg.providers()["groq"].api_key == "abc"
    monkeypatch.delenv("GROQ_API_KEY")


def test_custom_provider_without_base_url_is_skipped(capsys):
    cfg = make_config({"brain": ["a:m"]}, providers={
        "a": {"base_url": "http://a"}, "benim": {"api_key_env": "X", "custom": True}, "bozuk": "metin"})
    assert set(cfg.providers()) == {"a"}
    assert set(cfg.provider_errors()) == {"benim", "bozuk"}
    assert "providers.benim" in capsys.readouterr().err
    cfg.providers()
    assert "providers.benim" not in capsys.readouterr().err  # warns once per process


def test_provider_vision_glob():
    pc = ProviderConfig("x", "http://x", vision_models=["gemini-*"])
    assert pc.supports_vision("gemini-flash-latest") and not pc.supports_vision("gpt-oss")


def test_write_roles_updates_config(isolated_home):
    from alice.cli import write_roles

    init_home()
    write_roles({"brain": ["groq:new-model", "gemini:x"], "fast": [], "vision": ["gemini:v"]})
    cfg = load_config()
    assert cfg.roles["brain"] == ["groq:new-model", "gemini:x"]
    assert cfg.roles["vision"] == ["gemini:v"]
    assert cfg.roles["fast"][0] == "auto"  # empty suggestion left untouched


def test_cli_status_line_and_prompt_session(workdir):
    from alice.cli import build, make_prompt_session, status_line

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"]})
    agent, router, ctx = build(cfg, workdir, "auto")
    line = status_line(agent, router, ctx)
    assert "a:m" in line and "izin: auto" in line and "context" in line
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    # No real console in CI (especially Windows); set up with fake input/output.
    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        assert make_prompt_session(agent, router, ctx).multiline


def test_cli_slash_commands(workdir, isolated_home, monkeypatch):
    from alice.cli import build, handle_slash

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"]})
    agent, router, ctx = build(cfg, workdir, "auto")
    assert handle_slash("/plan", agent, router, ctx, cfg) is None and agent.permissions.mode == "plan"
    assert handle_slash("/init", agent, router, ctx, cfg)[0] == "run"
    d = workdir / ".alice" / "commands"
    d.mkdir(parents=True)
    (d / "test.md").write_text("Testleri çalıştır: $ARGUMENTS", encoding="utf-8")
    assert handle_slash("/test hızlı", agent, router, ctx, cfg) == ("run", "Testleri çalıştır: hızlı")
    agent.messages.append({"role": "user", "content": "eski mesaj"})
    monkeypatch.setattr("alice.cli.console.input", lambda *a, **k: "1")
    assert handle_slash("/rewind", agent, router, ctx, cfg) == ("prefill", "eski mesaj")
    assert len(agent.messages) == 1


def test_find_installed_apps_scans_program_folders(tmp_path, monkeypatch):
    from alice.tools import desktop

    pf = tmp_path / "Program Files"
    app = pf / "Blender Foundation" / "Blender 4.2"
    app.mkdir(parents=True)
    for name in ("blender.exe", "blender-launcher.exe", "uninstall.exe"):
        (app / name).write_bytes(b"")
    (pf / "Other").mkdir()
    (pf / "Other" / "other.exe").write_bytes(b"")
    monkeypatch.setattr(desktop, "IS_WIN", True)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    monkeypatch.setattr(desktop, "_steam_libraries", lambda: [])
    monkeypatch.setattr(desktop, "_shortcut_roots", lambda: [])
    monkeypatch.setattr(desktop, "start_apps", lambda refresh=False: [])  # PowerShell only exists on Windows
    for k in ("ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ProgramFiles", str(pf))

    hits = desktop.find_installed_apps("Blender")
    paths = [h.split(": ", 1)[1] for h in hits]
    assert str(app / "blender.exe") in paths
    assert not any("uninstall" in p or "other.exe" in p for p in paths)
    assert desktop.find_installed_apps("photoshop") == []


def test_blender_tool_parses_error_and_scene(tmp_path, monkeypatch):
    import sys as _sys

    from alice.tools import blender as bl
    from alice.tools.base import ToolContext

    # A small script that imitates Blender's output instead of running the real Blender.
    fake = tmp_path / "fake_blender.py"
    fake.write_text(
        "print('Blender 5.2.1 LTS')\n"
        "print('kullanici ciktisi')\n"
        "print('@@ALICE@@HATA')\n"
        "print('AttributeError: World has no attribute gas_color')\n"
        "print('@@ALICE@@SAHNE')\n"
        "print('Cube [MESH] konum=(0, 0, 0) boyut=(2, 2, 2)')\n"
        "print('@@ALICE@@SON')\n"
        "raise SystemExit(1)\n", encoding="utf-8")
    if _sys.platform == "win32":
        exe = tmp_path / "fake_blender.cmd"
        exe.write_text(f'@"{_sys.executable}" "{fake}"\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_blender.sh"
        exe.write_text(f"#!/bin/sh\nexec {_sys.executable} {fake}\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(bl, "find_blender", lambda ctx=None: str(exe))
    monkeypatch.setattr(bl, "alice_home", lambda: tmp_path)

    class Cfg:
        data: dict = {}
        agent: dict = {}

    ctx = ToolContext(cwd=tmp_path, config=Cfg(), router=None)
    res = bl.Blender().run(ctx, action="run", code="import bpy", new=True)
    assert res.is_error
    assert "gas_color" in res.text and "BETİK HATASI" in res.text
    assert "Cube [MESH]" in res.text and "kullanici ciktisi" in res.text
    assert "Blender 5.2.1" not in res.text


def test_default_browser_helpers():
    from alice.tools.browser import cdp_alive, exe_from_command, is_chromium

    cmd = r'"C:\Users\k\AppData\Local\Programs\Opera GX\opera.exe" --single-argument %1'
    assert exe_from_command(cmd) == r"C:\Users\k\AppData\Local\Programs\Opera GX\opera.exe"
    assert exe_from_command(r"C:\Firefox\firefox.exe -osint -url %1") == r"C:\Firefox\firefox.exe"
    assert is_chromium(r"C:\Users\k\AppData\Local\Programs\Opera GX\opera.exe")
    assert not is_chromium(r"C:\Firefox\firefox.exe")
    assert not cdp_alive("http://127.0.0.1:1", timeout=0.2)


def test_windows_navigate_reuses_open_site_tab(monkeypatch):
    from alice.tools import computer, desktop

    did = []
    titles = iter(["Ayarlar - Opera", "Kedi - YouTube - Opera"])
    state = {"title": "Gmail - Opera"}

    def key(k):
        did.append(("key", k))
        if k == "ctrl+tab":
            state["title"] = next(titles, state["title"])

    monkeypatch.setattr(desktop, "browser_window_title", lambda: "Opera")
    monkeypatch.setattr(desktop, "focus_window", lambda t: "x")
    monkeypatch.setattr(desktop, "active_title", lambda: state["title"])
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    monkeypatch.setattr(computer, "press_keys", key)
    monkeypatch.setattr(computer, "type_text", lambda t: did.append(("type", t)))
    res = desktop.Windows().run(None, "navigate", target="youtube.com/results?search_query=comfort")
    assert did[-3:] == [("key", "ctrl+l"), ("type", "youtube.com/results?search_query=comfort"), ("key", "enter")]
    assert "YouTube" in res.text and ("key", "ctrl+t") not in did


def test_windows_navigate_new_tab_when_site_not_open(monkeypatch):
    from alice.tools import computer, desktop

    did = []
    monkeypatch.setattr(desktop, "browser_window_title", lambda: "Opera")
    monkeypatch.setattr(desktop, "focus_window", lambda t: "x")
    monkeypatch.setattr(desktop, "active_title", lambda: "Gmail - Opera")
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    monkeypatch.setattr(computer, "press_keys", lambda k: did.append(k))
    monkeypatch.setattr(computer, "type_text", lambda t: did.append(t))
    desktop.Windows().run(None, "navigate", target="github.com")
    assert did[-3:] == ["ctrl+t", "github.com", "enter"]
    did.clear()
    monkeypatch.setattr(desktop, "active_title", lambda: "Yeni sekme - Opera")
    desktop.Windows().run(None, "navigate", target="github.com")
    assert did[-3:] == ["ctrl+l", "github.com", "enter"]


def test_site_key():
    from alice.tools.desktop import site_key

    assert site_key("https://www.youtube.com/watch?v=x") == "youtube"
    assert site_key("mail.google.com") == "gmail"
    assert site_key("kedi videoları") is None


def test_youtube_tool_plays_first_result_at_time(monkeypatch):
    import httpx

    from alice.tools import desktop, extras

    html = ('..."videoRenderer":{"videoId":"abcdefghijk","thumbnail":{},"title":{"runs":[{"text":"Concorde \\u0026 Co"}]}'
            '..."videoRenderer":{"videoId":"bbbbbbbbbbb","title":{"runs":[{"text":"İkinci"}]}')
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    assert extras.youtube_search("concorde", client=client) == [("Concorde & Co", "abcdefghijk"), ("İkinci", "bbbbbbbbbbb")]
    went = []
    monkeypatch.setattr(extras, "youtube_search", lambda q: [("Concorde", "abcdefghijk")])
    monkeypatch.setattr(desktop, "navigate_tab", lambda url, *a, **k: went.append((url, k)) or "açık sekmede açıldı")
    res = extras.YouTube().run(None, "play", query="concorde", at=120)
    assert went[0][0] == "https://www.youtube.com/watch?v=abcdefghijk&t=120"
    assert "Concorde" in res.text
    monkeypatch.setattr(desktop, "current_tab_url", lambda: "https://www.youtube.com/watch?v=abcdefghijk&t=5")
    extras.YouTube().run(None, "seek", at=60)
    assert went[-1] == ("https://www.youtube.com/watch?v=abcdefghijk&t=60", {"new_tab": False})


def test_browser_does_not_open_separate_window_without_consent(monkeypatch):
    import pytest

    from alice.tools import browser
    from alice.tools.base import ToolError

    monkeypatch.setattr(browser, "cdp_alive", lambda e, timeout=0.4: False)
    # If a browser with an open debug port (Opera GX, the bridge) is on this machine, don't let the test attach to it.
    monkeypatch.setattr(browser, "user_browser_endpoint", lambda cdp=None, bridge_port=None: None)
    with pytest.raises(ToolError, match="ayrı pencere"):
        browser.BrowserSession(cdp="http://127.0.0.1:9222", allow_launch=False)


def test_devtools_active_port_endpoint(tmp_path, monkeypatch):
    from alice.tools import browser

    d = tmp_path / "Opera GX Stable"
    d.mkdir()
    (d / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc\n", encoding="utf-8")
    monkeypatch.setattr(browser, "_port_open", lambda port, timeout=0.3: port == 9333)
    assert browser.devtools_active_endpoint([tmp_path / "yok", d]) == "ws://127.0.0.1:9333/devtools/browser/abc"
    monkeypatch.setattr(browser, "_port_open", lambda port, timeout=0.3: False)
    assert browser.devtools_active_endpoint([d]) is None
    assert browser.host_of("https://www.youtube.com/watch?v=1") == "youtube.com"


def test_navigate_site_root_only_switches_tab(monkeypatch):
    from alice.tools import computer, desktop

    did = []
    monkeypatch.setattr(desktop, "browser_window_title", lambda: "Opera")
    monkeypatch.setattr(desktop, "focus_window", lambda t: "x")
    monkeypatch.setattr(desktop, "active_title", lambda: "Concorde - YouTube - Opera")
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    monkeypatch.setattr(computer, "press_keys", lambda k: did.append(k))
    monkeypatch.setattr(computer, "type_text", lambda t: did.append(t))
    res = desktop.Windows().run(None, "navigate", target="https://www.youtube.com/")
    assert did == [] and "geçildi" in res.text
    assert desktop.is_site_root("youtube.com") and not desktop.is_site_root("youtube.com/watch?v=1")


def test_youtube_search_results_are_cached(monkeypatch):
    import httpx

    from alice.tools import extras

    calls = []
    html = '"videoRenderer":{"videoId":"abcdefghijk","title":{"runs":[{"text":"A"}]}'
    real = httpx.Client

    def fake_client(*a, **k):
        calls.append(1)
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))

    monkeypatch.setattr(extras.httpx, "Client", fake_client)
    extras._YT_CACHE.clear()
    assert extras.youtube_search("Kedi") == extras.youtube_search("kedi ") == [("A", "abcdefghijk")]
    assert len(calls) == 1


def test_browser_evaluate_wraps_with_timeout():
    from types import SimpleNamespace

    from alice.tools.browser import BrowserSession

    calls = []
    loc = SimpleNamespace(evaluate=lambda js, arg, timeout: calls.append((js, arg, timeout)) or 1)
    b = BrowserSession.__new__(BrowserSession)
    b.attached, b.page = False, SimpleNamespace(locator=lambda sel: loc)
    b.evaluate("(limit) => limit", 5)
    b.evaluate("document.title")
    assert calls[0] == ("(_el, arg) => ((limit) => limit)(arg)", 5, 10000)
    assert calls[1][0] == "(_el, arg) => (() => (document.title))(arg)"


def test_start_apps_match_store_apps(monkeypatch):
    from alice.tools import desktop

    monkeypatch.setattr(desktop, "_START_APPS", [("WhatsApp", "5319275A.WhatsAppDesktop_x!App"),
                                                 ("Medya Oynatıcı", "Microsoft.ZuneMusic_x!Microsoft.ZuneMusic")])
    assert desktop.match_start_app("whatsapp") == ("WhatsApp", "5319275A.WhatsAppDesktop_x!App")
    assert desktop.match_start_app("medya") == ("Medya Oynatıcı", "Microsoft.ZuneMusic_x!Microsoft.ZuneMusic")
    assert desktop.match_start_app("matlab") is None


@pytest.mark.parametrize("js, want", [
    ("() => document.title", "() => document.title"),
    ("(limit) => limit * 2", "(limit) => limit * 2"),
    ("x => x + 1", "x => x + 1"),
    ("async () => 1", "async () => 1"),
    ("function () { return 1 }", "function () { return 1 }"),
    # used to wrap an "async" function without "=>", and it was never called
    ("async function () { return 1 }", "async function () { return 1 }"),
    # used to treat a "("-started expression containing "=>" as a function; calling it gave "not a function"
    ("(() => document.title)()", "() => ((() => document.title)())"),
    ("([1, 2].map(x => x * 2))", "() => (([1, 2].map(x => x * 2)))"),
    ("document.title;", "() => (document.title)"),
])
def test_browser_js_function_wrapping(js, want):
    from alice.tools.browser import js_function

    assert js_function(js) == want


def test_list_dir_known_folders_and_newest(tmp_path, monkeypatch):
    import os
    import time as _t
    from pathlib import Path

    from alice.tools import files
    from alice.tools.base import ToolContext
    from tests.conftest import make_config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dl = tmp_path / "Downloads"
    dl.mkdir()
    for i, name in enumerate(["eski.zip", "orta.pdf", "yeni.exe"]):
        f = dl / name
        f.write_text("x" * (i + 1), encoding="utf-8")
        os.utime(f, (_t.time() - 1000 + i * 100,) * 2)
    ctx = ToolContext(cwd=tmp_path, config=make_config({"brain": ["a:m"]}))
    out = files.ListDir().run(ctx, path="İndirilenler", sort="newest", limit=2).text
    lines = out.splitlines()
    assert lines[0] == str(dl) and lines[1].startswith("yeni.exe") and lines[2].startswith("orta.pdf")
    assert "1 öğe daha" in out and "." in lines[1].split("(")[1]  # has a date
    assert files.ListDir().run(ctx, path="downloads").text.splitlines()[1].startswith("eski.zip")  # by name
    (tmp_path / "OneDrive" / "Desktop").mkdir(parents=True)
    assert files.known_folder("Masaüstü") == tmp_path / "OneDrive" / "Desktop"


def test_macos_resolved_system_paths_count_as_system(monkeypatch):
    """macOS: /etc and /var are links into /private, and callers resolve paths first, so '/etc/hosts' is checked as
    '/private/etc/hosts'. It was not recognized and auto mode allowed writing it (found by the macOS CI job)."""
    from pathlib import PurePosixPath  # macOS paths, also when CI runs this test on Windows

    from alice import permissions

    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    for p in ("/private/etc/hosts", "/private/etc/cron.d/x", "/private/var/db/x", "/etc/hosts", "/System/x"):
        assert permissions.is_system_path(PurePosixPath(p)), p
    for p in ("/private/var/folders/x/project/a.txt", "/Users/me/project/a.txt", "/private/etcetera"):
        assert not permissions.is_system_path(PurePosixPath(p)), p
