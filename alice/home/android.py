"""ADB ile Android cihazlar: Android TV ve telefon.

İlk kurulum (bir kere):
* TV: Ayarlar > Cihaz tercihleri > Hakkında > Yapı numarasına 7 kez bas; Geliştirici seçenekleri > Ağ üzerinden
  hata ayıklama (ya da USB hata ayıklama) açık. İlk bağlanışta TV'de çıkan pencerede "Her zaman izin ver".
* Telefon (Android 11+): Geliştirici seçenekleri > Kablosuz hata ayıklama > "Eşleme koduyla cihaz eşle";
  ekrandaki IP:port ve 6 haneli kodla `pair`, sonra ana ekrandaki IP:port ile `connect`.
* Bilgisayarda adb: `winget install Google.PlatformTools` (ya da config'te [android] adb = "tam/yol/adb.exe").
"""

from __future__ import annotations

import base64
import io
import re
import shlex
import shutil

from alice.home.net import run

TV_APPS = {
    "youtube": "com.google.android.youtube.tv", "netflix": "com.netflix.ninja",
    "spotify": "com.spotify.tv.android", "prime": "com.amazon.amazonvideo.livingroom",
    "disney": "com.disney.disneyplus", "twitch": "tv.twitch.android.app", "plex": "com.plexapp.android",
    "kodi": "org.xbmc.kodi", "ayarlar": "com.android.tv.settings", "settings": "com.android.tv.settings",
}
PHONE_APPS = {
    "youtube": "com.google.android.youtube", "spotify": "com.spotify.music", "whatsapp": "com.whatsapp",
    "instagram": "com.instagram.android", "kamera": "android.media.action.STILL_IMAGE_CAMERA",
    "ayarlar": "com.android.settings", "settings": "com.android.settings", "chrome": "com.android.chrome",
    "harita": "com.google.android.apps.maps", "maps": "com.google.android.apps.maps",
}
KEYS = {
    "home": "HOME", "back": "BACK", "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT",
    "right": "DPAD_RIGHT", "ok": "DPAD_CENTER", "play_pause": "MEDIA_PLAY_PAUSE", "next": "MEDIA_NEXT",
    "previous": "MEDIA_PREVIOUS", "volume_up": "VOLUME_UP", "volume_down": "VOLUME_DOWN", "mute": "VOLUME_MUTE",
    "power": "POWER", "on": "WAKEUP", "off": "SLEEP", "input": "TV_INPUT", "menu": "MENU",
}
PACKAGE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$")
ADB_HINT = "adb bulunamadı: `winget install Google.PlatformTools` ile kur ya da config'te [android] adb yolunu ver"


class AdbError(RuntimeError):
    pass


def adb_path(configured: str = "") -> str:
    p = configured or shutil.which("adb") or ""
    if not p:
        raise AdbError(ADB_HINT)
    return p


def serial(host: str, port: int = 5555) -> str:
    host = host.strip()
    return host if ":" in host else f"{host}:{port}"


def youtube_id(text: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})", text)
    return m.group(1) if m else (text if re.fullmatch(r"[\w-]{11}", text) else None)


def parse_wakefulness(dumpsys: str) -> str | None:
    m = re.search(r"mWakefulness=(\w+)", dumpsys)
    return m.group(1) if m else None


def parse_battery(dumpsys: str) -> dict:
    vals = dict(re.findall(r"^\s*([\w ]+):\s*(\S+)\s*$", dumpsys, re.M))
    level = int(vals.get("level", "0") or 0)
    scale = int(vals.get("scale", "100") or 100)
    plugged = any(vals.get(k) == "true" for k in ("AC powered", "USB powered", "Wireless powered"))
    temp = vals.get("temperature")
    return {"percent": round(level * 100 / scale) if scale else level, "charging": plugged,
            "temp_c": int(temp) / 10 if temp and temp.lstrip("-").isdigit() else None}


def parse_notifications(dumpsys: str, limit: int = 15) -> list[dict]:
    """`dumpsys notification --noredact` → [{app, title, text}] (yeni olan önce, sistem kayıtları atlanır)."""
    out: list[dict] = []
    for block in re.split(r"\n\s*NotificationRecord\(", dumpsys)[1:]:
        pkg = re.search(r"pkg=([\w.]+)", block)
        title = re.search(r"android\.title=\w*\s*\((.*?)\)\s*$", block, re.M)
        text = re.search(r"android\.text=\w*\s*\((.*?)\)\s*$", block, re.M)
        if not pkg or not (title or text):
            continue
        if pkg.group(1) in ("android", "com.android.systemui"):
            continue
        item = {"app": pkg.group(1), "title": title.group(1) if title else "", "text": text.group(1) if text else ""}
        if item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def input_text_arg(text: str) -> str:
    """`input text` boşluğu %s ile ister; geri kalanı uzaktaki kabuk için tırnakla."""
    return shlex.quote(text.replace(" ", "%s"))


