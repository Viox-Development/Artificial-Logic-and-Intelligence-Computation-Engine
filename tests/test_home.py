"""Home control: parsing netsh/powercfg/adb output and setting up tool commands (no real devices)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alice.home import android, net, pc, wifi
from alice.tools.base import ToolContext, ToolError
from alice.tools.home import PC, Android, Wifi

@pytest.fixture(autouse=True)
def _home_network(monkeypatch):
    # Run the tests as if on the home network (the CI machine's own IP may be 10.x).
    monkeypatch.setattr(net, "local_ip", lambda: "192.168.1.34")


POWERCFG_TR = """
Mevcut Güç Düzenleri
-----------------------------------
Güç Düzeni GUID'si: 381b4222-f694-41f0-9685-ff5bb260df2e  (Dengeli) *
Güç Düzeni GUID'si: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (Yüksek performans)
Güç Düzeni GUID'si: a1841308-3bc4-4ac8-ba48-0d6b2e1bf3f2  (Güç tasarrufu)
"""

IFACE_EN = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : EvAgi 5G
    BSSID                  : aa:bb:cc:dd:ee:ff
    Radio type             : 802.11ax
    Channel                : 36
    Receive rate (Mbps)    : 1201
    Signal                 : 87%
    Profile                : EvAgi 5G
"""

IFACE_TR_DISCONNECTED = """
Sistemde 1 arabirim var:

    Ad                     : Wi-Fi
    Durum                  : bağlantı kesildi
    Radyo durumu           : Donanım Açık
"""

NETWORKS = """
Interface name : Wi-Fi
There are 2 networks currently visible.

SSID 1 : Komsu
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : 11:22:33:44:55:66
         Signal             : 40%
SSID 2 : EvAgi 5G
    Ağ türü                 : Altyapı
    Kimlik doğrulama        : WPA3-Personal
    Şifreleme               : CCMP
    BSSID 1                 : aa:bb:cc:dd:ee:ff
         Sinyal             : 60%
    BSSID 2                 : aa:bb:cc:dd:ee:00
         Sinyal             : 91%
"""

PROFILES_TR = """
Wi-Fi arabirimindeki profiller:

Grup ilkesi profilleri (salt okunur)
---------------------------------
    <Yok>

Kullanıcı profilleri
-------------
    Tüm Kullanıcı Profili     : EvAgi 5G
    Tüm Kullanıcı Profili     : Okul Misafir
"""


def test_power_plans_parse_and_find_in_turkish():
    plans = pc.parse_plans(POWERCFG_TR)
    assert [p["name"] for p in plans] == ["Dengeli", "Yüksek performans", "Güç tasarrufu"]
    assert plans[0]["active"] and not plans[1]["active"]
    assert pc.find_plan(plans, "performans")["guid"].startswith("8c5e7fda")
    assert pc.find_plan(plans, "high performance")["guid"].startswith("8c5e7fda")
    assert pc.find_plan(plans, "tasarruf")["name"] == "Güç tasarrufu"
    assert pc.find_plan(plans, "balanced")["name"] == "Dengeli"
    assert pc.find_plan(plans, "oyun") is None


def test_wifi_status_parse_language_independent():
    s = wifi.parse_status(IFACE_EN)
    assert s["connected"] and s["ssid"] == "EvAgi 5G" and s["signal"] == 87
    d = wifi.parse_status(IFACE_TR_DISCONNECTED)
    assert not d["connected"] and d["signal"] is None


def test_wifi_networks_take_best_bssid_and_sort():
    nets = wifi.parse_networks(NETWORKS)
    assert [n["ssid"] for n in nets] == ["EvAgi 5G", "Komsu"]
    assert nets[0]["signal"] == 91 and nets[0]["auth"] == "WPA3-Personal"
    assert nets[1]["auth"] == "WPA2-Personal"


def test_wifi_profiles_parse_skips_none_marker():
    assert wifi.parse_profiles(PROFILES_TR) == ["EvAgi 5G", "Okul Misafir"]


