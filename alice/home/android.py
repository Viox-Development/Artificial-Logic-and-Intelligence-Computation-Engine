"""ADB for Android devices: Android TV and phone.

First-time setup (once):
* TV: Settings > Device preferences > About > tap Build number 7 times; Developer options > Network debugging
  (or USB debugging) on. On the first connection, tick "Her zaman izin ver" (Always allow) in the TV's prompt.
* Phone (Android 11+): Developer options > Wireless debugging > "Eşleme koduyla cihaz eşle" (Pair device with code);
  use `pair` with the IP:port and 6-digit code shown on screen, then `connect` with the IP:port on the main screen.
* adb on the computer: `winget install Google.PlatformTools` (or set adb = "full/path/adb.exe" under
  [android] in config).
"""

from __future__ import annotations

import base64
import io
import re
import shlex
import shutil
import subprocess

from alice.home import net

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
ADB_HINT = "adb not found: install it with `winget install Google.PlatformTools` or give the adb path in config [android]"


class AdbError(RuntimeError):
    pass


def run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    """adb output (device notifications, app names) is UTF-8 on every system; decoding it with the Windows console
    code page (cp857/cp1254) garbled Turkish characters."""
    return net.run(cmd, timeout=timeout, encoding="utf-8")


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
    """`dumpsys notification --noredact` → [{app, title, text}] (newest first, system records skipped)."""
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
    """`input text` needs spaces as %s; quote the rest for the remote shell."""
    return shlex.quote(text.replace(" ", "%s"))


