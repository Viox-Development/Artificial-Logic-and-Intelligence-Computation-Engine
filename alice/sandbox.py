"""Komut sandbox'ı: run_command'ı izole çalıştır. Komut her yeri okuyabilir ama sadece çalıştığı klasöre (proje) ve
geçici klasöre yazabilir; böylece yanlış ya da kötü niyetli bir komut (ör. bir web sayfasının kandırdığı) ev
klasörünü, sistem dosyalarını ya da başka projeleri bozamaz.

Altyapı:
  * Windows: düşük bütünlük seviyesi (Low Integrity, Chrome/Edge sandbox'ının da kullandığı Windows mekanizması).
    Komut, bütünlük seviyesini düşüren küçük bir yardımcıdan (alice.sandbox_run) başlar; düşük seviyeli süreç normal
    dosyalara yazamaz. Proje klasörü bir kez "düşük seviye yazabilir" diye etiketlenir (icacls), geçici klasör
    LocalLow altındadır. Ağ açık kalır.
  * Linux: bubblewrap (bwrap): kök dizin salt okunur, proje klasörü yazılabilir, /tmp boş ve geçici; istenirse ağ kapalı.
  * macOS: sandbox-exec: proje ve geçici klasör dışına yazma yasak; istenirse ağ kapalı.

config.toml [agent]:
  sandbox = "auto"          # off | auto | on
  sandbox_network = true    # Linux/macOS: false = sandbox'ta ağ yok

  off : hiç kullanılmaz
  auto: dış içerik (web, e-posta...) okunan turda komutlar sorulmak yerine sandbox'ta çalışır (varsayılan)
  on  : her komut sandbox'ta çalışır

Model gerçekten sandbox dışı bir komut gerekiyorsa run_command'a unsandboxed=true verir; bu her zaman onay ister.
Sandbox kurulamıyorsa (bwrap yok) komut sandbox'sız çalıştırılmaz: auto modda normal onay akışına dönülür, on modda
hata verilir.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WIN = sys.platform == "win32"
MODES = ("off", "auto", "on")
_probe: dict[str, Any] = {}


def mode(ctx: Any) -> str:
    agent = getattr(getattr(ctx, "config", None), "agent", None)
    m = str(agent.get("sandbox", "auto")) if isinstance(agent, dict) else "auto"
    return m if m in MODES else "auto"


def network(ctx: Any) -> bool:
    agent = getattr(getattr(ctx, "config", None), "agent", None)
    return bool(agent.get("sandbox_network", True)) if isinstance(agent, dict) else True


def backend() -> tuple[str, str]:
    """(altyapı adı, sorun). Sorun boşsa kullanılabilir. Sonuç süreç boyunca hatırlanır."""
    if "result" in _probe:
        return _probe["result"]
    if IS_WIN:
        res = ("lowil", "")
    elif sys.platform == "darwin":
        res = ("sandbox-exec", "" if shutil.which("sandbox-exec") else "sandbox-exec bulunamadı")
    else:
        exe = shutil.which("bwrap")
        if not exe:
            res = ("bwrap", "bubblewrap (bwrap) kurulu değil: sudo apt install bubblewrap")
        else:
            try:
                r = subprocess.run([exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                                   capture_output=True, timeout=10)
                res = ("bwrap", "" if r.returncode == 0 else f"bwrap çalışmıyor: {r.stderr.decode()[:160]}")
            except (OSError, subprocess.SubprocessError) as e:
                res = ("bwrap", f"bwrap çalışmıyor: {e}")
    _probe["result"] = res
    return res


def boxable(folder: Path) -> str:
    """Bu klasör sandbox'ın yazılabilir alanı olabilir mi? Sorun yoksa "". Ev klasörü, sürücü kökü ya da sistem
    klasörü olamaz: bütün ev klasörünü yazılabilir yapmak sandbox'ı anlamsız kılar (Windows'ta da hepsini etiketlerdi)."""
    from alice.permissions import is_system_path

    try:
        p = Path(folder).resolve()
        home = Path.home().resolve()
    except OSError as e:
        return str(e)
    if p == Path(p.anchor) or p == home or p in home.parents or is_system_path(p):
        return (f"{p} sandbox alanı olamaz (ev klasörü, kök ya da sistem klasörü); bir proje klasöründe çalış "
                "ya da cwd ver")
    return ""


def _target(ctx: Any, args: dict[str, Any]) -> Path:
    cwd = args.get("cwd")
    try:
        return Path(ctx.resolve(cwd)) if cwd else Path(ctx.cwd)
    except Exception:  # noqa: BLE001
        return Path(getattr(ctx, "cwd", "."))


def applies(ctx: Any, args: dict[str, Any]) -> bool:
    """Bu run_command çağrısı sandbox'ta mı çalışacak?"""
    if args.get("unsandboxed"):
        return False
    m = mode(ctx)
    if m == "off":
        return False
    if m == "auto":
        from alice import guard

        if not guard.tainted(ctx):
            return False
    ok = not backend()[1] and not boxable(_target(ctx, args))
    return ok or m == "on"  # on modda kurulamıyorsa hata (run içinde), auto'da normal onay akışına düşer


