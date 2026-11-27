"""Phone app update: the computer pulls the android-apk branch and hands it to the phone."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from alice.app import phone_apk


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path):
    """A fake remote repo with an android-apk branch, and a clone that knows it as origin."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git yok")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "android-apk")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", str(origin))

    def publish(code: int, apk: bytes, sha: str | None = None):
        (origin / "ALICE.apk").write_bytes(apk)
        meta = {"version_code": code, "version_name": f"0.2.{code}",
                "sha256": sha if sha is not None else hashlib.sha256(apk).hexdigest()}
        (origin / "version.json").write_text(json.dumps(meta))
        _git(origin, "add", ".")
        _git(origin, "commit", "-q", "-m", f"v{code}")

    return clone, publish


def test_refresh_downloads_newer_apk_and_serves_it(remote):
    clone, publish = remote
    assert phone_apk.view() == {"available": False, "checking": False}
    publish(5, b"PK\x03\x04apk-5\x00\xff")
    got = phone_apk.refresh(force=True, repo=clone)
    assert got["version_code"] == 5 and got["version_name"] == "0.2.5"
    assert phone_apk.apk_path().read_bytes() == b"PK\x03\x04apk-5\x00\xff"  # binary content stays intact
    publish(7, b"PK-apk-7")
    assert phone_apk.refresh(force=True, repo=clone)["version_code"] == 7
    assert phone_apk.view()["available"] is True


def test_refresh_rejects_corrupt_apk(remote):
    clone, publish = remote
    publish(3, b"PK-good")
    phone_apk.refresh(force=True, repo=clone)
    publish(4, b"PK-bad", sha="0" * 64)
    assert phone_apk.refresh(force=True, repo=clone)["version_code"] == 3  # the old one stays in place
    assert "damaged" in phone_apk.view()["error"]


def test_refresh_is_throttled(remote, monkeypatch):
    clone, publish = remote
    publish(1, b"PK-1")
    phone_apk.refresh(force=True, repo=clone)
    publish(2, b"PK-2")
    assert phone_apk.refresh(repo=clone)["version_code"] == 1  # not pulled again before 30 minutes pass


def test_api_serves_version_and_apk(remote):
    import urllib.request

    from alice.app.server import AppServer, AppState
    from alice.config import load_config

    clone, publish = remote
    publish(9, b"PK-apk-9")
    phone_apk.refresh(force=True, repo=clone)
    st = AppState(load_config(), clone, "auto")
    srv = AppServer(st, 0).start()
    try:
        base = f"http://127.0.0.1:{srv.httpd.server_address[1]}"
        with urllib.request.urlopen(f"{base}/api/phone/app?t={st.token}") as r:
            assert json.loads(r.read())["version_code"] == 9
        with urllib.request.urlopen(f"{base}/api/phone/app.apk?t={st.token}") as r:
            assert r.headers["Content-Type"] == "application/vnd.android.package-archive"
            assert r.read() == b"PK-apk-9"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/api/phone/app.apk")  # cannot be downloaded without the key
    finally:
        srv.stop()


def test_phone_check_fetches_before_answering(remote, monkeypatch):
    """The phone's own check (?refresh=1) fetches the android-apk branch first. It used to get the old version while
    the fetch ran in the background, and then waited 3 hours before asking again (#461)."""
    import urllib.request

    from alice import updater
    from alice.app.server import AppServer, AppState
    from alice.config import load_config

    clone, publish = remote
    monkeypatch.setattr(updater, "repo_dir", lambda: clone)
    publish(1, b"PK-1")
    phone_apk.refresh(force=True, repo=clone)
    publish(2, b"PK-2")  # a new release lands within the 30-minute throttle
    st = AppState(load_config(), clone, "auto")
    srv = AppServer(st, 0).start()
    try:
        base = f"http://127.0.0.1:{srv.httpd.server_address[1]}"
        with urllib.request.urlopen(f"{base}/api/phone/app?t={st.token}") as r:
            assert json.loads(r.read())["version_code"] == 1  # the settings page: throttled background fetch
        with urllib.request.urlopen(f"{base}/api/phone/app?refresh=1&t={st.token}") as r:
            data = json.loads(r.read())
        assert data["version_code"] == 2 and data["checking"] is False
    finally:
        srv.stop()
