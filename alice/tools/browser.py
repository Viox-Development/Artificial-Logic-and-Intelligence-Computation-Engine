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
              '[role=searchbox],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
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
        """Tarayıcı bağlantısı hâlâ çalışıyor mu (Opera yeniden başladı, köprü kapandı...)."""
        try:
            if self._browser is not None:
                return bool(self._browser.is_connected())
            return True  # ALICE'in kendi penceresi: kapanırsa Playwright hatası zaten anlaşılır
        except Exception:  # noqa: BLE001
            return False

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
        body = js.strip()
        fn = body if (body.startswith(("(", "function", "async")) and "=>" in body or body.startswith("function")) \
            else f"() => ({body})"
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
        loc = self.page.locator(f'[data-alice-id="{int(element_id)}"]')
        if loc.count() == 0:
            raise ToolError("öğe bulunamadı; sayfa değişmiş olabilir, browser action=snapshot ile yenile")
        return loc.first

    def close(self) -> None:
        try:
            if not self.attached:  # kullanıcının kendi tarayıcısını kapatma, sadece bağlantıyı bırak
                self.ctx.close()
        finally:
            self._pw.stop()


def get_browser(ctx: ToolContext, separate: bool = False) -> BrowserSession:
    b = ctx.state.get("browser")
    if b is not None and not b.alive():  # bağlantı ölmüş: her araç çağrısı "browser has been closed" demesin
        ctx.state.pop("browser", None)
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass
        b = None
    if b is None:
        a = ctx.config.agent
        allow = separate or bool(a.get("browser_headless", False)) or bool(a.get("browser_allow_separate", False))
        b = BrowserSession(headless=bool(a.get("browser_headless", False)), executable=a.get("browser_executable") or None,
                           cdp=a.get("browser_cdp") or None, allow_launch=allow,
                           bridge_port=int(a.get("browser_bridge_port", 9339) or 0) or None)
        ctx.state["browser"] = b
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

    def run(self, ctx: ToolContext, action: str, url: str | None = None, element_id: int | None = None,
            text: str | None = None, submit: bool = False, keys: str | None = None, amount: int = 600,
            js: str | None = None, index: int | None = None, separate: bool = False) -> ToolResult:
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
            loc = b.locator(element_id if element_id is not None else -1)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=8000)
            page.wait_for_timeout(600)
            return snap(f"tıklandı: [{element_id}]")
        if action == "type":
            loc = b.locator(element_id if element_id is not None else -1)
            loc.fill(text or "", timeout=8000)
            if submit:
                loc.press("Enter")
                page.wait_for_timeout(1000)
            return snap(f"yazıldı: [{element_id}]")
        if action == "select":
            b.locator(element_id if element_id is not None else -1).select_option(label=text, timeout=8000)
            return snap(f"seçildi: {text}")
        if action == "press":
            page.keyboard.press(keys or "Enter")
            page.wait_for_timeout(500)
            return snap(f"tuş: {keys}")
        if action == "scroll":
            page.mouse.wheel(0, amount)
            page.wait_for_timeout(400)
            return snap(f"kaydırıldı: {amount}")
        if action == "back":
            page.go_back(wait_until="domcontentloaded")
            return snap("geri")
        if action == "text":
            return ToolResult(truncate(f"{b.title_of(page)}\n{page.url}\n\n{b.evaluate(TEXT_JS)}", limit))
        if action == "screenshot":
            png = page.screenshot(type="png")
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