def test_profile_xml_escapes_and_open_network():
    x = wifi.profile_xml("Ali & Veli", "p<ss")
    assert "<name>Ali &amp; Veli</name>" in x and "<keyMaterial>p&lt;ss</keyMaterial>" in x and "WPA2PSK" in x
    assert "<authentication>open</authentication>" in wifi.profile_xml("Kafe", "")


def test_wifi_connect_refuses_unknown_secured_network_without_password(monkeypatch):
    monkeypatch.setattr(wifi, "profiles", lambda: [])
    monkeypatch.setattr(wifi, "scan", lambda: [{"ssid": "Komsu", "signal": 40, "auth": "WPA2-Personal"}])
    with pytest.raises(RuntimeError, match="password"):
        wifi.connect("Komsu")


def test_arp_parse_windows_and_linux():
    win = """
Interface: 192.168.1.34 --- 0x7
  Internet Address      Physical Address      Type
  192.168.1.1           a4-2b-b0-11-22-33     dynamic
  192.168.1.50          3c-2a-f4-aa-bb-cc     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""
    lin = "? (192.168.1.1) at a4:2b:b0:11:22:33 [ether] on wlan0\nrouter (10.0.0.1) at <incomplete> on eth0\n"
    assert net.parse_arp(win) == [("192.168.1.1", "a4:2b:b0:11:22:33"), ("192.168.1.50", "3c:2a:f4:aa:bb:cc")]
    assert net.parse_arp(lin) == [("192.168.1.1", "a4:2b:b0:11:22:33")]


def test_magic_packet():
    p = net.magic_packet("AA-BB-CC-DD-EE-FF")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("aabbccddeeff")
    with pytest.raises(ValueError):
        net.magic_packet("aa:bb")


def test_subnet_hosts_excludes_self():
    hosts = net.subnet_hosts("192.168.1.34")
    assert len(hosts) == 253 and "192.168.1.34" not in hosts and hosts[0] == "192.168.1.1"


def test_android_parsers():
    assert android.serial("192.168.1.50") == "192.168.1.50:5555"
    assert android.serial("192.168.1.9:41233") == "192.168.1.9:41233"
    assert android.youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == "dQw4w9WgXcQ"
    assert android.youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert android.youtube_id("lofi") is None
    assert android.parse_wakefulness("  mWakefulness=Asleep\n") == "Asleep"
    b = android.parse_battery("Current Battery Service state:\n  AC powered: false\n  USB powered: true\n"
                              "  level: 83\n  scale: 100\n  temperature: 312\n")
    assert b == {"percent": 83, "charging": True, "temp_c": 31.2}
    assert android.input_text_arg("merhaba dünya; rm") == "'merhaba%sdünya;%srm'"
    mdns = "List of discovered mdns services\nadb-R58N-abc\t_adb-tls-connect._tcp\t192.168.1.9:41233\n"
    assert android.parse_mdns(mdns) == [("_adb-tls-connect._tcp", "192.168.1.9:41233")]


def test_android_notifications_parse():
    dump = """
  NotificationRecord(0x1 pkg=com.whatsapp user=UserHandle{0} id=1 importance=4)
      extras={
        android.title=String (Abla)
        android.text=String (Eve gelirken ekmek al)
      }
  NotificationRecord(0x2 pkg=com.android.systemui user=UserHandle{0} id=2)
        android.title=String (USB hata ayıklama bağlı)
  NotificationRecord(0x3 pkg=com.google.android.gm user=UserHandle{0} id=3)
        android.title=String (Staj programı)
