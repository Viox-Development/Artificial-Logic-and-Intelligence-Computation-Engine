"""Robustness: a single error must not permanently break the voice assistant, the core, or the chat history."""

from __future__ import annotations

import time

import pytest

from alice.computer_agent import ComputerAgent, normalize_action
from alice.tools import ToolContext
from alice.tools.files import Glob, glob_regex
from alice.voice.audio import Audio
from alice.voice.listen import is_yes
from alice.voice.stt import STTError, STTRouter
from alice.voice.tts import SpeechCache, TTSRouter
from tests.conftest import make_config, tool_call
from tests.test_agent import make_agent
from tests.test_computer_agent import FakeScreen
from tests.test_voice import FakeAgent, FakeSpeaker, FakeTTS, tone, wait_for


# --------------------------------------------------------------- voice confirmation
@pytest.mark.parametrize("text,yes", [
    ("evet", True), ("Tamamdır, yap.", True), ("peki", True), ("onaylıyorum", True),
    ("yapma", False), ("hayır, yapma", False), ("onaylama", False), ("dur bekle", False), ("", False),
])
def test_is_yes_is_word_based(text, yes):
    assert is_yes(text) is yes


def test_ask_yes_no_ignores_stale_answer():
    from alice.voice.assistant import VoiceAssistant

    va = VoiceAssistant(FakeAgent(), FakeSpeaker(), stt=None)
    va._answers.put("evet")  # left over from the previous question
    assert va.ask_yes_no("Silinsin mi?", timeout=0.2) is False


# --------------------------------------------------------- voice worker thread
def test_voice_worker_survives_unexpected_error():
    from alice.voice.assistant import VoiceAssistant

    class Boom(FakeAgent):
        def run_turn(self, text):
            if not self.turns:
                self.turns.append("boom")
                raise RuntimeError("CUDA out of memory")
            return super().run_turn(text)

    agent = Boom()
    va = VoiceAssistant(agent, FakeSpeaker(), stt=None)
    va.handle_text("Hey Alice, bir", direct=True)
    va.handle_text("Hey Alice, iki", direct=True)
    assert wait_for(lambda: "iki" in agent.turns)


def test_voice_worker_thread_survives_crash_outside_turn(monkeypatch):
    from alice.voice import assistant as mod

    agent = FakeAgent()
    va = mod.VoiceAssistant(agent, FakeSpeaker(), stt=None)
    calls = []
    real = mod.trim_history

    def flaky(msgs, keep):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("bozuk geçmiş")
        return real(msgs, keep)

    monkeypatch.setattr(mod, "trim_history", flaky)
    agent.messages = [{"role": "system", "content": "s"}]
    va.handle_text("Hey Alice, bir", direct=True)
    va.handle_text("Hey Alice, iki", direct=True)
    assert wait_for(lambda: agent.turns == ["iki"])


def test_stt_router_falls_back_on_any_engine_error():
    class Cuda:
        name = "local"

        def transcribe(self, a):
            raise RuntimeError("CUDA out of memory")

    class Good:
        name = "groq"

        def transcribe(self, a):
            return "merhaba"

    r = STTRouter([Cuda(), Good()])
    assert r.transcribe(tone()) == "merhaba"
    with pytest.raises(STTError, match="RuntimeError"):
        STTRouter([Cuda()]).transcribe(tone())


def test_wake_gate_recovers_after_single_error():
    from alice.voice.stt import WakeGate

    class Model:
        def __init__(self):
            self.n = 0

        def transcribe(self, *a, **k):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("CUDA out of memory")
            return iter([type("S", (), {"text": "Hey Alice, saat kaç?", "avg_logprob": -0.2})()]), None

    model = Model()
    gate = WakeGate()
    gate.available = lambda: True
    gate._load = lambda: model
    gate._model = model
    assert gate.heard(tone(1.0)) is None
    assert gate.heard(tone(1.0)) == "Hey Alice, saat kaç?"  # did not stay disabled permanently


def test_tts_router_falls_back_on_any_engine_error():
    class Oom(FakeTTS):
        def synth(self, text, emotion="calm"):
            self.calls.append(text)
            raise RuntimeError("CUDA out of memory")

    local, cloud = Oom("local"), FakeTTS("cloud")
    r = TTSRouter(cloud, local, prefer="local", policy=lambda: {"local_gpu": True})
    r.synth("merhaba")
    assert r.last == "cloud"
    r.synth("tekrar")
    assert len(local.calls) == 1  # the broken engine is kept on hold


