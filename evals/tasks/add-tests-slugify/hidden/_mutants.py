"""Eval check: the agent's tests must pass on slugify.py and fail on broken copies of it."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path("slugify.py")
ORIGINAL = SRC.read_text(encoding="utf-8")
MUTANTS = {
    "no lower()": ('.strip("-").lower()', '.strip("-")'),
    "drops accented letters": ('unicodedata.normalize("NFKD", text)', "text"),
    "no strip of dashes": ('.strip("-").lower()', '.lower()'),
    "no max_len": ("if len(text) > max_len:", "if False:"),
    "dash left after cut": ('text[:max_len].rstrip("-")', "text[:max_len]"),
}


def run_tests() -> bool:
    shutil.rmtree(".pytest_cache", ignore_errors=True)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_slugify.py"],
                       capture_output=True, text=True, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    return r.returncode == 0


def main() -> int:
    if not Path("tests/test_slugify.py").exists():
        print("tests/test_slugify.py missing")
        return 1
    if not run_tests():
        print("tests fail on the real slugify.py")
        return 1
    caught = []
    try:
        for name, (old, new) in MUTANTS.items():
            assert old in ORIGINAL, name
            SRC.write_text(ORIGINAL.replace(old, new, 1), encoding="utf-8")
            if not run_tests():
                caught.append(name)
    finally:
        SRC.write_text(ORIGINAL, encoding="utf-8")
    print(f"caught {len(caught)}/{len(MUTANTS)}: {', '.join(caught)}")
    return 0 if len(caught) >= 4 else 1


if __name__ == "__main__":
    sys.exit(main())
