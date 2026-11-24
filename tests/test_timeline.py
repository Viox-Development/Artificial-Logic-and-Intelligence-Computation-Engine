from __future__ import annotations

import sqlite3
import sys

import pytest
from PIL import Image

from alice import timeline
from alice.timeline import Recorder, Timeline, excluded


def fake_enc(b: bytes) -> bytes:
    return bytes(x ^ 0x5A for x in b)


@pytest.fixture
def tl(tmp_path):
    t = Timeline(tmp_path / "timeline.db", enc=fake_enc, dec=fake_enc)
    yield t
    t.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_dpapi_roundtrip_and_ciphertext():
    data = "Discord: https://örnek.link/abc şifreli".encode()
    enc = timeline.protect(data)
    assert enc != data and data not in enc
    assert timeline.unprotect(enc) == data


def test_text_is_stored_encrypted(tl):
    tl.add("discord.exe", "Discord - #genel", "Ali: şu linke bak https://example.com/oyun", ts=1000.0)
    raw = sqlite3.connect(str(tl.path)).execute("SELECT title, text FROM shots").fetchone()
    assert b"example.com" not in raw[1] and b"genel" not in raw[0]


def test_search_all_words_newest_first_and_days(tl):
    tl.add("discord.exe", "Discord - #genel", "Ali: şu linke bak https://example.com/oyun", ts=1000.0)
    tl.add("opera.exe", "Haber", "Bugün hava güzel", ts=2000.0)
    tl.add("discord.exe", "Discord - #oyun", "Veli: yeni LINK https://example.com/yeni", ts=3000.0)
    rows = tl.search("discord link", now=3100.0)
    assert [r["title"] for r in rows] == ["Discord - #oyun", "Discord - #genel"]
    assert "https://example.com/yeni" in rows[0]["snippet"]
    assert tl.search("hava", days=0.01, now=3100.0) == []  # 15 min back: none
    assert tl.search("yok böyle bir şey", now=3100.0) == []


def test_prune_and_wipe(tl):
    tl.add("a.exe", "t", "eski", ts=0.0)
    tl.add("a.exe", "t", "yeni", ts=86400 * 40)
    assert tl.prune(30, now=86400 * 40) == 1 and tl.count() == 1
    assert tl.wipe() == 1 and tl.count() == 0


def test_close_waits_for_writer_and_later_writes_are_dropped(tmp_path):
    import threading

    t = Timeline(tmp_path / "timeline.db", enc=fake_enc, dec=fake_enc)
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            try:
                t.add("a.exe", "t", "kare")
                t.prune(30)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
                return

    th = threading.Thread(target=writer)
    th.start()
    t.add("a.exe", "t", "ilk")
    t.close()
    t.close()  # a second close is harmless
    stop.set()
    th.join(5)
    assert errors == []
    t.add("a.exe", "t", "sonra")  # after closing, it is silently dropped
    assert t.prune(30) == 0


def test_exclusions():
    assert excluded("KeePassXC.exe", "veritabanı")
    assert excluded("opera.exe", "Yeni InPrivate penceresi")
    assert excluded("chrome.exe", "Ziraat Bankası - Giriş")
    assert excluded("aces.exe", "War Thunder")
    assert excluded("whatsapp.root.exe", "WhatsApp", extra=["whatsapp"])
    assert not excluded("code.exe", "alice - VS Code")


def img(color):
    return Image.new("RGB", (640, 360), color)


def recorder(tl, frames, texts, mode="normal", fg=("code.exe", "alice - VS Code", (0, 0, 640, 360))):
    frames, texts = iter(frames), iter(texts)
    return Recorder(tl, {"keep_days": 30}, mode=lambda: mode, foreground=lambda: fg,
                    grab=lambda box: next(frames), ocr=lambda im: [(next(texts), None)])


def test_recorder_saves_only_changed_screens(tl):
    r = recorder(tl, [img("white"), img("white"), img("black"), img("gray")],
                 ["def main():", "hata: KeyError", "hata: KeyError"])  # same screen: no OCR, no text spent
    assert r.step(1000.0) and not r.step(1030.0)
    assert r.step(1060.0)
    assert not r.step(1090.0)  # the screen changed but the text is the same
    assert tl.count() == 2 and r.saved == 2


