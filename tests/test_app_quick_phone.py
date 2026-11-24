"""alice/app/quick.py and alice/app/phone.py: without a real server, browser, clipboard or network."""

from __future__ import annotations

import base64
import io
import sys
import types
from types import SimpleNamespace

import pytest
from PIL import Image

from alice.app import phone, quick
from alice.config import Config, alice_home, default_config_dict
from tests.conftest import patch_sleep


class FakeServer:
    def __init__(self, state, port=0, host="127.0.0.1", owns_state=True):
        self.state, self.port, self.host = state, port, host
        self.httpd = SimpleNamespace(server_address=(host, port or 5555))
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


def fake_state():
    s = SimpleNamespace(token="TOK", agent=SimpleNamespace(busy=False), _queued=[], quick_pending=None,
                        bus=FakeBus(), new_sessions=0)

    def new_session():
        s.new_sessions += 1

    s.new_session = new_session
    return s


@pytest.fixture
def fake_server(monkeypatch):
    monkeypatch.setattr("alice.app.server.AppServer", FakeServer)
    return FakeServer


def make_qb(fake_server, quick_cfg=None):
    data = default_config_dict()
    if quick_cfg is not None:
        data["quick"] = quick_cfg
    qb = quick.QuickBar(Config(data), state=fake_state())
    urls = []
    qb.launcher = urls.append
    return qb, urls


# ------------------------------------------------------------------ quick
def test_screen_size_fallback(monkeypatch):
    monkeypatch.setattr(quick.sys, "platform", "linux")
    assert quick.screen_size() == (1920, 1080)
    import ctypes

    monkeypatch.setattr(quick.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", None, raising=False)  # windll.user32 -> AttributeError
    assert quick.screen_size() == (1920, 1080)


def test_screen_size_windows_metrics(monkeypatch):
    import ctypes

    monkeypatch.setattr(quick.sys, "platform", "win32")
    user32 = SimpleNamespace(GetSystemMetrics=lambda i: (2560, 1440)[i])
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    assert quick.screen_size() == (2560, 1440)


def test_png_b64_keeps_small_and_converts_rgba():
    data = base64.b64decode(quick.png_b64(Image.new("RGBA", (300, 100), (1, 2, 3, 0))))
    img = Image.open(io.BytesIO(data))
    assert img.size == (300, 100) and img.mode == "RGB"


def test_read_clipboard_prefers_pyperclip(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyperclip", SimpleNamespace(paste=lambda: "hata"))
    assert quick.read_clipboard() == "hata"
    monkeypatch.setitem(sys.modules, "pyperclip", SimpleNamespace(paste=lambda: None))
    assert quick.read_clipboard() == ""


def test_read_clipboard_tk_fallback(monkeypatch):
    destroyed = []

    class Tk:
        def withdraw(self):
            pass

        def clipboard_get(self):
            return "tk metni"

        def destroy(self):
            destroyed.append(1)

    monkeypatch.setitem(sys.modules, "pyperclip", None)
    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(Tk=Tk))
    assert quick.read_clipboard() == "tk metni" and destroyed == [1]

    class EmptyTk(Tk):
        def clipboard_get(self):
            raise RuntimeError("pano boş")

    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(Tk=EmptyTk))
    assert quick.read_clipboard() == "" and len(destroyed) == 2


def test_quickbar_hotkeys_from_config_and_url(fake_server):
    qb, _ = make_qb(fake_server, {"hotkey": "<f9>", "clipboard_hotkey": ""})
    assert qb.hotkeys == {"open": "<f9>", "region": "<ctrl>+<alt>+s", "clipboard": ""}
    assert qb.url == "http://127.0.0.1:5555/quick?t=TOK"
    qb.stop()
    assert qb.server.stopped


def test_open_sets_only_truthy_pending(fake_server):
    qb, urls = make_qb(fake_server)
    qb.open(text="selam", autosend=False, listen=True)
    assert qb.state.quick_pending == {"text": "selam", "listen": True}
    qb.open()
    assert qb.state.quick_pending is None
    assert urls == [qb.url, qb.url]