class Device:
    def __init__(self, host: str, kind: str = "tv", adb: str = ""):
        if not host:
            raise AdbError(f"{kind} için adres yok: önce connect (host ile) ya da find")
        self.serial = serial(host)
        self.kind = kind
        self.adb = adb_path(adb)

    def _run(self, *args: str, timeout: float = 15, binary: bool = False):
        cmd = [self.adb, "-s", self.serial, *args]
        if binary:
            import subprocess

            return subprocess.run(cmd, capture_output=True, timeout=timeout)
        return run(cmd, timeout=timeout)

    def shell(self, command: str, timeout: float = 15) -> str:
        r = self._run("shell", command, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            raise AdbError(self._explain(out))
        return r.stdout or ""

    def _explain(self, out: str) -> str:
        low = out.lower()
        if "unauthorized" in low or "authenticate" in low:
            return (f"{self.kind} bağlantıya izin vermedi: ekranda çıkan 'hata ayıklamaya izin ver' penceresinde "
                    "'Her zaman izin ver'i işaretleyip onayla, sonra tekrar dene")
        if "refused" in low or "cannot connect" in low or "timed out" in low:
            return (f"{self.serial} bağlantıyı kabul etmedi: cihaz açık ve aynı ağda mı, Geliştirici seçeneklerinde "
                    "ağ/kablosuz hata ayıklama açık mı? (Telefonda port her açılışta değişir.)")
        if "offline" in low or "not found" in low or "no devices" in low:
            return f"{self.kind} ({self.serial}) bağlı değil: cihaz açık ve aynı ağda mı? Önce connect"
        return out.strip() or "adb hata verdi"

    # ----- bağlantı -----
    def connect(self) -> str:
        check_reachable(self.serial)
        try:
            r = run([self.adb, "connect", self.serial], timeout=10)
        except TimeoutError as e:
            raise AdbError(f"{self.serial} cevap vermedi: cihaz açık ve aynı ağda mı?") from e
        out = (r.stdout + r.stderr).strip()
        if "connected to" in out.lower() and "cannot" not in out.lower():
            return f"{self.kind} bağlı ({self.serial})"
        raise AdbError(self._explain(out) if out else f"{self.serial} adresine bağlanılamadı")

    def ensure(self) -> None:
        r = run([self.adb, "devices"], timeout=10)
        for line in r.stdout.splitlines():
            if line.startswith(self.serial) and line.split()[-1] == "device":
                return
        self.connect()

    # ----- ortak işler -----
    def keys(self, name: str, times: int = 1) -> str:
        if name not in KEYS:
            raise AdbError(f"bilinmeyen tuş: {name}. Geçerli: {', '.join(KEYS)}")
        n = max(1, min(int(times or 1), 30))
        self.shell("input keyevent " + " ".join([f"KEYCODE_{KEYS[name]}"] * n))
        return f"{self.kind}: {name}" + (f" x{n}" if n > 1 else "")

    def awake(self) -> str | None:
        return parse_wakefulness(self.shell("dumpsys power | grep -m1 mWakefulness="))

    def power(self, state: str) -> str:
        cur = self.awake()
        if state == "status":
            return f"{self.kind}: {'açık' if cur == 'Awake' else 'uykuda' if cur else 'bilinmiyor'}"
        if state == "on" and cur != "Awake":
            self.shell("input keyevent KEYCODE_WAKEUP")
        elif state == "off" and cur == "Awake":
            self.shell("input keyevent KEYCODE_SLEEP")
        elif state == "toggle":
            self.shell("input keyevent KEYCODE_POWER")
        return f"{self.kind}: {state}"

    def packages(self) -> list[str]:
        return sorted(line.split(":", 1)[1].strip() for line in self.shell("pm list packages -3").splitlines()
                      if line.startswith("package:"))

    def resolve_app(self, name: str) -> str:
        table = TV_APPS if self.kind == "tv" else PHONE_APPS
        key = name.strip().casefold()
        if key in table:
            return table[key]
        if PACKAGE.match(name.strip()):
            return name.strip()
        hits = [p for p in self.packages() if key.replace(" ", "") in p.casefold()]
        if not hits:
            raise AdbError(f"'{name}' adında uygulama bulunamadı; apps ile listeye bak")
        return min(hits, key=len)

    def open_app(self, name: str) -> str:
        pkg = self.resolve_app(name)
        if not PACKAGE.match(pkg):  # kamera gibi intent eylemleri
            self.shell(f"am start -a {shlex.quote(pkg)}")
            return f"{self.kind}: {name} açıldı"
        cat = "android.intent.category.LEANBACK_LAUNCHER" if self.kind == "tv" else "android.intent.category.LAUNCHER"
        out = self.shell(f"monkey -p {pkg} -c {cat} 1")
        if "No activities found" in out and self.kind == "tv":
            out = self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
        if "No activities found" in out:
            raise AdbError(f"{pkg} başlatılamadı (başlatıcısı yok)")
        return f"{self.kind}: {name} açıldı"

    def open_url(self, url: str, package: str = "") -> str:
        cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
        if package:
            cmd += f" {package}"
        self.shell(cmd)
        return f"{self.kind}: açıldı {url}"

    def youtube(self, video_id: str) -> str:
        pkg = (TV_APPS if self.kind == "tv" else PHONE_APPS)["youtube"]
        return self.open_url(f"https://www.youtube.com/watch?v={video_id}", pkg)

    def type_text(self, text: str) -> str:
        self.shell(f"input text {input_text_arg(text)}")
        return f"{self.kind}: yazıldı"

    def screenshot(self, width: int = 1280) -> str:
        """Ekran görüntüsü, base64 PNG (görebilen model için küçültülmüş)."""
        r = self._run("exec-out", "screencap", "-p", timeout=20, binary=True)
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
            raise AdbError(self._explain((r.stderr or b"").decode("utf-8", "replace")) or "ekran görüntüsü alınamadı")
        from PIL import Image

        img = Image.open(io.BytesIO(r.stdout))
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    # ----- telefona özgü -----
    def battery(self) -> str:
        b = parse_battery(self.shell("dumpsys battery"))
        return (f"pil %{b['percent']}" + (", şarj oluyor" if b["charging"] else "")
                + (f", {b['temp_c']:.0f}°C" if b["temp_c"] is not None else ""))

    def notifications(self) -> list[dict]:
        return parse_notifications(self.shell("dumpsys notification --noredact", timeout=20))

    def push(self, local: str, remote_dir: str = "/sdcard/Download/") -> str:
        r = self._run("push", local, remote_dir, timeout=300)
        if r.returncode != 0:
            raise AdbError(self._explain(r.stdout + r.stderr))
        return f"gönderildi: {remote_dir}"


def check_reachable(host: str) -> None:
    from alice.home.net import network_mismatch

    if why := network_mismatch(host):
        raise AdbError(why)


def pair(host_port: str, code: str, adb: str = "") -> str:
    """Android 11+ kablosuz hata ayıklama eşlemesi (bir kere)."""
    check_reachable(host_port)
    try:
        r = run([adb_path(adb), "pair", host_port, code.strip()], timeout=20)
    except TimeoutError as e:
        raise AdbError("eşleme cevap vermedi: adres ya da port yanlış, telefon başka ağda, ya da eşleme penceresi "
                       "kapandı (kapanınca kod geçersiz olur; yeni kodla tekrar dene)") from e
    out = (r.stdout + r.stderr).strip()
    if "successfully paired" not in out.lower():
        raise AdbError(out or "eşleme başarısız")
    return "eşlendi; şimdi telefonun Kablosuz hata ayıklama ekranındaki IP:port ile connect"


def parse_mdns(text: str) -> list[tuple[str, str]]:
    """`adb mdns services` → [(servis türü, ip:port)]."""
    return re.findall(r"(_adb[\w-]*\._tcp)\.?\s+(\d{1,3}(?:\.\d{1,3}){3}:\d+)", text)


def mdns_connect_address(ip: str, adb: str = "") -> str | None:
    """Telefonun kablosuz hata ayıklama portu her açılışta değişir; mDNS'ten aynı IP'nin güncel portunu bul."""
    try:
        out = run([adb_path(adb), "mdns", "services"], timeout=10).stdout
    except (OSError, AdbError):
        return None
    for kind, addr in parse_mdns(out):
        if kind.startswith("_adb-tls-connect") and addr.split(":")[0] == ip:
            return addr
    return None
