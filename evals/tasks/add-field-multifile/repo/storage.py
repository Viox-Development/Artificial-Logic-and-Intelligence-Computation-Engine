"""Save and load users as JSON."""

import json
from pathlib import Path

from models import User


def save_users(path, users):
    data = [{"id": u.id, "name": u.name} for u in users]
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_users(path):
    p = Path(path)
    if not p.exists():
        return []
    return [User(id=int(d["id"]), name=d["name"]) for d in json.loads(p.read_text(encoding="utf-8"))]