def test_corrupt_speech_cache_file_is_replaced(tmp_path):
    cache = SpeechCache(tmp_path)
    cache.put("cloud", "calm", "Hemen bakıyorum.", Audio(b"\x01\x00" * 100, 16000))
    f = cache._file("cloud", "calm", "Hemen bakıyorum.")
    f.write_bytes(b"RIFF\0\0\0\0WAVE")  # half-written
    assert cache.get("cloud", "calm", "Hemen bakıyorum.") is None
    assert not f.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_audio_out_death_stops_playing():
    from alice.voice.live import AudioOut

    out = AudioOut.__new__(AudioOut)
    out.q = __import__("queue").Queue()
    out._gen = 0
    out.busy_until = time.monotonic() + 100
    out._play = lambda: (_ for _ in ()).throw(OSError("aygıt yok"))
    out._run()
    assert out.playing is False and isinstance(out.dead, OSError)
    out.put(b"\0\0")
    assert out.q.empty()


# ------------------------------------------------------------- screen task
@pytest.mark.parametrize("raw,want", [
    ({"key": "ctrl+f"}, {"action": "key", "keys": "ctrl+f"}),
    ({"enter": True, "type": "stalk"}, {"action": "type", "text": "stalk", "enter": True}),
    ({"type": "click", "id": 3}, {"action": "click", "id": 3}),
    ({"action": "key", "key": "enter"}, {"action": "key", "keys": "enter"}),
    ({"target": "Discord"}, {"action": "open", "target": "Discord"}),
    ({"point": [10, 20]}, {"action": "click", "point": [10, 20]}),
    ({"seconds": 2}, {"action": "wait", "seconds": 2}),
])
def test_normalize_action(raw, want):
    assert normalize_action(raw) == want


def test_agent_runs_actions_without_action_name():
    answers = iter(['{"actions": [{"key": "ctrl+f"}, {"enter": true, "type": "stalk"}]}',
                    '{"action": "done", "summary": "bulundu"}'])
    s = FakeScreen()
    res = ComputerAgent(s, lambda m: next(answers), settle=0).run("ara")
    assert res.status == "done"
    assert [a["action"] for a in s.acts] == ["key", "type"]


def test_computer_task_refuses_scripts():
    from alice.computer_agent import Screen

    with pytest.raises(ValueError):
        Screen(None).act({"action": "open", "target": r"C:\Users\x\Downloads\setup.bat"}, None)


# --------------------------------------------------------------------- glob
@pytest.mark.parametrize("pat,path,ok", [
    ("**/*.py", "a.py", True), ("**/*.py", "x/y/a.py", True), ("*.py", "x/a.py", False),
    ("src/**/*.cs", "src/a.cs", True), ("src/**/*.cs", "src/b/c/a.cs", True), ("src/**/*.cs", "lib/a.cs", False),
    ("./*.txt", "a.txt", True), ("d?ta/[ab].json", "data/a.json", True),
])
def test_glob_regex(pat, path, ok):
    assert bool(glob_regex(pat).match(path)) is ok


def test_glob_skips_heavy_dirs_and_respects_deadline(workdir, monkeypatch):
    from alice.tools import files

    (workdir / "src" / "deep").mkdir(parents=True)
    (workdir / "src" / "deep" / "a.py").write_text("", encoding="utf-8")
    (workdir / "node_modules" / "pkg").mkdir(parents=True)
    (workdir / "node_modules" / "pkg" / "b.py").write_text("", encoding="utf-8")
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = Glob().run(ctx, pattern="**/*.py").text
    assert "a.py" in out and "node_modules" not in out
    assert Glob().run(ctx, pattern="*.py").text == "eşleşme yok"  # derine inmez
    monkeypatch.setattr(files, "SEARCH_SECONDS", -1.0)
    res = Glob().run(ctx, pattern="**/*.py")
    assert res.is_error and "YARIDA KESİLDİ" in res.text


# ------------------------------------------------------------------ security
@pytest.mark.parametrize("cmd", [r"rm .\build -Recurse -Force", r"ri C:\x -r", r"del C:\proj -Recurse",
                                 r"Get-ChildItem C:\x | Remove-Item"])
