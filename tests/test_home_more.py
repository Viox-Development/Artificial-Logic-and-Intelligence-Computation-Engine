"""Home control, extra tests: code pages, timeouts, unreachable device messages, modem/TV error paths.

No real network or devices: subprocess, httpx (MockTransport) and WebSocket are mocked.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from alice.home import android, modem, net, pc, samsung, wifi, zte
from alice.tools.base import ToolContext, ToolError
from alice.tools.home import PC, Android, SamsungTV, Wifi


@pytest.fixture(autouse=True)
def _home_network(monkeypatch):
    monkeypatch.setattr(net, "local_ip", lambda: "192.168.1.34")


def _ctx(tmp_path, **sections):
    from alice.config import load_config

    cfg = load_config()
    cfg.data.update(sections)
    return ToolContext(cwd=Path(tmp_path), config=cfg)


def _done(cmd, out="", err="", code=0):
    return subprocess.CompletedProcess(cmd, code, out, err)


# ---------- net.run: code page and timeout ----------

def test_run_uses_console_codepage_on_windows_but_utf8_for_adb(monkeypatch):
    seen = []
    monkeypatch.setattr(net.subprocess, "run", lambda cmd, **kw: seen.append(kw["encoding"]) or _done(cmd))
    monkeypatch.setattr(net.sys, "platform", "win32")
    net.run(["netsh", "wlan", "show", "interfaces"])
    android.run(["adb", "devices"])
    assert seen == ["oem", "utf-8"]


def test_adb_output_with_turkish_letters_survives_decoding():
    # adb returns device text as UTF-8; it must not be decoded with the console code page, even on Windows.
    code = "import sys; sys.stdout.buffer.write('Şükrü: ekmek al ğıİ'.encode('utf-8'))"
    assert android.run([sys.executable, "-c", code]).stdout == "Şükrü: ekmek al ğıİ"


def test_missing_command_raises_oserror_for_tools_to_explain(tmp_path):
    with pytest.raises(OSError):
        net.run([str(tmp_path / "olmayan-komut")])


# ---------- net: ARP, devices, WoL ----------

def test_arp_parse_macos_short_octets_and_dedupe():
    text = ("? (192.168.1.1) at 0:1a:2b:3c:4d:5e on en0 ifscope [ethernet]\n"
            "? (192.168.1.7) at a4:b1:c:d:e:f on en0 ifscope [ethernet]\n"
            "? (192.168.1.9) at (incomplete) on en0 ifscope [ethernet]\n"
            "? (192.168.1.1) at 0:1a:2b:3c:4d:5e on en0 ifscope [ethernet]\n")
    assert net.parse_arp(text) == [("192.168.1.1", "00:1a:2b:3c:4d:5e"), ("192.168.1.7", "a4:b1:0c:0d:0e:0f")]


def test_arp_parse_windows_turkish_header_is_ignored():
    text = ("\nArabirim: 192.168.1.34 --- 0xb\n  İnternet Adresi       Fiziksel Adres        Tür\n"
            "  192.168.1.1           a4-b1-c1-d1-e1-f1     dinamik\n"
            "  192.168.1.255         ff-ff-ff-ff-ff-ff     statik\n")
    assert net.parse_arp(text) == [("192.168.1.1", "a4:b1:c1:d1:e1:f1")]


def test_devices_keeps_only_own_subnet_and_names(monkeypatch):
    arp = ("  192.168.1.1   a4-b1-c1-d1-e1-f1   dinamik\n"
           "  10.0.0.5      00-11-22-33-44-55   dinamik\n"
           "  192.168.1.50  3c-2a-f4-aa-bb-cc   dinamik\n")
    monkeypatch.setattr(net, "run", lambda cmd, timeout=15: _done(cmd, arp))
    monkeypatch.setattr(net, "_hostname", lambda ip: "modem" if ip.endswith(".1") else "")
    assert net.devices(sweep=False) == [{"ip": "192.168.1.1", "mac": "a4:b1:c1:d1:e1:f1", "name": "modem"},
                                        {"ip": "192.168.1.50", "mac": "3c:2a:f4:aa:bb:cc", "name": ""}]


def test_magic_packet_rejects_bad_mac():
    with pytest.raises(ValueError, match="geçersiz MAC"):
        net.magic_packet("aa:bb:cc")


def test_wifi_tool_wake_sends_to_subnet_broadcast_and_explains_bad_mac(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(net, "wake_on_lan", lambda mac, bc: sent.append((mac, bc)))
    out = Wifi().run(_ctx(tmp_path), action="wake", mac="aa:bb:cc:dd:ee:ff").text
    assert "sent" in out and sent == [("aa:bb:cc:dd:ee:ff", "192.168.1.255")]
    monkeypatch.undo()
    monkeypatch.setattr(net, "local_ip", lambda: "192.168.1.34")
    with pytest.raises(ToolError, match="geçersiz MAC"):
        Wifi().run(_ctx(tmp_path), action="wake", mac="zz")


# ---------- Wi-Fi ----------

def test_wifi_signal_with_leading_percent_turkish_format():
    s = wifi.parse_status("    SSID                   : EvAgi\n    Sinyal                 : %87\n")
    assert s["connected"] and s["signal"] == 87
    nets = wifi.parse_networks("SSID 1 : A\n    Kimlik doğrulama : Açık\n    BSSID 1 : aa:bb:cc:dd:ee:ff\n"
                               "         Sinyal : %35\nSSID 2 : B\n         Sinyal : 70 %\n")
    assert [(n["ssid"], n["signal"], n["auth"]) for n in nets] == [("B", 70, ""), ("A", 35, "Açık")]


class FakeNetsh:
    def __init__(self, replies, fail=()):
        self.replies, self.fail, self.calls = replies, fail, []

    def __call__(self, cmd, timeout=15):
        self.calls.append(cmd)
        joined = " ".join(cmd[2:])
        if any(f in joined for f in self.fail):
            return _done(cmd, "Belirtilen ağ kullanılamıyor.", "", 1)
        return _done(cmd, next((v for k, v in self.replies.items() if joined.startswith(k)), ""))


@pytest.fixture
def netsh(monkeypatch):
    monkeypatch.setattr(wifi.sys, "platform", "win32")

    def make(replies, fail=()):
        f = FakeNetsh(replies, fail)
        monkeypatch.setattr(wifi, "run", f)
        return f
    return make


def test_wifi_tool_status_scan_saved_disconnect(netsh, tmp_path):
    netsh({"show interfaces": "    SSID                   : EvAgi\n    Signal                 : 87%\n",
           "show networks": "SSID 1 : EvAgi\n    Authentication : WPA2-Personal\n    Signal : 90%\nSSID 2 : \n",
           "show profiles": "Kullanıcı profilleri\n    Tüm Kullanıcı Profili     : EvAgi\n"})
    ctx = _ctx(tmp_path)
    assert Wifi().run(ctx, action="status").text.startswith("Bağlı: EvAgi, sinyal %87")
    assert Wifi().run(ctx, action="scan").text == "EvAgi: %90 WPA2-Personal\n(gizli): %0 "
    assert Wifi().run(ctx, action="saved").text == "EvAgi"
    assert Wifi().run(ctx, action="disconnect").text == "Wi-Fi bağlantısı kesildi"
    with pytest.raises(ToolError, match="ssid is required"):
        Wifi().run(ctx, action="connect")


def test_wifi_connect_writes_profile_removes_temp_file_and_hides_password(netsh, tmp_path):
    written = {}
    f = netsh({"show profiles": "", "show networks": ""}, fail=("connect name=",))
    orig = f.__call__

    def spy(cmd, timeout=15):
        arg = next((a for a in cmd if a.startswith("filename=")), None)
        if arg:
            path = Path(arg.split("=", 1)[1])
            written["path"], written["xml"] = path, path.read_text(encoding="utf-8")
        return orig(cmd, timeout)

    wifi.run = spy  # the netsh fixture's monkeypatch restores the original at the end
    with pytest.raises(ToolError) as e:
        Wifi().run(_ctx(tmp_path), action="connect", ssid="Ev & Ağ", password="gizli<sifre>")
    assert "gizli" not in str(e.value) and "kullanılamıyor" in str(e.value)
    assert "<keyMaterial>gizli&lt;sifre&gt;</keyMaterial>" in written["xml"]
    assert "<name>Ev &amp; Ağ</name>" in written["xml"]
    assert not written["path"].exists()


def test_wifi_known_network_connects_without_profile(netsh):
    f = netsh({"show profiles": "    All User Profile     : EvAgi\n"})
    assert wifi.connect("EvAgi") == "'EvAgi' ağına bağlanılıyor"
    assert [c[2:] for c in f.calls] == [["show", "profiles"], ["connect", "name=EvAgi"]]


def test_wifi_netsh_error_without_output_has_default_message(monkeypatch):
    monkeypatch.setattr(wifi.sys, "platform", "win32")
    monkeypatch.setattr(wifi, "run", lambda cmd, timeout=15: _done(cmd, "", "", 1))
    with pytest.raises(RuntimeError, match="netsh hata verdi"):
        wifi.disconnect()


# ---------- PC (mocking Windows commands) ----------

@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "win32")

    def make(fn):
        monkeypatch.setattr(pc, "run", fn)
    return make


POWERCFG = """
Güç Düzeni GUID'si: 381b4222-f694-41f0-9685-ff5bb260df2e  (Dengeli) *
Güç Düzeni GUID'si: a1841308-3bc4-4ac8-ba48-0d6b2e1bf3f2  (Güç tasarrufu)
"""


def test_pc_plans_and_set_plan(windows, tmp_path):
    calls = []

    def fake(cmd, timeout=15):
        calls.append(cmd)
        return _done(cmd, POWERCFG if cmd[1] == "/list" else "")

    windows(fake)
    assert PC().run(_ctx(tmp_path), action="plans").text == "* Dengeli\n  Güç tasarrufu"
    assert PC().run(_ctx(tmp_path), action="set_plan", plan="power saver").text == "power plan: Güç tasarrufu"
    assert calls[-1] == ["powercfg", "/setactive", "a1841308-3bc4-4ac8-ba48-0d6b2e1bf3f2"]
    with pytest.raises(ToolError, match="Available: Dengeli, Güç tasarrufu"):
        PC().run(_ctx(tmp_path), action="set_plan", plan="oyun")
    with pytest.raises(ToolError, match="plan is required"):
        PC().run(_ctx(tmp_path), action="set_plan")


def test_pc_restart_and_errors(windows, tmp_path):
    windows(lambda cmd, timeout=15: _done(cmd))
    assert pc.restart_in(2) == "restarting in 120 s (cancel: media cancel_shutdown)"
    assert pc.restart_in(-5) == "restarting"
    windows(lambda cmd, timeout=15: _done(cmd, "", "Erişim engellendi.", 5))
    with pytest.raises(ToolError, match="Erişim engellendi"):
        PC().run(_ctx(tmp_path), action="restart", minutes=1)

    def slow(cmd, timeout=15):
        raise TimeoutError("powercfg 15 sn içinde cevap vermedi")

    windows(slow)
    with pytest.raises(ToolError, match="cevap vermedi"):
        PC().run(_ctx(tmp_path), action="plans")


def test_pc_internal_display_brightness_via_wmi(windows):
    seen = []
    windows(lambda cmd, timeout=15: seen.append(cmd[-1]) or _done(cmd, "70\r\n" if "CurrentBrightness" in cmd[-1]
                                                                   else "ok"))
    assert pc._wmi_brightness(None) == "built-in screen 70%"
    assert pc._wmi_brightness(40) == "built-in screen 40%" and "[byte]40" in seen[-1]
    windows(lambda cmd, timeout=15: _done(cmd, "", "Desteklenmiyor", 1))
    with pytest.raises(RuntimeError, match="DDC/CI"):
        pc._wmi_brightness(None)


def test_pc_wol_info(windows):
    def fake(cmd, timeout=15):
        if cmd[0] == "powershell":
            return _done(cmd, "Ethernet|Realtek PCIe GbE|D8-BB-C1-00-11-22|802.3\r\n")
        return _done(cmd, "Realtek PCIe GbE Family Controller\r\n")

    windows(fake)
    out = pc.wol_info()
    assert "IP: 192.168.1.34" in out and "Broadcast address: 192.168.1.255" in out
    assert "MAC D8:BB:C1:00:11:22" in out and "Devices that can wake it: Realtek" in out


def test_pc_unknown_action(tmp_path):
    with pytest.raises(ToolError, match="unknown action"):
        PC().run(_ctx(tmp_path), action="dans")


# ---------- Android ----------

def _adb(monkeypatch, fn):
    monkeypatch.setattr(android.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(android, "run", fn)


def test_ensure_does_not_mistake_longer_port_for_our_device(monkeypatch):
    calls = []

    def fake(cmd, timeout=15):
        calls.append(cmd[1:])
        if cmd[1] == "devices":
            return _done(cmd, "List of devices attached\n192.168.1.50:55551\tdevice\n\n")
        return _done(cmd, "connected to 192.168.1.50:5555")

    _adb(monkeypatch, fake)
    android.Device("192.168.1.50", "tv").ensure()
    assert ["connect", "192.168.1.50:5555"] in calls


def test_ensure_skips_connect_when_listed(monkeypatch):
    calls = []
    _adb(monkeypatch, lambda cmd, timeout=15: calls.append(cmd[1:]) or
         _done(cmd, "List of devices attached\n192.168.1.50:5555\tdevice\n"))
    android.Device("192.168.1.50", "tv").ensure()
    assert calls == [["devices"]]


def test_screenshot_timeout_is_explained(monkeypatch):
    _adb(monkeypatch, lambda cmd, timeout=15: _done(cmd))

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(android.subprocess, "run", slow)
    with pytest.raises(android.AdbError, match="20 sn içinde cevap vermedi"):
        android.Device("192.168.1.50", "tv").screenshot()


def test_screenshot_failure_and_success(monkeypatch):
    _adb(monkeypatch, lambda cmd, timeout=15: _done(cmd))
    monkeypatch.setattr(android.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"error: device offline"))
    with pytest.raises(android.AdbError, match="bağlı değil"):
        android.Device("192.168.1.50", "tv").screenshot()

    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000)).save(buf, "PNG")
    monkeypatch.setattr(android.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, buf.getvalue(), b""))
    png = base64.b64decode(android.Device("192.168.1.50", "tv").screenshot(width=500))
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (500, 250)


def test_android_tool_screenshot_timeout_becomes_tool_error(monkeypatch, tmp_path):
    _adb(monkeypatch, lambda cmd, timeout=15: _done(cmd, "List of devices attached\n192.168.1.50:5555\tdevice\n"))

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(android.subprocess, "run", slow)
    with pytest.raises(ToolError, match="cevap vermedi"):
        Android().run(_ctx(tmp_path, android={"tv": "192.168.1.50:5555"}), device="tv", action="screenshot")


def test_shell_errors_are_explained(monkeypatch):
    d_out = {"cmd": ("", "error: device '192.168.1.50:5555' not found", 1)}
    _adb(monkeypatch, lambda cmd, timeout=15: _done(cmd, *d_out["cmd"]))
    d = android.Device("192.168.1.50", "phone")
    with pytest.raises(android.AdbError, match="bağlı değil"):
        d.shell("echo")
    d_out["cmd"] = ("", "", 1)
    with pytest.raises(android.AdbError, match="adb hata verdi"):
        d.shell("echo")
    d_out["cmd"] = ("", "failed to connect to '192.168.1.50:5555': Connection refused", 1)
    with pytest.raises(android.AdbError, match="kabul etmedi"):
        d.shell("echo")


def test_connect_timeout_and_plain_failure(monkeypatch):
    def slow(cmd, timeout=15):
        raise TimeoutError("adb 10 sn içinde cevap vermedi")

    _adb(monkeypatch, slow)
    with pytest.raises(android.AdbError, match="aynı ağda mı"):
        android.Device("192.168.1.50", "tv").connect()
    _adb(monkeypatch, lambda cmd, timeout=15: _done(cmd))
    with pytest.raises(android.AdbError, match="adresine bağlanılamadı"):
        android.Device("192.168.1.50", "tv").connect()


def test_phone_battery_notifications_push_and_text(monkeypatch, tmp_path):
    replies = {"dumpsys battery": "  AC powered: false\n  USB powered: true\n  level: 45\n  scale: 100\n"
                                  "  temperature: 312\n",
               "dumpsys notification": "",
               "devices": "List of devices attached\n192.168.1.11:41001\tdevice\n"}
    calls = []

    def fake(cmd, timeout=15):
        calls.append(cmd)
        j = " ".join(cmd)
        if cmd[3:4] == ["push"]:
            return _done(cmd, "", "adb: error: failed to copy: No space left", 1)
        return _done(cmd, next((v for k, v in replies.items() if k in j), ""))

    monkeypatch.setattr(android.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(android, "run", fake)
    monkeypatch.setattr(android.Device, "_run", lambda self, *a, timeout=15, binary=False:
                        fake([self.adb, "-s", self.serial, *a]))
    ctx = _ctx(tmp_path, android={"phone": "192.168.1.11:41001"})
    assert Android().run(ctx, device="phone", action="battery").text == "pil %45, şarj oluyor, 31°C"
    assert Android().run(ctx, device="phone", action="notifications").text == "no notifications"
    Android().run(ctx, device="phone", action="type", text="merhaba dünya")
    assert calls[-1][-1] == "input text 'merhaba%sdünya'"
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ToolError, match="No space left"):
        Android().run(ctx, device="phone", action="send_file", path=str(f))
    with pytest.raises(ToolError, match="file not found"):
        Android().run(ctx, device="phone", action="send_file", path=str(tmp_path / "yok.txt"))


def test_android_tool_missing_arguments(monkeypatch, tmp_path):
    _adb(monkeypatch, lambda cmd, timeout=15: _done(cmd, "List of devices attached\n192.168.1.50:5555\tdevice\n"))
    ctx = _ctx(tmp_path, android={"tv": "192.168.1.50:5555"})
    for action, msg in (("key", "key is required"), ("open_app", "app is required"), ("open_url", "url is required"),
                        ("youtube", "query or url"), ("send_file", "path is required")):
        with pytest.raises(ToolError, match=msg):
            Android().run(ctx, device="tv", action=action)
    with pytest.raises(ToolError, match="pair needs host"):
        Android().run(ctx, device="phone", action="pair")


# ---------- Samsung TV ----------

class ConnectionClosed(Exception):
    """Stand-in for websockets.exceptions.ConnectionClosed (even when the package is not installed)."""


class ConnectionClosedError(ConnectionClosed):
    pass


class ClosingWS:
    def __init__(self, hello=True, on_send=False):
        self.hello, self.on_send = hello, on_send

    def recv(self, timeout=None):
        if self.hello:
            self.hello = False
            return '{"event": "ms.channel.connect", "data": {"token": "77"}}'
        raise ConnectionClosedError("no close frame received or sent")

    def send(self, data):
        if self.on_send:
            raise ConnectionClosedError("sent 1000 (OK)")

    def close(self):
        pass


def _ws(monkeypatch, ws):
    import types

    mod = types.ModuleType("websockets.sync.client")
    mod.connect = lambda *a, **k: ws
    monkeypatch.setitem(sys.modules, "websockets.sync.client", mod)
    monkeypatch.setattr(samsung.time, "sleep", lambda s: None)


def test_samsung_closed_connection_is_explained(monkeypatch):
    _ws(monkeypatch, ClosingWS())
    with samsung.Remote("192.168.1.60", "77") as r:
        with pytest.raises(samsung.TVError, match="closed the remote-control connection"):
            r.installed_apps()
    _ws(monkeypatch, ClosingWS(on_send=True))
    with samsung.Remote("192.168.1.60", "77") as r:
        with pytest.raises(samsung.TVError, match="closed the remote-control connection"):
            r.keys("KEY_HOME")
    _ws(monkeypatch, ClosingWS(hello=False))
    with pytest.raises(samsung.TVError, match="closed the remote-control connection"):
        with samsung.Remote("192.168.1.60", "77"):
            pass


def test_samsung_tool_key_on_closed_connection_is_tool_error(monkeypatch, tmp_path):
    _ws(monkeypatch, ClosingWS(on_send=True))
    ctx = _ctx(tmp_path, samsung_tv={"host": "192.168.1.60", "token": "77"})
    with pytest.raises(ToolError, match="closed the remote-control connection"):
        SamsungTV().run(ctx, action="key", key="volume_up")


def test_samsung_info_without_host_and_odd_reply(monkeypatch):
    with pytest.raises(samsung.TVError, match="no Samsung TV address"):
        samsung.info("")
    monkeypatch.setattr(samsung.httpx, "get", lambda url, timeout: httpx.Response(
        200, json=[1, 2], request=httpx.Request("GET", url)))
    with pytest.raises(samsung.TVError, match="like a Samsung TV"):
        samsung.info("192.168.1.60")

    def down(url, timeout):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(samsung.httpx, "get", down)
    with pytest.raises(samsung.TVError, match="off or not on the network"):
        samsung.info("192.168.1.60")


def test_samsung_status_before_connect_is_clear(tmp_path):
    assert SamsungTV().run(_ctx(tmp_path, samsung_tv={}), action="status").text == "the TV is off (not answering on the network)"


def test_samsung_connect_keeps_token_even_if_rest_info_fails(monkeypatch, tmp_path):
    from alice.config import load_config

    _ws(monkeypatch, ClosingWS())

    def down(host, timeout=3):
        raise samsung.TVError("kapalı")

    monkeypatch.setattr(samsung, "info", down)
    ctx = _ctx(tmp_path, samsung_tv={})
    out = SamsungTV().run(ctx, action="connect", host="192.168.1.60").text
    assert "connected" in out and "could not read the MAC" in out
    assert ctx.config.data["samsung_tv"] == {"host": "192.168.1.60", "token": "77"}
    assert load_config().data["samsung_tv"]["token"] == "77"


def test_samsung_power_with_bad_saved_mac_is_tool_error(monkeypatch, tmp_path):
    def down(host, timeout=3):
        raise samsung.TVError("kapalı")

    monkeypatch.setattr(samsung, "info", down)
    ctx = _ctx(tmp_path, samsung_tv={"host": "192.168.1.60", "mac": "bozuk"})
    with pytest.raises(ToolError, match="geçersiz MAC"):
        SamsungTV().run(ctx, action="power", state="on")


def test_samsung_status_and_unknown_key(monkeypatch, tmp_path):
    monkeypatch.setattr(samsung, "info", lambda host, timeout=3: {"device": {"PowerState": "on", "name": "Salon",
                                                                             "modelName": "QE55"}})
    ctx = _ctx(tmp_path, samsung_tv={"host": "192.168.1.60", "token": "77"})
    assert SamsungTV().run(ctx, action="status").text == "TV on: Salon QE55"
    assert SamsungTV().run(ctx, action="power", state="on").text == "the TV is already on"
    _ws(monkeypatch, ClosingWS())
    with pytest.raises(ToolError, match="unknown key"):
        SamsungTV().run(ctx, action="key", key="uç")


# ---------- modem (UPnP) ----------

IGD = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<friendlyName>Ev Modemi</friendlyName><manufacturer>ZTE</manufacturer><modelName>H3600</modelName>
<serviceList>
<service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
<controlURL>/ctl/IPConn</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
<controlURL>/ctl/CmnIfCfg</controlURL></service>
</serviceList></device></root>"""


