"""Focused tests that pin down fixes for concrete bugs in the tools."""

from __future__ import annotations

import pytest

from alice.tools import ToolContext
from alice.tools.files import EditFile, ReadFile, mark_read
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), state={"checkpoint": {}})


def _edit(ctx, name, old, new):
    ReadFile().run(ctx, path=name)
    return EditFile().run(ctx, path=name, old_string=old, new_string=new)


def test_edit_keeps_utf8_bom(ctx, workdir):
    # Windows PowerShell 5.1 reads a UTF-8 script without a BOM as ANSI: editing must not drop the BOM
    f = workdir / "s.ps1"
    f.write_bytes("﻿Write-Host 'çalıştı'\n".encode("utf-8"))
    _edit(ctx, "s.ps1", "çalıştı", "bitti ğ")
    assert f.read_bytes() == "﻿Write-Host 'bitti ğ'\n".encode("utf-8")


def test_edit_keeps_cp1254(ctx, workdir):
    f = workdir / "a.bat"
    f.write_bytes("echo şişe\r\n".encode("cp1254"))
    _edit(ctx, "a.bat", "şişe", "çiçek")
    assert f.read_bytes() == "echo çiçek\r\n".encode("cp1254")


def test_edit_cp1254_falls_back_to_utf8_for_unencodable(ctx, workdir):
    f = workdir / "a.txt"
    f.write_bytes("şişe\n".encode("cp1254"))
    _edit(ctx, "a.txt", "şişe", "日本")
    assert f.read_bytes().decode("utf-8") == "日本\n"


def test_edit_in_crlf_file_inserts_crlf_lines(ctx, workdir):
    f = workdir / "w.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    _edit(ctx, "w.txt", "b", "b1\nb2")
    assert f.read_bytes() == b"a\r\nb1\r\nb2\r\nc\r\n"


def test_edit_in_lf_file_left_alone(ctx, workdir):
    f = workdir / "w.txt"
    f.write_bytes(b"a\nb\n")
    _edit(ctx, "w.txt", "b", "b1\nb2")
    assert f.read_bytes() == b"a\nb1\nb2\n"


def test_read_offset_past_end_is_error_not_empty(ctx, workdir):
    from alice.tools.base import ToolError

    (workdir / "r.txt").write_text("a\nb\nc\n", encoding="utf-8")
    with pytest.raises(ToolError, match="3 satır"):
        ReadFile().run(ctx, path="r.txt", offset=50)
    (workdir / "e.txt").write_text("", encoding="utf-8")
    assert "boş dosya" in ReadFile().run(ctx, path="e.txt").text


def _case_insensitive_fs(path) -> bool:
    probe = path / "CaseProbe.tmp"
    probe.write_text("x")
    try:
        return (path / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_file_ops_case_only_rename(ctx, workdir):
    from alice.tools.files import FileOps

    if not _case_insensitive_fs(workdir):
        pytest.skip("büyük/küçük harf duyarlı dosya sistemi")
    (workdir / "rapor.txt").write_text("içerik", encoding="utf-8")
    FileOps().run(ctx, action="move", path="rapor.txt", destination="Rapor.txt")
    assert [p.name for p in workdir.iterdir()] == ["Rapor.txt"]
    assert (workdir / "Rapor.txt").read_text(encoding="utf-8") == "içerik"
    (workdir / "proje").mkdir()
    (workdir / "proje" / "a.txt").write_text("a")
    FileOps().run(ctx, action="move", path="proje", destination="Proje")
    assert sorted(p.name for p in workdir.iterdir()) == ["Proje", "Rapor.txt"]
    assert (workdir / "Proje" / "a.txt").read_text() == "a"


def test_apply_patch_two_updates_same_file_both_kept(ctx, workdir):
    from alice.tools.patch import ApplyPatch

    (workdir / "a.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    patch = ("*** Begin Patch\n*** Update File: a.py\n-x = 1\n+x = 10\n"
             "*** Update File: a.py\n-z = 3\n+z = 30\n*** End Patch")
    mark_read(ctx, workdir / "a.py")
    ApplyPatch().run(ctx, patch=patch)
    assert (workdir / "a.py").read_text(encoding="utf-8") == "x = 10\ny = 2\nz = 30\n"


def test_apply_patch_keeps_encoding_and_marks_read(ctx, workdir):
    from alice.tools.patch import ApplyPatch

    f = workdir / "s.ps1"
    f.write_bytes("﻿Write-Host 'ç'\r\n".encode("utf-8"))
    g = workdir / "u.txt"
    g.write_bytes("şey\r\n".encode("utf-16"))
    patch = ("*** Begin Patch\n*** Update File: s.ps1\n-Write-Host 'ç'\n+Write-Host 'ğ'\n"
             "*** Update File: u.txt\n*** Move to: v.txt\n-şey\n+öz\n*** End Patch")
    mark_read(ctx, f)
    mark_read(ctx, g)
    ApplyPatch().run(ctx, patch=patch)
    assert f.read_bytes() == "﻿Write-Host 'ğ'\r\n".encode("utf-8")
    assert not g.exists() and (workdir / "v.txt").read_bytes() == "öz\r\n".encode("utf-16")
    # a file written by the patch must be editable with edit_file right away
    EditFile().run(ctx, path="s.ps1", old_string="ğ", new_string="ü")


def test_apply_patch_add_then_delete_same_file(ctx, workdir):
    from alice.tools.patch import ApplyPatch

    patch = "*** Begin Patch\n*** Add File: t.txt\n+a\n*** Delete File: t.txt\n*** End Patch"
    ApplyPatch().run(ctx, patch=patch)
    assert not (workdir / "t.txt").exists()


def test_process_wait_sees_last_line_written_just_before_exit(ctx, workdir):
    from alice.tools.shell import BgProc, ProcessWait

    log = workdir / "bg.log"
    log.write_bytes(b"basliyor\n")

    class Pop:
        returncode = None

        def poll(self):
            # the process writes its last line and exits right away (between the read and the poll)
            if self.returncode is None:
                with open(log, "ab") as fh:
                    fh.write(b"ERROR boom\n")
                self.returncode = 1
            return self.returncode

    ctx.state["bg_procs"] = {7: BgProc(7, "x", Pop(), log, 0.0)}
    r = ProcessWait().run(ctx, pid=7, pattern="error", timeout=5)
    assert "desen göründü" in r.text and "ERROR boom" in r.text


def test_moved_downloads_folder_is_found(tmp_path, monkeypatch):
    from pathlib import Path

    from alice.tools import fetch_file, files, findfiles

    moved = tmp_path / "D" / "Indirmeler"
    moved.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(fetch_file, "downloads_dir", lambda: moved)
    assert files.known_folder("İndirilenler") == moved
    assert findfiles.downloads_dir() == moved


def test_tidy_plan_never_targets_same_destination_twice(tmp_path):
    import os
    import time

    from alice.tools import findfiles as ff

    dl = tmp_path / "Downloads"
    (dl / "Belgeler").mkdir(parents=True)
    (dl / "Belgeler" / "x.pdf").write_bytes(b"eski")
    old = time.time() - 3 * 86400
    for name in ("x.pdf", "x (1).pdf"):
        (dl / name).write_bytes(name.encode())
        os.utime(dl / name, (old, old))
    dests = [d for _, d in ff.tidy_plan(dl)]
    assert len(dests) == len(set(dests)) == 2
