"""monitor (return to the chat when an event happens) and session (start a separate session / hand over)."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from alice.tools.base import ToolContext, ToolError
from alice.tools.monitor import Monitor
from tests.conftest import make_config


def ctx_with_inbox(workdir):
    got: list[str] = []
    ev = threading.Event()
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    ctx.state["deliver"] = lambda text: (got.append(text), ev.set())
    return ctx, got, ev


def test_file_monitor_reports_matching_new_lines_once(workdir):
    ctx, got, ev = ctx_with_inbox(workdir)
    log = workdir / "app.log"
    log.write_text("eski ERROR satırı\n", encoding="utf-8")  # lines written earlier are not counted
    out = Monitor().run(ctx, "start", kind="file", target=str(log), pattern="error", note="hatayı düzelt")
    assert "İzleme #" in out.text
    with open(log, "a", encoding="utf-8") as f:
        f.write("bilgi\nERROR: veritabanı yok\n")
    assert ev.wait(10)
    assert "veritabanı yok" in got[0] and "eski" not in got[0] and "hatayı düzelt" in got[0]
    assert "çalışan izleme yok" in Monitor().run(ctx, "list").text


def test_process_monitor_reports_exit(workdir):
    import subprocess

    from alice.tools.shell import BgProc

    ctx, got, ev = ctx_with_inbox(workdir)
    log = workdir / "p.log"
    with open(log, "wb") as f:
        p = subprocess.Popen([sys.executable, "-c", "print('derleme tamam')"], stdout=f, stderr=subprocess.STDOUT)
    ctx.state["bg_procs"] = {7: BgProc(7, "derle", p, log, time.time())}
    Monitor().run(ctx, "start", kind="process", target="7")
    assert ev.wait(10)
    assert "süreç bitti" in got[0] and "derleme tamam" in got[0]


def test_monitor_validation_and_stop(workdir):
    ctx, _, _ = ctx_with_inbox(workdir)
    with pytest.raises(ToolError, match="pattern"):
        Monitor().run(ctx, "start", kind="file", target=str(workdir / "x.log"))
    with pytest.raises(ToolError, match="arka plan süreci yok"):
        Monitor().run(ctx, "start", kind="process", target="99")
    Monitor().run(ctx, "start", kind="file", target=str(workdir / "y.log"), pattern="x")
    w = next(iter(ctx.state["monitors"].values()))
    assert "durduruldu" in Monitor().run(ctx, "stop", id=w.id).text and w.stop.is_set()


def test_fork_drops_the_unanswered_call_that_started_the_session():
    from alice.app.server import fork_messages

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "iş"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "içerik"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "function": {"name": "session"}}]}]
    out = fork_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool"]


def test_session_runs_separately_and_reports_back(monkeypatch, workdir):
    from alice.agent import Agent
    from alice.app.server import AppState

    state = AppState(make_config({"brain": ["a:m"], "fast": ["a:m"]}), workdir, "auto", provider_factory=lambda pc: None)
    parent = state.chat
    reported = threading.Event()
    seen: dict[str, list[str]] = {}

    def fake_turn(self, prompt, images=None, max_steps=None):
        seen.setdefault(self.ctx.state.get("session_id"), []).append(prompt)
        self.messages.append({"role": "user", "content": prompt})
        self.messages.append({"role": "assistant", "content": f"bitti: {prompt[:20]}"})
        if prompt.startswith("[Oturum bitti]"):
            reported.set()
        return "ok"

    monkeypatch.setattr(Agent, "run_turn", fake_turn)
    api = parent.ctx.state["sessions"]
    sid, err = api.start("README'yi Türkçeye çevir", "Çeviri", fork=False)
    assert sid and not err
    assert reported.wait(10)
    assert seen[sid] == ["README'yi Türkçeye çevir"]
    back = seen[parent.sid][-1]
    assert back.startswith("[Oturum bitti] 'Çeviri'") and "bitti: README" in back
    assert api.linked()[0]["id"] == sid
    assert "böyle bir oturum yok" in api.message("baska", "x")
    state.shutdown()
