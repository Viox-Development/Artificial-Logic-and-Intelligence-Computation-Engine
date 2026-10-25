"""Masaüstü: pencereler, uygulama açma, pano ve Windows UI Automation ağacı.

ui_elements, ekrandaki pencerenin tıklanabilir öğelerini ID'li bir listeye çevirir.
Bu liste hem beyin modeline (pixel tahmin etmeden tıklasın diye) hem de Jev reflex
döngüsüne aday menüsü olarak verilir.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from alice.tools.base import COMPUTER, EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

INTERACTIVE = {"Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "MenuItem", "ListItem", "TabItem",
               "Hyperlink", "TreeItem", "SplitButton", "Slider", "Spinner", "DataItem", "MenuBar", "Document"}


# ------------------------------------------------------------------ pencereler
def fold(s: str) -> str:
    """Büyük/küçük harf ve Türkçe i/ı farkını yok sayan karşılaştırma anahtarı. "İ".lower() "i̇" (i + birleşen
    nokta) verdiği için "indirilenler", "İndirilenler" başlığında bulunamıyordu; ı/i de birbirine eşlenir."""
    return (s or "").casefold().replace("\u0307", "").replace("ı", "i")


def pick_window(wins: list[Any], title: str, text_of=lambda w: w.title or "", strict: bool = False) -> Any:
    """Başlığa göre pencere seç: önce tam eşleşme, sonra başlığı o parçayla başlayan, sonra içeren.
    strict=True (kapatma gibi geri alınamaz işler) iken birden çok farklı pencere eşleşirse yanlışını
    kapatmamak için hata verir ve adayları listeler."""
    want = fold(title.strip())
    if not want:
        raise ToolError("pencere başlığı boş olamaz")
    hits = [w for w in wins if want in fold(text_of(w))]
    if not hits:
        raise ToolError(f"'{title}' başlıklı pencere yok")
    for test in (lambda t: t == want, lambda t: t.startswith(want)):
        best = [w for w in hits if test(fold(text_of(w)).strip())]
        if best:
            hits = best
            break
    names = list(dict.fromkeys(text_of(w) for w in hits))
    if strict and len(names) > 1:
        raise ToolError(f"'{title}' birden çok pencereyle eşleşiyor: " + " | ".join(names[:8])
                        + ". Hangisi olduğunu tam başlıkla belirt.")
    return hits[0]


def list_windows() -> list[str]:
    if IS_WIN or IS_MAC:
        try:
            import pygetwindow as gw
        except ImportError as e:
            raise ToolError("pygetwindow kurulu değil: pip install 'alice-harness[computer]'") from e
        return [t for t in gw.getAllTitles() if t.strip()]
    if shutil.which("wmctrl"):
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout
        return [line.split(None, 3)[-1] for line in out.splitlines() if len(line.split(None, 3)) == 4]
    raise ToolError("Linux'ta pencere listesi için wmctrl gerekli (sudo apt install wmctrl)")


def focus_window(title: str) -> str:
    if IS_WIN or IS_MAC:
        import pygetwindow as gw

        w = pick_window(gw.getAllWindows(), title)
        try:
            if getattr(w, "isMinimized", False):
                w.restore()
            w.activate()
        except Exception:  # noqa: BLE001 - Windows bazen ön plana almayı reddeder (oyun öndeyken)
            if not IS_WIN:
                raise
        if IS_WIN and not force_foreground(getattr(w, "_hWnd", 0)):
            raise ToolError(f"'{w.title}' öne alınamadı (Windows izin vermedi; tam ekran oyun açık olabilir)")
        return w.title
    if not title.strip():
        raise ToolError("pencere başlığı boş olamaz")
    _wmctrl(["-a", title], title)
    return title


def _wmctrl(args: list[str], title: str) -> None:
    try:
        r = subprocess.run(["wmctrl", *args], check=False, capture_output=True, timeout=10)
    except FileNotFoundError as e:
        raise ToolError("Linux'ta pencere işlemleri için wmctrl gerekli (sudo apt install wmctrl)") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("wmctrl 10 sn içinde cevap vermedi") from e
    if r.returncode != 0:  # eşleşen pencere yok: sessizce "öne alındı" demesin
        raise ToolError(f"'{title}' başlıklı pencere yok")


def force_foreground(hwnd: int) -> bool:
    """Pencereyi gerçekten öne al. Windows, öndeki uygulama (ör. tam ekran oyun) başka bir sürece
    ait olunca SetForegroundWindow'u sessizce reddeder; alt tuşu ve iş parçacığı girdisini bağlama
    hileleriyle tekrar dene. Sonunda öndeki pencereyi doğrula."""
    if not IS_WIN or not hwnd:
        return True
    import ctypes

    u = ctypes.windll.user32
    if u.GetForegroundWindow() == hwnd:
        return True
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    u.keybd_event(0x12, 0, 0, 0)  # alt bas-bırak: "son girdi bizden" sayılır, kilit açılır
    u.keybd_event(0x12, 0, 2, 0)
    u.SetForegroundWindow(hwnd)
    if u.GetForegroundWindow() != hwnd:
        fg = u.GetForegroundWindow()
        fg_thread = u.GetWindowThreadProcessId(fg, None)
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = fg_thread and fg_thread != me and u.AttachThreadInput(me, fg_thread, True)
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(me, fg_thread, False)
    time.sleep(0.1)
    return u.GetForegroundWindow() == hwnd


def window_op(title: str, op: str) -> str:
    ops = ("minimize", "maximize", "restore", "close")
    if op not in ops:
        raise ToolError(f"bilinmeyen pencere işlemi: {op}. Geçerli: {', '.join(ops)}")
    if not title.strip():
        raise ToolError("pencere başlığı boş olamaz")
    if not (IS_WIN or IS_MAC):
        if op == "close":
            _wmctrl(["-c", title], title)
        elif op == "maximize":
            _wmctrl(["-r", title, "-b", "add,maximized_vert,maximized_horz"], title)
        else:
            raise ToolError(f"Linux'ta {op} desteklenmiyor")
        return title
    import pygetwindow as gw

    w = pick_window(gw.getAllWindows(), title, strict=op == "close")
    {"minimize": w.minimize, "maximize": w.maximize, "restore": w.restore, "close": w.close}[op]()
    return w.title


def _shortcut_roots() -> list[Path]:
    return [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
            Path.home() / "Desktop", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]


def _start_menu_lookup(name: str) -> Path | None:
    roots = _shortcut_roots()
    needle = fold(name.strip())
    if not needle:
        return None
    best: Path | None = None
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() not in (".lnk", ".url", ".appref-ms"):
                continue
            stem = fold(p.stem)
            if stem == needle:
                return p
            if needle in stem and (best is None or len(stem) < len(best.stem)):
                best = p
    return best


def _norm(s: str) -> str:
    return "".join(ch for ch in fold(s) if ch.isalnum())


def _resolve_lnks(lnks: list[Path]) -> dict[Path, str]:
    """Kısayolların hedef exe yollarını tek PowerShell çağrısıyla çöz."""
    lnks = [p for p in lnks if p.suffix.lower() == ".lnk"]
    if not lnks:
        return {}
    paths = ",".join("'" + str(p).replace("'", "''") + "'" for p in lnks)
    ps = (f"$w=New-Object -ComObject WScript.Shell;foreach($p in @({paths}))"
          "{try{Write-Output ($p+'|'+$w.CreateShortcut($p).TargetPath)}catch{}}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15,
                           creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[Path, str] = {}
    for line in r.stdout.decode("utf-8", errors="replace").splitlines():
        lnk, _, target = line.strip().partition("|")
        if target:
            out[Path(lnk)] = target
    return out


def _steam_libraries() -> list[Path]:
    import re

    roots = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
             Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
    libs: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        libs.append(root / "steamapps" / "common")
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps" / "common")
        except OSError:
            pass
    return [p for p in dict.fromkeys(libs) if p.is_dir()]


def _exes_in(folder: Path, needle: str, depth: int = 3) -> list[Path]:
    """Klasörde adı aranan kelimeyi içeren exe'ler (yoksa klasördeki tüm exe'ler), sığ arama."""
    found: list[Path] = []
    stack = [(folder, 0)]
    while stack and len(found) < 40:
        d, lvl = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_file() and e.suffix.lower() == ".exe":
                found.append(e)
            elif e.is_dir() and lvl < depth:
                stack.append((e, lvl + 1))
    named = [f for f in found if needle in _norm(f.stem)]
    skip = ("unins", "setup", "crash", "update", "helper")
    return [f for f in (named or found) if not any(k in f.stem.lower() for k in skip)]


_START_APPS: list[tuple[str, str]] | None = None


def start_apps(refresh: bool = False) -> list[tuple[str, str]]:
    """Başlat menüsündeki TÜM uygulamalar (Microsoft Store / UWP dahil: WhatsApp, Spotify...) -> (ad, AppID).
    Store uygulamalarının exe'si/kısayolu yok; shell:AppsFolder\\<AppID> ile açılır."""
    global _START_APPS
    if _START_APPS is not None and not refresh:
        return _START_APPS
    apps: list[tuple[str, str]] = []
    if IS_WIN:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
                                 capture_output=True, timeout=15, creationflags=0x08000000)
            text = out.stdout.decode("utf-8", "replace") if out.returncode == 0 else ""
            for line in text.splitlines():
                name, _, app_id = line.partition("	")
                if name.strip() and app_id.strip():
                    apps.append((name.strip(), app_id.strip()))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    _START_APPS = apps
    return apps