"""
    assert android.parse_notifications(dump) == [
        {"app": "com.whatsapp", "title": "Abla", "text": "Eve gelirken ekmek al"},
        {"app": "com.google.android.gm", "title": "Staj programı", "text": ""}]


class FakeAdb:
    """Stands in for subprocess.run: records the commands and returns the output adb would give."""

    def __init__(self, replies=None):
        self.calls: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, cmd, timeout=15):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        out = next((v for k, v in self.replies.items() if k in joined), "")
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def fake_adb(monkeypatch):
    f = FakeAdb({"devices": "List of devices attached\n192.168.1.50:5555\tdevice\n",
                 "mWakefulness": "mWakefulness=Asleep",
                 "pm list packages": "package:com.netflix.ninja\npackage:tr.com.exxen.tv\n",
                 "monkey": "Events injected: 1"})
    monkeypatch.setattr(android, "run", f)
    monkeypatch.setattr(android.shutil, "which", lambda n: "/usr/bin/adb")
    return f


def _ctx(tmp_path, android_conf):
    from alice.config import load_config

    cfg = load_config()
    cfg.data["android"] = android_conf
    return ToolContext(cwd=Path(tmp_path), config=cfg)


def test_android_tool_power_on_and_open_app(fake_adb, tmp_path):
    ctx = _ctx(tmp_path, {"tv": "192.168.1.50:5555"})
    tool = Android()
    assert tool.run(ctx, device="tv", action="power", state="on").text == "tv: on"
    assert ["/usr/bin/adb", "-s", "192.168.1.50:5555", "shell", "input keyevent KEYCODE_WAKEUP"] in fake_adb.calls
    tool.run(ctx, device="tv", action="open_app", app="exxen")
    assert any("monkey -p tr.com.exxen.tv -c android.intent.category.LEANBACK_LAUNCHER 1" in c for c in
               map(" ".join, fake_adb.calls))
    tool.run(ctx, device="tv", action="key", key="volume_up", times=3)
    assert fake_adb.calls[-1][-1] == "input keyevent " + " ".join(["KEYCODE_VOLUME_UP"] * 3)


def test_android_tool_youtube_url_opens_tv_app(fake_adb, tmp_path):
    ctx = _ctx(tmp_path, {"tv": "192.168.1.50:5555"})
    Android().run(ctx, device="tv", action="youtube", url="https://youtu.be/dQw4w9WgXcQ")
    last = fake_adb.calls[-1][-1]
    assert "watch?v=dQw4w9WgXcQ" in last and last.endswith("com.google.android.youtube.tv")


def test_android_tool_without_address_explains(fake_adb, tmp_path):
    with pytest.raises(ToolError, match="connect"):
        Android().run(_ctx(tmp_path, {}), device="phone", action="battery")


def test_android_unauthorized_is_explained(monkeypatch, tmp_path):
    def fake(cmd, timeout=15):
        if "connect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "failed to authenticate to 192.168.1.50:5555", "")
        return subprocess.CompletedProcess(cmd, 0, "List of devices attached\n", "")

    monkeypatch.setattr(android, "run", fake)
    monkeypatch.setattr(android.shutil, "which", lambda n: "/usr/bin/adb")
    with pytest.raises(ToolError, match="Always allow"):
        Android().run(_ctx(tmp_path, {"tv": "192.168.1.50"}), device="tv", action="key", key="home")


def test_missing_adb_gives_install_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(android.shutil, "which", lambda n: None)
    with pytest.raises(ToolError, match="winget install Google.PlatformTools"):
        Android().run(_ctx(tmp_path, {"tv": "192.168.1.50"}), device="tv", action="key", key="home")


def test_risk_levels():
    assert PC().risk_for({"action": "sleep"}) == "danger"
    assert PC().risk_for({"action": "brightness"}) == "read"
    assert PC().risk_for({"action": "brightness", "level": 40}) == "computer"
    assert Wifi().risk_for({"action": "scan"}) == "read"
    assert Wifi().risk_for({"action": "connect"}) == "computer"
    assert Android().risk_for({"action": "power", "state": "status"}) == "read"
    assert Android().risk_for({"action": "power", "state": "off"}) == "computer"


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="Windows dışı davranış")
def test_windows_only_actions_fail_clearly_elsewhere(tmp_path):
    with pytest.raises(ToolError, match="Windows"):
        PC().run(_ctx(tmp_path, {}), action="wol_info")


# ---------- Samsung TV ----------

from alice.home import samsung  # noqa: E402
from alice.tools.home import SamsungTV  # noqa: E402


def test_samsung_messages_and_app_lookup():
    assert samsung.key_message("KEY_VOLUP")["params"]["DataOfCmd"] == "KEY_VOLUP"
    assert samsung.text_message("ğ")["params"]["Cmd"] == "xJ8="
    m = samsung.launch_message("111299001912", "v=dQw4w9WgXcQ")["params"]["data"]
    assert m == {"appId": "111299001912", "action_type": "DEEP_LINK", "metaTag": "v=dQw4w9WgXcQ"}
    assert samsung.launch_message("3201907018807")["params"]["data"]["action_type"] == "NATIVE_LAUNCH"
    installed = [{"name": "YouTube", "appId": "111299001912"}, {"name": "Exxen", "appId": "3202001019999"}]
    assert samsung.find_app("exxen", installed) == "3202001019999"
    assert samsung.find_app("netflix", installed) == "3201907018807"
    assert samsung.find_app("3201606009684", []) == "3201606009684"
    assert samsung.find_app("bilinmeyen", installed) is None
    assert samsung.power_state({"device": {"PowerState": "standby"}}) == "standby"
    assert samsung.power_state({"device": {}}) == "on"


class FakeWS:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent: list[dict] = []

    def recv(self, timeout=None):
        if not self.incoming:
            raise TimeoutError
        import json

        return json.dumps(self.incoming.pop(0))

    def send(self, data):
        import json

        self.sent.append(json.loads(data))

    def close(self):
        pass


def _fake_connect(monkeypatch, ws):
    import types

    mod = types.ModuleType("websockets.sync.client")
    mod.connect = lambda *a, **k: ws
    monkeypatch.setitem(__import__("sys").modules, "websockets.sync.client", mod)


def test_samsung_remote_takes_token_and_sends_keys(monkeypatch):
    ws = FakeWS([{"event": "ms.channel.connect", "data": {"token": "12345678"}}])
    _fake_connect(monkeypatch, ws)
    monkeypatch.setattr(samsung.time, "sleep", lambda s: None)
    with samsung.Remote("192.168.1.60") as r:
        r.keys("KEY_VOLUP", 2)
    assert r.token == "12345678"
    assert [m["params"]["DataOfCmd"] for m in ws.sent] == ["KEY_VOLUP", "KEY_VOLUP"]


def test_samsung_remote_denied_is_explained(monkeypatch):
    _fake_connect(monkeypatch, FakeWS([{"event": "ms.channel.unauthorized"}]))
    with pytest.raises(samsung.TVError, match="İzin ver"):
        with samsung.Remote("192.168.1.60"):
            pass


def test_samsung_power_on_when_off_sends_wake_on_lan(monkeypatch, tmp_path):
    def off(host, timeout=3):
        raise samsung.TVError("kapalı")

    sent = []
    monkeypatch.setattr(samsung, "info", off)
    monkeypatch.setattr(net, "wake_on_lan", lambda mac, bc: sent.append((mac, bc)))
    ctx = _ctx(tmp_path, {})
    ctx.config.data["samsung_tv"] = {"host": "192.168.1.60", "mac": "aa:bb:cc:dd:ee:ff"}
    out = SamsungTV().run(ctx, action="power", state="on").text
    assert "wake packet sent to the TV" in out and sent == [("aa:bb:cc:dd:ee:ff", "192.168.1.255")]
    assert SamsungTV().run(ctx, action="power", state="off").text == "the TV is already off"
    ctx.config.data["samsung_tv"] = {"host": "192.168.1.60"}
    with pytest.raises(ToolError, match="MAC"):
        SamsungTV().run(ctx, action="power", state="on")


def test_samsung_youtube_deep_link(monkeypatch, tmp_path):
    ws = FakeWS([{"event": "ms.channel.connect", "data": {}}])
    _fake_connect(monkeypatch, ws)
    monkeypatch.setattr(samsung.time, "sleep", lambda s: None)
    ctx = _ctx(tmp_path, {})
    ctx.config.data["samsung_tv"] = {"host": "192.168.1.60", "token": "1"}
    SamsungTV().run(ctx, action="youtube", url="https://youtu.be/dQw4w9WgXcQ")
    assert ws.sent[-1]["params"]["data"] == {"appId": "111299001912", "action_type": "DEEP_LINK",
                                             "metaTag": "v=dQw4w9WgXcQ"}


# ---------- modem (UPnP) ----------

from alice.home import modem  # noqa: E402

IGD_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <friendlyName>Superonline Fiber</friendlyName>
  <manufacturer>Huawei</manufacturer>
  <modelName>HG8245Q2</modelName>
  <modelNumber>V5</modelNumber>
  <deviceList><device>
   <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
   <serviceList><service>
    <serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
    <controlURL>/upnp/control/WANCommonIFC1</controlURL>
   </service></serviceList>
   <deviceList><device>
    <serviceList><service>
     <serviceType>urn:schemas-upnp-org:service:WANPPPConnection:1</serviceType>
     <controlURL>/upnp/control/WANPPPConn1</controlURL>
    </service></serviceList>
   </device></deviceList>
  </device></deviceList>
 </device>
</root>"""

