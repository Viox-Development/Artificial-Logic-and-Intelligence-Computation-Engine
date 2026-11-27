"""The coverage step-summary script used by the Python 3.13 CI job (#452)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "coverage_summary.py"


@pytest.fixture
def cs():
    spec = importlib.util.spec_from_file_location("coverage_summary", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _xml(tmp_path, classes):
    parts = ['<?xml version="1.0" ?>', '<coverage version="7" line-rate="0.75" branch-rate="0">']
    for filename, rate, missed, total in classes:
        parts.append(f'<class filename="{filename}" line-rate="{rate}"><lines>')
        for n in range(1, total + 1):
            hits = 0 if n <= missed else 1
            parts.append(f'<line number="{n}" hits="{hits}"/>')
        parts.append('</lines></class>')
    parts.append('</coverage>')
    path = tmp_path / "coverage.xml"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def test_summary_ignores_small_files_and_groups_packages(cs, tmp_path):
    xml = _xml(tmp_path, [
        ("alice/tools/findfiles.py", 0.2, 40, 50),   # included: 50 lines
        ("alice/tools/tiny.py", 0.0, 5, 5),          # excluded: under 50 lines
        ("alice/voice/tts.py", 0.8, 10, 50),
    ])
    total, least, packages = cs.rows(xml)
    assert total == 75.0
    assert [name for name, *_ in least] == ["alice/tools/findfiles.py", "alice/voice/tts.py"]
    assert least[0][1] == 20.0
    by_name = {name: pct for name, pct, *_ in packages}
    # Package totals include every file, even the tiny one omitted from the "least covered" table.
    assert by_name == {"alice/tools": 18.2, "alice/voice": 80.0}


def test_main_appends_markdown_to_summary(cs, tmp_path):
    xml = _xml(tmp_path, [("alice/tools/findfiles.py", 0.2, 40, 50)])
    summary = tmp_path / "step_summary.md"
    summary.write_text("existing header\n", encoding="utf-8")
    assert cs.main([str(xml), str(summary)]) == 0
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("existing header\n## Coverage")
    assert "**Total line coverage: 75%**" in text
    assert "| `alice/tools` | 20% | 40 | 50 |" in text