def test_open_idle_does_not_reset_busy_or_queued(fake_server, monkeypatch):
    qb, _ = make_qb(fake_server)
    t = [1000.0]
    monkeypatch.setattr(quick.time, "monotonic", lambda: t[0])
    qb.open()
    t[0] += quick.IDLE_NEW + 1
    qb.state.agent.busy = True
    qb.open()
    assert qb.state.new_sessions == 0
    qb.state.agent.busy = False
    t[0] += quick.IDLE_NEW + 1
    qb.state._queued = ["bekleyen"]
    qb.open()
    assert qb.state.new_sessions == 0
    qb.state._queued = []
    t[0] += quick.IDLE_NEW - 1  # time has not run out yet
    qb.open()
    assert qb.state.new_sessions == 0
    t[0] += quick.IDLE_NEW + 1
    qb.open()
    assert qb.state.new_sessions == 1


def test_ask_region_paths(fake_server, monkeypatch):
    qb, urls = make_qb(fake_server)

    def broken():
        raise RuntimeError("tkinter yok")

    monkeypatch.setattr(quick, "select_region", broken)
    qb.ask_region()
    assert qb.state.bus.events == [{"type": "info", "text": "could not select an area: tkinter yok"}] and urls == []
    monkeypatch.setattr(quick, "select_region", lambda: None)  # Esc
    qb.ask_region()
    assert urls == []
    monkeypatch.setattr(quick, "select_region", lambda: Image.new("RGB", (40, 20)))
    qb.ask_region()
    img = qb.state.quick_pending["image"]
    with Image.open(io.BytesIO(base64.b64decode(img))) as image:
        assert image.size == (40, 20)
    assert len(urls) == 1


def test_explain_clipboard_truncates_and_empty(fake_server, monkeypatch):
    qb, urls = make_qb(fake_server)
    monkeypatch.setattr(quick, "read_clipboard", lambda: "  " + "x" * 7000 + "  ")
    qb.explain_clipboard()
    p = qb.state.quick_pending
    assert p["autosend"] is True and p["text"] == quick.EXPLAIN_CLIPBOARD.format(text="x" * 6000)
    monkeypatch.setattr(quick, "read_clipboard", lambda: "   \n")
    qb.explain_clipboard()
    assert qb.state.quick_pending is None and len(urls) == 2


