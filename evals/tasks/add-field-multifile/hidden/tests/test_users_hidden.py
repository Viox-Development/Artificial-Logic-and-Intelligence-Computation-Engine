import json
import subprocess
import sys

from models import User
from storage import load_users, save_users


def test_default_email():
    assert User(1, "Ada").email == ""


def test_round_trip(tmp_path):
    p = tmp_path / "u.json"
    save_users(p, [User(1, "Ada", "ada@example.org"), User(2, "Bo")])
    users = load_users(p)
    assert [(u.id, u.name, u.email) for u in users] == [(1, "Ada", "ada@example.org"), (2, "Bo", "")]
    assert json.loads(p.read_text(encoding="utf-8"))[0]["email"] == "ada@example.org"


def test_old_file(tmp_path):
    p = tmp_path / "old.json"
    p.write_text('[{"id": 3, "name": "Cy"}]', encoding="utf-8")
    assert load_users(p)[0].email == ""


def run(*args):
    r = subprocess.run([sys.executable, "cli.py", *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_cli(tmp_path):
    p = str(tmp_path / "c.json")
    run("add", p, "1", "Ada", "ada@example.org")
    run("add", p, "2", "Bo")
    lines = run("list", p).splitlines()
    assert lines == ["1  Ada  ada@example.org", "2  Bo"]
