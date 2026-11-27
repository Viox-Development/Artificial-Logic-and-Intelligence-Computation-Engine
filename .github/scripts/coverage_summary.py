"""Write a coverage table to the GitHub step summary from pytest-cov's coverage.xml (#452).

Usage: python coverage_summary.py coverage.xml SUMMARY_FILE

Standard library only, like coverage_badge.py. No third-party action or service.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MIN_LINES = 50          # ignore tiny files in the "least covered" list
LEAST_COUNT = 15        # number of files to list


def _pct(rate: str) -> float:
    try:
        return round(float(rate) * 100, 1)
    except (TypeError, ValueError):
        return 0.0


def _package(filename: str) -> str:
    """Top-level package, e.g. alice/tools, from a Cobertura filename."""
    parts = Path(filename).as_posix().split("/")
    if len(parts) >= 2 and parts[0] == "alice":
        return "/".join(parts[:2])
    return parts[0] or "(root)"


def rows(coverage_path: Path) -> tuple[float, list[tuple[str, float, int, int]], list[tuple[str, float, int, int]]]:
    """Return (overall %, least-covered files, per-package) from a Cobertura XML.

    Each file/package row is (name, percent, missed lines, total lines).
    """
    root = ET.parse(coverage_path).getroot()
    total_pct = _pct(root.get("line-rate"))

    files: dict[str, tuple[float, int, int]] = {}
    packages: dict[str, tuple[int, int]] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename")
        if not filename:
            continue
        lines = cls.findall(".//line")
        count = len(lines) if lines else int(cls.get("lines-valid") or 0)
        pct = _pct(cls.get("line-rate"))
        missed = count - round(count * pct / 100) if count else 0
        files[filename] = (pct, missed, count)
        hit = count - missed
        prev_hit, prev_count = packages.get(_package(filename), (0, 0))
        packages[_package(filename)] = (prev_hit + hit, prev_count + count)

    least = [(name, *data) for name, data in files.items() if data[2] >= MIN_LINES]
    least.sort(key=lambda row: row[1])
    per_package = [(name, round(100 * hit / count, 1) if count else 0.0, count - hit, count)
                   for name, (hit, count) in sorted(packages.items())]
    return total_pct, least[:LEAST_COUNT], per_package


def markdown(total: float, least: list, per_package: list) -> str:
    out = ["## Coverage", "", f"**Total line coverage: {total:g}%**", "",
           f"### {LEAST_COUNT} least-covered files (at least {MIN_LINES} lines)", "",
           "| File | Coverage | Missed | Lines |", "|---|---:|---:|---:|"]
    out += [f"| `{name}` | {pct:g}% | {missed} | {count} |" for name, pct, missed, count in least]
    out += ["", "### Per top-level package", "", "| Package | Coverage | Missed | Lines |",
            "|---|---:|---:|---:|"]
    out += [f"| `{name}` | {pct:g}% | {missed} | {count} |" for name, pct, missed, count in per_package]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    summary = Path(argv[1])
    total, least, per_package = rows(Path(argv[0]))
    with summary.open("a", encoding="utf-8") as handle:
        handle.write(markdown(total, least, per_package))
    print(f"coverage summary: {total:g}% across {len(per_package)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
