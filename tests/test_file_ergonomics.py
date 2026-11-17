"""File tool ergonomics: read_file symbol-focused reading, edit error hints, grep context/limit notes.

Symbol-focused reading lets the agent locate a function in a large file without reading it line by line;
error messages also explain what went wrong and show candidate lines, instead of saying "read the file again"."""

from __future__ import annotations

import pytest

from alice.tools import ToolContext
from alice.tools.base import ToolError
from alice.tools.files import EditFile, Grep, ReadFile
from tests.conftest import make_config

PY_SRC = '''import os


def alpha(x):
    """bir şey yapar"""
    y = x + 1
    return y


class Beta:
    def __init__(self):
        self.n = 0

    def run(self):
        return self.n + 1


def gamma():
    return "son"
'''

JS_SRC = """
function alpha(x) {
  return x + 1;
}

function beta(y) {
  const z = y * 2;
  return z;
}

function gamma() { return 3; }
"""


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), state={"checkpoint": {}})


def test_read_symbol_python_function(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    out = ReadFile().run(ctx, path="m.py", symbol="alpha").text
    assert "Sembol 'alpha'" in out
    assert "def alpha(x):" in out
    assert "return y" in out
    assert "class Beta" not in out and "def gamma" not in out


def test_read_symbol_python_class(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    out = ReadFile().run(ctx, path="m.py", symbol="Beta").text
    assert "class Beta:" in out and "return self.n + 1" in out and "def alpha" not in out


def test_read_symbol_js_braces(ctx, workdir):
    (workdir / "m.js").write_text(JS_SRC, encoding="utf-8")
    out = ReadFile().run(ctx, path="m.js", symbol="beta").text
    assert "function beta(y)" in out and "return z" in out and "alpha" not in out and "gamma" not in out


def test_read_symbol_unknown_raises(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    with pytest.raises(ToolError, match="bulunamadı"):
        ReadFile().run(ctx, path="m.py", symbol="yok_boyle_bir_sey")


def test_symbol_read_covers_exact_block_for_edit(ctx, workdir):
    """A block read by symbol gives content that can be copied verbatim into edit_file."""
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    out = ReadFile().run(ctx, path="m.py", symbol="gamma").text
    body = "\n".join(line.split("\t", 1)[1] for line in out.splitlines()[1:] if "\t" in line)
    assert "return \"son\"" in body
    EditFile().run(ctx, path="m.py", old_string='return "son"', new_string='return "yeni"')
    assert 'return "yeni"' in (workdir / "m.py").read_text(encoding="utf-8")


def test_edit_missing_old_string_gives_hint(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    ReadFile().run(ctx, path="m.py")
    with pytest.raises(ToolError) as e:
        EditFile().run(ctx, path="m.py", old_string="def alpha(x):\n    y = x + 2", new_string="def alpha(x):")
    msg = str(e.value)
    assert "bulunamadı" in msg and "İPUCU" in msg
    assert "alpha" in msg  # a candidate line is shown


def test_edit_indentation_mismatch_gets_hint(ctx, workdir):
    (workdir / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ReadFile().run(ctx, path="m.py")
    with pytest.raises(ToolError) as e:
        EditFile().run(ctx, path="m.py", old_string="return  1", new_string="return 2")  # two spaces
    assert "girinti" in str(e.value) or "boşluk" in str(e.value)


def test_edit_ambiguous_old_string_shows_locations(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    ReadFile().run(ctx, path="m.py")
    with pytest.raises(ToolError) as e:
        EditFile().run(ctx, path="m.py", old_string="return", new_string="return None")
    assert "satır" in str(e.value)


def test_edit_stale_file_names_file(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    ReadFile().run(ctx, path="m.py")
    (workdir / "m.py").write_text(PY_SRC.replace("return y", "return y + 100"), encoding="utf-8")
    with pytest.raises(ToolError) as e:
        EditFile().run(ctx, path="m.py", old_string="return y", new_string="return z")
    msg = str(e.value)
    assert "m.py" in msg and "diskte değişti" in msg


def test_edit_unread_file_names_file(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    with pytest.raises(ToolError, match="okunmadı"):
        EditFile().run(ctx, path="m.py", old_string="x", new_string="y")


def test_multi_edit_nonexisting_edit_suggests_line(ctx, workdir):
    (workdir / "m.py").write_text(PY_SRC, encoding="utf-8")
    ReadFile().run(ctx, path="m.py")
    with pytest.raises(ToolError) as e:
        from alice.tools.files import MultiEdit

        MultiEdit().run(ctx, path="m.py", edits=[
            {"old_string": "y = x + 1", "new_string": "y = x + 2"},
            {"old_string": "return su_yok", "new_string": "return su_var"},
        ])
    msg = str(e.value)
    assert "edit 2/2" in msg and "İPUCU" in msg
    # no change was written
    assert "y = x + 1" in (workdir / "m.py").read_text(encoding="utf-8")


def test_grep_no_match_reports_scope_and_filters(ctx, workdir):
    (workdir / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    out = Grep().run(ctx, pattern=r"def \w+", glob="*.md").text
    assert "eşleşme yok" in out
    assert "pattern=" in out and "glob='*.md'" in out and "taranan alan" in out


def test_grep_truncation_note_counts_total(ctx, workdir):
    for i in range(10):
        (workdir / f"f{i}.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    out = Grep().run(ctx, pattern=r"def foo", max_results=3).text
    assert "toplam 10" in out and "offset=3" in out
    assert "daralt" in out  # suggestion to narrow with path/glob


def test_grep_pagination_consistent(ctx, workdir):
    (workdir / "a.py").write_text("hit\n" * 12, encoding="utf-8")
    first = Grep().run(ctx, pattern="hit", max_results=5).text
    assert len([l for l in first.splitlines() if "\t" in l or "a.py:" in l]) == 5
    rest = Grep().run(ctx, pattern="hit", max_results=5, offset=5).text
    assert "sonuç yok" not in rest


def test_grep_description_documents_pagination():
    d = Grep().description
    assert "files_with_matches" in d and "offset" in d and "glob" in d


def test_read_file_description_documents_symbol():
    assert "symbol" in ReadFile().description


def test_symbol_param_documented_in_schema():
    props = ReadFile().parameters["properties"]
    assert "symbol" in props and len(props["symbol"]["description"]) >= 8


def test_utf16_text_file_reads_and_edit_keeps_encoding(ctx, workdir):
    # PowerShell `>` writes UTF-16: it must not be mistaken for binary, and editing must keep the encoding.
    f = workdir / "out.txt"
    f.write_bytes("ışık açık\r\nikinci satır\r\n".encode("utf-16"))
    out = ReadFile().run(ctx, path="out.txt").text
    assert "ışık açık" in out and "ikinci satır" in out
    EditFile().run(ctx, path="out.txt", old_string="ikinci", new_string="2.")
    raw = f.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    assert raw.decode("utf-16") == "ışık açık\r\n2. satır\r\n"
