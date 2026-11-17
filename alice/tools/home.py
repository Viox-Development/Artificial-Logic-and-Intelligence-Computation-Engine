"""Things outside the computer: PC power/display settings, Wi-Fi and home network, Android TV and phone via ADB."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, DANGER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class PC(Tool):
    name = "pc"
    description = ("Bu bilgisayarın kendisi: kilitle, ekranı kapat, uyku, hazırda beklet, yeniden başlat, güç planı "
                   "(dengeli/yüksek performans/tasarruf), monitör parlaklığı (DDC/CI ya da dizüstü ekranı), "
                   "Wake-on-LAN bilgisi (bilgisayarı telefondan açmak için MAC ve gerekenler). Ses ve kapatma "
                   "zamanlayıcısı için media.")
    parameters = obj({
        "action": {"type": "string", "enum": ["lock", "monitor_off", "sleep", "hibernate", "restart", "plans",
                                              "set_plan", "brightness", "wol_info"],
                   "description": "lock | monitor_off | sleep | hibernate | restart (minutes sonra, varsayılan hemen) "
                   "| plans: güç planlarını listele | set_plan: plan'a geç | brightness: level verilirse ayarla, "
                   "verilmezse oku | wol_info: uzaktan açma için ağ kartı, MAC, durum."},
        "plan": {"type": "string", "description": "set_plan için ad ya da kısaltma: 'dengeli', 'performans', "
                 "'tasarruf'."},
        "level": {"type": "integer", "description": "brightness için 0-100."},
        "monitor": {"type": "integer", "description": "brightness için monitör sırası (1'den). Boş: hepsi."},
        "minutes": {"type": "number", "description": "restart için kaç dakika sonra. Varsayılan 0."},
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
                                  or "güç planı bulunamadı")
            if action == "set_plan":
                if not plan:
                    raise ToolError("plan gerekli")
                return ToolResult(pc.set_plan(plan))
            if action == "brightness":
                return ToolResult(pc.brightness(level, monitor))
            if action == "wol_info":
                return ToolResult(pc.wol_info())
        except (RuntimeError, OSError) as e:
            raise ToolError(str(e)) from e
        raise ToolError(f"bilinmeyen action: {action}")


class Wifi(Tool):
    name = "wifi"
    description = ("Wi-Fi ve ev ağı: bağlantı durumu ve sinyal, çevredeki ağlar, kayıtlı ağlar, bir ağa bağlan "
                   "(kayıtlı değilse şifreyle) ya da bağlantıyı kes, ağdaki cihazları listele (IP, MAC, ad), bir "
                   "cihazı Wake-on-LAN ile uyandır, modem bilgisi (marka/model, dış IP, bağlantı, açık kalma süresi, trafik); ZTE "
                   "modemde bağlı cihaz listesi ve modemi yeniden başlatma.")
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "scan", "saved", "connect", "disconnect", "devices", "wake",
                                              "modem", "modem_devices", "modem_restart"],
                   "description": "status | scan: çevredeki ağlar | saved: kayıtlı ağlar | connect: ssid'e bağlan | "
                   "disconnect | devices: ağdaki cihazlar (birkaç saniye sürer) | wake: mac'teki cihaza WoL paketi | "
                   "modem: modem modeli ve internet bağlantısı (UPnP) | modem_devices: modemin bildiği bağlı cihazlar "
                   "(kablo/Wi-Fi, ad) | modem_restart: modemi yeniden başlat (internet 2-3 dk gider)."},
        "ssid": {"type": "string", "description": "connect için ağ adı, tam yazımıyla."},
        "password": {"type": "string", "description": "connect için şifre; ağ zaten kayıtlıysa gerekmez."},
        "mac": {"type": "string", "description": "wake için cihazın MAC adresi, ör. 'aa:bb:cc:dd:ee:ff'."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a == "modem_restart":
            return DANGER
        return COMPUTER if a in ("connect", "disconnect", "wake") else READ

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
                                  or "ağ bulunamadı")
            if action == "saved":
                return ToolResult("\n".join(wifi.profiles()) or "kayıtlı ağ yok")
            if action == "connect":
                if not ssid:
                    raise ToolError("ssid gerekli")
                return ToolResult(wifi.connect(ssid, password))
            if action == "disconnect":
                return ToolResult(wifi.disconnect())
            if action == "devices":
                devs = net.devices()
                return ToolResult("\n".join(f"{d['ip']}  {d['mac']}  {d['name']}".rstrip() for d in devs)
                                  or "cihaz bulunamadı")
            if action == "wake":
                if not mac:
                    raise ToolError("mac gerekli")
                me = net.local_ip()
                net.wake_on_lan(mac, me.rsplit(".", 1)[0] + ".255")
                return ToolResult(f"uyandırma paketi gönderildi: {mac}")
        except ValueError as e:
            raise ToolError(str(e)) from e
        except (RuntimeError, OSError) as e:
            raise ToolError(str(e)) from e
        raise ToolError(f"bilinmeyen action: {action}")

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
                return "modem yeniden başlıyor; internet 2-3 dakika gidip gelecek"
            rows = router.devices()
            return "\n".join(f"{r['name'] or '(adsız)'}  {r['ip']}  {r['mac']}  {r['link']}" for r in rows) \
                or "modem bağlı cihaz bildirmedi"
        except httpx.HTTPStatusError as e:
            raise zte.ModemError(f"modem {host} HTTP {e.response.status_code} döndü: ZTE ZXHN arayüzü değil ya da "
                                 "bu yazılım sürümü desteklenmiyor") from e
        except httpx.TimeoutException as e:
            raise zte.ModemError(f"modem ({host}) zamanında cevap vermedi") from e
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise zte.ModemError(f"modeme ulaşılamadı ({host}): {e.__class__.__name__}; [modem] host doğru mu?") from e
        finally:
            # ZTE modems allow only one admin session at a time: log out even if there was an error, so that a
            # user who wants to log in from the browser does not get stuck on the "başka kullanıcı bağlı" warning.
            if logged_in:
                router.logout()
            router.close()


class Android(Tool):
    name = "android"
    description = ("ADB ile Android TV ve Android telefon. Her ikisinde: aç/kapa, tuş (ses, oynat, yön, ana ekran), "
                   "uygulama aç, YouTube'da video aç, yazı yaz, ekran görüntüsü, uygulama listesi. Telefonda ayrıca "
                   "pil, bildirimler, bilgisayardan dosya gönderme. İlk kullanımda find (TV'yi ağda arar) ya da "
                   "host ile connect; telefon için pair (eşler ve kendisi bağlanır). Adres bir kere kaydedilir.")
    parameters = obj({
        "device": {"type": "string", "enum": ["tv", "phone"], "description": "Hangi cihaz."},
        "action": {"type": "string", "enum": ["connect", "pair", "find", "power", "key", "open_app", "youtube",
                                              "open_url", "type", "screenshot", "apps", "battery", "notifications",
                                              "send_file"],
                   "description": "connect: host'a bağlan ve kaydet | "
                   "pair: telefonu eşle ve bağlan (host=eşleme penceresindeki IP:port, code) | "
                   "find: ağda ADB'si açık TV ara | power: state'e göre | key: key tuşuna times kez bas | open_app: "
                   "app | youtube: query ya da url'deki videoyu aç | open_url | type: text yaz | screenshot | apps | "
                   "battery | notifications | send_file: path'teki dosyayı telefonun İndirilenler'ine."},
        "host": {"type": "string", "description": "connect/pair için IP ya da IP:port."},
        "code": {"type": "string", "description": "pair için telefonda görünen 6 haneli kod."},
        "state": {"type": "string", "enum": ["on", "off", "toggle", "status"], "description": "power için."},
        "key": {"type": "string", "enum": ["home", "back", "up", "down", "left", "right", "ok", "play_pause", "next",
                                           "previous", "volume_up", "volume_down", "mute", "input", "menu"],
                "description": "key için tuş."},
        "times": {"type": "integer", "description": "key için basış sayısı. Varsayılan 1."},
        "app": {"type": "string", "description": "open_app için ad (youtube, netflix, spotify...) ya da paket adı."},
        "query": {"type": "string", "description": "youtube için arama, ör. 'lofi hip hop'."},
        "url": {"type": "string", "description": "youtube ya da open_url için adres."},
        "text": {"type": "string", "description": "type için yazılacak metin."},
        "path": {"type": "string", "description": "send_file için bilgisayardaki dosya yolu."},
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
                    raise ToolError("pair için host (telefonun eşleme ekranındaki IP:port) ve code gerekli")
                msg, addr = adb.pair_and_connect(host, code, exe)
                if addr:
                    self._save(ctx, conf, device, addr)
                return ToolResult(msg)
            if action == "find":
                from alice.home.net import scan_port

                hits = scan_port(5555)
                return ToolResult(("ADB'si açık cihazlar: " + ", ".join(hits) + ". Bağlanmak için connect host=...")
                                  if hits else "Ağda ADB'si açık cihaz yok: TV'de Geliştirici seçenekleri > Ağ "
                                  "üzerinden hata ayıklama açık mı?")
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
                    raise ToolError("key gerekli")
                return ToolResult(d.keys(key, times))
            if action == "open_app":
                if not app:
                    raise ToolError("app gerekli")
                return ToolResult(d.open_app(app))
            if action == "youtube":
                vid = adb.youtube_id(url) if url else None
                title = url
                if not vid:
                    if not query:
                        raise ToolError("youtube için query ya da url gerekli")
                    from alice.tools.extras import youtube_search

                    hits = youtube_search(query, 1)
                    if not hits:
                        return ToolResult("video bulunamadı")
                    title, vid = hits[0]
                return ToolResult(f"{d.youtube(vid)} ({title})")
            if action == "open_url":
                if not url:
                    raise ToolError("url gerekli")
                return ToolResult(d.open_url(url))
            if action == "type":
                return ToolResult(d.type_text(text))
            if action == "screenshot":
                return ToolResult(f"{device} ekranı", images=[d.screenshot()])
            if action == "apps":
                return ToolResult("\n".join(d.packages()) or "uygulama yok")
            if action == "battery":
                return ToolResult(d.battery())
            if action == "notifications":
                items = d.notifications()
                return ToolResult("\n".join(f"[{n['app']}] {n['title']}: {n['text']}".rstrip(": ") for n in items)
                                  or "bildirim yok")
            if action == "send_file":
                if not path:
                    raise ToolError("path gerekli")
                p = ctx.resolve(path)
                if not p.is_file():
                    raise ToolError(f"dosya yok: {p}")
                return ToolResult(d.push(str(p)))
        except adb.AdbError as e:
            raise ToolError(str(e)) from e
        except (OSError, TimeoutError) as e:
            raise ToolError(f"{device} komutu çalışmadı: {e}") from e
        raise ToolError(f"bilinmeyen action: {action}")

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
    description = ("Samsung (Tizen) akıllı TV: aç/kapa, durum, ses, kanal, tuşlar (yön, tamam, geri, ana ekran, "
                   "kaynak/HDMI), uygulama aç (YouTube, Netflix ya da TV'de kurulu herhangi biri), YouTube'da video "
                   "aç, açık klavyeye yazı yaz, kurulu uygulamalar. İlk kullanımda find (ağda TV'yi arar) ya da host "
                   "ile connect; TV ekranında çıkan izin sorusu onaylanmalı. Android TV ve telefon için android.")
    parameters = obj({
        "action": {"type": "string", "enum": ["find", "connect", "power", "key", "open_app", "youtube", "type",
                                              "apps", "status"],
                   "description": "find | connect: host'a bağlan ve kaydet | power: state'e göre | key: key tuşuna "
                   "times kez | open_app: app | youtube: query ya da url'deki video | type: text | apps | status."},
        "host": {"type": "string", "description": "connect için TV'nin IP adresi."},
        "state": {"type": "string", "enum": ["on", "off", "toggle"], "description": "power için."},
        "key": {"type": "string", "description": "key için: volume_up, volume_down, mute, channel_up, channel_down, "
                "up, down, left, right, ok, back, home, source, hdmi, play, pause, stop, menu, exit ya da 0-9."},
        "times": {"type": "integer", "description": "key için basış sayısı. Varsayılan 1."},
        "app": {"type": "string", "description": "open_app için uygulama adı, ör. 'netflix', 'exxen'."},
        "query": {"type": "string", "description": "youtube için arama."},
        "url": {"type": "string", "description": "youtube için video adresi."},
        "text": {"type": "string", "description": "type için yazı (TV'de klavye açıkken)."},
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
                return ToolResult(("Bulunan Samsung TV: " + ", ".join(tvs) + ". Bağlanmak için connect host=...")
                                  if tvs else "Ağda açık bir Samsung TV bulunamadı (TV açık ve aynı ağda mı?)")
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
                return ToolResult(f"Samsung TV bağlandı ({target})" + ("" if mac else "; MAC okunamadı, kapalıyken "
                                  "açma çalışmayabilir"))
            host = conf.get("host", "")
            if action == "power":
                return ToolResult(self._power(ctx, conf, state))
            if action == "status":
                try:
                    d = samsung.info(host)
                except samsung.TVError:
                    return ToolResult("TV kapalı (ağda cevap vermiyor)")
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
                        raise ToolError(f"bilinmeyen tuş: {key}. Geçerli: {', '.join(samsung.KEYS)}")
                    r.keys(k, times)
                    return ToolResult(f"tv: {key}" + (f" x{times}" if times and times > 1 else ""))
                if action == "apps":
                    apps = r.installed_apps()
                    return ToolResult("\n".join(f"{a.get('name')} ({a.get('appId')})" for a in apps)
                                      or "TV uygulama listesini vermedi")
                if action == "open_app":
                    if not app:
                        raise ToolError("app gerekli")
                    app_id = samsung.find_app(app, r.installed_apps())
                    if not app_id:
                        raise ToolError(f"'{app}' TV'de bulunamadı; apps ile listeye bak")
                    r.launch(app_id)
                    return ToolResult(f"tv: {app} açılıyor")
                if action == "youtube":
                    from alice.home.android import youtube_id

                    vid = youtube_id(url) if url else None
                    title = url
                    if not vid:
                        if not query:
                            raise ToolError("youtube için query ya da url gerekli")
                        from alice.tools.extras import youtube_search

                        hits = youtube_search(query, 1)
                        if not hits:
                            return ToolResult("video bulunamadı")
                        title, vid = hits[0]
                    r.launch(samsung.APPS["youtube"], f"v={vid}")
                    return ToolResult(f"tv: YouTube'da açılıyor ({title})")
                if action == "type":
                    r.send(samsung.text_message(text))
                    return ToolResult("tv: yazıldı")
        except samsung.TVError as e:
            raise ToolError(str(e)) from e
        except (OSError, ValueError) as e:  # Wake-on-LAN: invalid MAC or no network
            raise ToolError(f"tv komutu çalışmadı: {e}") from e
        raise ToolError(f"bilinmeyen action: {action}")

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
            return "TV zaten açık"
        if state == "off" and cur != "on":
            return "TV zaten kapalı"
        if cur == "off":  # NIC is asleep: keys can't be sent, so use Wake-on-LAN
            if not conf.get("mac"):
                raise ToolError("TV kapalı ve MAC adresi bilinmiyor: TV açıkken bir kere connect yap")
            net.wake_on_lan(conf["mac"], (host.rsplit(".", 1)[0] + ".255") if host else "255.255.255.255")
            return ("TV'ye açılma paketi gönderildi (açılmazsa TV'de Ayarlar > Genel > Ağ > Uzman Ayarları > "
                    "'Mobil ile aç' açık olmalı)")
        with samsung.Remote(host, conf.get("token", "")) as r:
            r.keys("KEY_POWER")
        return "TV kapatıldı" if cur == "on" else "TV açıldı"


TOOLS = [PC(), Wifi(), Android(), SamsungTV()]
