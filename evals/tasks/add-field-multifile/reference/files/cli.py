"""Tiny user admin CLI.

  python cli.py list <file>
  python cli.py add <file> <id> <name>
"""

import sys

from models import User
from storage import load_users, save_users


def format_user(u):
    return f"{u.id}  {u.name}  {u.email}" if u.email else f"{u.id}  {u.name}"


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, path = argv[1], argv[2]
    if cmd == "list":
        for u in load_users(path):
            print(format_user(u))
        return 0
    if cmd == "add":
        users = load_users(path)
        users.append(User(id=int(argv[3]), name=argv[4], email=argv[5] if len(argv) > 5 else ""))
        save_users(path, users)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
