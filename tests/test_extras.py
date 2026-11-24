from __future__ import annotations

import sys
from pathlib import Path

from alice.agent import Agent, NullUI
from alice.commands import catalog, expand_custom
from alice.mcp import load_mcp
from alice.mentions import complete_paths, expand_mentions
from alice.permissions import Permissions
from alice.providers.router import Router
from alice.tools import ToolContext, all_tools
from tests.conftest import FakeProvider, make_config, tool_call

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp.py"


def make_agent(script, workdir, mode="auto"):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ui = NullUI()
    agent = Agent(cfg, router, all_tools(), ToolContext(cwd=workdir, config=cfg, router=router), Permissions(mode), ui,
                  system_prompt="s")
    return agent, prov, ui


def test_rewind_restores_files_and_conversation(workdir):
    (workdir / "a.txt").write_text("orijinal\n", encoding="utf-8")
    script = [tool_call("read_file", {"path": "a.txt"}, "r1"),
              tool_call("edit_file", {"path": "a.txt", "old_string": "orijinal", "new_string": "bir"}), "ok1",
              tool_call("write_file", {"path": "yeni.txt", "content": "x"}, "c2"),
              tool_call("edit_file", {"path": "a.txt", "old_string": "bir", "new_string": "iki"}, "c3"), "ok2"]
    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("birinci")
    second = len(agent.messages)
    agent.run_turn("ikinci")
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "iki\n" and (workdir / "yeni.txt").exists()
    text, restored = agent.rewind(second)
    assert text == "ikinci" and len(agent.messages) == second
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "bir\n" and not (workdir / "yeni.txt").exists()
    text, _ = agent.rewind(1)
    assert text == "birinci" and (workdir / "a.txt").read_text(encoding="utf-8") == "orijinal\n"
    assert len(agent.messages) == 1


def test_plan_mode_blocks_writes(workdir):
    script = [tool_call("write_file", {"path": "x.txt", "content": "x"}), "plan: önce x.txt yazacağım"]
    agent, prov, _ = make_agent(script, workdir, mode="plan")
    agent.run_turn("x.txt oluştur")
    assert not (workdir / "x.txt").exists()
    assert "plan mode" in [m for m in agent.messages if m["role"] == "tool"][0]["content"]
    assert "[PLAN MODE" in prov.calls[0]["messages"][1]["content"]
    assert agent.messages[1]["_display"] == "x.txt oluştur"


