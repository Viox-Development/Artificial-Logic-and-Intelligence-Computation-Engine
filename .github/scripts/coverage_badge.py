"""Turn coverage.xml into a shields.io endpoint badge (coverage.json) for the coverage-badge CI job (#228).

Usage: python coverage_badge.py coverage.xml OUT_DIR
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# (lowest percent, shields colour), checked top to bottom
COLORS = ((90, "brightgreen"), (75, "green"), (60, "yellowgreen"), (45, "yellow"), (30, "orange"), (0, "red"))


def percent(xml_path: Path) -> float:
    """Line coverage from a Cobertura coverage.xml (pytest-cov --cov-report=xml), 0-100."""
    root = ET.parse(xml_path).getroot()
    return round(float(root.get("line-rate", "0")) * 100, 1)


def badge(pct: float) -> dict:
    color = next(c for low, c in COLORS if pct >= low)
    return {"schemaVersion": 1, "label": "coverage", "message": f"{pct:g}%", "color": color}


def main(argv: list[str]) -> int:
    xml_path, out = Path(argv[0]), Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    data = badge(percent(xml_path))
    (out / "coverage.json").write_text(json.dumps(data) + "\n", encoding="utf-8")
    print(f"coverage: {data['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
