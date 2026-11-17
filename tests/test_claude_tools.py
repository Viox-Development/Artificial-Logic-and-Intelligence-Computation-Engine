"""ALICE counterparts of Claude Code tool behaviors: no editing without reading, process_wait, send_file,
exit_plan_mode (see docs/TOOL-STYLE.md)."""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace as NS

import pytest

from alice.permissions import Permissions
from alice.tools import ToolContext
from alice.tools.base import ToolError
from alice.tools.files import EditFile, MultiEdit, ReadFile, WriteFile
from alice.tools.meta import SENT_FILES, ExitPlanMode, SendFile
from alice.tools.shell import ProcessWait, RunCommand
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_edit_requires_read_and_fresh_copy(ctx, workdir):
    f = workdir / "a.txt"
    f.write_text("bir\n", encoding="utf-8")
    with pytest.raises(ToolError, match="okunmadı"):
        EditFile().run(ctx, path="a.txt", old_string="bir", new_string="iki")
    with pytest.raises(ToolError, match="okunmadı"):
        WriteFile().run(ctx, path="a.txt", content="x")
    with pytest.raises(ToolError, match="okunmadı"):
        MultiEdit().run(ctx, path="a.txt", edits=[{"old_string": "bir", "new_string": "iki"}])
    ReadFile().run(ctx, path="a.txt")
    EditFile().run(ctx, path="a.txt", old_string="bir", new_string="iki")
    EditFile().run(ctx, path="a.txt", old_string="iki", new_string="üç")  # it knows what it wrote itself: no re-read
    time.sleep(0.02)
    f.write_text("başkası değiştirdi\n", encoding="utf-8")
    import os

    os.utime(f, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    with pytest.raises(ToolError, match="değişti"):
        EditFile().run(ctx, path="a.txt", old_string="başkası", new_string="ben")


def test_new_file_needs_no_read_and_guard_can_be_disabled(ctx, workdir):
    WriteFile().run(ctx, path="yeni.txt", content="x")  # new file: nothing to read
    (workdir / "b.txt").write_text("b", encoding="utf-8")
    ctx.config.data.setdefault("agent", {})["require_read_before_edit"] = False
    WriteFile().run(ctx, path="b.txt", content="c")
    assert (workdir / "b.txt").read_text(encoding="utf-8") == "c"


PY = f'& "{sys.executable}"' if sys.platform == "win32" else f'"{sys.executable}"'  # PowerShell: & is needed for a quoted exe


def test_process_wait_returns_when_pattern_appears(ctx):
    cmd = f'{PY} -c "import time; print(\'hazirlaniyor\', flush=True); time.sleep(3); ' \
          f'print(\'READY on 8080\', flush=True); time.sleep(30)"'
    ctx.config.data.setdefault("agent", {})["sandbox"] = False
    out = RunCommand().run(ctx, command=cmd, background=True).text
    pid = int(out.split("pid=")[1].split()[0].strip(",):"))
    try:
        r = ProcessWait().run(ctx, pid=pid, pattern="ready|error", timeout=20)
        assert "READY on 8080" in r.text and not r.is_error
    finally:
        from alice.tools.shell import ProcessStop

        ProcessStop().run(ctx, pid=pid)


def test_process_wait_reports_exit(ctx):
    ctx.config.data.setdefault("agent", {})["sandbox"] = False
    out = RunCommand().run(ctx, command=f'{PY} -c "print(1)"', background=True).text
    pid = int(out.split("pid=")[1].split()[0].strip(",):"))
    r = ProcessWait().run(ctx, pid=pid, timeout=20)
    assert "bitti (kod 0)" in r.text


def test_send_file_shows_cards(ctx, workdir):
    (workdir / "rapor.txt").write_text("sonuç", encoding="utf-8")
    r = SendFile().run(ctx, paths=["rapor.txt", "yok.txt"], caption="Rapor hazır")
    assert r.files[0]["name"] == "rapor.txt" and r.files[0]["size"] == len("sonuç".encode()) and "Bulunamadı" in r.text
    assert str(workdir / "rapor.txt") in SENT_FILES or str((workdir / "rapor.txt").resolve()) in SENT_FILES
    with pytest.raises(ToolError):
        SendFile().run(ctx, paths=["yok.txt"])


class ChoiceUI:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def ask_choice(self, question, options, multi=False):
        self.asked.append((question, options))
        return self.answer


def test_exit_plan_mode_restores_previous_mode_on_approval(ctx):
    perms = Permissions("ask")
    perms.mode = "plan"
    ui = ChoiceUI("Onayla, uygula")
    ctx.state["agent"] = NS(permissions=perms, ui=ui)
    r = ExitPlanMode().run(ctx, plan="1. a.txt'yi düzenle\n2. testleri çalıştır")
    assert perms.mode == "ask" and "onayladı" in r.text and "a.txt" in ui.asked[0][0]


def test_exit_plan_mode_rejected_keeps_plan(ctx):
    perms = Permissions("auto")
    perms.mode = "plan"
    ctx.state["agent"] = NS(permissions=perms, ui=ChoiceUI("Önce testleri yaz"))
    r = ExitPlanMode().run(ctx, plan="plan")
    assert perms.mode == "plan" and "Önce testleri yaz" in r.text
    perms.mode = "auto"
    with pytest.raises(ToolError, match="plan modunda değilsin"):
        ExitPlanMode().run(ctx, plan="plan")