class Device:
    def __init__(self, host: str, kind: str = "tv", adb: str = ""):
        if not host:
            raise AdbError(f"no address for {kind}: first connect (with host) or find")
        self.serial = serial(host)
        self.kind = kind
        self.adb = adb_path(adb)

    def _run(self, *args: str, timeout: float = 15, binary: bool = False):
        cmd = [self.adb, "-s", self.serial, *args]
        if binary:
            try:
                return subprocess.run(cmd, capture_output=True, timeout=timeout,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired as e:
                raise AdbError(f"{self.kind} ({self.serial}) did not answer within {timeout:g} s") from e
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
            return (f"{self.kind} didn't allow the connection: in the 'allow USB debugging' prompt on its screen, tick "
                    "'Always allow' and confirm, then try again")
        if "refused" in low or "cannot connect" in low or "timed out" in low:
            return (f"{self.serial} refused the connection: is the device on and on the same network, and is network/wireless debugging "
                    "on in Developer options? (On a phone the port changes every time it's turned on.)")
        if "offline" in low or "not found" in low or "no devices" in low:
            return f"{self.kind} ({self.serial}) is not connected: is the device on and on the same network? connect first"
        return out.strip() or "adb failed"

    # ----- connection -----
    def connect(self) -> str:
        check_reachable(self.serial)
        try:
            r = run([self.adb, "connect", self.serial], timeout=10)
        except TimeoutError as e:
            raise AdbError(f"{self.serial} did not answer: is the device on and on the same network?") from e
        out = (r.stdout + r.stderr).strip()
        if "connected to" in out.lower() and "cannot" not in out.lower():
            return f"{self.kind} connected ({self.serial})"
        raise AdbError(self._explain(out) if out else f"could not connect to {self.serial}")

    def ensure(self) -> None:
        r = run([self.adb, "devices"], timeout=10)
        for line in r.stdout.splitlines():
            parts = line.split()
            # Exact match: looking for 192.168.1.5:5555, a 192.168.1.5:55551 line must not count as "bağlı"
            # (connected).
            if len(parts) >= 2 and parts[0] == self.serial and parts[-1] == "device":
                return
        self.connect()

    # ----- shared operations -----
    def keys(self, name: str, times: int = 1) -> str:
        if name not in KEYS:
            raise AdbError(f"unknown key: {name}. Valid: {', '.join(KEYS)}")
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
            raise AdbError(f"no app called '{name}' found; look at the list with apps")
        return min(hits, key=len)

    def open_app(self, name: str) -> str:
        pkg = self.resolve_app(name)
        if not PACKAGE.match(pkg):  # intent actions, such as the camera
            self.shell(f"am start -a {shlex.quote(pkg)}")
            return f"{self.kind}: opened {name}"
        cat = "android.intent.category.LEANBACK_LAUNCHER" if self.kind == "tv" else "android.intent.category.LAUNCHER"
        out = self.shell(f"monkey -p {pkg} -c {cat} 1")
        if "No activities found" in out and self.kind == "tv":
            out = self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
        if "No activities found" in out:
            raise AdbError(f"could not start {pkg} (it has no launcher)")
        return f"{self.kind}: opened {name}"

    def open_url(self, url: str, package: str = "") -> str:
        cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
        if package:
            cmd += f" {package}"
        self.shell(cmd)
        return f"{self.kind}: opened {url}"

    def youtube(self, video_id: str) -> str:
        pkg = (TV_APPS if self.kind == "tv" else PHONE_APPS)["youtube"]
        return self.open_url(f"https://www.youtube.com/watch?v={video_id}", pkg)

    def type_text(self, text: str) -> str:
        self.shell(f"input text {input_text_arg(text)}")
        return f"{self.kind}: typed"

    def screenshot(self, width: int = 1280) -> str:
        """Screenshot as base64 PNG (downscaled for a model that can see images)."""
        r = self._run("exec-out", "screencap", "-p", timeout=20, binary=True)
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
            raise AdbError(self._explain((r.stderr or b"").decode("utf-8", "replace")) or "could not take a screenshot")
        from PIL import Image

        img = Image.open(io.BytesIO(r.stdout))
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    # ----- phone-only -----
    def battery(self) -> str:
        b = parse_battery(self.shell("dumpsys battery"))
        return (f"battery {b['percent']}%" + (", charging" if b["charging"] else "")
                + (f", {b['temp_c']:.0f}°C" if b["temp_c"] is not None else ""))

    def notifications(self) -> list[dict]:
        return parse_notifications(self.shell("dumpsys notification --noredact", timeout=20))

    def push(self, local: str, remote_dir: str = "/sdcard/Download/") -> str:
        r = self._run("push", local, remote_dir, timeout=300)
        if r.returncode != 0:
            raise AdbError(self._explain(r.stdout + r.stderr))
        return f"sent: {remote_dir}"


def check_reachable(host: str) -> None:
    if why := net.network_mismatch(host):
        raise AdbError(why)


def pair(host_port: str, code: str, adb: str = "") -> str:
    """Android 11+ wireless debugging pairing (once)."""
    check_reachable(host_port)
    try:
        r = run([adb_path(adb), "pair", host_port, code.strip()], timeout=20)
    except TimeoutError as e:
        raise AdbError("pairing did not answer: the address or port is wrong, the phone is on another network, or the pairing window "
                       "closed (the code is invalid once it closes; try again with a new code)") from e
    out = (r.stdout + r.stderr).strip()
    if "successfully paired" not in out.lower():
        if "protocol fault" in out.lower() or "wrong password" in out.lower():
            raise AdbError("the pairing code or port is no longer valid: the pairing window closed or the phone is already paired. "
                           "If it's already paired, there is no need to pair again, connect is enough; otherwise try again with a new code.")
        raise AdbError(out or "pairing failed")
    return "paired"


# The phone's connect port differs from its pairing port and changes on every startup (Linux's random port range).
CONNECT_PORTS = range(30000, 61000)


def find_connect_address(ip: str, adb: str = "", skip: int | None = None, tries: int = 3, wait: float = 1.5,
                         sleep=None) -> str | None:
    """Current connect address of a paired phone: mDNS first; if that fails, try each open port on the IP."""
    import time

    sleep = sleep or time.sleep
    for i in range(tries):
        if addr := mdns_connect_address(ip, adb):
            return addr
        if i < tries - 1:
            sleep(wait)
    for port in [p for p in net.open_ports(ip, CONNECT_PORTS) if p != skip][:6]:
        try:
            r = run([adb_path(adb), "connect", f"{ip}:{port}"], timeout=6)
        except TimeoutError:
            continue
        out = (r.stdout + r.stderr).lower()
        if "connected to" in out and "cannot" not in out and "failed" not in out:
            return f"{ip}:{port}"
    return None


def pair_and_connect(host_port: str, code: str, adb: str = "") -> tuple[str, str | None]:
    """Pair, then find the connect port yourself and connect. (message, connected address or None)"""
    pair(host_port, code, adb)
    ip, _, port = host_port.partition(":")
    addr = find_connect_address(ip, adb, skip=int(port) if port.isdigit() else None)
    if not addr:
        return ("paired, but the connection port wasn't found. Give the 'IP address and port' line from the main Wireless "
                "debugging screen on the phone (not the port in the pairing window, that one is different).", None)
    d = Device(addr, "phone", adb)
    return f"paired and {d.connect()}", d.serial


def parse_mdns(text: str) -> list[tuple[str, str]]:
    """`adb mdns services` → [(service type, ip:port)]."""
    return re.findall(r"(_adb[\w-]*\._tcp)\.?\s+(\d{1,3}(?:\.\d{1,3}){3}:\d+)", text)


def mdns_connect_address(ip: str, adb: str = "") -> str | None:
    """The phone's wireless debugging port changes on every startup; find the current port for the same IP via mDNS."""
    try:
        out = run([adb_path(adb), "mdns", "services"], timeout=10).stdout
    except (OSError, AdbError):
        return None
    for kind, addr in parse_mdns(out):
        if kind.startswith("_adb-tls-connect") and addr.split(":")[0] == ip:
            return addr
    return None
