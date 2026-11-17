"""Eval check for --json: valid JSON, same objects sorted by id, table output unchanged."""

import json
import subprocess
import sys

TABLE = """ ID  DONE  TITLE
  1  x     Fix the login bug
  2        Review "pricing" PR
  3        Write the release notes"""


def run(*args):
    r = subprocess.run([sys.executable, "todo.py", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit(f"todo.py {' '.join(args)} failed: {r.stderr[-500:]}")
    return r.stdout


data = json.loads(run("list", "--json"))
with open("todos.json", encoding="utf-8") as f:
    want = sorted(json.load(f), key=lambda t: t["id"])
if data != want:
    sys.exit(f"--json output differs: {data!r}")
if run("list").rstrip("\n") != TABLE:
    sys.exit("table output changed")
from formatter import format_json  # noqa: E402

if json.loads(format_json(want)) != want:
    sys.exit("format_json does not return the JSON text")
print("ok")