def usable(ctx: Any, args: dict[str, Any]) -> bool:
    """Sandbox gerçekten kurulabiliyor ve bu çağrıya uygulanacak mı (izin kararı için)."""
    return applies(ctx, args) and not backend()[1] and not boxable(_target(ctx, args))


# ------------------------------------------------------------------------------------------- sarmalama
def wrap(argv: list[str], cwd: Path, ctx: Any) -> tuple[list[str], dict[str, str]]:
    """(yeni argv, ek ortam değişkenleri). Kurulamıyorsa RuntimeError."""
    name, problem = backend()
    problem = problem or boxable(cwd)
    if problem:
        raise RuntimeError(problem)
    cwd = Path(cwd).resolve()
    if name == "lowil":
        label_low(cwd)
        tmp = low_temp()
        return [sys.executable, "-m", "alice.sandbox_run", "--", *argv], {"TEMP": str(tmp), "TMP": str(tmp)}
    if name == "sandbox-exec":
        return ["sandbox-exec", "-p", mac_profile(cwd, network(ctx)), *argv], {}
    bw = shutil.which("bwrap") or "bwrap"
    out = [bw, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
           "--bind", str(cwd), str(cwd), "--chdir", str(cwd), "--die-with-parent", "--new-session"]
    if not network(ctx):
        out.append("--unshare-net")
    return out + argv, {}


def mac_profile(cwd: Path, net: bool) -> str:
    q = json.dumps(str(cwd))
    rules = ["(version 1)", "(allow default)", "(deny file-write*)",
             f"(allow file-write* (subpath {q}) (subpath \"/private/tmp\") (subpath \"/private/var/folders\") "
             "(literal \"/dev/null\") (literal \"/dev/tty\"))"]
    if not net:
        rules.append("(deny network*)")
    return "".join(rules)


# ------------------------------------------------------------------------------------------- Windows
def _labels_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "sandbox_dirs.json"


def label_low(folder: Path) -> None:
    """Klasörü düşük bütünlüklü süreçlerin yazabileceği şekilde bir kez etiketle (alt klasörler dahil)."""
    if not IS_WIN:
        return
    try:
        done = set(json.loads(_labels_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        done = set()
    key = str(folder).lower()
    if key in done:
        return
    r = subprocess.run(["icacls", str(folder), "/setintegritylevel", "(OI)(CI)low", "/T", "/C", "/Q"],
                       capture_output=True, text=True, timeout=300, creationflags=0x08000000)
    if r.returncode != 0:
        raise RuntimeError(f"proje klasörü sandbox için hazırlanamadı (icacls): {(r.stderr or r.stdout)[:200]}")
    done.add(key)
    try:
        _labels_path().parent.mkdir(parents=True, exist_ok=True)
        _labels_path().write_text(json.dumps(sorted(done)), encoding="utf-8")
    except OSError:
        pass


def low_temp() -> Path:
    base = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "LocalLow" / "ALICE-sandbox"
    base.mkdir(parents=True, exist_ok=True)
    return base


def lower_integrity() -> None:
    """Bu sürecin (ve bundan sonra başlatacağı çocukların) bütünlük seviyesini Low yap. Windows'ta bir süreç kendi
    seviyesini düşürebilir, yükseltemez. Başarısızsa OSError (komut sandbox'sız çalıştırılmasın)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

    class SID_AND_ATTRIBUTES(ctypes.Structure):  # noqa: N801 - Windows yapı adı
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):  # noqa: N801
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0080 | 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)):  # Low Mandatory Level
        raise OSError(ctypes.get_last_error(), "ConvertStringSidToSid")
    label = TOKEN_MANDATORY_LABEL(SID_AND_ATTRIBUTES(sid, 0x20))  # SE_GROUP_INTEGRITY
    size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
    if not advapi32.SetTokenInformation(token, 25, ctypes.byref(label), size):  # TokenIntegrityLevel
        raise OSError(ctypes.get_last_error(), "SetTokenInformation")
    kernel32.CloseHandle(token)
