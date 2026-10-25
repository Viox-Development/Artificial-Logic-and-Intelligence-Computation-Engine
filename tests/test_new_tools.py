"""grep modları, apply_patch, file_ops, PDF/ipynb okuma."""

from __future__ import annotations

import json

import pytest

from alice.tools import ToolContext, groups
from alice.tools import files as files_mod
from alice.tools.base import DANGER, WRITE, ToolError
from alice.tools.files import FileOps, Grep, ReadFile
from alice.tools.patch import ApplyPatch, parse_patch
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), state={"checkpoint": {}})


@pytest.fixture(params=["rg", "python"])
def grep_ctx(request, ctx, workdir, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(files_mod.shutil, "which", lambda name: None)
    elif files_mod.shutil.which("rg") is None:
        pytest.skip("ripgrep yok")
    (workdir / "src").mkdir()
    (workdir / "src" / "a.py").write_text("import os\n\ndef foo():\n    return 1\n\ndef bar():\n    return 2\n")
    (workdir / "src" / "b.ts").write_text("function foo() {}\n")
    (workdir / "notes.txt").write_text("foo bar\n")
    return ctx


# ------------------------------------------------------------------ grep


def test_grep_files_with_matches_and_count(grep_ctx):
    out = Grep().run(grep_ctx, pattern="foo", output_mode="files_with_matches").text.replace("\\", "/")
    assert sorted(out.splitlines()) == ["notes.txt", "src/a.py", "src/b.ts"]
    out = Grep().run(grep_ctx, pattern=r"def \w+", output_mode="count").text.replace("\\", "/")
    assert out.strip() == "src/a.py:2"


def test_grep_context_lines(grep_ctx):
    out = Grep().run(grep_ctx, pattern="def bar", path="src/a.py", context=1).text
    lines = out.splitlines()
    assert any(ln.endswith("6:def bar():") for ln in lines)
    assert any(ln.endswith("7-    return 2") for ln in lines)
    assert any(ln.endswith("5-") for ln in lines)


def test_grep_multiline(grep_ctx):
    out = Grep().run(grep_ctx, pattern=r"def foo\(\):\n\s+return 1", multiline=True, glob="*.py").text
    assert "3:def foo():" in out.replace("\\", "/")
    # '\n' içeren desen kendiliğinden çok satırlı aranır
    assert "a.py" in Grep().run(grep_ctx, pattern=r"foo\(\):\n\s+return", glob="*.py").text


def test_grep_multiline_crlf(grep_ctx, workdir):
    (workdir / "src" / "w.py").write_bytes(b"def baz():\r\n    return 3\r\n")
    out = Grep().run(grep_ctx, pattern=r"baz\(\):\n\s+return 3", glob="*.py").text
    assert "w.py" in out and "1:def baz():" in out.replace("\\", "/")


def test_grep_brace_glob_and_offset(grep_ctx):
    out = Grep().run(grep_ctx, pattern="foo", glob="*.{py,ts}", output_mode="files_with_matches").text
    assert "notes.txt" not in out and "a.py" in out and "b.ts" in out
    page = Grep().run(grep_ctx, pattern="foo|bar|return", path="src/a.py", max_results=2).text
    assert "offset=2" in page
    rest = Grep().run(grep_ctx, pattern="foo|bar|return", path="src/a.py", max_results=2, offset=2).text
    assert "offset=" not in rest and len(rest.splitlines()) == 2


def test_grep_missing_path(ctx):
    with pytest.raises(ToolError, match="yol yok"):
        Grep().run(ctx, pattern="x", path="yok")


# ------------------------------------------------------------------ apply_patch

PATCH = """*** Begin Patch
*** Update File: src/a.py
@@ def foo():
-    return 1
+    return 10
*** Add File: src/new.py
+X = 1
+Y = 2
*** Delete File: old.txt
*** End Patch"""


def test_apply_patch_add_update_delete(ctx, workdir):
    (workdir / "src").mkdir()
    (workdir / "src" / "a.py").write_text("def foo():\n    return 1\n\ndef bar():\n    return 1\n")
    (workdir / "old.txt").write_text("eski\n")
    r = ApplyPatch().run(ctx, patch="```\n" + PATCH + "\n```")
    assert "M src/a.py" in r.text and "A src/new.py" in r.text and "D old.txt" in r.text
    assert (workdir / "src" / "a.py").read_text() == "def foo():\n    return 10\n\ndef bar():\n    return 1\n"
    assert (workdir / "src" / "new.py").read_text() == "X = 1\nY = 2\n"
    assert not (workdir / "old.txt").exists()
    assert "+    return 10" in r.diff
    assert str(workdir / "old.txt") in ctx.state["checkpoint"]


def test_apply_patch_is_all_or_nothing(ctx, workdir):
    (workdir / "a.txt").write_text("bir\niki\n")
    patch = ("*** Begin Patch\n*** Update File: a.txt\n-bir\n+BİR\n*** Update File: a.txt\n-yok\n+x\n"
             "*** End Patch")
    with pytest.raises(ToolError, match="bulunamadı"):
        ApplyPatch().run(ctx, patch=patch)
    assert (workdir / "a.txt").read_text() == "bir\niki\n"


def test_apply_patch_whitespace_tolerance_crlf_and_move(ctx, workdir):
    (workdir / "w.cs").write_bytes(b"class A {\r\n    int x;  \r\n}\r\n")
    patch = ("*** Begin Patch\n*** Update File: w.cs\n*** Move to: lib/W.cs\n class A {\n-    int x;\n+    int y;\n }\n"
             "*** End Patch")
    r = ApplyPatch().run(ctx, patch=patch)
    assert "R w.cs -> lib/W.cs" in r.text
    assert not (workdir / "w.cs").exists()
    assert (workdir / "lib" / "W.cs").read_bytes() == b"class A {\r\n    int y;\r\n}\r\n"


def test_apply_patch_rejects_bad_input(ctx, workdir):
    with pytest.raises(ToolError, match="Begin Patch"):
        parse_patch("merhaba")
    with pytest.raises(ToolError, match="'\\+' ile"):
        parse_patch("*** Begin Patch\n*** Add File: a\nx\n*** End Patch")
    (workdir / "var.txt").write_text("x")
    with pytest.raises(ToolError, match="zaten var"):
        ApplyPatch().run(ctx, patch="*** Begin Patch\n*** Add File: var.txt\n+y\n*** End Patch")
    assert ApplyPatch().summary({"patch": PATCH}) == "update src/a.py, add src/new.py, delete old.txt"


# ------------------------------------------------------------------ file_ops


def test_file_ops_move_copy_mkdir(ctx, workdir):
    (workdir / "a.txt").write_text("a")
    (workdir / "b.txt").write_text("b")
    t = FileOps()
    t.run(ctx, action="mkdir", path="arsiv/2026")
    assert (workdir / "arsiv" / "2026").is_dir()
    t.run(ctx, action="copy", path="a.txt", destination="kopya.txt")
    assert (workdir / "kopya.txt").read_text() == "a"
    t.run(ctx, action="move", paths=["a.txt", "b.txt"], destination="arsiv/2026")
    assert (workdir / "arsiv" / "2026" / "b.txt").read_text() == "b" and not (workdir / "a.txt").exists()
    with pytest.raises(ToolError, match="zaten var"):
        t.run(ctx, action="copy", path="kopya.txt", destination="arsiv/2026/a.txt")
    t.run(ctx, action="copy", path="kopya.txt", destination="arsiv/2026/a.txt", overwrite=True)
    t.run(ctx, action="copy", path="arsiv", destination="yedek")
    assert (workdir / "yedek" / "2026" / "a.txt").read_text() == "a"
    with pytest.raises(ToolError, match="kendi içine"):
        t.run(ctx, action="move", path="arsiv", destination="arsiv/2026")


def test_file_ops_delete_goes_to_trash(ctx, workdir, isolated_home):
    (workdir / "sil.txt").write_text("veri")
    (workdir / "klasor").mkdir()
    (workdir / "klasor" / "x").write_text("x")
    r = FileOps().run(ctx, action="delete", paths=["sil.txt", "klasor"])
    assert not (workdir / "sil.txt").exists() and not (workdir / "klasor").exists()
    trashed = list((isolated_home / "trash").rglob("sil.txt"))
    assert trashed and trashed[0].read_text() == "veri" and "çöpe" in r.text
    assert ctx.state["checkpoint"][str(workdir / "sil.txt")] == b"veri"
    with pytest.raises(ToolError, match="yok"):
        FileOps().run(ctx, action="delete", path="sil.txt")


def test_file_ops_risk(ctx):
    t = FileOps()
    assert t.risk_for({"action": "move", "path": "a", "destination": "b"}) == WRITE
    sys_dir = "C:\\Windows\\x" if __import__("sys").platform == "win32" else "/etc/x"
    assert t.risk_for({"action": "copy", "path": "a", "destination": sys_dir}) == DANGER
    assert t.risk_for({"action": "delete", "paths": [f"f{i}" for i in range(30)]}) == DANGER


# ------------------------------------------------------------------ read_file: PDF ve ipynb


def _pdf(texts: list[str]) -> bytes:
    """Metin içeren en küçük geçerli PDF (sayfa başına bir satır)."""
    n = len(texts)
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(n)) + f"] /Count {n} >>"]
    font = 3 + 2 * n
    for i, t in enumerate(texts):
        stream = f"BT /F1 12 Tf 72 720 Td ({t}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for k, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{off:010d} 00000 n \n" for off in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_read_pdf_pages(ctx, workdir):
    pytest.importorskip("pypdf")
    (workdir / "d.pdf").write_bytes(_pdf(["Birinci sayfa", "Ikinci sayfa", "Ucuncu sayfa"]))
    out = ReadFile().run(ctx, path="d.pdf").text
    assert "sayfa 1/3" in out and "Birinci sayfa" in out and "Ucuncu sayfa" in out
    out = ReadFile().run(ctx, path="d.pdf", pages="2").text
    assert "Ikinci sayfa" in out and "Birinci" not in out
    with pytest.raises(ToolError, match="aralık dışında"):
        ReadFile().run(ctx, path="d.pdf", pages="9")


