"""CLIProxyAPI automatic setup tests: release asset selection, checksum, config generation and install."""

from __future__ import annotations

import hashlib
import io
import zipfile
import pytest

from alice.app import cliproxy, cliproxy_setup


def test_asset_name_per_platform():
    assert cliproxy_setup.asset_name("v8.0.57", "Windows", "AMD64") == "CLIProxyAPI_8.0.57_windows_amd64.zip"
    assert cliproxy_setup.asset_name("v8.0.57", "Linux", "aarch64") == "CLIProxyAPI_8.0.57_linux_aarch64.tar.gz"
    assert cliproxy_setup.asset_name("v8.0.57", "Darwin", "arm64") == "CLIProxyAPI_8.0.57_darwin_aarch64.tar.gz"


def test_checksum_parse_and_verify(tmp_path):
    data = tmp_path / "a.bin"
    data.write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    text = f"{digest}  a.bin\n{'0' * 64}  b.bin\n"
    assert cliproxy_setup.checksum_for(text, "a.bin") == digest
    with pytest.raises(ValueError):
        cliproxy_setup.checksum_for(text, "c.bin")


def test_ensure_config_creates_keys_and_env(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    keys = cliproxy_setup.ensure_config()
    assert (home / "config.yaml").exists() and (home / "keys.txt").exists()
    assert keys["api_key"] in (home / "config.yaml").read_text(encoding="utf-8")
    # a second call leaves it untouched
    again = cliproxy_setup.ensure_config()
    assert again["api_key"] == keys["api_key"]


def test_install_downloads_verifies_and_extracts(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    name = cliproxy_setup.asset_name("v9.9.9")
    buf = io.BytesIO()
    if name.endswith(".zip"):  # Windows
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(cliproxy.binary_name(), b"MZfake")
            z.writestr("README.md", "readme")
    else:  # Linux/Mac builds are .tar.gz (CI failed with "file could not be opened" because it made a zip)
        import tarfile

        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for fname, data in ((cliproxy.binary_name(), b"MZfake"), ("README.md", b"readme")):
                info = tarfile.TarInfo(fname)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    digest = hashlib.sha256(blob).hexdigest()

    monkeypatch.setattr(cliproxy_setup, "_release_json",
                        lambda version: {"assets": [{"name": name, "browser_download_url": "https://example/" + name}]})
    monkeypatch.setattr(cliproxy_setup, "_find_checksums", lambda release: f"{digest}  {name}\n")
    def fake_download(url, dest):
        dest.write_bytes(blob)
        return dest

    monkeypatch.setattr(cliproxy_setup, "_download", fake_download)
    r = cliproxy_setup.install("v9.9.9")
    assert r["ok"] and (home / cliproxy.binary_name()).read_bytes() == b"MZfake"
    assert (home / "config.yaml").exists()
    assert "api_key" not in r  # the key is not returned to the UI
    assert "api_key" not in cliproxy_setup.install("v9.9.9")  # also when already installed


def test_extract_rejects_tar_path_traversal(tmp_path):
    import tarfile

    archive = tmp_path / "kotu.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo("../disari.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    dest = tmp_path / "hedef"
    with pytest.raises((tarfile.TarError, ValueError)):
        cliproxy_setup._extract(archive, dest)
    assert not (tmp_path / "disari.txt").exists()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX izinleri")
def test_keys_file_is_private(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    cliproxy_setup.ensure_config()
    assert (home / "keys.txt").stat().st_mode & 0o777 == 0o600
    assert (home / "config.yaml").stat().st_mode & 0o777 == 0o600


def test_install_rejects_bad_checksum(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    name = cliproxy_setup.asset_name("v9.9.9")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(cliproxy.binary_name(), b"MZfake")
    blob = buf.getvalue()
    monkeypatch.setattr(cliproxy_setup, "_release_json",
                        lambda version: {"assets": [{"name": name, "browser_download_url": "https://example/" + name}]})
    monkeypatch.setattr(cliproxy_setup, "_find_checksums", lambda release: f"{'0' * 64}  {name}\n")
    def fake_download(url, dest):
        dest.write_bytes(blob)
        return dest

    monkeypatch.setattr(cliproxy_setup, "_download", fake_download)
    r = cliproxy_setup.install("v9.9.9")
    assert not r["ok"] and "checksum" in r["error"]


def test_cliproxy_key_comes_from_proxy_keys_file(tmp_path, monkeypatch):
    """Even if the .env copy is broken, ALICE sends the key the proxy accepts (keys.txt)."""
    from alice.config import ProviderConfig

    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    monkeypatch.setenv("CLIPROXY_API_KEY", "eski-bozuk")
    pc = ProviderConfig("cliproxy", "http://127.0.0.1:8317/v1", api_key_env="CLIPROXY_API_KEY")
    assert pc.api_keys == ["eski-bozuk"]  # without keys.txt, the .env value is used
    (tmp_path / "cliproxy").mkdir()
    (tmp_path / "cliproxy" / "keys.txt").write_text("api_key=dogru\nmanagement_key=y\n", encoding="utf-8")
    assert pc.api_keys == ["dogru"]
    other = ProviderConfig("groq", "http://x", api_key_env="CLIPROXY_API_KEY")
    assert other.api_keys == ["eski-bozuk"]  # only for the proxy
