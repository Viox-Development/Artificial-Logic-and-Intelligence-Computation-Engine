"""alice/cli.py: command dispatch, slash commands, status line, diff/tool output formatting, role writing."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from alice import cli
from alice.config import alice_home, init_home
from alice.permissions import Permissions
from alice.providers.router import AllProvidersFailed, Router
from alice.tools.base import ToolContext, ToolResult
from tests.conftest import make_config


@pytest.fixture
def out(monkeypatch):
    """Replace cli.console with a capturable console; returns the function that gives back what was written."""
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, highlight=False, color_system=None))
    return buf.getvalue


def _agent(cfg, **kw):
    a = SimpleNamespace(permissions=Permissions("auto"), tools={}, memory=None, config=cfg,
                        messages=[{"role": "system", "content": "sys"}], decider=None, compact_at=50000,
                        reset_called=False)
    a.reset = lambda: setattr(a, "reset_called", True)
    a.compact = lambda: False
    a.context_tokens = lambda: 1500
    a.__dict__.update(kw)
    return a


@pytest.fixture
def env(workdir):
    cfg = make_config({"brain": ["a:m1", "b:m2"], "fast": ["a:f"]})
    router = Router(cfg, auto_cache={})
    ctx = ToolContext(cwd=workdir, config=cfg)
    return cfg, router, ctx, _agent(cfg)


# ---------------------------------------------------------------- helpers
def test_ktok_formats_thousands_and_millions():
    assert cli._ktok(1500) == "1.5k"
    assert cli._ktok(999) == "1.0k"
    assert cli._ktok(2_500_000) == "2.5M"


def test_status_line_uses_last_ref_window_and_shortens_cwd(env, tmp_path):
    cfg, router, ctx, agent = env
    line = cli.status_line(agent, router, ctx)
    assert " a:m1 " in line and "mode: auto" in line and "context 1.5k/" in line and "decider: -" in line
    router.last_ref["brain"] = "b:m2"
    agent.decider = SimpleNamespace(names=["llm:fast"])
    deep = tmp_path / ("x" * 50)
    ctx.cwd = deep
    line = cli.status_line(agent, router, ctx)
    assert " b:m2 " in line and "decider: llm:fast" in line
    assert "…" + str(deep)[-39:] in line


def test_status_line_without_any_model_uses_compact_at(env):
    cfg, _router, ctx, agent = env
    router = Router(make_config({"brain": []}), auto_cache={})
    line = cli.status_line(agent, router, ctx)
    assert "no model" in line and "context 1.5k/50.0k" in line


def test_check_ready(env, out):
    _cfg, router, _ctx, _agent_ = env
    assert cli._check_ready(router) is True
    empty = Router(make_config({"brain": []}), auto_cache={})
    assert cli._check_ready(empty) is False
    assert "No usable brain model" in out()


def test_print_diff_counts_and_truncates(out):
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old\n+new\n+more\n ctx\n"
    cli.print_diff(diff)
    text = out()
    assert "x.py +2 -1" in text
    assert "@@ -1,2 +1,2 @@" in text and "-old" in text and "+new" in text and " ctx" in text
    assert "--- a/x.py" not in text  # header lines are not repeated in the body
    cli.print_diff("\n".join(f"+l{i}" for i in range(50)), max_lines=10)
    assert "… +40 lines" in out()


def test_print_diff_without_file_header(out):
    cli.print_diff("+a\n-b")
    assert "⎿  +1 -1" in out()


# --------------------------------------------------------------------- RichUI
def test_tool_output_compact_and_long(out):
    ui = cli.RichUI()
    ui.tool_start("1", "read_file", "x.py")
    ui.tool_end("1", "read_file", ToolResult("tek satır"))
    ui.tool_start("2", "grep", "foo")
    ui.tool_end("2", "grep", ToolResult("a\nb\nc", is_error=True))
    text = out()
    assert "read_file x.py  ⎿ tek satır" in text
    assert "grep foo  ⎿ 3 lines" in text
    ui.tool_start("3", "run_command", "ls")
    ui.tool_end("3", "run_command", ToolResult("\n".join(f"s{i}" for i in range(10)), images=["img"]))
    text = out()
    assert "run_command ls" in text and "⎿ s0" in text and "s5" in text and "s6" not in text
    assert "… +4 lines" in text and "(+1 images)" in text


def test_tool_output_with_diff(out):
    ui = cli.RichUI()
    ui.tool_end("1", "edit_file", ToolResult("ok", diff="+++ b/f.txt\n+x"))
    assert "f.txt +1 -0" in out()
    ui.tool_end("2", "edit_file", ToolResult("eşleşme yok", is_error=True, diff="+++ b/g.txt\n-y"))
    assert "⎿ eşleşme yok" in out()


def test_tool_output_empty_text(out):
    cli.RichUI().tool_end("1", "write_file", ToolResult(""))
    assert "⎿" in out()


@pytest.mark.parametrize("answers,expected", [(["", ], "y"), (["evet"], "y"), (["zzz", "h"], "n"),
                                               (["no"], "n"), (["hep"], "a"), (["A"], "a")])
def test_ask_permission_parses_answers(monkeypatch, out, answers, expected):
    it = iter(answers)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": next(it))
    assert cli.RichUI().ask_permission("run_command", "rm x", "danger") == expected
    assert "Permission needed" in out()


def test_stream_and_thinking(out):
    ui = cli.RichUI(verbose=True, model_label=lambda: "a:m1")
    ui.stream_start()
    ui.thinking_delta("ilk düşünce " * 40)
    assert len(ui._think) == 300
    ui.stream_delta("Mer")
    ui.stream_delta("haba")
    assert ui._buf == "Merhaba"
    ui.stream_reset()
    assert ui._buf == "" and "fallback model" in out()
    ui.stream_end("**Bitti**", "a:m1")
    ui.info("bilgi [x]")
    text = out()
    assert "Bitti" in text and "(a:m1)" in text and "bilgi [x]" in text
    assert ui._spin is None and ui._live is None


def test_tail_renders_last_lines():
    c = Console(file=io.StringIO(), width=80, height=10)
    tail = cli._Tail("\n\n".join(f"satır {i}" for i in range(40)))
    c.print(tail)
    text = c.file.getvalue()
    assert "satır 39" in text and "satır 0\n" not in text


# --------------------------------------------------------------- slash commands
def test_slash_exit_init_kurul(env):
    cfg, router, ctx, agent = env
    for c in ("/exit", "/quit", "/Q"):
        assert cli.handle_slash(c, agent, router, ctx, cfg) == "exit"
    kind, prompt = cli.handle_slash("/init", agent, router, ctx, cfg)
    assert kind == "run" and "ALICE.md" in prompt
    kind, prompt = cli.handle_slash("/kurul mimari karar", agent, router, ctx, cfg)
    assert kind == "run" and "mimari karar" in prompt


def test_slash_custom_command_from_project(env):
    cfg, router, ctx, agent = env
    d = ctx.cwd / ".alice" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("---\ndescription: incele\n---\nŞunu incele: $ARGUMENTS", encoding="utf-8")
    assert cli.handle_slash("/review hız", agent, router, ctx, cfg) == ("run", "Şunu incele: hız")


def test_slash_mode_plan_clear_compact(env, out):
    cfg, router, ctx, agent = env
    assert cli.handle_slash("/mode yolo", agent, router, ctx, cfg) is None
    assert agent.permissions.mode == "yolo"
    cli.handle_slash("/mode bilinmeyen", agent, router, ctx, cfg)
    assert agent.permissions.mode == "yolo"
    cli.handle_slash("/plan", agent, router, ctx, cfg)
    assert agent.permissions.mode == "plan"
    cli.handle_slash("/clear", agent, router, ctx, cfg)
    assert agent.reset_called
    cli.handle_slash("/compact", agent, router, ctx, cfg)
    agent.compact = lambda: True
    cli.handle_slash("/compact", agent, router, ctx, cfg)
    text = out()
    assert "Permission mode: plan" in text and "Conversation cleared" in text
    assert "Nothing to summarize" in text and "Summarized" in text


def test_slash_model_override_reset_and_bad_ref(env, out):
    cfg, router, ctx, agent = env
    cli.handle_slash("/model brain b:m2", agent, router, ctx, cfg)
    assert router.chain("brain")[0] == "b:m2"
    assert "Role chains" in out()
    cli.handle_slash("/model brain reset", agent, router, ctx, cfg)
    assert router.chain("brain") == ["a:m1", "b:m2"]
    assert cli.handle_slash("/model brain bozukref", agent, router, ctx, cfg) is None
    assert "brain" not in router.role_overrides


def test_slash_think_sets_overrides(env, out):
    cfg, router, ctx, agent = env
    cli.handle_slash("/think high", agent, router, ctx, cfg)
    assert router.thinking_overrides == {"*": "high"}
    cli.handle_slash("/think fast off", agent, router, ctx, cfg)
    assert router.thinking_overrides["fast"] == "off"
    assert router.thinking_level("fast") == "off" and router.thinking_level("brain") == "high"
    cli.handle_slash("/think saçma", agent, router, ctx, cfg)
    assert router.thinking_overrides == {"*": "high", "fast": "off"}
    assert "Thinking: brain=high, fast=off" in out() and "[thinking]" in out()


def test_slash_tools_mcp_usage_memory(env, out):
    cfg, router, ctx, agent = env
    cli.handle_slash("/mcp", agent, router, ctx, cfg)
    assert "No MCP servers" in out()
    agent.tools = {"read_file": SimpleNamespace(name="read_file", risk="read", description="Dosya okur\nayrıntı"),
                   "mcp__gh__issues": SimpleNamespace(name="mcp__gh__issues", risk="write", description="Issue")}
    cli.handle_slash("/tools", agent, router, ctx, cfg)
    cli.handle_slash("/mcp", agent, router, ctx, cfg)
    router.usage.record("a:m1", {"prompt_tokens": 7, "completion_tokens": 3}, True)
    cli.handle_slash("/usage", agent, router, ctx, cfg)
    agent.memory = SimpleNamespace(export_markdown=lambda: "# Hafıza [x]")
    cli.handle_slash("/memory", agent, router, ctx, cfg)
    cli.handle_slash("/olmayan", agent, router, ctx, cfg)
    text = out()
    assert "read_file (read) Dosya okur" in text and "ayrıntı" not in text
    assert "mcp__gh__issues (write) Issue" in text
    assert "a:m1" in text and "Usage today" in text
    assert "# Hafıza [x]" in text
    assert "Unknown command /olmayan" in text


def test_slash_sessions_and_resume(env, out):
    from alice import session as sess

    cfg, router, ctx, agent = env
    cli.handle_slash("/resume", agent, router, ctx, cfg)
    assert "No session" in out()
    sess.save("20260101-101010", [{"role": "system", "content": "s"}, {"role": "user", "content": "eski soru"}],
              ctx.cwd)
    cli.handle_slash("/sessions", agent, router, ctx, cfg)
    assert "20260101-101010" in out() and "eski soru" in out()
    cli.handle_slash("/resume 20260101-101010", agent, router, ctx, cfg)
    assert agent.messages == [{"role": "system", "content": "sys"}, {"role": "user", "content": "eski soru"}]


def test_slash_cwd(env, out):
    cfg, router, ctx, agent = env
    (ctx.cwd / "alt").mkdir()
    cli.handle_slash("/cwd alt", agent, router, ctx, cfg)
    assert ctx.cwd.name == "alt"
    cli.handle_slash("/cwd yok-boyle", agent, router, ctx, cfg)
    assert ctx.cwd.name == "alt" and "No such folder" in out()
    cli.handle_slash("/cwd", agent, router, ctx, cfg)


def test_slash_help(env, out):
    cfg, router, ctx, agent = env
    cli.handle_slash("/help", agent, router, ctx, cfg)
    assert "/rewind" in out()


# --------------------------------------------------------------------- rewind
def test_rewind_prompt_no_turns(env, out):
    _cfg, _router, _ctx, agent = env
    agent.messages += [{"role": "user", "content": "[Önceki konuşmanın özeti] ..."}]
    assert cli.rewind_prompt(agent) is None
    assert "No message to rewind to" in out()


def test_rewind_prompt_picks_numbered_turn(env, monkeypatch, out):
    _cfg, _router, _ctx, agent = env
    agent.messages += [{"role": "user", "content": "birinci\nikinci satır"}, {"role": "assistant", "content": "c"},
                       {"role": "user", "content": [{"type": "image"}], "_image": True},
                       {"role": "user", "content": [{"type": "text"}]},
                       {"role": "user", "content": "x", "_display": "görünen"}]
    calls = []
    agent.rewind = lambda i: (calls.append(i) or ("eski metin", ["f1", "f2"]))
    monkeypatch.setattr(cli.console, "input", lambda prompt="": "3")
    assert cli.rewind_prompt(agent) == ("prefill", "eski metin")
    assert calls == [1]  # 3rd (oldest) user message
    text = out()
    assert "1 görünen" in text and "2 (message with an image)" in text and "3 birinci" in text
    assert "ikinci satır" not in text and "2 files restored" in text


@pytest.mark.parametrize("ans", ["", "0", "9", "abc"])
def test_rewind_prompt_cancel(env, monkeypatch, ans):
    _cfg, _router, _ctx, agent = env
    agent.messages.append({"role": "user", "content": "x"})
    agent.rewind = lambda i: pytest.fail("geri sarmamalıydı")
    monkeypatch.setattr(cli.console, "input", lambda prompt="": ans)
    assert cli.rewind_prompt(agent) is None


# ----------------------------------------------------------------- write_roles
def test_write_roles_replaces_only_roles_lists(isolated_home):
    init_home()
    path = cli.write_roles({"brain": ["auto", "x:y"], "fast": [], "vision": ["auto"]})
    text = path.read_text(encoding="utf-8")
    assert 'brain = ["auto", "x:y"]' in text and 'vision = ["auto"]' in text
    assert 'fast = ["auto", "ollama:qwen3:8b"]' in text  # empty suggestion: left untouched
    assert 'fast = "off"' in text and 'voice = "off"' in text  # same-named keys under [thinking]
    from alice.config import tomllib

    data = tomllib.loads(text)
    assert data["roles"]["brain"] == ["auto", "x:y"] and data["thinking"]["fast"] == "off"


def test_write_roles_adds_missing_role(isolated_home):
    init_home()
    path = alice_home() / "config.toml"
    import re

    path.write_text(re.sub(r"(?m)^vision = \[.*?\]\n", "", path.read_text(encoding="utf-8")), encoding="utf-8")
    cli.write_roles({"vision": ["auto", "gemini:x"]})
    from alice.config import tomllib

    assert tomllib.loads(path.read_text(encoding="utf-8"))["roles"]["vision"] == ["auto", "gemini:x"]


def test_write_roles_only_touches_roles_section_with_comment_header(isolated_home):
    init_home()
    path = alice_home() / "config.toml"
    path.write_text('[other]\nvision = ["dokunma"]\n\n[roles]  # modeller\nbrain = [\n  "a:b",  # ilk\n  "c:d",\n]'
                    '  # beyin\n\n[thinking]\nfast = "off"\n', encoding="utf-8")
    cli.write_roles({"brain": ["auto", "x:y"], "vision": ["auto", "gemini:x"]})
    from alice.config import tomllib

    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["roles"] == {"brain": ["auto", "x:y"], "vision": ["auto", "gemini:x"]}
    assert data["other"]["vision"] == ["dokunma"] and data["thinking"]["fast"] == "off"
    assert "[roles]  # modeller" in text and text.count("[roles]") == 1


# ------------------------------------------------------------ default_workspace
def test_default_workspace_prefers_config_then_last_cwd(isolated_home, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert cli.default_workspace() == fake_home / "Documents" / "ALICE"
    assert (fake_home / "Documents" / "ALICE").is_dir()

    last = tmp_path / "son"
    last.mkdir()
    alice_home().mkdir(parents=True, exist_ok=True)
    (alice_home() / "last_cwd").write_text(str(last) + "\n", encoding="utf-8")
    assert cli.default_workspace() == last

    ws = tmp_path / "ws"
    ws.mkdir()
    init_home()
    from alice.config import set_config_value

    set_config_value("app", "workspace", str(ws))
    assert cli.default_workspace() == ws


def test_default_workspace_without_documents(isolated_home, tmp_path, monkeypatch):
    fake_home = tmp_path / "home2"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert cli.default_workspace() == fake_home / "ALICE"


# ------------------------------------------------------------------------ main
def test_main_init(out, isolated_home):
    assert cli.main(["init"]) == 0
    assert (alice_home() / "config.toml").is_file()
    assert "oluşturuldu" in out()


@pytest.mark.parametrize("cmd,mod,attr", [
    ("core", "alice.core.cli", "core_cmd"), ("rules", "alice.core.cli", "rules_cmd"),
    ("tasks", "alice.core.cli", "tasks_cmd"), ("notes", "alice.extras.cli", "notes_cmd"),
    ("brief", "alice.extras.cli", "brief_cmd"), ("report", "alice.extras.cli", "report_cmd"),
    ("watch", "alice.app.phone", "watch_cmd"), ("phone", "alice.app.phone", "phone_cmd"),
    ("dcs", "alice.sims.dcs", "dcs_cmd"), ("quick", "alice.app.quick", "quick_cmd"),
    ("voice", "alice.voice.cli", "voice_cmd"), ("plugins", "alice.plugins", "plugins_cmd"),
])
def test_main_dispatches_subcommands(monkeypatch, cmd, mod, attr):
    import importlib

    m = importlib.import_module(mod)
    seen = []
    monkeypatch.setattr(m, attr, lambda argv: seen.append(argv) or 7)
    assert cli.main([cmd, "x", "--y"]) == 7
    assert seen == [["x", "--y"]]


def test_main_doctor_models_shortcut_app(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "doctor", lambda cfg: seen.append("doctor") or 3)
    monkeypatch.setattr(cli, "models_cmd", lambda cfg, r, w: seen.append(("models", r, w)) or 4)
    monkeypatch.setattr(cli, "shortcut_cmd", lambda: seen.append("shortcut") or 5)
    monkeypatch.setattr(cli, "app_cmd", lambda argv: seen.append(("app", argv)) or 6)
    assert cli.main(["doctor"]) == 3
    assert cli.main(["models", "--write"]) == 4
    assert cli.main(["shortcut"]) == 5
    assert cli.main(["app", "--window", "none"]) == 6
    assert seen == ["doctor", ("models", False, True), "shortcut", ("app", ["--window", "none"])]


def test_main_app_logs_errors(monkeypatch):
    from alice.app import server

    logged = []
    monkeypatch.setattr(server, "log_error", lambda: logged.append(True))

    def boom(argv):
        raise RuntimeError("pencere yok")

    monkeypatch.setattr(cli, "app_cmd", boom)
    with pytest.raises(RuntimeError):
        cli.main(["app"])
    assert logged == [True]


def test_main_reflex_parses_arguments(monkeypatch):
    got = {}
    monkeypatch.setattr(cli, "reflex_cmd", lambda cfg, a: got.update(vars(a)) or 0)
    assert cli.main(["reflex", "giriş yap", "--target", "desktop", "--title", "Not", "--input", "kullanici=jev",
                     "--input", "sifre=x=y", "--max-steps", "5", "--close"]) == 0
    assert got == {"goal": "giriş yap", "target": "desktop", "url": None, "title": "Not",
                   "input": ["kullanici=jev", "sifre=x=y"], "max_steps": 5, "close": True}


class _FakeAgent:
    def __init__(self, result="tamam"):
        self.turns: list[str] = []
        self.result = result

    def run_turn(self, text):
        self.turns.append(text)
        return self.result


def _patch_build(monkeypatch, agent, ready=True):
    built = {}
    router = SimpleNamespace(role_overrides={}, providers_cfg={}, first_available=lambda role: "a:m" if ready else None)

    def build(cfg, cwd, mode, verbose=False):
        built.update(cwd=cwd, mode=mode, verbose=verbose)
        return agent, router, SimpleNamespace(cwd=cwd)

    monkeypatch.setattr(cli, "build", build)
    monkeypatch.setattr(cli, "cleanup", lambda ctx: built.setdefault("cleaned", True))
    return built, router


def test_main_oneshot_runs_prompt_and_cleans_up(monkeypatch, workdir, out):
    agent = _FakeAgent()
    built, router = _patch_build(monkeypatch, agent)
    assert cli.main(["-p", "--yolo", "--cwd", str(workdir), "--model", "x:y", "-v", "merhaba", "dünya"]) == 0
    assert agent.turns == ["merhaba dünya"]
    assert built["mode"] == "yolo" and built["verbose"] and built["cwd"] == workdir.resolve() and built["cleaned"]
    assert router.role_overrides == {"brain": ["x:y"]}


def test_main_oneshot_reads_stdin_and_reports_failure(monkeypatch, workdir):
    agent = _FakeAgent(result="")
    _patch_build(monkeypatch, agent)
    monkeypatch.setattr(sys, "stdin", io.StringIO("  stdin görevi \n"))
    assert cli.main(["-p", "--mode", "plan", "--cwd", str(workdir)]) == 1
    assert agent.turns == ["stdin görevi"]


def test_main_not_ready_returns_2(monkeypatch, workdir, out):
    agent = _FakeAgent()
    _patch_build(monkeypatch, agent, ready=False)
    monkeypatch.setattr(cli, "_check_ready", lambda r: False)
    assert cli.main(["-p", "x", "--cwd", str(workdir)]) == 2
    assert agent.turns == []


def test_main_repl_path(monkeypatch, workdir):
    agent = _FakeAgent()
    built, _ = _patch_build(monkeypatch, agent)
    seen = {}
    monkeypatch.setattr(cli, "repl", lambda a, r, c, cfg, resume: seen.update(resume=resume))
    assert cli.main(["ilk", "--resume", "--cwd", str(workdir)]) == 0
    assert agent.turns == ["ilk"] and seen == {"resume": ""} and built["mode"] == "auto"


def test_main_rejects_bad_mode():
    with pytest.raises(SystemExit):
        cli.main(["--mode", "tanrı"])


# ------------------------------------------------------------------------ repl
def test_repl_loop_handles_slash_prefill_and_exit(monkeypatch, env, out):
    cfg, router, ctx, _ = env
    agent = _FakeAgent()
    agent.messages = [{"role": "system", "content": "s"}]
    inputs = iter(["", "/help", "/init", "selam", KeyboardInterrupt, "/rewind", "/exit"])
    defaults = []

    class PS:
        def prompt(self, msg, default=""):
            defaults.append(default)
            v = next(inputs)
            if v is KeyboardInterrupt:
                raise KeyboardInterrupt
            return v

    monkeypatch.setattr(cli, "make_prompt_session", lambda *a: PS())
    monkeypatch.setattr(cli, "rewind_prompt", lambda a: ("prefill", "geri"))
    monkeypatch.setattr(cli, "cleanup", lambda c: None)
    cli.repl(agent, router, ctx, cfg, resume=None)
    assert agent.turns[1] == "selam" and "ALICE.md" in agent.turns[0]
    assert defaults[-1] == "geri"  # the prompt text is prefilled after /rewind
    from alice import session as sess

    assert sess.list_sessions()  # saved after every turn


def test_repl_resume_missing_and_eof(monkeypatch, env, out):
    cfg, router, ctx, _ = env
    agent = _FakeAgent()
    agent.messages = [{"role": "system", "content": "s"}]

    class PS:
        def prompt(self, msg, default=""):
            raise EOFError

    monkeypatch.setattr(cli, "make_prompt_session", lambda *a: PS())
    monkeypatch.setattr(cli, "cleanup", lambda c: None)
    cli.repl(agent, router, ctx, cfg, resume="yok")
    assert "No session to load" in out()


def test_repl_resume_existing(monkeypatch, env, out):
    from alice import session as sess

    cfg, router, ctx, _ = env
    sess.save("20250101-000000", [{"role": "system"}, {"role": "user", "content": "eski"}], ctx.cwd)
    agent = _FakeAgent()
    agent.messages = [{"role": "system", "content": "s"}]

    class PS:
        def prompt(self, msg, default=""):
            raise EOFError

    monkeypatch.setattr(cli, "make_prompt_session", lambda *a: PS())
    monkeypatch.setattr(cli, "cleanup", lambda c: None)
    cli.repl(agent, router, ctx, cfg, resume="")
    assert agent.messages[1] == {"role": "user", "content": "eski"}
    assert "Session loaded: 20250101-000000 (1 messages)" in out()


# ---------------------------------------------------------------- doctor/models
def test_doctor_reports_providers_and_decider(monkeypatch, out):
    cfg = make_config({"brain": ["a:m1"], "fast": ["b:m2"]})
    calls = []

    def chat(self, ref, messages, **kw):
        calls.append(ref)
        if ref.startswith("b:"):
            raise AllProvidersFailed("x", ["b: 401 yetkisiz"])
        return SimpleNamespace(text=" ok \n")

    monkeypatch.setattr(Router, "chat", chat)

    class Dec:
        names = ["llm:fast"]

        def decide(self, obs, qs):
            from alice.decider import DeciderError

            raise DeciderError("motor yok")

    monkeypatch.setattr(cli.Decider, "from_config", staticmethod(lambda cfg, router, **kw: Dec()))
    assert cli.doctor(cfg) == 0
    text = out()
    assert sorted(calls) == ["a:m1", "b:m2"]
    assert "working" in text and "401 yetkisiz" in text and "decision engine not working: motor yok" in text
    assert "Decision engines: llm:fast" in text and "playwright" in text


def test_models_cmd_from_cache(monkeypatch, out, isolated_home):
    from alice.providers import ranking

    cache = {"probed_at": "2026-10-10T10:00", "results": {
        "a:m1": {"ref": "a:m1", "ok": True, "error_kind": "", "quality": 90, "latency": 1.25, "ttft": 0.5,
                 "tokens": 40, "tps": 30.0},
        "b:m2": {"ref": "b:m2", "ok": False, "error_kind": "not_found", "quality": 70, "latency": 0.1, "ttft": 0.1,
                 "tokens": 0, "tps": 0.0}},
        "catalog_errors": {"groq": "GROQ_API_KEY tanımlı değil"}, "chains": {"brain": ["a:m1"], "fast": []}}
    monkeypatch.setattr(ranking, "load_cache", lambda path=None: cache)
    monkeypatch.setattr(ranking, "refresh", lambda *a, **k: pytest.fail("önbellek varken ölçmemeli"))
    cfg = make_config({"brain": ["a:m1"], "fast": ["a:m1"], "vision": ["b:m2"]})
    assert cli.models_cmd(cfg, refresh=False, write=False) == 0
    text = out()
    assert "2026-10-10T10:00" in text and "not_found" in text and "groq: GROQ_API_KEY" in text
    assert "brain: a:m1" in text and "fast: -" in text and "alice models --write" in text


def test_models_cmd_refresh_and_write(monkeypatch, out, isolated_home):
    from alice.providers import ranking

    monkeypatch.setattr(ranking, "load_cache", lambda path=None: {})
    fresh = {"probed_at": "now", "results": {}, "chains": {"brain": ["a:m1"]}}
    monkeypatch.setattr(ranking, "refresh", lambda router, emit=None: (emit and emit("a:m1: OK")) or fresh)
    reloaded = []
    monkeypatch.setattr(Router, "reload_auto", lambda self, c=None: reloaded.append(c))
    assert cli.models_cmd(make_config({"brain": ["a:m1"], "fast": ["a:m1"], "vision": ["auto"]}),
                          refresh=False, write=True) == 0
    assert reloaded == [fresh]
    from alice.config import tomllib

    roles = tomllib.loads((alice_home() / "config.toml").read_text(encoding="utf-8"))["roles"]
    assert roles["brain"] == ["auto", "ollama:qwen3:8b"] and roles["vision"][0] == "auto"
    assert 'roles set to "auto"' in out()


def test_reflex_cmd_wires_observer_and_inputs(monkeypatch, out):
    from alice.reflex import loop, observers

    seen = {}

    class Obs:
        def __init__(self, ctx, title):
            seen["title"] = title

    class Loop:
        def __init__(self, observer, decider, writer, min_confidence, emit):
            seen["min_conf"] = min_confidence
            emit("adım 1")

        def run(self, goal, max_steps, inputs):
            seen.update(goal=goal, max_steps=max_steps, inputs=inputs)
            return SimpleNamespace(status="done", report=lambda: "rapor")

    monkeypatch.setattr(observers, "DesktopObserver", Obs)
    monkeypatch.setattr(loop, "ReflexLoop", Loop)
    monkeypatch.setattr(loop, "make_text_writer", lambda router: None)
    monkeypatch.setattr(cli, "cleanup", lambda ctx: seen.setdefault("cleaned", True))
    a = SimpleNamespace(target="desktop", url=None, title="Hesap", input=["ad=Jev", "not=a=b"], goal="hedef",
                        max_steps=3, close=True)
    assert cli.reflex_cmd(make_config({"fast": ["a:x"]}), a) == 0
    assert seen["inputs"] == {"ad": "Jev", "not": "a=b"} and seen["title"] == "Hesap" and seen["cleaned"]
    assert "rapor" in out() and "adım 1" in out()


# -------------------------------------------------------------- app/shortcut
def _fake_app(monkeypatch, ready=True):
    from alice.app import server, window

    seen = {"infos": []}

    class State:
        def __init__(self, cfg, cwd, mode):
            seen.update(cwd=cwd, mode=mode)
            self.ui = SimpleNamespace(info=lambda s: seen["infos"].append(s))
            self.router = SimpleNamespace(first_available=lambda role: "a:m" if ready else None)

        def watch_updates(self, pending):
            seen["pending"] = pending

    class Server:
        url = "http://127.0.0.1:1/"

        def __init__(self, state, port):
            seen["port"] = port

        def start(self):
            return self

        def stop(self):
            seen["stopped"] = True

    monkeypatch.setattr(server, "AppState", State)
    monkeypatch.setattr(server, "AppServer", Server)
    from alice import updater

    # The real update check looks at the repo's git state: if the test branch differs from main, it says
    # "yeni sürüm var" (a new version is available) and restarts the process, which killed the test worker.
    monkeypatch.setattr(updater, "startup_update", lambda *a, **k: None)
    monkeypatch.setattr(window, "open_window", lambda url, prefer: seen.update(window=(url, prefer)))
    return seen


def test_app_cmd_opens_window_and_stops_server(monkeypatch, workdir, out):
    seen = _fake_app(monkeypatch, ready=False)
    assert cli.app_cmd(["--cwd", str(workdir), "--port", "8123", "--mode", "ask", "--window", "browser"]) == 0
    assert seen["cwd"] == workdir.resolve() and seen["mode"] == "ask" and seen["port"] == 8123
    assert seen["window"] == ("http://127.0.0.1:1/", "browser") and seen["stopped"]
    assert any("Kullanılabilir model yok" in s for s in seen["infos"])
    assert "http://127.0.0.1:1/" in out()


def test_app_cmd_starts_core_when_voice_enabled(monkeypatch, workdir):
    from alice.config import set_config_value
    from alice.core import service

    seen = _fake_app(monkeypatch)
    init_home()
    set_config_value("voice", "enabled", True)
    monkeypatch.setattr(service, "start_detached", lambda: True)
    assert cli.app_cmd(["--cwd", str(workdir), "--window", "app"]) == 0
    assert any("çekirdeği başlatıldı" in s for s in seen["infos"])
    assert seen["mode"] == "auto"


def test_app_cmd_headless_until_ctrl_c(monkeypatch, workdir):
    seen = _fake_app(monkeypatch)

    def sleep(_s):
        raise KeyboardInterrupt

    from tests.conftest import patch_sleep

    patch_sleep(monkeypatch, cli, sleep)
    assert cli.app_cmd(["--cwd", str(workdir), "--window", "none"]) == 0
    assert seen["stopped"] and "window" not in seen


def test_shortcut_cmd(monkeypatch, out, tmp_path):
    from alice.app import window

    monkeypatch.setattr(window, "create_shortcut", lambda: [tmp_path / "ALICE.lnk"])
    assert cli.shortcut_cmd() == 0
    assert "ALICE.lnk" in out()


def test_make_prompt_session_cycles_mode_with_shift_tab(env):
    cfg, router, ctx, agent = env
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        ps = cli.make_prompt_session(agent, router, ctx)
        assert (alice_home() / "history").parent.is_dir()
        stab = next(b for b in ps.key_bindings.bindings if b.keys == (Keys.BackTab,))
        ev = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None))
        stab.handler(ev)
        assert agent.permissions.mode == "yolo"
        stab.handler(ev)
        assert agent.permissions.mode == "plan"
        assert "mode: plan" in ps.bottom_toolbar()