def test_read_ipynb(ctx, workdir):
    nb = {"cells": [{"cell_type": "markdown", "source": ["# Başlık"]},
                    {"cell_type": "code", "source": ["print(1)\n", "x = 2"],
                     "outputs": [{"output_type": "stream", "text": ["1\n"]}]}]}
    (workdir / "n.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    out = ReadFile().run(ctx, path="n.ipynb").text
    assert "hücre 1 [markdown]" in out and "print(1)" in out and "# çıktı:" in out and "# 1" in out


# ------------------------------------------------------------------ gruplar


def test_new_tools_are_visible():
    assert groups.group_of("apply_patch") == groups.group_of("file_ops") == "files"  # adıyla listede, gerekince yüklenir
    assert "files" in groups.groups_for_text("bu dosyayı masaüstüne taşı")
    assert groups.group_of("http_request") == "net"
    assert "net" in groups.groups_for_text("şu API'ye post isteği at")
    assert "net" in groups.groups_for_text("bu dosyayı indir")


def test_requests_are_small_and_tools_load_on_demand(tmp_path):
    """Her istekte sadece temel araçların tanımı gider; diğerleri adıyla listelenir, sözle ya da load_tools ile açılır."""
    from alice.agent import Agent
    from alice.permissions import Permissions
    from alice.providers.router import estimate_tokens
    from alice.tools import ToolContext, all_tools
    from tests.conftest import make_config

    cfg = make_config({"brain": ["a:m"]})
    ctx = ToolContext(cwd=tmp_path, config=cfg)
    agent = Agent(cfg, None, all_tools(cfg), ctx, Permissions("auto"))
    agent.sync_catalog()
    specs = agent.tool_specs()
    assert {s["function"]["name"] for s in specs} >= {"read_file", "run_command", "memory", "load_tools"}
    assert "youtube" not in {s["function"]["name"] for s in specs}
    assert estimate_tokens([], specs) < 6000  # önceden ~9.200
    system = agent.messages[0]["content"]
    assert "# Yüklenebilir araçlar" in system and "youtube" in system and "file_ops" in system
    agent.sync_catalog()
    assert agent.messages[0]["content"].count("# Yüklenebilir araçlar") == 1  # tekrar eklenmez
    agent._enable_groups_for("YouTube'da lofi aç")
    assert "youtube" in {s["function"]["name"] for s in agent.tool_specs()}


def test_old_tool_outputs_are_shortened(tmp_path):
    from alice.agent import Agent
    from alice.permissions import Permissions
    from alice.tools import ToolContext
    from tests.conftest import make_config

    cfg = make_config({"brain": ["a:m"]})
    agent = Agent(cfg, None, [], ToolContext(cwd=tmp_path, config=cfg), Permissions("auto"))
    for i in range(8):
        agent.messages.append({"role": "tool", "tool_call_id": str(i), "name": "read_file", "content": f"{i}" * 5000})
    agent._shrink_old_outputs()
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert all(len(m["content"]) < 1500 and "kısaltıldı" in m["content"] for m in tools[:2])
    assert all(len(m["content"]) == 5000 for m in tools[2:])  # son altı olduğu gibi
    assert tools[0]["tool_call_id"] == "0"  # araç çağrısıyla bağı kopmaz
    agent._shrink_old_outputs()
    assert len(tools[0]["content"]) == len(agent.messages[1]["content"])  # tekrar kısaltılmaz