def test_mentions_and_completion(workdir):
    (workdir / "src").mkdir()
    (workdir / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    out = expand_mentions("şuna bak @src/main.py ve @src lütfen, mail a@b.com", workdir)
    assert "<file path=" in out and "print('hi')" in out and "<folder path=" in out
    assert "a@b.com" in out and out.count("<file") == 1
    assert expand_mentions("@yok.txt", workdir) == "@yok.txt"
    assert complete_paths(workdir, "mai")[0] == "src/main.py"


def test_user_images_sent_as_parts(workdir):
    agent, prov, _ = make_agent(["gördüm"], workdir)
    agent.run_turn("bu ne", images=["AAAA"])
    sent = prov.calls[0]["messages"][1]["content"]
    assert isinstance(sent, str)  # provider 'a' does not support images, so the router converted them to text
    assert isinstance(agent.messages[1]["content"], list)


def test_custom_commands(workdir, isolated_home):
    d = workdir / ".alice" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("---\ndescription: incele\n---\nŞunu incele: $ARGUMENTS\n", encoding="utf-8")
    (isolated_home / "commands").mkdir(parents=True)
    (isolated_home / "commands" / "selam.md").write_text("Bana selam ver", encoding="utf-8")
    names = {c["name"]: c for c in catalog(workdir)}
    assert names["review"]["description"] == "incele" and names["selam"]["kind"] == "custom"
    assert expand_custom("/review hız", workdir) == "Şunu incele: hız"
    assert expand_custom("/selam", workdir) == "Bana selam ver"
    assert expand_custom("/yok", workdir) is None


def test_mcp_client_roundtrip(workdir):
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["mcp"] = {"servers": {"fake": {"command": sys.executable, "args": [str(FIXTURE)],
                                            "trust_annotations": True},
                                   "bozuk": {"command": "olmayan-komut-xyz"}}}
    events = []
    servers, tools = load_mcp(cfg, events.append)
    try:
        assert [t.name for t in tools] == ["mcp__fake__echo", "mcp__fake__fail"]  # pages merged
        assert tools[0].risk == "read" and tools[1].risk == "exec"  # trust_annotations: readOnlyHint is honored
        ctx = ToolContext(cwd=workdir, config=cfg)
        assert tools[0].run(ctx, text="merhaba").text == "echo: merhaba"
        assert tools[1].run(ctx).is_error
        assert any("bozuk" in e for e in events)
    finally:
        for s in servers:
            s.close()


def test_multi_edit_atomic(workdir):
    from alice.tools.files import MultiEdit

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    f = workdir / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    from alice.tools.files import mark_read

    mark_read(ctx, f)  # editing without reading is rejected
    r = MultiEdit().run(ctx, path="m.py", edits=[{"old_string": "a = 1", "new_string": "a = 10"},
                                                 {"old_string": "a = 10\nb = 2", "new_string": "a = 10\nb = 20"}])
    assert f.read_text(encoding="utf-8") == "a = 10\nb = 20\nc = 3\n" and "+b = 20" in r.diff
    import pytest

    from alice.tools.base import ToolError
    with pytest.raises(ToolError, match="edit 2/2"):
        MultiEdit().run(ctx, path="m.py", edits=[{"old_string": "c = 3", "new_string": "c = 30"},
                                                 {"old_string": "yok", "new_string": "x"}])
    assert f.read_text(encoding="utf-8") == "a = 10\nb = 20\nc = 3\n"  # none of them was written


def test_parallel_read_only_calls(workdir):
    import threading
    import time

    from alice.tools.base import Tool, ToolResult

    seen = {"active": 0, "max": 0}
    lock = threading.Lock()

    class SlowRead(Tool):
        name = "slow_read"
        description = "x"
        parallel_safe = True

        def run(self, ctx, n=0):
            with lock:
                seen["active"] += 1
                seen["max"] = max(seen["max"], seen["active"])
            time.sleep(0.3)
            with lock:
                seen["active"] -= 1
            return ToolResult(f"sonuç {n}")

    calls = {"content": "", "tool_calls": [
        {"id": f"c{n}", "type": "function", "function": {"name": "slow_read", "arguments": f'{{"n": {n}}}'}}
        for n in range(4)] + [{"id": "w", "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path": "x.txt", "content": "1"}'}}]}
    agent, _, ui = make_agent([calls, "bitti"], workdir)
    agent.tools["slow_read"] = SlowRead()
    agent.run_turn("oku")
    # All 4 reads were running at the same moment. (A wall-clock limit for the whole turn failed on the slow Windows
    # runner: 1.28 s against 1.0 s, though the calls did run in parallel.)
    assert seen["max"] == 4
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1", "c2", "c3", "w"]  # order is kept
    assert [m["content"] for m in tool_msgs[:4]] == [f"sonuç {n}" for n in range(4)]
    order = [e for e in ui.events if e[0] in ("tool", "result")]
    assert order[:4] == [("tool", "slow_read")] * 4  # all of them started first
    assert (workdir / "x.txt").exists()


def test_parallel_skips_tools_needing_permission(workdir):
    calls = {"content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "run_command", "arguments": '{"command": "rm -rf yok"}'}}]}
    agent, _, ui = make_agent([calls, "tamam"], workdir)
    agent.run_turn("x")
    assert ("ask", "run_command") in ui.events


def _cmd(cid, command, **extra):
    import json

    return {"id": cid, "type": "function",
            "function": {"name": "run_command", "arguments": json.dumps({"command": command, **extra})}}


def test_independent_commands_in_one_reply_run_at_the_same_time(workdir, monkeypatch):
    """Live recording (Nov 8): asked "paralel yap işleri" (run the jobs in parallel), the model requested three
    commands in one reply, but they ran one after another; the total time was the sum of all three."""
    import threading
    import time

    from alice.tools import shell
    from alice.tools.base import ToolResult

    seen = {"active": 0, "max": 0, "order": []}
    lock = threading.Lock()

    def fake_run(self, ctx, command, **kw):
        with lock:
            seen["active"] += 1
            seen["max"] = max(seen["max"], seen["active"])
            seen["order"].append(command)
        time.sleep(0.3)
        with lock:
            seen["active"] -= 1
        return ToolResult(f"exit code 0 {command}")

    monkeypatch.setattr(shell.RunCommand, "run", fake_run)
    calls = {"content": "", "tool_calls": [_cmd("a", "python a.py"), _cmd("b", "python b.py"), _cmd("c", "node c.js")]}
    agent, _, ui = make_agent([calls, "bitti"], workdir, mode="auto")
    agent.run_turn("üçünü paralel çalıştır")
    # All three were running at once. No wall-clock check: a slow CI runner can take longer than the 0.9 s a
    # sequential run would need even when the commands overlap.
    assert seen["max"] == 3
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b", "c"]  # results come back in the requested order
    assert [m["content"] for m in tool_msgs] == [f"exit code 0 {c}" for c in ("python a.py", "python b.py", "node c.js")]

    # Two git commands on the same repo, and two installs, never run side by side (index.lock, half-finished install)
    seen.update(active=0, max=0, order=[])
    calls = {"content": "", "tool_calls": [_cmd("g1", "git add -A"), _cmd("g2", "git commit -m x")]}
    agent, _, ui = make_agent([calls, "bitti"], workdir, mode="auto")
    agent.run_turn("commit")
    assert seen["max"] == 1 and seen["order"] == ["git add -A", "git commit -m x"]
    run = shell.RunCommand()
    assert run.exclusive_key({"command": "pip install x"}) == run.exclusive_key({"command": "cd a; npm install"}) != ""
    assert run.exclusive_key({"command": "python -m pip install x"}) and not run.exclusive_key({"command": "python x.py"})

    # with the setting off, commands still run one at a time
    seen.update(active=0, max=0, order=[])
    calls = {"content": "", "tool_calls": [_cmd("a", "python a.py"), _cmd("b", "python b.py")]}
    agent, _, ui = make_agent([calls, "bitti"], workdir, mode="auto")
    agent.config.data.setdefault("agent", {})["parallel_commands"] = False
    assert not run.parallel_for(agent.ctx, {"command": "python a.py"})
    agent.run_turn("x")
    assert seen["max"] == 1


def test_background_logs_never_share_a_file(workdir, monkeypatch):
    import time

    from alice.tools import shell

    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)  # same millisecond
    monkeypatch.setattr(time, "sleep", lambda s: None)
    agent, _, _ = make_agent(["x"], workdir)
    run = shell.RunCommand()
    a = run.run(agent.ctx, "echo bir", background=True).text
    b = run.run(agent.ctx, "echo iki", background=True).text
    log = lambda t: t.split("log: ")[1].splitlines()[0]  # noqa: E731
    assert log(a) != log(b)
    for p in list(agent.ctx.state["bg_procs"].values()):
        p.popen.wait(timeout=20)
        p.log.unlink(missing_ok=True)


def test_powershell_error_hints():
    from alice.tools.shell import _ps_hint

    assert "${ad}" in _ps_hint('"$p: $r"', "Variable reference is not valid. ':' was not followed by a valid "
                                           "variable name character.")
    assert "@{" in _ps_hint("$m = @@{ 'a' = 1 }", "Unrecognized token in source text.")
    assert "write_file" in _ps_hint('& python.exe -c "print(d.get(1))"', "SyntaxError: f-string expression")
    assert _ps_hint("git status", "fatal: not a git repository") == ""
