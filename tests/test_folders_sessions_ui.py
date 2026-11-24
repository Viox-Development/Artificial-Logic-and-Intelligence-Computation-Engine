"""Folder picker (without typing paths by hand) and chat renaming/pinning APIs."""

from __future__ import annotations

import httpx
import pytest

from alice import session as sess
from alice.app import folders
from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config


def test_listing_hides_dotfolders_and_marks_git(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "A" / ".git").mkdir(parents=True)
    (tmp_path / ".gizli").mkdir()
    (tmp_path / "dosya.txt").write_text("x")
    out = folders.listing(str(tmp_path))
    assert out["ok"] and [d["name"] for d in out["dirs"]] == ["A", "b"]
    assert out["dirs"][0]["git"] and not out["dirs"][1]["git"]
    assert out["crumbs"][-1]["path"] == str(tmp_path.resolve())
    assert [d["name"] for d in folders.listing(str(tmp_path), show_hidden=True)["dirs"]] == [".gizli", "A", "b"]
    assert not folders.listing(str(tmp_path / "yok"))["ok"]


def test_make_dir_rejects_bad_names(tmp_path):
    assert folders.make_dir(str(tmp_path), "yeni")["ok"] and (tmp_path / "yeni").is_dir()
    assert not folders.make_dir(str(tmp_path), "yeni")["ok"]  # zaten var
    for bad in ("", "..", "a/b", "a\\b", "x:y"):
        assert not folders.make_dir(str(tmp_path), bad)["ok"]


def test_recent_dirs_newest_first_and_unique(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    folders.remember_dir(a)
    folders.remember_dir(b)
    folders.remember_dir(a)
    assert folders.recent_dirs() == [str(a), str(b)]


def test_session_meta_set_and_clear():
    assert sess.set_meta("s1", title="Ad", pinned=True) == {"title": "Ad", "pinned": True}
    assert sess.set_meta("s1", pinned=False) == {"title": "Ad"}
    sess.set_meta("s1", title="")
    assert "s1" not in sess.load_meta()


@pytest.fixture
def client(workdir):
    state = AppState(make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}, stream=False), workdir, "auto",
                     provider_factory=lambda pc: FakeProvider("a", lambda *a, **k: "tamam"))
    srv = AppServer(state).start()
    c = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield c, workdir
    srv.stop()


def test_dirs_api_and_cwd_remembered(client):
    c, workdir = client
    (workdir / "alt").mkdir()
    d = c.get("/api/dirs", params={"path": str(workdir)}).json()
    assert d["ok"] and [x["name"] for x in d["dirs"]] == ["alt"] and d["places"][0]["label"] == "Home"
    assert c.post("/api/dirs/mkdir", json={"parent": str(workdir), "name": "proje"}).json()["ok"]
    assert c.post("/api/cwd", json={"path": str(workdir / "proje")}).json()["ok"]
    assert c.get("/api/dirs", params={"path": str(workdir)}).json()["recent"][0] == str(workdir / "proje")
    assert c.post("/api/dirs/mkdir", json={"parent": str(workdir), "name": "../dışarı"}).status_code == 400


def test_session_rename_and_pin_over_api(client):
    c, _ = client
    assert c.post("/api/send", json={"text": "ilk mesaj"}).json()["ok"]
    for _ in range(50):
        rows = c.get("/api/sessions").json()
        if rows:
            break
        import time

        time.sleep(0.1)
    sid = rows[0]["id"]
    assert c.post("/api/session/rename", json={"id": sid, "title": "Güzel ad"}).json()["ok"]
    assert c.post("/api/session/pin", json={"id": sid, "pinned": True}).json()["ok"]
    row = c.get("/api/sessions").json()[0]
    assert row["title"] == "Güzel ad" and row["auto_title"] == "ilk mesaj" and row["pinned"]
    assert c.post("/api/session/rename", json={"id": "../../etc", "title": "x"}).status_code == 404
    assert c.post("/api/session/delete", json={"id": sid}).json()["ok"]
    assert sid not in sess.load_meta()