SOAP_REPLY = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
<u:GetStatusInfoResponse xmlns:u="urn:schemas-upnp-org:service:WANPPPConnection:1">
<NewConnectionStatus>Connected</NewConnectionStatus><NewLastConnectionError>ERROR_NONE</NewLastConnectionError>
<NewUptime>183900</NewUptime></u:GetStatusInfoResponse></s:Body></s:Envelope>"""


def test_modem_description_and_soap_parse():
    d = modem.parse_description(IGD_XML, "http://192.168.1.1:37215/desc.xml")
    assert (d["manufacturer"], d["model"], d["model_number"]) == ("Huawei", "HG8245Q2", "V5")
    assert d["wan"] == ("urn:schemas-upnp-org:service:WANPPPConnection:1",
                        "http://192.168.1.1:37215/upnp/control/WANPPPConn1")
    assert d["common"] == "http://192.168.1.1:37215/upnp/control/WANCommonIFC1"
    r = modem.parse_soap(SOAP_REPLY)
    assert r["NewConnectionStatus"] == "Connected" and r["NewUptime"] == "183900"
    assert modem.fmt_uptime(183900) == "2 gün 3 sa 5 dk"
    assert modem.fmt_bytes(5 * 1024 ** 3) == "5.0 GB"
    assert modem.page_title("<html><TITLE>\n ZXHN H298A </TITLE>") == "ZXHN H298A"


# ---------- ZTE modem ----------

import hashlib  # noqa: E402

import httpx  # noqa: E402

from alice.home import zte  # noqa: E402

LAN_XML = """<ajax_response_xml_root><IF_ERRORSTR>SUCC</IF_ERRORSTR><OBJ_ACCESSDEV_ID>
<Instance><ParaName>_InstID</ParaName><ParaValue>DEV.1</ParaValue><ParaName>HostName</ParaName>
<ParaValue>HAYAT-PC</ParaValue><ParaName>IPAddress</ParaName><ParaValue>192.168.1.34</ParaValue>
<ParaName>MACAddress</ParaName><ParaValue>D8:BB:C1:00:11:22</ParaValue></Instance>
</OBJ_ACCESSDEV_ID></ajax_response_xml_root>"""

WIFI_XML = """<ajax_response_xml_root><IF_ERRORSTR>SUCC</IF_ERRORSTR><OBJ_ACCESSDEV_ID>
<Instance><ParaName>MACAddress</ParaName><ParaValue>3c:2a:f4:aa:bb:cc</ParaValue><ParaName>IPAddress</ParaName>
<ParaValue>192.168.1.50</ParaValue><ParaName>HostName</ParaName><ParaValue>Galaxy-A54</ParaValue>
<ParaName>ESSID</ParaName><ParaValue>SUPERONLINE_5G</ParaValue></Instance>
</OBJ_ACCESSDEV_ID></ajax_response_xml_root>"""


class FakeZTE:
    """Imitates the modem's web interface: login, menu token, session timeout, restart."""

    def __init__(self, password="sifre123"):
        self.password = password
        self.logged_in = False
        self.posts: list[tuple[str, str, dict]] = []
        self.expire_once = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        tag = q.get("_tag", "")
        body = req.content.decode()
        if req.method == "POST":
            self.posts.append((tag, body, dict(req.headers)))
        if tag == "login_entry" and req.method == "GET":
            return httpx.Response(200, json={"lockingTime": 0, "loginErrMsg": "", "sess_token": "S0"})
        if tag == "login_token":
            return httpx.Response(200, text="<ajax_response_xml_root>4242</ajax_response_xml_root>")
        if tag == "login_entry":
            fields = dict(p.split("=", 1) for p in body.split("&"))
            ok = fields["Password"] == hashlib.sha256((self.password + "4242").encode()).hexdigest()
            self.logged_in = ok
            return httpx.Response(200, json={"sess_token": "S1"} if ok else
                                  {"lockingTime": 0, "loginErrMsg": "pwd error", "sess_token": "S0"})
        if not self.logged_in or (self.expire_once and tag.endswith(".lua")):
            self.expire_once = False
            self.logged_in = False
            return httpx.Response(200, text="<ajax_response_xml_root><IF_ERRORSTR>SessionTimeout</IF_ERRORSTR>"
                                            "</ajax_response_xml_root>")
        if q.get("_type") == "menuView":
            return httpx.Response(200, text='<script>\n_sessionTmpToken = "\\x54\\x4b\\x31";\n</script>')
        if tag == "accessdev_landevs_lua.lua":
            return httpx.Response(200, text=LAN_XML)
        if tag == "accessdev_ssiddev_lua.lua":
            return httpx.Response(200, text=WIFI_XML)
        if tag in ("devmgr_statusmgr_lua.lua", "devmgr_restartmgr_lua.lua"):
            return httpx.Response(200, text="<ajax_response_xml_root><IF_ERRORSTR>SUCC</IF_ERRORSTR>"
                                            "<_sessionTmpTokenByPost>P9</_sessionTmpTokenByPost>"
                                            "</ajax_response_xml_root>")
        return httpx.Response(404)


