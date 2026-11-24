"""Things outside the computer: PC power/display settings, Wi-Fi and home network, Android TV and phone via ADB."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, DANGER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class PC(Tool):
    name = "pc"
    description = ("This computer itself: lock, screen off, sleep, hibernate, restart, power plan (balanced/high "
                   "performance/power saver), monitor brightness (DDC/CI or the laptop screen), Wake-on-LAN info (the "
                   "MAC and what is needed to turn the computer on from the phone). For volume and the shutdown timer "
                   "use media.")
    parameters = obj({
        "action": {"type": "string", "enum": ["lock", "monitor_off", "sleep", "hibernate", "restart", "plans",
                                              "set_plan", "brightness", "wol_info"],
                   "description": "lock | monitor_off | sleep | hibernate | restart (after minutes, default now) | "
                                  "plans: list power plans | set_plan: switch to plan | brightness: set it if level "
                                  "is given, otherwise read it | wol_info: network card, MAC and status for remote "
                                  "wake-up."},
        "plan": {"type": "string", "description": "For set_plan: a name or short form: 'balanced', 'performance', "
                                                  "'saver' (Turkish 'dengeli', 'performans', 'tasarruf' work too)."},
        "level": {"type": "integer", "description": "For brightness: 0-100."},
        "monitor": {"type": "integer", "description": "For brightness: monitor number (from 1). Empty: all."},
        "minutes": {"type": "number", "description": "For restart: after how many minutes. Default 0."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("sleep", "hibernate", "restart"):
            return DANGER
        if a in ("plans", "wol_info") or (a == "brightness" and args.get("level") is None):
            return READ
        return COMPUTER

    def run(self, ctx: ToolContext, action: str, plan: str = "", level: int | None = None,
            monitor: int | None = None, minutes: float = 0) -> ToolResult:
        from alice.home import pc

        try:
            if action == "lock":
                return ToolResult(pc.lock())
            if action == "monitor_off":
                return ToolResult(pc.monitor_off())
            if action in ("sleep", "hibernate"):
                return ToolResult(pc.suspend(action == "hibernate"))
            if action == "restart":
                return ToolResult(pc.restart_in(minutes))
            if action == "plans":
                return ToolResult("\n".join(f"{'* ' if p['active'] else '  '}{p['name']}" for p in pc.plans())
                                  or "power plan not found")
            if action == "set_plan":
                if not plan:
                    raise ToolError("plan is required")
                return ToolResult(pc.set_plan(plan))
            if action == "brightness":
                return ToolResult(pc.brightness(level, monitor))
            if action == "wol_info":
                return ToolResult(pc.wol_info())
        except (RuntimeError, OSError) as e:
            raise ToolError(str(e)) from e
        raise ToolError(f"unknown action: {action}")


class Wifi(Tool):
    name = "wifi"
    description = ("Wi-Fi and the home network: connection status and signal, nearby networks, saved networks, "
                   "connect to a network (with a password if it isn't saved) or disconnect, list devices on the "
                   "network (IP, MAC, name), wake a device with Wake-on-LAN, modem info (brand/model, external IP, "
                   "connection, uptime, traffic); on a ZTE modem, the connected device list and restarting the modem.")
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "scan", "saved", "connect", "disconnect", "radio_on",
                                              "radio_off", "devices", "wake", "modem", "modem_devices",
                                              "modem_restart"],
                   "description": "status | scan: nearby networks | saved: saved networks | connect: connect to ssid "
                                  "| disconnect | radio_on/radio_off: turn Wi-Fi on/off (Linux) | devices: devices on "
                                  "the network (takes a few seconds) | wake: WoL packet to the device at mac | modem: "
                                  "modem model and internet connection (UPnP) | modem_devices: connected devices the "
                                  "modem knows (cable/Wi-Fi, name) | modem_restart: restart the modem (internet drops "
                                  "for 2-3 min)."},
        "ssid": {"type": "string", "description": "For connect: the network name, spelled exactly."},
        "password": {"type": "string", "description": "For connect: the password; not needed if the network is "
                                                      "already saved."},
        "mac": {"type": "string", "description": "For wake: the device's MAC address, e.g. 'aa:bb:cc:dd:ee:ff'."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a == "modem_restart":
            return DANGER
        return COMPUTER if a in ("connect", "disconnect", "radio_on", "radio_off", "wake") else READ

    def run(self, ctx: ToolContext, action: str, ssid: str = "", password: str = "", mac: str = "") -> ToolResult:
        from alice.home import net, wifi

        try:
            if action == "modem":
                from alice.home import modem

                return ToolResult(modem.info())
            if action in ("modem_devices", "modem_restart"):
                return ToolResult(self._zte(ctx, action))
            if action == "status":
                return ToolResult(wifi.status())
            if action == "scan":
                nets = wifi.scan()
                return ToolResult("\n".join(f"{n['ssid'] or '(gizli)'}: %{n['signal']} {n['auth']}" for n in nets)
                                  or "network not found")
            if action == "saved":
                return ToolResult("\n".join(wifi.profiles()) or "no saved networks")
            if action == "connect":
                if not ssid:
                    raise ToolError("ssid is required")
                return ToolResult(wifi.connect(ssid, password))
            if action == "disconnect":
                return ToolResult(wifi.disconnect())
            if action in ("radio_on", "radio_off"):
                return ToolResult(wifi.radio(action == "radio_on"))
            if action == "devices":
                devs = net.devices()
                return ToolResult("\n".join(f"{d['ip']}  {d['mac']}  {d['name']}".rstrip() for d in devs)
                                  or "device not found")
            if action == "wake":
                if not mac:
                    raise ToolError("mac is required")
                me = net.local_ip()
                net.wake_on_lan(mac, me.rsplit(".", 1)[0] + ".255")
                return ToolResult(f"wake packet sent: {mac}")
        except ValueError as e:
            raise ToolError(str(e)) from e
        except (RuntimeError, OSError) as e:
            raise ToolError(str(e)) from e
        raise ToolError(f"unknown action: {action}")

    @staticmethod
    def _zte(ctx: ToolContext, action: str) -> str:
        import os

        import httpx

        from alice.home import modem, zte

        conf = ctx.config.data.get("modem", {}) or {}
        host = conf.get("host") or modem.gateway_guess()
        router = zte.ZTE(host, conf.get("user", "admin"), os.environ.get("MODEM_PASSWORD", ""))
        logged_in = False
        try:
            router.login()
            logged_in = True
            if action == "modem_restart":
                router.restart()
                logged_in = False  # the modem is shutting down, so don't wait for the logout request in vain
                return "the modem is restarting; the internet will drop for 2-3 minutes"
            rows = router.devices()
            return "\n".join(f"{r['name'] or '(unnamed)'}  {r['ip']}  {r['mac']}  {r['link']}" for r in rows) \
                or "the modem reported no connected devices"
        except httpx.HTTPStatusError as e:
            raise zte.ModemError(f"modem {host} returned HTTP {e.response.status_code}: not a ZTE ZXHN interface, or "
                                 "this firmware version isn't supported") from e
        except httpx.TimeoutException as e:
            raise zte.ModemError(f"the modem ({host}) did not answer in time") from e
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise zte.ModemError(f"could not reach the modem ({host}): {e.__class__.__name__}; is [modem] host right?") from e
        finally:
            # ZTE modems allow only one admin session at a time: log out even if there was an error, so that a
            # user who wants to log in from the browser does not get stuck on the "başka kullanıcı bağlı" warning.
            if logged_in:
                router.logout()
            router.close()


class Android(Tool):
    name = "android"
    description = ("Android TV and Android phones over ADB. On both: power on/off, keys (volume, play, arrows, home), "
                   "open an app, open a video on YouTube, type text, screenshot, app list. On a phone also battery, "
                   "notifications and sending files from the computer. On first use, find (searches the network for "
                   "the TV) or connect with host; for a phone, pair (pairs and connects by itself). The address is "
                   "saved once.")
    parameters = obj({
        "device": {"type": "string", "enum": ["tv", "phone"], "description": "Hangi cihaz."},
        "action": {"type": "string", "enum": ["connect", "pair", "find", "power", "key", "open_app", "youtube",
                                              "open_url", "type", "screenshot", "apps", "battery", "notifications",
                                              "send_file"],
                   "description": "connect: connect to host and save it | pair: pair the phone and connect "
                                  "(host=IP:port from the pairing window, code) | find: search the network for a TV "
                                  "with ADB on | power: per state | key: press key times times | open_app: app | "
                                  "youtube: open the video for query or url | open_url | type: type text | screenshot "
                                  "| apps | battery | notifications | send_file: the file at path to the phone's "
                                  "Downloads."},
        "host": {"type": "string", "description": "For connect/pair: IP or IP:port."},
        "code": {"type": "string", "description": "For pair: the 6-digit code shown on the phone."},
        "state": {"type": "string", "enum": ["on", "off", "toggle", "status"], "description": "For power."},
        "key": {"type": "string", "enum": ["home", "back", "up", "down", "left", "right", "ok", "play_pause", "next",
                                           "previous", "volume_up", "volume_down", "mute", "input", "menu"],
                "description": "For key: the key."},
        "times": {"type": "integer", "description": "For key: number of presses. Default 1."},
        "app": {"type": "string", "description": "For open_app: a name (youtube, netflix, spotify...) or a package "
                                                 "name."},
        "query": {"type": "string", "description": "For youtube: search, e.g. 'lofi hip hop'."},
        "url": {"type": "string", "description": "For youtube or open_url: the address."},
        "text": {"type": "string", "description": "For type: the text to type."},
        "path": {"type": "string", "description": "For send_file: file path on the computer."},
    }, ["device", "action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("find", "apps", "battery", "screenshot") or (a == "power" and args.get("state") == "status"):
            return READ
        return COMPUTER

    def run(self, ctx: ToolContext, device: str, action: str, host: str = "", code: str = "", state: str = "status",
            key: str = "", times: int = 1, app: str = "", query: str = "", url: str = "", text: str = "",
            path: str = "") -> ToolResult:
        from alice.home import android as adb

        conf = ctx.config.data.get("android", {}) or {}
        exe = conf.get("adb", "")
        try:
            if action == "pair":
                if not host or not code:
                    raise ToolError("pair needs host (the IP:port on the phone's pairing screen) and code")
                msg, addr = adb.pair_and_connect(host, code, exe)
                if addr:
                    self._save(ctx, conf, device, addr)
                return ToolResult(msg)
            if action == "find":
                from alice.home.net import scan_port

                hits = scan_port(5555)
                return ToolResult(("Devices with ADB on: " + ", ".join(hits) + ". To connect: connect host=...")
                                  if hits else "No device with ADB on in the network: is Developer options > Network "
                                  "debugging on, on the TV?")
            if action == "connect":
                target = host or conf.get(device, "")
                d = adb.Device(target, device, exe)
                try:
                    msg = d.connect()
                except adb.AdbError:
                    # The phone's port may be the pairing port or an old one: find the current port for this IP.
                    ip, _, port = d.serial.partition(":")
                    new = adb.find_connect_address(ip, exe, skip=int(port) if port.isdigit() else None) \
                        if device == "phone" else None
                    if not new:
                        raise
                    d = adb.Device(new, device, exe)
                    msg = d.connect()
                if host or d.serial != conf.get(device):
                    self._save(ctx, conf, device, d.serial)
                return ToolResult(msg)
            d = self._device(ctx, conf, device, exe)
            if action == "power":
                return ToolResult(d.power(state))
            if action == "key":
                if not key:
                    raise ToolError("key is required")
                return ToolResult(d.keys(key, times))
            if action == "open_app":
                if not app:
                    raise ToolError("app is required")
                return ToolResult(d.open_app(app))
            if action == "youtube":
                vid = adb.youtube_id(url) if url else None
                title = url
                if not vid:
                    if not query:
                        raise ToolError("youtube needs query or url")
                    from alice.tools.extras import youtube_search

                    hits = youtube_search(query, 1)
                    if not hits:
                        return ToolResult("video not found")
                    title, vid = hits[0]
                return ToolResult(f"{d.youtube(vid)} ({title})")
            if action == "open_url":
                if not url:
                    raise ToolError("url is required")
                return ToolResult(d.open_url(url))
            if action == "type":
                return ToolResult(d.type_text(text))
            if action == "screenshot":
                return ToolResult(f"{device} screen", images=[d.screenshot()])
            if action == "apps":
                return ToolResult("\n".join(d.packages()) or "no apps")
            if action == "battery":
                return ToolResult(d.battery())
            if action == "notifications":
                items = d.notifications()
                return ToolResult("\n".join(f"[{n['app']}] {n['title']}: {n['text']}".rstrip(": ") for n in items)
                                  or "no notifications")
            if action == "send_file":
                if not path:
                    raise ToolError("path is required")
                p = ctx.resolve(path)
                if not p.is_file():
                    raise ToolError(f"file not found: {p}")
                return ToolResult(d.push(str(p)))
        except adb.AdbError as e:
            raise ToolError(str(e)) from e
        except (OSError, TimeoutError) as e:
            raise ToolError(f"{device} command failed: {e}") from e
        raise ToolError(f"unknown action: {action}")

    @staticmethod
    def _save(ctx: ToolContext, conf: dict, device: str, serial: str) -> None:
        from alice.config import set_config_value

        set_config_value("android", device, serial)
        conf[device] = serial
        ctx.config.data["android"] = conf

    @staticmethod
    def _device(ctx: ToolContext, conf: dict, device: str, exe: str):
        from alice.home import android as adb

        d = adb.Device(conf.get(device, ""), device, exe)
        try:
            d.ensure()
        except adb.AdbError:
            # The phone's wireless debugging port changes every time: find the new port for the same IP via mDNS.
            new = adb.mdns_connect_address(d.serial.split(":")[0], exe) if device == "phone" else None
            if not new or new == d.serial:
                raise
            from alice.config import set_config_value

            d = adb.Device(new, device, exe)
            d.connect()
            set_config_value("android", device, d.serial)
            conf[device] = d.serial
        return d


class SamsungTV(Tool):
    name = "tv"
    description = ("Samsung (Tizen) smart TV: power on/off, status, volume, channel, keys (arrows, ok, back, home, "
                   "source/HDMI), open an app (YouTube, Netflix or any app installed on the TV), open a video on "
                   "YouTube, type into the open keyboard, installed apps. On first use, find (searches the network "
                   "for the TV) or connect with host; the permission prompt on the TV screen must be accepted. For "
                   "Android TV and phones use android.")
    parameters = obj({
        "action": {"type": "string", "enum": ["find", "connect", "power", "key", "open_app", "youtube", "type",
                                              "apps", "status"],
                   "description": "find | connect: connect to host and save it | power: per state | key: key, times "
                                  "times | open_app: app | youtube: the video for query or url | type: text | apps | "
                                  "status."},
        "host": {"type": "string", "description": "For connect: the TV's IP address."},
        "state": {"type": "string", "enum": ["on", "off", "toggle"], "description": "For power."},
        "key": {"type": "string", "description": "For key: volume_up, volume_down, mute, channel_up, channel_down, "
                                                 "up, down, left, right, ok, back, home, source, hdmi, play, pause, "
                                                 "stop, menu, exit or 0-9."},
        "times": {"type": "integer", "description": "For key: number of presses. Default 1."},
        "app": {"type": "string", "description": "For open_app: the app name, e.g. 'netflix', 'exxen'."},
        "query": {"type": "string", "description": "For youtube: search."},
        "url": {"type": "string", "description": "For youtube: the video address."},
        "text": {"type": "string", "description": "For type: the text (while the keyboard is open on the TV)."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") in ("find", "apps", "status") else COMPUTER

    def run(self, ctx: ToolContext, action: str, host: str = "", state: str = "toggle", key: str = "",
            times: int = 1, app: str = "", query: str = "", url: str = "", text: str = "") -> ToolResult:
        from alice.home import samsung

        conf = ctx.config.data.setdefault("samsung_tv", {})
        try:
            if action == "find":
                from alice.home.net import scan_port

                tvs = []
                for ip in scan_port(8001):
                    try:
                        d = samsung.info(ip, 2)
                    except samsung.TVError:
                        continue
                    if samsung.is_samsung(d):
                        tvs.append(f"{ip} ({(d.get('device') or {}).get('name') or d.get('name') or 'Samsung TV'})")
                return ToolResult(("Bulunan Samsung TV: " + ", ".join(tvs) + ". To connect: connect host=...")
                                  if tvs else "No Samsung TV found on the network (is the TV on and on the same network?)")
            if action == "connect":
                target = host or conf.get("host", "")
                with samsung.Remote(target, conf.get("token", "") if not host or host == conf.get("host") else "") as r:
                    token = r.token
                # The user approved on the TV, so keep the address and token even if REST info can't be read.
                self._save(ctx, conf, host=target, token=token)
                try:
                    mac = (samsung.info(target).get("device") or {}).get("wifiMac") or ""
                except samsung.TVError:
                    mac = ""
                self._save(ctx, conf, mac=mac)
                return ToolResult(f"Samsung TV connected ({target})" + ("" if mac else "; could not read the MAC, so turning it on while "
                                  "off may not work"))
            host = conf.get("host", "")
            if action == "power":
                return ToolResult(self._power(ctx, conf, state))
            if action == "status":
                try:
                    d = samsung.info(host)
                except samsung.TVError:
                    return ToolResult("the TV is off (not answering on the network)")
                dev = d.get("device") or {}
                return ToolResult(f"TV {samsung.power_state(d)}: {dev.get('name') or ''} {dev.get('modelName') or ''}"
                                  .strip())
            with samsung.Remote(host, conf.get("token", "")) as r:
                if r.token and r.token != conf.get("token"):
                    self._save(ctx, conf, token=r.token)
                if action == "key":
                    k = samsung.KEYS.get(key.strip().lower()) or (key.upper() if key.upper().startswith("KEY_")
                                                                  else None)
                    if not k:
                        raise ToolError(f"unknown key: {key}. Valid: {', '.join(samsung.KEYS)}")
                    r.keys(k, times)
                    return ToolResult(f"tv: {key}" + (f" x{times}" if times and times > 1 else ""))
                if action == "apps":
                    apps = r.installed_apps()
                    return ToolResult("\n".join(f"{a.get('name')} ({a.get('appId')})" for a in apps)
                                      or "the TV didn't return its app list")
                if action == "open_app":
                    if not app:
                        raise ToolError("app is required")
                    app_id = samsung.find_app(app, r.installed_apps())
                    if not app_id:
                        raise ToolError(f"'{app}' not found on the TV; look at the list with apps")
                    r.launch(app_id)
                    return ToolResult(f"tv: opening {app}")
                if action == "youtube":
                    from alice.home.android import youtube_id

                    vid = youtube_id(url) if url else None
                    title = url
                    if not vid:
                        if not query:
                            raise ToolError("youtube needs query or url")
                        from alice.tools.extras import youtube_search

                        hits = youtube_search(query, 1)
                        if not hits:
                            return ToolResult("video not found")
                        title, vid = hits[0]
                    r.launch(samsung.APPS["youtube"], f"v={vid}")
                    return ToolResult(f"tv: opening on YouTube ({title})")
                if action == "type":
                    r.send(samsung.text_message(text))
                    return ToolResult("tv: typed")
        except samsung.TVError as e:
            raise ToolError(str(e)) from e
        except (OSError, ValueError) as e:  # Wake-on-LAN: invalid MAC or no network
            raise ToolError(f"tv command failed: {e}") from e
        raise ToolError(f"unknown action: {action}")

    @staticmethod
    def _save(ctx: ToolContext, conf: dict, **vals: str) -> None:
        from alice.config import set_config_value

        for k, v in vals.items():
            if v and conf.get(k) != v:
                set_config_value("samsung_tv", k, v)
                conf[k] = v

    def _power(self, ctx: ToolContext, conf: dict, state: str) -> str:
        from alice.home import net, samsung

        host = conf.get("host", "")
        try:
            cur = samsung.power_state(samsung.info(host))
        except samsung.TVError:
            cur = "off"
        if state == "on" and cur == "on":
            return "the TV is already on"
        if state == "off" and cur != "on":
            return "the TV is already off"
        if cur == "off":  # NIC is asleep: keys can't be sent, so use Wake-on-LAN
            if not conf.get("mac"):
                raise ToolError("the TV is off and its MAC address is unknown: connect once while the TV is on")
            net.wake_on_lan(conf["mac"], (host.rsplit(".", 1)[0] + ".255") if host else "255.255.255.255")
            return ("wake packet sent to the TV (if it doesn't turn on, 'Power On with Mobile' must be on in "
                    "Settings > General > Network > Expert Settings on the TV)")
        with samsung.Remote(host, conf.get("token", "")) as r:
            r.keys("KEY_POWER")
        return "TV turned off" if cur == "on" else "TV turned on"


TOOLS = [PC(), Wifi(), Android(), SamsungTV()]