def test_launch_uses_app_mode_browser(monkeypatch):
    calls = []
    monkeypatch.setattr("alice.app.window._browser_candidates", lambda: ["/bin/edge", "/bin/chrome"])
    monkeypatch.setattr(quick, "screen_size", lambda: (1000, 900))
    monkeypatch.setattr(quick.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(quick.webbrowser, "open", lambda url: pytest.fail("tarayıcıya düşmemeli"))
    quick.QuickBar._launch("http://x/quick")
    assert len(calls) == 1
    args = calls[0][0]
    assert args[0] == "/bin/edge" and "--app=http://x/quick" in args
    assert "--window-size=680,440" in args and "--window-position=160,180" in args
    profile = alice_home() / "quick-window"
    assert f"--user-data-dir={profile}" in args and profile.is_dir()


def test_launch_small_screen_and_webbrowser_fallback(monkeypatch):
    opened = []
    monkeypatch.setattr("alice.app.window._browser_candidates", lambda: [])
    monkeypatch.setattr(quick.webbrowser, "open", opened.append)
    monkeypatch.setattr(quick.subprocess, "Popen", lambda *a, **k: pytest.fail("Popen çağrılmamalı"))
    quick.QuickBar._launch("http://y")
    assert opened == ["http://y"]


def test_start_hotkeys_without_pynput(fake_server, monkeypatch):
    qb, _ = make_qb(fake_server)
    monkeypatch.setattr("alice.hotkeys.IS_WIN", False)  # pynput path; a real shortcut must not be registered on Windows
    monkeypatch.setitem(sys.modules, "pynput", None)
    assert qb.start_hotkeys() is False and qb._listener is None


def test_start_hotkeys_registers_nonempty_and_runs_in_thread(fake_server, monkeypatch):
    qb, _ = make_qb(fake_server, {"region_hotkey": ""})
    monkeypatch.setattr("alice.hotkeys.IS_WIN", False)
    monkeypatch.setattr("alice.hotkeys.pynput_problem", lambda: "")  # as on an X11 desktop
    made = []

    class GlobalHotKeys:
        def __init__(self, mapping):
            self.mapping, self.started, self.stopped, self.daemon = mapping, False, False, False
            made.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    pynput = types.ModuleType("pynput")
    pynput.keyboard = SimpleNamespace(GlobalHotKeys=GlobalHotKeys)
    monkeypatch.setitem(sys.modules, "pynput", pynput)
    hit = []
    monkeypatch.setattr(qb, "explain_clipboard", lambda: hit.append("clip"))
    assert qb.start_hotkeys() is True
    lst = made[0]
    assert set(lst.mapping) == {"<ctrl>+<alt>+a", "<ctrl>+<alt>+c"} and lst.started and lst.daemon
    import threading

    started = []
    real = threading.Thread

    class T(real):
        def start(self):
            started.append(self)
            super().start()

    monkeypatch.setattr(threading, "Thread", T)
    lst.mapping["<ctrl>+<alt>+c"]()
    started[0].join(2)
    assert hit == ["clip"] and started[0].daemon
    qb.stop()
    assert lst.stopped and qb.server.stopped


def test_quick_cmd_prints_and_stops(monkeypatch, capsys):
    made = []

    class QB:
        def __init__(self, cfg):
            self.opened = 0
            self.stopped = False
            self.url = "http://q"
            self.hotkeys = {"open": "A", "region": "S", "clipboard": "C"}
            made.append(self)

        def open(self):
            self.opened += 1

        def start_hotkeys(self):
            return self.ok

        def stop(self):
            self.stopped = True

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(quick, "QuickBar", QB)
    monkeypatch.setattr("alice.config.load_config", lambda: Config(default_config_dict()))
    patch_sleep(monkeypatch, quick, interrupt)
    QB.ok = True
    assert quick.quick_cmd(["--open"]) == 0
    out = capsys.readouterr().out
    assert "A open" in out and made[0].opened == 1 and made[0].stopped
    QB.ok = False
    QB.hotkey_problem = "pynput yok"  # the real reason is shown (pynput missing, Wayland, no display)
    assert quick.quick_cmd([]) == 0
    out = capsys.readouterr().out
    assert "pynput yok" in out and "http://q" in out and made[1].opened == 0 and made[1].stopped


# ------------------------------------------------------------------ phone
def test_lan_ip(monkeypatch):
    class Sock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def connect(self, addr):
            assert addr[0] == "10.255.255.255"

        def getsockname(self):
            return ("192.168.1.50", 1234)

    monkeypatch.setattr(phone.socket, "socket", Sock)
    assert phone.lan_ip() == "192.168.1.50"

    class Down(Sock):
        def connect(self, addr):
            raise OSError("ağ yok")

    monkeypatch.setattr(phone.socket, "socket", Down)
    assert phone.lan_ip() == "127.0.0.1"


def test_token_persistent_short_and_new():
    t1 = phone.token()
    assert len(t1) >= 24 and phone.token() == t1
    p = alice_home() / "phone-token"
    assert p.read_text(encoding="utf-8") == t1
    p.write_text("  kisa \n", encoding="utf-8")
    t2 = phone.token()
    assert t2 != "kisa" and len(t2) >= 24
    t3 = phone.token(new=True)
    assert t3 != t2 and phone.token() == t3


def test_token_survives_chmod_error(monkeypatch):
    from pathlib import Path

    def bad_chmod(self, mode):
        raise OSError("desteklenmiyor")

    monkeypatch.setattr(Path, "chmod", bad_chmod)
    assert len(phone.token()) >= 24


def test_phone_server_config_port_hosts_and_urls(fake_server, monkeypatch):
    monkeypatch.setattr(phone, "lan_ip", lambda: "10.0.0.7")
    data = default_config_dict()
    data["phone"] = {"port": 9999}
    st = fake_state()
    ps = phone.PhoneServer(Config(data), store="STORE", state=st)
    assert ps.port == 9999 and ps.server.port == 9999 and ps.server.host == "0.0.0.0"
    assert st.allowed_hosts == {"127.0.0.1", "localhost", "10.0.0.7"} and st.watch_store == "STORE"
    assert st.token == phone.token()
    assert ps.url == f"http://10.0.0.7:9999/m?t={st.token}"
    assert ps.watch_url == "http://10.0.0.7:9999/api/watch"
    ps.stop()
    assert ps.server.stopped
    ps2 = phone.PhoneServer(Config(data), store="S", port=0, state=fake_state(), new_token=True)
    assert ps2.port == 0 and ps2.state.token != st.token  # an explicitly given port=0 does not fall back to the config


def test_phone_server_default_port(fake_server, monkeypatch):
    monkeypatch.setattr(phone, "lan_ip", lambda: "1.2.3.4")
    data = default_config_dict()
    data.pop("phone", None)
    assert phone.PhoneServer(Config(data), store="S", state=fake_state()).port == 8765


def test_print_qr(monkeypatch):
    lines = []
    console = SimpleNamespace(print=lines.append)

    class QR:
        def __init__(self, border):
            self.data = None

        def add_data(self, d):
            self.data = d

        def make(self):
            pass

        def get_matrix(self):
            return [[True, False], [False, True]]

    monkeypatch.setitem(sys.modules, "qrcode", SimpleNamespace(QRCode=QR))
    phone.print_qr(console, "http://x")
    assert lines == ["██  ", "  ██"]
    monkeypatch.setitem(sys.modules, "qrcode", None)
    phone.print_qr(console, "http://x")  # silently passes
    assert len(lines) == 2


def test_phone_cmd(monkeypatch, capsys):
    made = []

    class PS:
        def __init__(self, cfg, new_token=False):
            self.new_token = new_token
            self.url, self.watch_url = "http://ip:1/m?t=T", "http://ip:1/api/watch"
            self.projects_url = "http://ip:1/?t=T#projects"
            self.app_url = "http://ip:1/api/phone/app.apk?t=T"
            self.state = SimpleNamespace(token="T")
            self.stopped = False
            made.append(self)

        def stop(self):
            self.stopped = True

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(phone, "PhoneServer", PS)
    monkeypatch.setattr(phone, "print_qr", lambda c, t: None)
    monkeypatch.setattr("alice.config.load_config", lambda: Config(default_config_dict()))
    patch_sleep(monkeypatch, phone, interrupt)
    assert phone.phone_cmd(["--new-token"]) == 0
    out = capsys.readouterr().out
    assert made[0].new_token and made[0].stopped and "http://ip:1/m?t=T" in out and "X-Alice-Token: T" in out


def test_watch_cmd_status_empty_and_usage(capsys):
    assert phone.watch_cmd([]) == 0
    assert "no watch data yet" in capsys.readouterr().out
    assert phone.watch_cmd(["nonsense"]) == 2
    assert "alice watch status" in capsys.readouterr().out


@pytest.mark.parametrize("age,expected", [(30, "30 s ago"), (600, "10 min ago"), (9000, "2.5 h ago"),
                                          (None, "never")])
def test_watch_cmd_status_age_format(monkeypatch, capsys, age, expected):
    def cadence(st, key):
        if key == "watch.hr":
            return {"count": 3 if age is not None else 1, "last_age_s": age, "median_gap_s": 12.0,
                    "verdict": "canlı"}
        return {"count": 0, "last_age_s": None, "verdict": "veri yok"}

    monkeypatch.setattr("alice.health.watch.cadence", cadence)
    assert phone.watch_cmd(["status"]) == 0
    out = " ".join(capsys.readouterr().out.split())  # the console may wrap long lines
    assert f"last sample {expected}" in out and "typical gap 12 s" in out and "canlı" in out
    assert "watch.steps" not in out


def test_watch_cmd_import(monkeypatch, capsys, tmp_path):
    got = []

    def imp(path, st):
        got.append(path)
        return {"watch.hr": 5}

    monkeypatch.setattr("alice.health.watch.import_gadgetbridge", imp)
    assert phone.watch_cmd(["import", str(tmp_path / "Gadgetbridge.db")]) == 0
    out = capsys.readouterr().out
    assert "imported:" in out and "watch.hr 5" in out and got[0].name == "Gadgetbridge.db"
    monkeypatch.setattr("alice.health.watch.import_gadgetbridge", lambda p, s: {})
    phone.watch_cmd(["import", "x.db"])
    assert "yeni veri yok" in capsys.readouterr().out


def test_start_hotkeys_windows_uses_registerhotkey(fake_server, monkeypatch):
    qb, _ = make_qb(fake_server, {"region_hotkey": ""})
    made = []

    class FakeWin:
        def __init__(self, mapping):
            self.mapping, self.ok = mapping, True
            made.append(self)

        def start(self):
            return self.ok

        def stop(self):
            pass

    monkeypatch.setattr("alice.hotkeys.IS_WIN", True)
    monkeypatch.setattr("alice.hotkeys.WinHotkeys", FakeWin)
    monkeypatch.setitem(sys.modules, "pynput", None)  # the Windows path never touches pynput
    assert qb.start_hotkeys() is True and qb._listener is made[0]
    assert set(made[0].mapping) - {""} == {"<ctrl>+<alt>+a", "<ctrl>+<alt>+c"}  # WinHotkeys drops the empty ones
    FakeWin.start = lambda self: False
    qb._listener = None
    assert qb.start_hotkeys() is False and qb._listener is None


def test_phone_token_leaves_url_after_pairing(monkeypatch):
    """The ?t= in the QR address becomes a cookie and redirects to the keyless address; then a cookie (GET) or
    a Bearer/X-Alice-Token header is enough. A cookie alone is not enough for POST; a wrong key is always denied."""
    import http.client
    import threading

    from alice.app import relay
    from alice.app.server import QuietHTTPServer, make_handler

    monkeypatch.setattr(relay, "host", lambda: "")
    tok = "T" * 32
    st = SimpleNamespace(token=tok, allowed_hosts={"127.0.0.1"})
    httpd = QuietHTTPServer(("127.0.0.1", 0), phone.phone_handler(make_handler(st), st))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def req(method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=b"{}" if method == "POST" else None, headers=headers or {})
        r = c.getresponse()
        r.read()
        c.close()
        return r

    try:
        r = req("GET", f"/m?t={tok}&r=https%3A%2F%2Fx")
        assert r.status == 303 and r.getheader("Location") == "/m?r=https%3A%2F%2Fx"
        cookie = r.getheader("Set-Cookie")
        assert f"{phone.COOKIE}={tok}" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert "Secure" not in cookie  # local network, plain HTTP
        assert req("GET", "/m?t=yanlis").status == 403  # a wrong key is not redirected
        assert req("GET", "/m").status == 403
        jar = {"Cookie": f"{phone.COOKIE}={tok}"}
        assert req("GET", "/m", jar).status == 200
        assert req("GET", "/m", {"Cookie": f"{phone.COOKIE}=yanlis"}).status == 403
        assert req("GET", "/m", {"Authorization": f"Bearer {tok}"}).status == 200
        assert req("GET", f"/m?t={tok}", {"Host": "evil.example"}).status == 403  # the Host check comes first
        assert req("POST", "/api/yok", jar).status == 403  # a cookie is not enough for a request that writes
        assert req("POST", "/api/yok", {"Authorization": f"Bearer {tok}"}).status != 403
        assert req("POST", "/api/yok", {"X-Alice-Token": tok}).status != 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_phone_server_installs_phone_handler(monkeypatch):
    """PhoneServer replaces the real server's handler with the phone handler (before starting it)."""
    calls = []

    class Srv(FakeServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.httpd.RequestHandlerClass = type("Base", (), {})

        def start(self):
            calls.append(self.httpd.RequestHandlerClass.__name__)
            return self

    monkeypatch.setattr("alice.app.server.AppServer", Srv)
    monkeypatch.setattr(phone, "lan_ip", lambda: "10.0.0.7")
    ps = phone.PhoneServer(Config(default_config_dict()), store="S", state=fake_state())
    assert calls == ["PhoneHandler"] and ps.url.endswith(f"/m?t={ps.state.token}")