def _soap(action, **vals):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in vals.items())
    return (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="x">{body}</u:{action}Response></s:Body></s:Envelope>')


def _mock_httpx(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(modem.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_modem_info_full_report_skips_broken_location(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/desc.xml":
            return httpx.Response(200, text=IGD)
        if "SOAPAction" not in req.headers:
            return httpx.Response(404)
        action = req.headers["SOAPAction"].split("#")[1].strip('"')
        replies = {
            "GetExternalIPAddress": _soap(action, NewExternalIPAddress="88.1.2.3"),
            "GetStatusInfo": _soap(action, NewConnectionStatus="Connected", NewUptime="3700"),
            "GetCommonLinkProperties": _soap(action, NewLayer1DownstreamMaxBitRate="100000000",
                                             NewLayer1UpstreamMaxBitRate="bilinmiyor"),
            "GetTotalBytesReceived": _soap(action, NewTotalBytesReceived=str(3 * 1024 ** 3)),
            "GetTotalBytesSent": _soap(action, NewTotalBytesSent=str(512 * 1024 ** 2)),
        }
        return httpx.Response(200, text=replies[action])

    monkeypatch.setattr(modem, "discover", lambda: ["http://host:abc/desc.xml", "http://xn--/d.xml",
                                                   "http://192.168.1.1:5000/desc.xml"])
    _mock_httpx(monkeypatch, handler)
    out = modem.info().splitlines()
    assert out == ["Modem: ZTE H3600 (192.168.1.1)", "External IP: 88.1.2.3",
                   "Connection: Connected, up for 1 h 1 min", "Line speed: 100 / 0 Mbps",
                   "Traffic (since the modem started, the counter can wrap): downloaded 3.0 GB, uploaded 512.0 MB"]


def test_modem_info_soap_failure_is_skipped(monkeypatch):
    def handler(req):
        if req.url.path == "/desc.xml":
            return httpx.Response(200, text=IGD)
        return httpx.Response(500, text="<s:Fault/>")

    monkeypatch.setattr(modem, "discover", lambda: ["http://192.168.1.1:5000/desc.xml"])
    _mock_httpx(monkeypatch, handler)
    assert modem.info() == "Modem: ZTE H3600 (192.168.1.1)"


def test_modem_info_without_upnp_reads_gateway_title(monkeypatch):
    monkeypatch.setattr(modem, "discover", lambda: [])
    _mock_httpx(monkeypatch, lambda req: httpx.Response(200, text="<title>ZXHN H3600</title>",
                                                        headers={"server": "Mini web server"}))
    out = modem.info()
    assert "gateway 192.168.1.1" in out and "'ZXHN H3600'" in out and "server: Mini web server" in out


def test_modem_info_when_nothing_answers(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    monkeypatch.setattr(modem, "discover", lambda: [])
    _mock_httpx(monkeypatch, handler)
    assert "Modem not found (UPnP is off, 192.168.1.1 does not answer)" in modem.info()


def test_discover_collects_unique_locations(monkeypatch):
    replies = [b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.1:5000/desc.xml\r\n\r\n",
               b"HTTP/1.1 200 OK\r\nLocation: http://192.168.1.1:5000/desc.xml\r\n\r\n",
               b"HTTP/1.1 200 OK\r\nST: x\r\n\r\n"]

    class Sock:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def sendto(self, data, addr):
            assert b"InternetGatewayDevice" in data

        def recvfrom(self, n):
            if replies:
                return replies.pop(0), ("192.168.1.1", 1900)
            raise OSError("ağ yok")

        def close(self):
            pass

    monkeypatch.setattr(modem.socket, "socket", Sock)
    assert modem.discover(timeout=1) == ["http://192.168.1.1:5000/desc.xml"]


def test_fmt_helpers_edges():
    assert modem.fmt_bytes(512) == "512 B"
    assert modem.fmt_bytes(3 * 1024 ** 5) == "3072.0 TB"
    assert modem.fmt_uptime(59) == "0 h 0 min"
    assert modem.page_title("<html>başlıksız</html>") == ""


# ---------- ZTE ----------

class MiniZTE:
    def __init__(self, login_reply=None, lan_status=200, password="pw"):
        self.login_reply, self.lan_status, self.password = login_reply, lan_status, password
        self.tags: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        tag = dict(req.url.params).get("_tag", "")
        self.tags.append(tag)
        if tag == "login_entry" and req.method == "GET":
            return httpx.Response(200, json={"lockingTime": "0", "loginErrMsg": "", "sess_token": "S0"})
        if tag == "login_token":
            return httpx.Response(200, text="<ajax_response_xml_root>99</ajax_response_xml_root>")
        if tag == "login_entry":
            if self.login_reply is not None:
                return httpx.Response(200, text=self.login_reply)
            return httpx.Response(200, json={"sess_token": "S1"})
        if tag == "accessdev_landevs_lua.lua" and self.lan_status != 200:
            return httpx.Response(self.lan_status)
        return httpx.Response(200, text='_sessionTmpToken = "\\x41B\\x43";')


def test_zte_tmp_token_with_mixed_escapes_does_not_crash():
    assert zte.tmp_token('_sessionTmpToken = "\\x41B\\x43\\xc5\\x9f";') == "ABCş"


def test_zte_non_json_login_reply_is_modem_error():
    r = zte.ZTE("192.168.1.1", "admin", "pw", httpx.Client(transport=httpx.MockTransport(
        MiniZTE(login_reply="<html>Giriş</html>"))))
    with pytest.raises(zte.ModemError, match="login answer"):
        r.login()


def test_zte_odd_locking_time_does_not_crash():
    zte.ZTE._login_error({"lockingTime": "yok", "loginErrMsg": ""})
    with pytest.raises(zte.ModemError, match="locked for 60 s"):
        zte.ZTE._login_error({"lockingTime": "60"})
    zte.ZTE._login_error({"lockingTime": -1, "loginErrMsg": ""})  # -1 means no lock: must not say "-1 sn kilitli"


def test_zte_password_hash_never_contains_plain_password():
    fake = MiniZTE()
    posts = []
    orig = fake.__call__

    def spy(req):
        if req.method == "POST":
            posts.append(req.content.decode())
        return orig(req)

    r = zte.ZTE("192.168.1.1", "admin", "Gizli123", httpx.Client(transport=httpx.MockTransport(spy)))
    r.login()
    assert posts and all("Gizli123" not in p for p in posts)
    assert hashlib.sha256(b"Gizli12399").hexdigest() in posts[0]


@pytest.fixture
def zte_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEM_PASSWORD", "pw")
    real = httpx.Client

    def make(fake):
        monkeypatch.setattr(zte.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(fake), **kw))
        return _ctx(tmp_path, modem={"host": "192.168.1.1"})
    return make


def test_zte_tool_logs_out_even_when_device_list_fails(zte_tool):
    fake = MiniZTE(lan_status=404)
    ctx = zte_tool(fake)
    with pytest.raises(ToolError, match="HTTP 404"):
        Wifi().run(ctx, action="modem_devices")
    assert fake.tags[-1] == "logout_entry"


def test_zte_tool_success_lists_devices_and_logs_out(zte_tool):
    fake = MiniZTE()
    assert Wifi().run(zte_tool(fake), action="modem_devices").text == "the modem reported no connected devices"
    assert fake.tags[-1] == "logout_entry"


def test_zte_tool_timeout_and_unreachable_messages(zte_tool):
    def slow(req):
        raise httpx.ConnectTimeout("timed out", request=req)

    with pytest.raises(ToolError, match="did not answer in time"):
        Wifi().run(zte_tool(slow), action="modem_devices")

    def refused(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ToolError, match=r"could not reach the modem \(192.168.1.1\): ConnectError"):
        Wifi().run(zte_tool(refused), action="modem_devices")


def test_zte_tool_wrong_password_does_not_log_out_or_leak(zte_tool, monkeypatch):
    monkeypatch.setenv("MODEM_PASSWORD", "YanlisSifre!")
    fake = MiniZTE(login_reply='{"lockingTime": 0, "loginErrMsg": "pwd error"}')
    with pytest.raises(ToolError) as e:
        Wifi().run(zte_tool(fake), action="modem_devices")
    assert "refused" in str(e.value) and "YanlisSifre" not in str(e.value)
    assert "logout_entry" not in fake.tags


def test_wifi_tool_unknown_action(tmp_path):
    with pytest.raises(ToolError, match="unknown action"):
        Wifi().run(_ctx(tmp_path), action="uç")