def test_recorder_skips_games_and_private_windows(tl):
    assert not recorder(tl, [img("white")], ["x"], mode="flight").step(1.0)
    assert not recorder(tl, [img("white")], ["x"], fg=("1password.exe", "Kasa", (0, 0, 10, 10))).step(1.0)
    assert not recorder(tl, [img("white")], ["x"], fg=("code.exe", "simge", None)).step(1.0)
    assert tl.count() == 0


def test_tool_is_untrusted_and_wipe_needs_approval():
    from alice.tools.base import DANGER, READ
    from alice.tools.timelinetool import TimelineSearch

    t = TimelineSearch()
    assert t.untrusted and t.risk_for({"action": "search"}) == READ and t.risk_for({"action": "wipe"}) == DANGER


def test_tool_reports_when_off():
    from types import SimpleNamespace

    from alice.tools.timelinetool import TimelineSearch

    assert "No timeline records" in TimelineSearch().run(SimpleNamespace(), action="search", query="link").text


def test_default_off():
    from alice.config import default_config_dict

    assert default_config_dict()["timeline"]["enabled"] is False


def test_recorder_stop_joins_thread_and_closes_timeline(tl):
    r = recorder(tl, [], [])
    r.cfg["every"] = 5
    r.start()
    r.stop(timeout=2)
    assert not r._thread.is_alive()
    tl.add("a", "b", "c", 1.0)  # after closing, writes are silently dropped
    r.stop()  # calling it a second time is fine


# ---------------------------------------------------------------- Linux/macOS: keyring + Fernet (#237)
class MemoryKeyring:
    """Stand-in keyring backend: get/set password in a dict, or fail like a missing Secret Service."""

    def __init__(self, fail: bool = False):
        self.store: dict[tuple[str, str], str] = {}
        self.fail = fail

    def get_password(self, service, user):
        if self.fail:
            from keyring.errors import NoKeyringError

            raise NoKeyringError("No recommended backend was available.")
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value


@pytest.fixture
def fake_keyring(monkeypatch):
    keyring = pytest.importorskip("keyring")
    pytest.importorskip("cryptography")
    monkeypatch.setattr(timeline.sys, "platform", "linux")
    monkeypatch.setattr(timeline, "_FERNET", [])

    def make(fail=False):
        kr = MemoryKeyring(fail)
        monkeypatch.setattr(keyring, "get_password", kr.get_password)
        monkeypatch.setattr(keyring, "set_password", kr.set_password)
        timeline._FERNET.clear()
        return kr
    return make


def test_keyring_fernet_roundtrip_creates_and_reuses_the_key(fake_keyring):
    kr = fake_keyring()
    data = "Discord: https://örnek.link/abc şifreli".encode()
    enc = timeline.protect(data)
    assert enc != data and data not in enc
    assert timeline.unprotect(enc) == data
    key = kr.store[(timeline.KEYRING_SERVICE, timeline.KEYRING_USER)]
    timeline._FERNET.clear()  # a new process reads the same key back from the keyring
    assert timeline.unprotect(enc) == data
    assert kr.store[(timeline.KEYRING_SERVICE, timeline.KEYRING_USER)] == key


def test_keyring_timeline_works_end_to_end(fake_keyring, tmp_path):
    fake_keyring()
    t = Timeline(tmp_path / "timeline.db")  # default enc/dec: protect/unprotect
    try:
        t.add("discord", "Discord - #genel", "Ali: şu linke bak https://example.com/oyun", ts=1000.0)
        raw = sqlite3.connect(str(t.path)).execute("SELECT text FROM shots").fetchone()[0]
        assert b"example.com" not in raw
        assert [r["app"] for r in t.search("example oyun", now=2000.0)] == ["discord"]
    finally:
        t.close()


def test_keyring_missing_backend_gives_a_clear_error(fake_keyring):
    fake_keyring(fail=True)
    with pytest.raises(RuntimeError, match="anahtar deposu kullanılamıyor"):
        timeline.protect(b"x")


def test_row_from_another_key_is_skipped_not_crashing(fake_keyring, tmp_path):
    kr = fake_keyring()
    enc = timeline.protect(b"eski")
    kr.store.clear()  # the key was lost/replaced: a new one is created
    timeline._FERNET.clear()
    with pytest.raises(OSError, match="çözülemedi"):
        timeline.unprotect(enc)


def test_missing_packages_explain_what_to_install(monkeypatch):
    monkeypatch.setattr(timeline.sys, "platform", "linux")
    monkeypatch.setattr(timeline, "_FERNET", [])
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(RuntimeError, match=r"alice-harness\[timeline\]"):
        timeline.protect(b"x")
