"""Large tool outputs are written to disk; the model gets the head, the tail and the file path."""

from __future__ import annotations

import re
from pathlib import Path

from alice import tool_results


def _saved_path(shaped: str) -> Path:
    m = re.search(r"\[tam çıktı dosyada: (.+?) \(\d+ karakter\)", shaped)
    assert m, shaped[-300:]
    return Path(m.group(1))


def test_same_call_id_does_not_overwrite_earlier_output():
    """An empty or repeated call id (e.g. "call_0") must not overwrite an earlier output's file."""
    a = tool_results.shape("A" * 50_000, 2000, "call_0", "run_command")
    b = tool_results.shape("B" * 50_000, 2000, "call_0", "run_command")
    pa, pb = _saved_path(a), _saved_path(b)
    assert pa != pb
    assert pa.read_text(encoding="utf-8") == "A" * 50_000
    assert pb.read_text(encoding="utf-8") == "B" * 50_000


def test_short_output_is_returned_as_is():
    assert tool_results.shape("kısa", 2000, "", "grep") == "kısa"