def match_start_app(name: str) -> tuple[str, str] | None:
    needle = _norm(name)
    if not needle:
        return None
    apps = start_apps()
    for n, app_id in apps:  # önce tam eşleşme, sonra içeren
        if _norm(n) == needle:
            return n, app_id
    for n, app_id in apps:
        if needle in _norm(n):
            return n, app_id
    return None


def find_installed_apps(name: str, limit: int = 15) -> list[str]:
    """Kurulu bir programın yerini bul: Başlat menüsü, kayıt defteri (Uninstall, App Paths),
    Program Files, LOCALAPPDATA\\Programs ve Steam kütüphaneleri. Satır başına 'kaynak: yol'."""
    if not IS_WIN:
        exe = shutil.which(name)
        return [f"PATH: {exe}"] if exe else []
    needle = _norm(name)
    if not needle:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(src: str, path: str) -> None:
        path = path.strip().strip('"')
        if path and path.lower() not in seen and Path(path).exists():
            seen.add(path.lower())
            out.append(f"{src}: {path}")

    w = shutil.which(name)
    if w:
        add("PATH", w)
    for n, app_id in start_apps():
        if needle in _norm(n) and len(out) < limit:
            out.append(f"Başlat uygulaması ({n}): shell:AppsFolder\\{app_id}")

    # Başlat menüsü / masaüstü kısayolları (hedef exe'leriyle birlikte)
    roots = _shortcut_roots()
    lnks: list[Path] = []
    for root in roots:
        if root.is_dir():
            lnks += [p for p in root.rglob("*") if p.suffix.lower() in (".lnk", ".url", ".appref-ms")
                     and needle in _norm(p.stem)]
    targets = _resolve_lnks(lnks[:20])
    for lnk in lnks[:20]:
        t = targets.get(lnk, "")
        if t.lower().endswith(".exe"):
            add("kısayol hedefi", t)
        add("kısayol", str(lnk))

    # Kayıt defteri
    try:
        import winreg

        hives = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
        for hive, path in hives:
            try:
                root = winreg.OpenKey(hive, path)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
                    disp = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                except OSError:
                    continue
                if needle not in _norm(disp):
                    continue
                for val in ("DisplayIcon", "InstallLocation"):
                    try:
                        v = str(winreg.QueryValueEx(sub, val)[0]).split(",")[0].strip('"')
                    except OSError:
                        continue
                    if v.lower().endswith(".exe"):
                        add(f"kayıt ({disp})", v)
                    elif v and Path(v).is_dir():
                        for exe in _exes_in(Path(v), needle, depth=2)[:3]:
                            add(f"kayıt ({disp})", str(exe))
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                root = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                key = winreg.EnumKey(root, i)
                if needle in _norm(Path(key).stem):
                    try:
                        add("App Paths", str(winreg.QueryValue(root, key)))
                    except OSError:
                        pass
    except ImportError:
        pass

    # Program klasörleri ve Steam: adı eşleşen klasörlerin içindeki exe'ler
    bases = [Path(os.environ.get(k, "")) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    bases.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")
    bases += _steam_libraries()
    for base in dict.fromkeys(bases):
        if not str(base) or not base.is_dir():
            continue
        try:
            subs = list(base.iterdir())
        except OSError:
            continue
        for d in subs:
            if not d.is_dir():
                continue
            # "Blender Foundation\Blender 4.2" gibi bir alt seviyeye de bak
            cands = [d] if needle in _norm(d.name) else []
            if not cands:
                try:
                    cands = [c for c in d.iterdir() if c.is_dir() and needle in _norm(c.name)]
                except OSError:
                    cands = []
            for c in cands:
                for exe in _exes_in(c, needle)[:3]:
                    add("klasör", str(exe))
        if len(out) >= limit:
            break
    return out[:limit]


SCRIPT_EXT = (".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".scr", ".hta", ".com",
              ".reg", ".pif", ".cpl", ".jar", ".msp")
CMD_META = set('&|<>^%"')


def is_script_target(target: str) -> bool:
    t = target.strip().strip('"').lower()
    if re.match(r"^https?://", t):  # web adresi tarayıcıda açılır, çalıştırılmaz
        return False
    # Windows dosya adının sonundaki nokta/boşluğu ve ::$DATA akışını yok sayar: "x.bat." de x.bat'i çalıştırır.
    # file:///C:/x.bat de ShellExecute ile çalışır.
    t = re.sub(r"::\$data$", "", t.split("?", 1)[0] if t.startswith("file:") else t).rstrip(". ")
    return t.endswith(SCRIPT_EXT)


def open_app(target: str) -> str:
    """Uygulama adı, exe yolu, dosya ya da URL aç."""
    target = (target or "").strip()
    if not target:  # Windows'ta Path("") "." olur: boş hedef çalışma klasörünü açıyordu
        raise ToolError("açılacak hedef boş")
    if IS_WIN:
        p = Path(os.path.expandvars(target)).expanduser()
        if p.exists() or "://" in target or shutil.which(target):
            os.startfile(str(p) if p.exists() else (shutil.which(target) or target))  # type: ignore[attr-defined]
            return f"açıldı: {target}"
        if target.lower().startswith("shell:appsfolder"):
            subprocess.Popen(["explorer.exe", target])
            return f"açıldı: {target}"
        lnk = _start_menu_lookup(target)
        if lnk:
            os.startfile(str(lnk))  # type: ignore[attr-defined]
            return f"açıldı: {lnk.stem} ({lnk})"
        app = match_start_app(target)
        if app:  # Store uygulamaları (WhatsApp vb.) dahil Başlat menüsündeki her uygulama
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app[1]}"])
            return f"açıldı: {app[0]}"
        for line in find_installed_apps(target):
            path = line.split(": ", 1)[1]
            if path.lower().endswith(".exe"):
                os.startfile(path)  # type: ignore[attr-defined]
                return f"açıldı: {path}"
        # Son çare: shell'in kendi çözümlemesi (App Paths kayıtları dahil). cmd.exe & | ^ gibi karakterleri komut
        # ayırıcı sayar: "x&calc" ikinci bir komut çalıştırmasın (hedef bir web sayfasından gelmiş olabilir).
        if CMD_META & set(target):
            raise ToolError(f"'{target}' bulunamadı (geçersiz karakter içeriyor)")
        subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
        return (f"'{target}' kurulu programlarda bulunamadı; başlatma komutu yine de gönderildi. "
                "Pencere açılmadıysa program kurulu değildir ya da adı farklıdır.")
    if IS_MAC:
        if Path(target).expanduser().exists() or "://" in target:
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["open", "-a", target])
        return f"açıldı: {target}"
    exe = shutil.which(target)
    if exe:
        subprocess.Popen([exe], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("gtk-launch") and not Path(target).exists():
        subprocess.Popen(["gtk-launch", target], start_new_session=True)
    else:
        subprocess.Popen(["xdg-open", target], start_new_session=True)
    return f"açıldı: {target}"


BROWSER_TITLES = {"opera": "Opera", "chrome": "Chrome", "msedge": "Edge", "brave": "Brave", "firefox": "Firefox",
                  "vivaldi": "Vivaldi", "yandex": "Yandex"}


def browser_window_title() -> str:
    """Kullanıcının varsayılan tarayıcısının pencere başlığında geçen ad (ör. 'Opera')."""
    from alice.tools.browser import default_browser_exe

    exe = (default_browser_exe() or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for key, name in BROWSER_TITLES.items():
        if key in exe:
            return name
    return "Opera"


BLANK_TABS = ("yeni sekme", "new tab", "speed dial", "hızlı arama", "gx corner", "start page", "başlangıç")
SITE_TITLES = {"mail.google": "gmail", "music.youtube": "youtube music", "web.whatsapp": "whatsapp",
               "youtu.be": "youtube", "x.com": "/ x", "twitter": "/ x"}


def site_key(target: str) -> str | None:
    """URL'nin sekme başlığında görünen adı: youtube.com/results?... -> 'youtube'. Arama metni için None."""
    t = target.strip().lower()
    if " " in t or ("." not in t and "://" not in t):
        return None
    host = re.sub(r"^[a-z]+://", "", t).split("/", 1)[0].split(":", 1)[0]
    host = host[4:] if host.startswith("www.") else host
    for key, name in SITE_TITLES.items():
        if host.startswith(key):
            return name
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def is_site_root(target: str) -> bool:
    """'youtube.com', 'https://www.youtube.com/' gibi sadece site adresi mi (yol/sorgu yok)?"""
    rest = re.sub(r"^[a-z]+://", "", target.strip().lower()).split("/", 1)
    return len(rest) == 1 or rest[1].strip("/") == ""


def active_title() -> str:
    try:
        import pygetwindow as gw

        w = gw.getActiveWindow()
        return (w.title or "") if w else ""
    except Exception:  # noqa: BLE001
        return ""


def switch_tab(want: str, title: str | None = None, max_tabs: int = 40) -> str | None:
    """Kullanıcının açık tarayıcısında başlığında `want` geçen sekmeye geç (Ctrl+Tab ile gezerek).
    Bulursa sekme başlığını, bulamazsa None döner (başladığı sekmeye geri döner)."""
    from alice.tools.computer import press_keys

    want = fold(want.strip())
    if not want:
        raise ToolError("sekme başlığı boş olamaz")
    focus_window(title or browser_window_title())
    time.sleep(0.2)
    start = active_title()
    if want in fold(start):
        return start
    for _ in range(max_tabs):
        press_keys("ctrl+tab")
        time.sleep(0.12)
        cur = active_title()
        if want in fold(cur):
            return cur
        if cur == start:
            break
    return None


def current_tab_url(title: str | None = None) -> str:
    """Kullanıcının tarayıcısında öndeki sekmenin adresi (adres çubuğundan kopyalanır, pano geri konur)."""
    from alice.tools.computer import press_keys

    try:
        import pyperclip
    except ImportError as e:
        raise ToolError("pyperclip kurulu değil") from e
    focus_window(title or browser_window_title())
    time.sleep(0.2)
    old = None
    try:
        old = pyperclip.paste()
    except Exception:  # noqa: BLE001
        pass
    pyperclip.copy("")
    press_keys("ctrl+l")
    time.sleep(0.1)
    press_keys("ctrl+c")
    time.sleep(0.15)
    press_keys("esc")
    url = (pyperclip.paste() or "").strip()
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:  # noqa: BLE001
            pass
    return url


def navigate_tab(target: str, title: str | None = None, new_tab: bool | None = None) -> str:
    """Kullanıcının açık tarayıcısında gez (CDP gerekmez). Site zaten bir sekmede açıksa o sekmeye geçip orada,
    öndeki sekme boşsa orada, yoksa yeni sekmede açar. new_tab=True/False bunu zorlar."""
    from alice.tools.computer import press_keys, type_text

    name = title or browser_window_title()
    try:
        focus_window(name)
    except ToolError:
        open_app(target if "://" in target or site_key(target) else f"https://www.google.com/search?q={target}")
        return f"{name} açık değildi; varsayılan tarayıcıda açıldı: {target}"
    where = None
    key = site_key(target)
    if new_tab is None and key:
        found = switch_tab(key, name)
        if found and is_site_root(target):  # sadece siteyi aç: açık sekmeye geçmek yeter, içindekini bozma
            return f"zaten açık sekmeye geçildi: {found}"
        if found:
            new_tab, where = False, f"açık '{found}' sekmesinde"
    if new_tab is None:
        cur = active_title().lower()
        new_tab = not any(b in cur for b in BLANK_TABS)
    time.sleep(0.15)
    press_keys("ctrl+t" if new_tab else "ctrl+l")
    time.sleep(0.15)
    type_text(target)
    press_keys("enter")
    return f"{where or ('yeni sekmede' if new_tab else 'öndeki sekmede')} açıldı: {target}"


# ------------------------------------------------------------------- UIA ağacı
def _foreground_uia(title: str | None):
    try:
        from pywinauto import Desktop
    except ImportError as e:
        raise ToolError("pywinauto kurulu değil: pip install 'alice-harness[computer]'") from e
    desk = Desktop(backend="uia")
    if title and title.strip():
        return pick_window(desk.windows(), title, text_of=lambda w: w.window_text() or "")
    import ctypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return desk.window(handle=hwnd).wrapper_object()


def collect_ui_elements(ctx: ToolContext, title: str | None = None, limit: int = 150,
                        budget: float = 5.0) -> tuple[str, list[dict[str, Any]]]:
    """Öndeki (ya da başlığı verilen) pencerenin etkileşimli öğelerini topla. Chromium/WebView2 tabanlı
    uygulamalarda (WhatsApp, Discord...) ağaç çok büyük: budget saniyede ne bulunduysa onunla dön."""
    if not IS_WIN:
        raise ToolError("ui_elements şimdilik sadece Windows'ta (UI Automation). Linux/macOS'ta computer aracını kullan.")
    win = _foreground_uia(title)
    deadline = time.monotonic() + budget
    if hasattr(win, "iter_descendants"):
        descendants = win.iter_descendants(depth=14)  # tembel: süre dolunca durabilelim
    else:
        try:
            descendants = win.descendants(depth=14)
        except TypeError:
            descendants = win.descendants()
    items: list[dict[str, Any]] = []
    wrappers: dict[int, Any] = {}
    for el in descendants:
        if time.monotonic() > deadline:
            break
        try:
            info = el.element_info
            ctype = info.control_type
            if ctype not in INTERACTIVE or not el.is_visible():
                continue
            r = info.rectangle
            if r.width() <= 1 or r.height() <= 1:
                continue
            name = (info.name or "").strip()
            value = ""
            # Document'ın değeri sayfanın bütün metni: WebView2 penceresinde (ALICE'in kendisi, Discord) okumak
            # tek başına bir dakika sürebiliyordu ve 80 karakterden fazlası zaten kullanılmıyor.
            if ctype in ("Edit", "ComboBox"):
                try:
                    value = (el.get_value() if hasattr(el, "get_value") else el.window_text()) or ""
                except Exception:  # noqa: BLE001
                    value = ""
            if not name and not value and not info.automation_id and ctype != "Document":
                continue
            idx = len(items) + 1
            wrappers[idx] = el
            items.append({
                "id": idx, "role": ctype, "name": name[:80], "value": str(value)[:80],
                "auto_id": (info.automation_id or "")[:40],
                "enabled": el.is_enabled(),
                "center": ((r.left + r.right) // 2, (r.top + r.bottom) // 2),
                "rect": (r.left, r.top, r.right, r.bottom),
            })
            if len(items) >= limit:
                break
        except Exception:  # noqa: BLE001 - UIA öğeleri tararken kaybolabilir
            continue
    ctx.state["ui_elems"] = wrappers
    ctx.state["ui_items"] = {i["id"]: i for i in items}
    return win.window_text(), items


def format_ui(title: str, items: list[dict[str, Any]]) -> str:
    rows = [f"pencere: {title}"]
    for i in items:
        bits = [f"[{i['id']}] {i['role']}"]
        if i["name"]:
            bits.append(f"'{i['name']}'")
        if i["value"]:
            bits.append(f"değer='{i['value']}'")
        if not i["enabled"]:
            bits.append("(pasif)")
        rows.append(" ".join(bits))
    return "\n".join(rows)


def ui_act(ctx: ToolContext, element_id: int, action: str, text: str | None = None) -> str:
    from alice.tools.computer import as_number

    element_id = as_number(element_id, "element_id", int)
    text = None if text is None else str(text)
    wrappers = ctx.state.get("ui_elems") or {}
    el = wrappers.get(element_id)
    if el is None:
        raise ToolError("öğe bulunamadı; önce ui_elements çağır (liste her çağrıda yenilenir)")
    if action == "click":
        try:
            el.invoke()
        except Exception:  # noqa: BLE001 - invoke pattern'i olmayan öğeler
            el.click_input()
    elif action == "double_click":
        el.double_click_input()
    elif action == "right_click":
        el.right_click_input()
    elif action == "focus":
        el.set_focus()
    elif action == "set_text":
        if text is None:
            raise ToolError("set_text için text gerekli")
        try:
            el.set_edit_text(text)
        except Exception:  # noqa: BLE001
            el.set_focus()
            from alice.tools.computer import press_keys, type_text

            press_keys("ctrl+a")
            type_text(text)
    elif action == "type":
        el.set_focus()
        from alice.tools.computer import type_text

        type_text(text or "")
    elif action in ("toggle", "expand", "collapse", "select"):
        getattr(el, action)()
    else:
        raise ToolError(f"bilinmeyen aksiyon: {action}")
    time.sleep(0.2)
    item = (ctx.state.get("ui_items") or {}).get(element_id, {})
    return f"{action} -> [{element_id}] {item.get('role', '')} '{item.get('name', '')}'"


# ----------------------------------------------------------------------- araçlar
class Windows(Tool):
    name = "windows"
    description = ("Masaüstü pencereleri ve uygulamalar: uygulama/dosya açma, pencereyi öne alma, küçültme, kapatma; "
                   "kurulu programı bulma; kullanıcının tarayıcısında klavyeyle sekme değiştirme ve adres açma.\n"
                   "- Bir uygulamayı açmak için ilk tercih open (Başlat menüsü ve Store uygulamaları dahil); "
                   "simgesine tıklamaya çalışma.\n"
                   "- Bir programın kurulu olup olmadığını ya da exe yolunu öğrenmek için find; glob ya da shell "
                   "araması yapma.\n"
                   "- Açık bir pencerede iş: list ile bul, focus ile öne al, sonra ui_elements/ui_act.\n"
                   "- Web sayfasında okuma/tıklama için önce browser aracı. navigate ve switch_tab, browser "
                   "'bağlanılamıyor' dediğinde ya da sadece bir siteyi/aramayı açmak için.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "focus", "minimize", "maximize", "restore", "close", "open", "find",
                                              "navigate", "switch_tab"],
                   "description": "list: açık pencereler | focus/minimize/maximize/restore/close: title'daki pencere | "
                   "open: target'ı aç (uygulama adı, exe, dosya ya da URL; URL varsayılan tarayıcıda açılır) | find: "
                   "target programın exe yolunu bul (Başlat menüsü, kayıt defteri, Program Files, Steam) | navigate: "
                   "kullanıcının tarayıcısında target'a git; site zaten bir sekmede açıksa o sekmeyi kullanır, öndeki "
                   "sekme boşsa onu, yoksa yeni sekme açar | switch_tab: başlığında target geçen sekmeye geç."},
        "title": {"type": "string", "description": "Pencere başlığından bir parça, ör. 'Discord' ya da 'Not Defteri'. "
                  "focus/minimize/maximize/restore/close için gerekli; navigate/switch_tab'da tarayıcı penceresi."},
        "target": {"type": "string", "description": "open: 'Discord', 'notepad', 'C:/yol/dosya.pdf' ya da "
                   "'https://...' | find: program adı | navigate: URL ya da arama ifadesi, ör. 'github.com' | "
                   "switch_tab: sekme başlığından parça, ör. 'Gmail'."},
        "new_tab": {"type": "boolean", "description": "navigate için: true yeni sekme, false öndeki sekme. Boş: "
                    "otomatik."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("action") in ("list", "find"):
            return READ
        if args.get("action") == "open" and is_script_target(str(args.get("target") or "")):
            return EXEC  # betik/exe çalıştırmak komut çalıştırmakla aynı: run_command gibi onaya tabi
        return COMPUTER

    def run(self, ctx: ToolContext, action: str, title: str | None = None, target: str | None = None,
            new_tab: bool | None = None) -> ToolResult:
        from alice.tools.computer import as_bool

        title = str(title).strip() if title is not None else None
        target = str(target).strip() if target is not None else None
        new_tab = None if new_tab is None else as_bool(new_tab, "new_tab")
        if action == "list":
            return ToolResult("\n".join(list_windows()) or "pencere yok")
        if action == "find":
            if not target:
                raise ToolError("find için target gerekli")
            hits = find_installed_apps(target)
            return ToolResult("\n".join(hits) if hits else f"'{target}' kurulu programlarda bulunamadı "
                              "(Başlat menüsü, kayıt defteri, Program Files, Steam tarandı).")
        if action == "open":
            if not target:
                raise ToolError("open için target gerekli")
            msg = open_app(target)
            if "://" not in target:  # uygulama penceresinin gelmesini bekle; site açmak beklemeye değmez
                time.sleep(1.5)
            return ToolResult(msg)
        if action == "navigate":
            if not target:
                raise ToolError("navigate için target gerekli")
            return ToolResult(navigate_tab(target, title, new_tab))
        if action == "switch_tab":
            if not target:
                raise ToolError("switch_tab için target (sekme başlığından bir parça) gerekli")
            found = switch_tab(target, title)
            return ToolResult(f"sekmeye geçildi: {found}" if found else
                              f"başlığında '{target}' geçen açık sekme yok; navigate ile açabilirsin")
        if not title:
            raise ToolError(f"{action} için title gerekli")
        if action == "focus":
            return ToolResult(f"öne alındı: {focus_window(title)}")
        return ToolResult(f"{action}: {window_op(title, action)}")


class UIElements(Tool):
    name = "ui_elements"
    description = ("Windows: öndeki (ya da title verilen) masaüstü penceresinin düğme, alan ve menü gibi öğelerini "
                   "[ID] ile listeler (UI Automation); sonra ui_act ile ID üzerinden tıkla/yaz. Koordinat tahmini "
                   "yerine bunu kullan. Liste her çağrıda yenilenir. Web sayfaları için browser daha iyi; liste boş "
                   "ya da işe yaramazsa (oyunlar, Blender) computer_task kullan.")
    parameters = obj({
        "title": {"type": "string", "description": "Pencere başlığından bir parça. Boş: öndeki pencere."},
        "limit": {"type": "integer", "description": "En fazla öğe. Varsayılan 150."},
    })

    def run(self, ctx: ToolContext, title: str | None = None, limit: int = 150) -> ToolResult:
        from alice.tools.computer import as_number

        limit = max(1, min(as_number(150 if limit is None else limit, "limit", int), 500))
        t, items = collect_ui_elements(ctx, title, limit)
        return ToolResult(format_ui(t, items))


class UIAct(Tool):
    name = "ui_act"
    description = ("Son ui_elements listesindeki bir öğede işlem yapar (tıkla, yaz, aç/kapa). ID'ler sadece son "
                   "listede geçerli; pencere değiştiyse önce ui_elements'i yeniden çağır.")
    parameters = obj({
        "element_id": {"type": "integer", "description": "ui_elements çıktısındaki [ID]."},
        "action": {"type": "string", "enum": ["click", "double_click", "right_click", "focus", "set_text", "type",
                                              "toggle", "expand", "collapse", "select"],
                   "description": "set_text: alanın içeriğini text ile değiştir | type: imleç konumuna text'i yaz | "
                   "toggle: onay kutusu | expand/collapse: ağaç/menü | select: liste öğesini seç."},
        "text": {"type": "string", "description": "set_text ve type için metin."},
    }, ["element_id", "action"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, element_id: int, action: str, text: str | None = None) -> ToolResult:
        return ToolResult(ui_act(ctx, element_id, action, text))


class Clipboard(Tool):
    name = "clipboard"
    description = ("Panodaki metni okur (get) ya da panoya metin koyar (set). 'Kopyaladığım şey', 'panodaki' "
                   "denince get; kullanıcının yapıştıracağı bir şey hazırlamak için set.")
    parameters = obj({
        "action": {"type": "string", "enum": ["get", "set"], "description": "get: oku | set: text'i panoya koy."},
        "text": {"type": "string", "description": "set için metin."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "get" else COMPUTER

    def run(self, ctx: ToolContext, action: str, text: str | None = None) -> ToolResult:
        try:
            import pyperclip
        except ImportError as e:
            raise ToolError("pyperclip kurulu değil") from e
        if action == "get":
            return ToolResult(pyperclip.paste() or "(pano boş)")
        if action != "set":
            raise ToolError(f"bilinmeyen action: {action}. Geçerli: get, set")
        if text is None:  # eksik text panoyu sessizce boşaltıyordu (kullanıcının kopyaladığı şey gidiyordu)
            raise ToolError("set için text gerekli")
        pyperclip.copy(str(text))
        return ToolResult("panoya kopyalandı")


TOOLS = [Windows(), UIElements(), UIAct(), Clipboard()]