def _router(fake, password="sifre123"):
    return zte.ZTE("192.168.1.1", "admin", password, httpx.Client(transport=httpx.MockTransport(fake)))


def test_zte_tmp_token_and_instances():
    assert zte.tmp_token('var a=1;\n_sessionTmpToken = "\\x61\\x62\\x63";') == "abc"
    assert zte.tmp_token("yok") is None
    rows = zte.device_rows(LAN_XML, WIFI_XML)
    assert rows == [
        {"mac": "d8:bb:c1:00:11:22", "ip": "192.168.1.34", "name": "HAYAT-PC", "link": "kablo"},
        {"mac": "3c:2a:f4:aa:bb:cc", "ip": "192.168.1.50", "name": "Galaxy-A54", "link": "Wi-Fi (SUPERONLINE_5G)"}]


def test_zte_login_devices_and_session_timeout_relogin():
    fake = FakeZTE()
    r = _router(fake)
    r.login()
    assert r.token == "S1"
    fake.expire_once = True  # the session drops on the first data request: it must log in again and continue
    rows = r.devices()
    assert {x["name"] for x in rows} == {"HAYAT-PC", "Galaxy-A54"}
    assert r.token == "TK1"


def test_zte_wrong_password_and_missing_password():
    with pytest.raises(zte.ModemError, match="şifre yanlış"):
        _router(FakeZTE(), "yanlis").login()
    with pytest.raises(zte.ModemError, match="MODEM_PASSWORD"):
        zte.ZTE("192.168.1.1", "admin", "")


