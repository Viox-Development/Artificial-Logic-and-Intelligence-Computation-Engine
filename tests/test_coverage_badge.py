"""The coverage badge script used by the coverage-badge CI job (#228)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "coverage_badge.py"


@pytest.fixture
def cb():
    spec = importlib.util.spec_from_file_location("coverage_badge", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def xml(tmp_path, rate):
    p = tmp_path / "coverage.xml"
    p.write_text(f'<?xml version="1.0" ?><coverage version="7" line-rate="{rate}" branch-rate="0"></coverage>',
                 encoding="utf-8")
    return p


@pytest.mark.parametrize("rate,message,color", [
    ("0.9312", "93.1%", "brightgreen"), ("0.6", "60%", "yellowgreen"), ("0.452", "45.2%", "yellow"),
    ("0.1", "10%", "red"),
])
def test_badge_json_from_coverage_xml(cb, tmp_path, rate, message, color):
    assert cb.main([str(xml(tmp_path, rate)), str(tmp_path / "out")]) == 0
    data = json.loads((tmp_path / "out" / "coverage.json").read_text(encoding="utf-8"))
    assert data == {"schemaVersion": 1, "label": "coverage", "message": message, "color": color}
