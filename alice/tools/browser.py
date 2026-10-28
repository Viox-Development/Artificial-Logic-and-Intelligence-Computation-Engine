"""Playwright ile tarayıcı kontrolü.

Profil ~/.alice/browser-profile'da kalıcı, yani bir kere giriş yaptığın siteler
sonraki oturumlarda da açık kalır. Sayfa, pixel yerine ID'li etkileşimli öğe
listesine (DOM snapshot) çevrilir; hem beyin hem reflex döngüsü bunu kullanır.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from alice.config import alice_home
from alice.tools.base import COMPUTER, NETWORK, READ, Tool, ToolContext, ToolError, ToolResult, obj, truncate

SNAPSHOT_JS = r"""
(limit) => {
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],' +
              '[role=tab],[role=menuitem],[role=checkbox],[role=option],[role=combobox],[role=textbox],' +
              '[role=searchbox],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"]),[draggable=true],[ondrop]';
  document.querySelectorAll('[data-alice-id]').forEach(e => e.removeAttribute('data-alice-id'));
  const out = [];
  const vh = window.innerHeight, vw = window.innerWidth;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (r.bottom < 0 || r.top > vh * 2.5 || r.right < 0 || r.left > vw) continue;
    const tag = el.tagName.toLowerCase();
    let text = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                el.title || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ');
    if (!text && tag === 'a') text = el.getAttribute('href') || '';
    if (!text && tag !== 'input' && tag !== 'textarea' && tag !== 'select') continue;
    const id = out.length + 1;
    el.setAttribute('data-alice-id', String(id));
    out.push({
      id, tag, role: el.getAttribute('role') || '', type: el.type || '',
      text: text.slice(0, 90),
      placeholder: (el.placeholder || '').slice(0, 40),
      value: (tag === 'input' || tag === 'textarea') && el.type !== 'password' ? (el.value || '').slice(0, 60) : '',
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 80) : '',
      inView: r.top >= 0 && r.bottom <= vh,
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""

TEXT_JS = "() => document.body ? document.body.innerText : ''"

# Fonksiyon başı: "function", "async function", "(a, b) =>", "x =>", "async () =>". "(() => 1)()" gibi
# hemen çağrılan ifadeler ya da içinde ok fonksiyonu geçen ifadeler bu kalıba uymaz, ifade olarak sarılır.
_JS_FN_HEAD = re.compile(r"^(?:async\b\s*)?(?:function\b|(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>)")


def js_function(js: str) -> str:
    """Modelin yazdığı JS'i çağrılabilir fonksiyon metnine çevir: zaten fonksiyonsa olduğu gibi, değilse ifade olarak sar."""
    body = js.strip()
    if _JS_FN_HEAD.match(body):
        return body
    return f"() => ({body.rstrip(';').rstrip()})"


CHROMIUM_FAMILY = ("opera", "chrome", "msedge", "brave", "vivaldi", "chromium", "yandex", "arc")


def default_browser_exe() -> str | None:
    """Windows'ta varsayılan tarayıcının exe yolu (ör. Opera GX). Bulunamazsa None."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    return exe_from_command(cmd)


def exe_from_command(cmd: str) -> str | None:
    m = re.match(r'\s*"([^"]+\.exe)"', cmd, re.I) or re.match(r"\s*(\S+\.exe)", cmd, re.I)
    return m.group(1) if m else None


def is_chromium(exe: str) -> bool:
    name = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return any(n in name for n in CHROMIUM_FAMILY)


def cdp_alive(endpoint: str, timeout: float = 0.4) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # yerel port, proxy'ye gitmesin
        with opener.open(endpoint.rstrip("/") + "/json/version", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


# Tarayıcının "Allow remote debugging for this browser instance" ayarı (opera://inspect, chrome://inspect)
# açıkken portu ve yolu kullanıcı veri klasöründeki DevToolsActivePort dosyasına yazar.
USER_DATA_DIRS = [
    ("APPDATA", r"Opera Software\Opera GX Stable"), ("APPDATA", r"Opera Software\Opera Stable"),
    ("LOCALAPPDATA", r"Google\Chrome\User Data"), ("LOCALAPPDATA", r"Microsoft\Edge\User Data"),
    ("LOCALAPPDATA", r"BraveSoftware\Brave-Browser\User Data"), ("LOCALAPPDATA", r"Vivaldi\User Data"),
]
BLANK_URLS = ("about:blank", "chrome://newtab", "opera://startpage", "chrome://new-tab-page", "edge://newtab",
              "opera://speeddial", "chrome-search://")


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def devtools_active_endpoint(dirs: list[Path] | None = None) -> str | None:
    """Kullanıcının açık tarayıcısında uzaktan hata ayıklama açıksa ws:// adresi."""
    if dirs is None:
        dirs = [Path(os.environ[env]) / sub for env, sub in USER_DATA_DIRS if os.environ.get(env)]
    for d in dirs:
        f = d / "DevToolsActivePort"
        try:
            lines = f.read_text(encoding="utf-8").split()
            port = int(lines[0])
        except (OSError, ValueError, IndexError):
            continue
        if _port_open(port):
            path = lines[1] if len(lines) > 1 else ""
            return f"ws://127.0.0.1:{port}{path}" if path else f"http://127.0.0.1:{port}"
    return None


def bridge_alive(port: int, timeout: float = 0.4) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/alice-bridge", timeout=timeout) as r:
            return r.read().startswith(b"alice-bridge")
    except Exception:  # noqa: BLE001
        return False


def ensure_bridge(port: int, wait: float = 8.0) -> bool:
    """Tarayıcı köprüsü çalışmıyorsa çekirdekten bağımsız (ayrık) bir süreç olarak başlat."""
    if bridge_alive(port):
        return True
    import subprocess
    import time

    exe = Path(sys.executable)
    if sys.platform == "win32" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"  # konsol penceresi açılmasın
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen([str(exe), "-m", "alice.tools.browser_bridge", "--port", str(port)], creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=sys.platform != "win32")
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if bridge_alive(port):
            return True
        time.sleep(0.2)
    return False


def user_browser_endpoint(cdp: str | None, bridge_port: int | None = None) -> str | None:
    if cdp and cdp_alive(cdp):
        return cdp
    found = devtools_active_endpoint()
    if found and bridge_port:
        # Doğrudan bağlanmak yerine köprü üzerinden: çekirdek yeniden başlasa da tarayıcı izni tekrar sormaz.
        from alice.tools.browser_bridge import endpoint

        if ensure_bridge(bridge_port):
            return endpoint(bridge_port)
    return found


def host_of(url: str) -> str:
    h = urlparse(url if "://" in url else "https://" + url).hostname or ""
    return h[4:] if h.startswith("www.") else h


NO_SEPARATE = ("Kullanıcının açık tarayıcısına bağlanılamıyor ve ayrı pencere açılmıyor (kullanıcı istemiyor). "
               "Bunun yerine kullanıcının kendi tarayıcısında çalış: windows action=switch_tab (açık sekmeye geç), "
               "windows action=navigate (sekmede adres/arama), ui_elements + ui_act (sayfada tıklama/yazma). "
               "Ayrı pencereyi sadece kullanıcı açıkça isterse separate=true ile aç.")


class BrowserSession:
    """Önce kullanıcının açık tarayıcısına (uzaktan hata ayıklama portu açıksa) bağlanır; yoksa
    varsayılan tarayıcıyı (Chromium tabanlıysa, ör. Opera GX) ALICE profiliyle açar."""

    def __init__(self, headless: bool = False, executable: str | None = None, cdp: str | None = None,
                 allow_launch: bool = True, bridge_port: int | None = None):
        endpoint = user_browser_endpoint(cdp, bridge_port)
        if not allow_launch and not endpoint:
            raise ToolError(NO_SEPARATE)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ToolError("playwright kurulu değil: pip install 'alice-harness[browser]' && playwright install chromium") from e
        self._pw = sync_playwright().start()
        _register_exit_cleanup()
        self.driver_pid = _driver_pid(self._pw)
        self.aborted = ""  # "timeout" | "stop": sürücü öldürüldü, bu oturum artık kullanılamaz
        self._bridge_port = None
        if bridge_port and endpoint:
            from alice.tools.browser_bridge import endpoint as bridge_endpoint

            if endpoint == bridge_endpoint(bridge_port):
                self._bridge_port = bridge_port
        try:
            self._start(endpoint, headless, executable)
        except BaseException:
            # Bağlantı/başlatma başarısız: Playwright sürücüsü açık kalırsa sonraki deneme de patlıyor.
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            raise

    def alive(self) -> bool:
        """Tarayıcı bağlantısı hâlâ çalışıyor mu (Opera yeniden başladı, köprü kapandı, ALICE penceresi kapatıldı)."""
        if getattr(self, "aborted", ""):
            return False
        port = getattr(self, "_bridge_port", None)
        if port and not bridge_alive(port):
            # Köprü tarayıcıyı ölü sayıp kapandığında Playwright bunu fark etmiyor (is_connected True kalıyor) ve
            # sonraki çağrı ölü sokete gidip sonsuza dek bekliyordu.
            return False
        try:
            if self._browser is not None:
                return bool(self._browser.is_connected())
            # ALICE'in kendi penceresi: kullanıcı kapatınca bağlam kapanır; eskiden True dönüyordu ve sonraki her
            # çağrı "Target page, context or browser has been closed" ile düşüyordu, yeniden açılmıyordu.
            return not getattr(self, "_ctx_closed", False)
        except Exception:  # noqa: BLE001
            return False

    def abort(self, reason: str) -> None:
        """Takılan çağrıyı kes: Playwright sürücüsünü öldür, bekleyen her çağrı "Connection closed" ile hemen döner.
        Başka thread'den (bekçi, Durdur düğmesi) çağrılabilir; kullanıcının tarayıcısına dokunmaz."""
        if getattr(self, "aborted", ""):
            return
        self.aborted = reason
        pid = getattr(self, "driver_pid", None)
        if not pid:
            return
        try:
            import psutil

            psutil.Process(pid).kill()
        except Exception:  # noqa: BLE001 - süreç zaten bitmiş olabilir
            pass

    def _mark_closed(self, *_: Any) -> None:
        self._ctx_closed = True

    def ensure_page(self):
        """Etkin sekme kapatıldıysa (kullanıcı kapattı, site kendini kapattı) açık bir sekmeye, o da yoksa yeni
        sekmeye geç. Yoksa her işlem kapanmış sayfada "Target closed" hatası veriyordu."""
        page = getattr(self, "page", None)
        if page is not None and not _is_closed(page):
            return page
        open_pages = [p for p in self.ctx.pages if not _is_closed(p)]
        self.page = open_pages[-1] if open_pages else self.ctx.new_page()
        return self.page

    def _start(self, endpoint, headless: bool, executable: str | None) -> None:
        self.attached = False
        self._browser = None
        self._cdp = None
        if endpoint:
            # Tarayıcı bağlantı için kullanıcıdan bir kez onay isteyebilir: ona zaman tanı.
            browser = self._pw.chromium.connect_over_cdp(endpoint, timeout=60000)
            self._browser = browser
            self.ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            self.attached = True
            self.page = self._visible_page() or (self.ctx.pages[-1] if self.ctx.pages else self.ctx.new_page())
            self.ctx.on("page", self._on_page)
            return
        profile = alice_home() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"headless": headless, "viewport": None if not headless else {"width": 1366, "height": 860},
                                  "args": ["--start-maximized"]}
        exe = executable or default_browser_exe()
        self.ctx = None
        if exe and (executable or is_chromium(exe)):
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(str(profile), executable_path=exe, **kwargs)
            except Exception:  # noqa: BLE001
                if executable:
                    raise
        if self.ctx is None:
            self.ctx = self._launch(profile, kwargs)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.ctx.on("page", self._on_page)
        self.ctx.on("close", self._mark_closed)

    def _launch(self, profile, kwargs):
        try:
            # Sistemde kurulu Chrome/Edge varsa onu kullan (ayrı indirme gerekmez).
            return self._pw.chromium.launch_persistent_context(str(profile), channel="chrome", **kwargs)
        except Exception:  # noqa: BLE001
            try:
                return self._pw.chromium.launch_persistent_context(str(profile), channel="msedge", **kwargs)
            except Exception:  # noqa: BLE001
                return self._pw.chromium.launch_persistent_context(str(profile), **kwargs)

    def targets(self) -> list[dict[str, Any]]:
        """Sekmelerin başlık/adresleri tarayıcının kendisinden (sayfaya sormadan: arka planda uyutulan sekmeler
        sayfa içi komutlara hiç cevap vermiyor, ALICE kilitleniyordu)."""
        if self._browser is None:
            return []
        try:
            if self._cdp is None:
                self._cdp = self._browser.new_browser_cdp_session()
            infos = self._cdp.send("Target.getTargets")["targetInfos"]
        except Exception:  # noqa: BLE001
            return []
        return [t for t in infos if t.get("type") == "page"]

    def title_of(self, page) -> str:
        if not self.attached:
            try:
                return page.title()
            except Exception:  # noqa: BLE001
                return ""
        for t in self.targets():
            if t.get("url") == page.url:
                return t.get("title", "")
        return ""

    def _visible_page(self):
        """Kullanıcının şu an baktığı sekme: öndeki tarayıcı penceresinin başlığıyla eşleşen sekme."""
        try:
            from alice.tools.desktop import active_title

            fg = re.sub(r"\s+-\s+[^-]+$", "", active_title()).strip()
        except Exception:  # noqa: BLE001
            fg = ""
        if not fg:
            return None
        urls = [t.get("url") for t in self.targets() if t.get("title", "").strip() == fg]
        for p in self.ctx.pages:
            if p.url in urls:
                return p
        return None

    def evaluate(self, js: str, arg: Any = None, timeout: float = 10000) -> Any:
        """Sayfada JS çalıştır, ama süre sınırıyla (page.evaluate sınırsız bekler)."""
        page = self.page
        if self.attached:
            try:
                page.bring_to_front()  # uyutulmuş sekmeyi uyandır
            except Exception:  # noqa: BLE001
                pass
        fn = js_function(js)
        try:
            return page.locator("html").evaluate(f"(_el, arg) => ({fn})(arg)", arg, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            if "Timeout" in type(e).__name__ or "timeout" in str(e).lower():
                raise ToolError(f"sayfa {timeout / 1000:.0f} sn içinde cevap vermedi (sekme donmuş/uyutulmuş olabilir)") from e
            raise

    def tab_for(self, url: str):
        """Kullanıcının tarayıcısında goto için sekme: aynı site açıksa o sekme, öndeki sekme boşsa o, yoksa yeni."""
        want = host_of(url)
        for p in self.ctx.pages:
            if want and host_of(p.url) == want:
                return p
        if self.page is not None and (not self.page.url or self.page.url.startswith(BLANK_URLS)):
            return self.page
        return self.ctx.new_page()

    def _on_page(self, page) -> None:
        self.page = page  # yeni sekme açılınca ona geç

    def snapshot(self, limit: int = 120) -> list[dict[str, Any]]:
        return self.evaluate(SNAPSHOT_JS, limit)

    def locator(self, element_id: int):
        from alice.tools.computer import as_number

        loc = self.page.locator(f'[data-alice-id="{as_number(element_id, "element_id", int)}"]')
        if loc.count() == 0:
            raise ToolError("öğe bulunamadı; sayfa değişmiş olabilir, browser action=snapshot ile yenile")
        return loc.first

    def close(self) -> None:
        try:
            if not self.attached and not getattr(self, "aborted", ""):  # kullanıcının tarayıcısını kapatma, bağlantıyı bırak
                self.ctx.close()
        finally:
            self._pw.stop()


def _driver_pid(pw: Any) -> int | None:
    try:
        return int(pw._impl_obj._connection._transport._proc.pid)
    except Exception:  # noqa: BLE001 - Playwright iç yapısı değişirse bekçi sadece kesemez
        return None


def _is_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:  # noqa: BLE001
        return True


def kill_playwright_drivers() -> int:
    """Bu sürecin başlattığı Playwright sürücülerini (node ... run-driver) sessizce sonlandır.

    ALICE kapanırken ya da güncelleme sonrası yeniden başlarken sürücü, kapanmış boruya yazmaya çalışıp
    "Error: EPIPE: broken pipe" yığınını core.log'a döküyordu. Playwright nesneleri oluşturuldukları iş parçacığına
    bağlı olduğundan çıkışta düzgün stop() her zaman mümkün değil; sürücü zaten sahipsiz kalacağı için öldürülür.
    Tarayıcı köprüsü (ayrı süreç) ve kullanıcının tarayıcısı etkilenmez."""
    try:
        import psutil

        kids = psutil.Process().children(recursive=True)
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for p in kids:
        try:
            cmd = " ".join(p.cmdline()).lower()
            if "playwright" in cmd and "run-driver" in cmd:
                p.kill()
                n += 1
        except Exception:  # noqa: BLE001 - süreç bu arada bitmiş olabilir
            continue
    return n


_ATEXIT = False


def _register_exit_cleanup() -> None:
    global _ATEXIT
    if not _ATEXIT:
        import atexit

        atexit.register(kill_playwright_drivers)
        _ATEXIT = True


# Playwright'ın bağlantı koptuğunda verdiği hatalar (Opera kapandı/yeniden başladı, köprü ölü bağlantıyı kesti).
_CONNECTION_LOST = ("has been closed", "target closed", "browser closed", "connection closed", "not connected",
                    "disconnected", "websocket error", "socket closed", "connection refused", "econnrefused")
# Bağlantı kopunca yeni bağlantıyla bir kez daha denenebilen eylemler: tekrar etmek bir şeyi iki kez yapmaz.
RETRY_SAFE = ("goto", "snapshot", "text", "screenshot", "tabs")
RECONNECT_HINT = ("tarayıcı bağlantısı koptu (Opera kapandı, yeniden başladı ya da cevap vermiyor); bağlantı "
                  "sıfırlandı. İşlem yapılmış olabilir: browser action=snapshot ile sayfaya bak, sonra gerekiyorsa tekrar dene.")


def connection_lost(e: BaseException) -> bool:
    msg = f"{type(e).__name__} {e}".lower()
    return any(k in msg for k in _CONNECTION_LOST)


# Bir eylemin en uzun süresi (sn). Playwright'ın bazı çağrıları (CDP komutları, ölü bağlantıdaki istekler) süre
# sınırı tanımıyor; süre dolunca sürücü öldürülür ki ALICE dakikalarca "çalışıyor" diye takılı kalmasın.
ACTION_BUDGET = {"goto": 120.0, "back": 60.0, "screenshot": 40.0}
DEFAULT_BUDGET = 45.0
CONNECT_BUDGET = 70.0  # yeni bağlantı: tarayıcı kullanıcıdan izin isteyebilir (connect_over_cdp 60 sn bekler)
STOPPED = "durduruldu (kullanıcı kesti)"


def abort_browser(ctx: ToolContext, reason: str = "stop") -> None:
    """Çalışan tarayıcı çağrısını kes (Durdur düğmesi, bekçi). Başka thread'den çağrılır. Çağrı yoksa bağlantıya
    dokunmaz: boşuna koparılırsa tarayıcı tekrar izin sorabilir."""
    b = ctx.state.get("browser")
    if b is not None and ctx.state.get("browser_busy"):
        b.abort(reason)


class _Watchdog:
    """Eylem süresini aşarsa o anki oturumu keser. Oturum eylemin içinde açılabildiği için ateşlendiği anda bakar."""

    def __init__(self, ctx: ToolContext, seconds: float):
        self.timer = threading.Timer(seconds, abort_browser, args=(ctx, "timeout"))
        self.timer.daemon = True

    def __enter__(self) -> "_Watchdog":
        self.timer.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.timer.cancel()


def budget_for(ctx: ToolContext, action: str) -> float:
    b = ctx.state.get("browser")
    fresh = b is None or not b.alive()
    return ACTION_BUDGET.get(action, DEFAULT_BUDGET) + (CONNECT_BUDGET if fresh else 0.0)


def _aborted(ctx: ToolContext) -> str:
    return str(getattr(ctx.state.get("browser"), "aborted", "") or "")


def _timeout_hint(action: str) -> str:
    return (f"tarayıcı '{action}' için cevap vermedi (bağlantı takıldı); bağlantı kesildi ve sıfırlandı. İşlem yapılmış "
            "olabilir: browser action=snapshot ile sayfaya bak.")


def drop_browser(ctx: ToolContext) -> None:
    """Oturumu bırak: sonraki çağrı yeniden bağlanır (kullanıcının tarayıcısı kapatılmaz)."""
    b = ctx.state.pop("browser", None)
    if b is not None:
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass


def get_browser(ctx: ToolContext, separate: bool = False) -> BrowserSession:
    b = ctx.state.get("browser")
    if b is not None and not b.alive():  # bağlantı ölmüş: her araç çağrısı "browser has been closed" demesin
        drop_browser(ctx)
        b = None
    if b is None:
        a = ctx.config.agent
        allow = separate or bool(a.get("browser_headless", False)) or bool(a.get("browser_allow_separate", False))
        b = BrowserSession(headless=bool(a.get("browser_headless", False)), executable=a.get("browser_executable") or None,
                           cdp=a.get("browser_cdp") or None, allow_launch=allow,
                           bridge_port=int(a.get("browser_bridge_port", 9339) or 0) or None)
        ctx.state["browser"] = b
    b.ensure_page()
    return b


def format_snapshot(page_title: str, url: str, items: list[dict[str, Any]]) -> str:
    rows = [f"sayfa: {page_title}", f"url: {url}"]
    for i in items:
        label = i["text"] or i["placeholder"] or i["href"]
        kind = i["role"] or i["tag"]
        if i["tag"] == "input" and i["type"]:
            kind = f"input[{i['type']}]"
        extra = f" değer='{i['value']}'" if i["value"] else ""
        off = "" if i["inView"] else " (aşağıda)"
        rows.append(f"[{i['id']}] {kind} '{label}'{extra}{off}")
    return "\n".join(rows)


class Browser(Tool):
    name = "browser"
    untrusted = True
    description = (
        "Kullanıcının kendi açık tarayıcısında (Opera vb., kendi girişleriyle) web sayfalarıyla çalışır: siteye "
        "gitme, sayfayı okuma, tıklama, form doldurma. Her sitede aynı şekilde çalışır.\n"
        "- Akış: goto ya da snapshot ile öğe listesini al, sonra click/type ile ID üzerinden işlem yap. ID'ler her "
        "snapshot'ta yenilenir; sayfa değişince eski ID'yi kullanma.\n"
        "- goto, site zaten bir sekmede açıksa o sekmeyi kullanır; yeni sekme açmaz.\n"
        "- Sayfanın içeriğini okumak için text, görsel düzen için screenshot.\n"
        "- 'Bağlanılamıyor' derse ayrı pencere açma: windows switch_tab/navigate + ui_elements/ui_act ile kullanıcının "
        "sekmelerinde çalış.\n"
        "- Ekrandaki bir şeye tek tıklama/yazma için reflex daha hızlı; birçok adımlı iş için run_plan."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["goto", "snapshot", "click", "type", "select", "press", "scroll", "back",
                                              "text", "screenshot", "eval", "tabs", "switch_tab", "close"],
                   "description": "goto: url'ye git | snapshot: tıklanabilir öğeleri [ID] ile listele | click: "
                   "element_id'ye tıkla | type: element_id alanına text yaz (submit=true Enter'a basar) | select: "
                   "açılır listede text seçeneğini seç | press: keys tuşuna bas | scroll: amount piksel kaydır | "
                   "back: geri | text: sayfanın düz metni | screenshot: sayfa görüntüsü | eval: js çalıştır | tabs: açık "
                   "sekmeler | switch_tab: index'li sekmeye geç | close. goto/click/type/press/scroll sonrası güncel "
                   "snapshot otomatik döner."},
        "url": {"type": "string", "description": "goto için adres, ör. 'https://mail.google.com' ya da 'github.com'."},
        "element_id": {"type": "integer", "description": "click/type/select için son snapshot'taki [ID]."},
        "text": {"type": "string", "description": "type için yazılacak metin; select için seçeneğin görünen adı."},
        "submit": {"type": "boolean", "description": "type sonrası Enter'a bas (arama kutuları için). Varsayılan false."},
        "keys": {"type": "string", "description": "press için tuş, ör. 'Enter', 'Escape', 'Control+A'."},
        "amount": {"type": "integer", "description": "scroll için piksel; pozitif aşağı, negatif yukarı. Varsayılan 600."},
        "js": {"type": "string", "description": "eval için JavaScript ifadesi, ör. 'document.title'."},
        "index": {"type": "integer", "description": "switch_tab için tabs çıktısındaki sekme numarası (0'dan başlar)."},
        "separate": {"type": "boolean", "description": "Ayrı ALICE penceresi aç. Sadece kullanıcı açıkça isterse."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("snapshot", "text", "screenshot", "tabs"):
            return READ
        if a == "goto":
            return NETWORK
        return COMPUTER

    def run(self, ctx: ToolContext, action: str, **kw: Any) -> ToolResult:
        try:
            return self._guarded(ctx, action, kw)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if action == "close" or not connection_lost(e):
                raise
            # Bağlantı ölmüş: oturumu at ki ALICE "browser has been closed" deyip orada kalmasın.
            reason = _aborted(ctx)
            drop_browser(ctx)
            if reason == "stop":
                raise ToolError(STOPPED) from e
            if action not in RETRY_SAFE:
                raise ToolError(_timeout_hint(action) if reason == "timeout" else RECONNECT_HINT) from e
        try:
            return self._guarded(ctx, action, kw)  # yeni bağlantıyla bir kez daha
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if not connection_lost(e):
                raise
            reason = _aborted(ctx)
            drop_browser(ctx)
            if reason == "stop":
                raise ToolError(STOPPED) from e
            if reason == "timeout":
                raise ToolError(_timeout_hint(action) + " Yeni bağlantıyla da olmadı: tarayıcıyı kapatıp aç ya da "
                                "windows/ui_act ile kullanıcının sekmelerinde çalış.") from e
            raise ToolError("tarayıcıya yeniden bağlanılamadı: Opera açık mı, opera://inspect'te 'Allow remote "
                            "debugging' açık mı? İzin penceresi çıktıysa onayla.") from e

    def _guarded(self, ctx: ToolContext, action: str, kw: dict[str, Any]) -> ToolResult:
        ctx.state["browser_busy"] = True
        try:
            with _Watchdog(ctx, budget_for(ctx, action)):
                return self._run(ctx, action, **kw)
        finally:
            ctx.state["browser_busy"] = False

    def _run(self, ctx: ToolContext, action: str, url: str | None = None, element_id: int | None = None,
             text: str | None = None, submit: bool = False, keys: str | None = None, amount: int = 600,
             js: str | None = None, index: int | None = None, separate: bool = False) -> ToolResult:
        from alice.tools.computer import as_bool, as_number

        # Zayıf modeller sayıları/bayrakları metin gönderiyor: "false" metni Enter'a bastırıyordu (bool("false")).
        submit, separate = as_bool(submit, "submit"), as_bool(separate, "separate")
        amount = as_number(600 if amount is None else amount, "amount", int)
        index = None if index is None else as_number(index, "index", int)
        if element_id is None and action in ("click", "type", "select"):
            raise ToolError(f"{action} için element_id gerekli (snapshot çıktısındaki [ID])")
        if action == "close":
            b = ctx.state.get("browser")
            if b is not None and b.attached:
                # Kullanıcının kendi tarayıcısı: bağlantıyı açık tut, yeniden bağlanmak Opera'da yine izin sorar.
                return ToolResult("kullanıcının tarayıcısı açık bırakıldı (ALICE'in ayrı penceresi yok)")
            ctx.state.pop("browser", None)
            if b:
                b.close()
            return ToolResult("tarayıcı kapatıldı")
        b = get_browser(ctx, separate)
        if b.attached and action in ("snapshot", "text", "screenshot"):
            # Kullanıcı bu arada başka sekmeye geçmiş olabilir: "ekrandaki" neyse ona bak.
            seen = b._visible_page()
            if seen is not None:
                b.page = seen
        page = b.page
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))

        def snap(prefix: str) -> ToolResult:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            p = b.page
            return ToolResult(truncate(f"{prefix}\n{format_snapshot(b.title_of(p), p.url, b.snapshot())}", limit))

        if action == "goto":
            if not url:
                raise ToolError("goto için url gerekli")
            if "://" not in url:
                url = "https://" + url
            if b.attached:  # kullanıcının kendi tarayıcısı: açık site sekmesini kullan, her seferinde yeni açma
                b.page = page = b.tab_for(url)
                page.bring_to_front()
                if host_of(page.url) == host_of(url) and urlparse(url).path.strip("/") == "" and not urlparse(url).query:
                    return snap(f"zaten açık sekmeye geçildi: {b.title_of(page)}")  # içindekini (çalan video) bozma
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:  # noqa: BLE001
                # Yeni açılan tarayıcı (ör. Opera GX'in GX Corner'ı) kendi başlangıç sayfasını yüklerken ilk gidiş
                # ERR_ABORTED alıyor; bir saniye sonra tekrar dene.
                if "ERR_ABORTED" not in str(e):
                    raise
                page.wait_for_timeout(1000)
                page = b.page
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return snap(f"açıldı: {url}")
        if action == "snapshot":
            return snap("snapshot")
        if action == "click":
            loc = b.locator(element_id)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=8000)
            page.wait_for_timeout(600)
            return snap(f"tıklandı: [{element_id}]")
        if action == "type":
            loc = b.locator(element_id)
            loc.fill("" if text is None else str(text), timeout=8000)
            if submit:
                loc.press("Enter")
                page.wait_for_timeout(1000)
            return snap(f"yazıldı: [{element_id}]")
        if action == "select":
            if text is None or not str(text).strip():
                raise ToolError("select için text (seçeneğin görünen adı) gerekli")
            b.locator(element_id).select_option(label=str(text), timeout=8000)
            return snap(f"seçildi: {text}")
        if action == "press":
            keys = str(keys or "Enter")
            page.keyboard.press(keys)
            page.wait_for_timeout(500)
            return snap(f"tuş: {keys}")
        if action == "scroll":
            page.mouse.wheel(0, amount)
            page.wait_for_timeout(400)
            return snap(f"kaydırıldı: {amount}")
        if action == "back":
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            return snap("geri")
        if action == "text":
            return ToolResult(truncate(f"{b.title_of(page)}\n{page.url}\n\n{b.evaluate(TEXT_JS)}", limit))
        if action == "screenshot":
            png = page.screenshot(type="png", timeout=15000)  # donmuş sekmede sonsuza dek beklemesin
            from io import BytesIO

            from PIL import Image

            from alice.tools.imaging import image_to_png_b64

            b64, _ = image_to_png_b64(Image.open(BytesIO(png)), int(ctx.config.agent.get("screenshot_max_width", 1366)))
            return ToolResult(f"tarayıcı görüntüsü: {page.url}", images=[b64])
        if action == "eval":
            if not js:
                raise ToolError("eval için js gerekli")
            return ToolResult(truncate(repr(b.evaluate(js)), limit))
        if action == "tabs":
            rows = [f"{i}: {b.title_of(p)} - {p.url}{'  <- aktif' if p is b.page else ''}" for i, p in enumerate(b.ctx.pages)]
            return ToolResult("\n".join(rows))
        if action == "switch_tab":
            pages = b.ctx.pages
            if index is None or not 0 <= index < len(pages):
                raise ToolError(f"geçersiz sekme index: {index}; 0 ile {len(pages) - 1} arası olmalı (action=tabs ile bak)")
            b.page = pages[index]
            b.page.bring_to_front()
            return snap(f"sekme {index}")
        raise ToolError(f"bilinmeyen action: {action}. Geçerli: {', '.join(self.parameters['properties']['action']['enum'])}")


def cleanup(ctx: ToolContext) -> None:
    b = ctx.state.pop("browser", None)
    if b:
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass


TOOLS = [Browser()]