def test_zte_restart_posts_tokens():
    fake = FakeZTE()
    r = _router(fake)
    r.login()
    r.restart()
    tag, body, headers = fake.posts[-1]
    assert tag == "devmgr_restartmgr_lua.lua"
    assert "IF_ACTION=Restart" in body and "_sessionTOKEN=TK1" in body and "_sessionTOKENByPost=P9" in body
    assert "check" in {k.lower() for k in headers} or zte.check_header("x") is None


def test_modem_restart_needs_confirmation():
    assert Wifi().risk_for({"action": "modem_restart"}) == "danger"
    assert Wifi().risk_for({"action": "modem_devices"}) == "read"


def test_network_mismatch_detects_vpn_address():
    msg = net.network_mismatch("10.2.0.2:39241", me="192.168.1.34")
    assert msg and "VPN" in msg and "192.168.1.x" in msg
    assert net.network_mismatch("192.168.1.50:5555", me="192.168.1.34") is None
    assert net.network_mismatch("192.168.1.50", me="127.0.0.1") is None


def test_pair_refuses_other_network_before_running_adb(monkeypatch):
    monkeypatch.setattr(net, "local_ip", lambda: "192.168.1.34")
    monkeypatch.setattr(android.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(android, "run", lambda *a, **k: pytest.fail("adb çalıştırılmamalıydı"))
    with pytest.raises(android.AdbError, match="VPN"):
        android.pair("10.2.0.2:39241", "029998")


def test_pair_timeout_is_explained(monkeypatch):
    monkeypatch.setattr(net, "local_ip", lambda: "192.168.1.34")
    monkeypatch.setattr(android.shutil, "which", lambda n: "/usr/bin/adb")

    def slow(*a, **k):
        raise TimeoutError("adb 20 sn içinde cevap vermedi")

    monkeypatch.setattr(android, "run", slow)
    with pytest.raises(android.AdbError, match="pairing did not answer"):
        android.pair("192.168.1.50:39241", "029998")


def test_run_timeout_becomes_short_timeout_error():
    import sys

    with pytest.raises(TimeoutError, match="cevap vermedi"):
        net.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)