def test_powershell_recursive_delete_is_dangerous(cmd):
    from alice.tools.shell import is_dangerous

    assert is_dangerous(cmd)


@pytest.mark.parametrize("cmd", ["git status", "rm file.txt", "python run.py -r", "dir | more"])
def test_safe_commands_stay_safe(cmd):
    from alice.tools.shell import is_dangerous

    assert not is_dangerous(cmd)


def test_open_script_needs_exec_and_cmd_meta_rejected(monkeypatch):
    from alice.tools import desktop
    from alice.tools.base import COMPUTER, EXEC, ToolError

    w = desktop.Windows()
    assert w.risk_for({"action": "open", "target": "C:/x/run.ps1"}) == EXEC
    assert w.risk_for({"action": "open", "target": "Discord"}) == COMPUTER
    monkeypatch.setattr(desktop, "IS_WIN", True)
    monkeypatch.setattr(desktop, "_start_menu_lookup", lambda n: None)
    monkeypatch.setattr(desktop, "match_start_app", lambda n: None)
    monkeypatch.setattr(desktop, "find_installed_apps", lambda n: [])
    monkeypatch.setattr(desktop.shutil, "which", lambda n: None)
    with pytest.raises(ToolError):
        desktop.open_app("x&calc")


def test_process_kill_by_pid_of_self_is_danger():
    import os

    from alice.tools.base import DANGER
    from alice.tools.system import ProcessKill

    assert ProcessKill().risk_for({"pid": os.getpid()}) == DANGER


# --------------------------------------------------------------------- agent
@pytest.mark.parametrize("args", [{"command": ["git", "status"]}, {"command": None}])
def test_bad_tool_args_return_error_and_keep_history_valid(workdir, args):
    script = [tool_call("run_command", args), "düzelttim"]
    agent, _, _ = make_agent(script, workdir)
    assert agent.run_turn("çalıştır") == "düzelttim"
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert tool_msgs and any(w in tool_msgs[0]["content"] for w in ("geçersiz", "eksik"))


def test_unexpected_error_repairs_tool_calls(workdir, monkeypatch):
    script = [tool_call("list_dir", {"path": "."})]
    agent, _, _ = make_agent(script, workdir)
    monkeypatch.setattr(agent, "_run_tools", lambda calls: (_ for _ in ()).throw(RuntimeError("beklenmeyen")))
    with pytest.raises(RuntimeError):
        agent.run_turn("listele")
    calls = [m for m in agent.messages if m.get("tool_calls")]
    answered = {m["tool_call_id"] for m in agent.messages if m["role"] == "tool"}
    assert {c["id"] for c in calls[-1]["tool_calls"]} <= answered


# --------------------------------------------------------------------- MCP
def test_mcp_reader_survives_garbage_lines():
    import io
    import threading

    from alice.mcp import MCPServer

    srv = MCPServer.__new__(MCPServer)
    ev, box = threading.Event(), {}
    srv._pending = {1: (ev, box)}
    srv._lock = threading.Lock()
    lines = ["Başlatıldı\n".encode("cp1254"), b"42\n", b'"ok"\n', b'{"jsonrpc":"2.0","id":1,"result":{"x":1}}\n']
    srv.proc = type("P", (), {"stdout": io.BytesIO(b"".join(lines))})()
    srv._reader()
    assert box.get("result") == {"x": 1}


# ------------------------------------------------------------------- settings
def test_broken_config_falls_back_to_defaults(tmp_path, monkeypatch, capsys):
    from alice.config import load_config

    p = tmp_path / "config.toml"
    p.write_text("[agent\nbozuk = ", encoding="utf-8")
    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    cfg = load_config(path=p)
    assert cfg.data and "okunamadı" in capsys.readouterr().err


def test_config_with_bom_loads(tmp_path, monkeypatch):
    from alice.config import load_config

    p = tmp_path / "config.toml"
    p.write_bytes("\ufeff[agent]\nmax_steps = 7\n".encode("utf-8"))
    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    assert load_config(path=p).data["agent"]["max_steps"] == 7


def test_unknown_min_severity_does_not_crash():
    from alice.core.modes import Policy

    assert Policy("quiet", min_severity="warning").allows("info")
