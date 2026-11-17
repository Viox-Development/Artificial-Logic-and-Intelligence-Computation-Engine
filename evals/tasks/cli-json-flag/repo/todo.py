"""Todo list.

  python todo.py list
"""

import argparse
import json
from pathlib import Path

from formatter import format_table

DB = Path(__file__).with_name("todos.json")


def load():
    return json.loads(DB.read_text(encoding="utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="todo.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        print(format_table(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