class PairSim:
    """Imitates adb commands: pair, mdns services, connect."""

    def __init__(self, mdns: str = "", connect_ok: set[str] | None = None, pair_out: str = "Successfully paired"):
        self.mdns, self.ok, self.pair_out, self.calls = mdns, connect_ok or set(), pair_out, []

    def __call__(self, cmd, timeout=15):
        import subprocess

        self.calls.append(cmd[1:])
        if cmd[1] == "pair":
            out = self.pair_out
        elif cmd[1] == "mdns":
            out = self.mdns
        elif cmd[1] == "connect":
            out = f"connected to {cmd[2]}" if cmd[2] in self.ok else f"failed to connect to {cmd[2]}"
        else:
            out = ""
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def adb_sim(monkeypatch):
    monkeypatch.setattr(android.shutil, "which", lambda n: "/usr/bin/adb")

    def make(**kw):
        f = PairSim(**kw)
        monkeypatch.setattr(android, "run", f)
        return f
    return make


def test_pair_then_connects_on_mdns_port_not_pairing_port(adb_sim):
    f = adb_sim(mdns="adb-x\t_adb-tls-connect._tcp\t192.168.1.11:41001\n", connect_ok={"192.168.1.11:41001"})
    msg, addr = android.pair_and_connect("192.168.1.11:37351", "016607")
    assert addr == "192.168.1.11:41001" and "connected" in msg
    assert ["connect", "192.168.1.11:37351"] not in f.calls


def test_pair_falls_back_to_port_scan_when_mdns_is_empty(adb_sim, monkeypatch):
    adb_sim(connect_ok={"192.168.1.11:44000"})
    monkeypatch.setattr(net, "open_ports", lambda ip, ports, **k: [37351, 43000, 44000])
    addr = android.find_connect_address("192.168.1.11", skip=37351, sleep=lambda s: None)
    assert addr == "192.168.1.11:44000"


def test_pair_without_connect_port_asks_for_the_right_line(adb_sim, monkeypatch):
    adb_sim()
    monkeypatch.setattr(net, "open_ports", lambda ip, ports, **k: [])
    monkeypatch.setattr(android, "find_connect_address",
                        lambda ip, adb="", skip=None: None)
    msg, addr = android.pair_and_connect("192.168.1.11:37351", "016607")
    assert addr is None and "not the port in the pairing window" in msg


def test_repeated_pair_protocol_fault_is_explained(adb_sim):
    adb_sim(pair_out="error: protocol fault (couldn't read status message): No error")
    with pytest.raises(android.AdbError, match="connect is enough"):
        android.pair("192.168.1.11:37351", "016607")


def test_tool_connect_with_wrong_port_finds_the_right_one(adb_sim, tmp_path):
    adb_sim(mdns="adb-x\t_adb-tls-connect._tcp\t192.168.1.11:41001\n", connect_ok={"192.168.1.11:41001"})
    ctx = _ctx(tmp_path, {})
    out = Android().run(ctx, device="phone", action="connect", host="192.168.1.11:37351").text
    assert "41001" in out and ctx.config.data["android"]["phone"] == "192.168.1.11:41001"
