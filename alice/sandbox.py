"""Command sandbox: runs run_command in isolation. The command can read everywhere but can only write to the folder
it runs in (the project) and the temp folder; so a wrong or malicious command (e.g. one tricked by a web page) cannot
break the home folder, system files or other projects.

Infrastructure:
  * Windows: Low Integrity level (the Windows mechanism that the Chrome/Edge sandbox also uses).
    The command starts from a small helper (alice.sandbox_run) that lowers the integrity level; a low-level process
    cannot write to normal files. The project folder is labeled once as "low can write" (icacls); the temp folder
    is under LocalLow. Network stays open.
  * Linux: bubblewrap (bwrap): root directory read-only, project folder writable, /tmp empty and temporary; network
    can be turned off.
  * macOS: sandbox-exec: writing outside the project and temp folder is forbidden; network can be turned off.

config.toml [agent]:
  sandbox = "auto"          # off | auto | on
  sandbox_network = true    # Linux/macOS: false = no network in the sandbox

  off : never used
  auto: when a turn reads external content (web, e-mail...), commands run in the sandbox instead of asking (default)
  on  : every command runs in the sandbox

If the model really needs a command outside the sandbox, it passes unsandboxed=true to run_command; this always
asks for approval.
If the sandbox cannot be set up (no bwrap), the command is not run without the sandbox: in auto mode it falls back to
the normal approval flow, in on mode an error is raised.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WIN = sys.platform == "win32"
# Ev klasöründe sandbox'ın yazılabilir alanı olamayacak kişisel klasörler (alt klasörleri dahil).
PERSONAL_DIRS = ("Downloads", "Desktop", "Documents", "Pictures", "Music", "Videos", "OneDrive", ".ssh", ".config")
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
    """(infrastructure name, problem). Usable if the problem is empty. The result is remembered for the process lifetime."""
    if "result" in _probe:
        return _probe["result"]
    if IS_WIN:
        res = ("lowil", "")
    elif sys.platform == "darwin":
        res = ("sandbox-exec", "" if shutil.which("sandbox-exec") else "sandbox-exec not found")
    else:
        exe = shutil.which("bwrap")
        if not exe:
            res = ("bwrap", "bubblewrap (bwrap) is not installed: sudo apt install bubblewrap")
        else:
            try:
                r = subprocess.run([exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                                   capture_output=True, timeout=10)
                res = ("bwrap", "" if r.returncode == 0 else f"bwrap does not work: {r.stderr.decode()[:160]}")
            except (OSError, subprocess.SubprocessError) as e:
                res = ("bwrap", f"bwrap does not work: {e}")
    _probe["result"] = res
    return res


def boxable(folder: Path) -> str:
    """Can this folder be the sandbox's writable area? If there is no problem, "". A home folder, drive root or
    system folder cannot be: making the whole home folder writable would defeat the sandbox (on Windows it would
    label all of it too)."""
    from alice.permissions import is_system_path

    try:
        p = Path(folder).resolve()
        home = Path.home().resolve()
    except OSError as e:
        return str(e)
    if p == Path(p.anchor) or p == home or p in home.parents or is_system_path(p):
        return ((f"{p} can't be a sandbox area (home folder, root or a system folder); work in a project folder "
                "or give cwd"))
    # Kişisel klasörler de olamaz: Windows'ta etiketlenen klasöre düşük seviyeli bir komut dosya bırakabilir.
    # Downloads etiketlenince oradan açılan kurulum dosyaları da düşük yetkiyle çalışıyordu.
    from alice.config import alice_home

    personal = [home / n for n in PERSONAL_DIRS] + [alice_home()]
    for d in personal:
        try:
            d = d.resolve()
        except OSError:
            continue
        if p == d or d in p.parents:
            return (f"{p} can't be a sandbox area (personal folder: {d.name}); work in a project folder or give cwd")
    return ""


def _target(ctx: Any, args: dict[str, Any]) -> Path:
    cwd = args.get("cwd")
    try:
        return Path(ctx.resolve(cwd)) if cwd else Path(ctx.cwd)
    except Exception:  # noqa: BLE001
        return Path(getattr(ctx, "cwd", "."))


def applies(ctx: Any, args: dict[str, Any]) -> bool:
    """Will this run_command call run in the sandbox?"""
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
    return ok or m == "on"  # on: error if it cannot be set up (raised in run); auto: falls back to the normal approval flow


def usable(ctx: Any, args: dict[str, Any]) -> bool:
    """Can the sandbox actually be set up, and does it apply to this call (for the permission decision)?"""
    return applies(ctx, args) and not backend()[1] and not boxable(_target(ctx, args))


# ------------------------------------------------------------------------------------------- wrapping
def wrap(argv: list[str], cwd: Path, ctx: Any) -> tuple[list[str], dict[str, str]]:
    """(new argv, extra environment variables). RuntimeError if it cannot be set up."""
    name, problem = backend()
    problem = problem or boxable(cwd)
    if problem:
        raise RuntimeError(problem)
    cwd = Path(cwd).resolve()
    if name == "lowil":
        repair_old_labels()
        label_low(cwd)
        hide_secrets()
        tmp = low_temp()
        return [sys.executable, "-m", "alice.sandbox_run", "--", *argv], {"TEMP": str(tmp), "TMP": str(tmp)}
    if name == "sandbox-exec":
        return ["sandbox-exec", "-p", mac_profile(cwd, network(ctx), secret_paths()), *argv], {}
    bw = shutil.which("bwrap") or "bwrap"
    out = [bw, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
           "--bind", str(cwd), str(cwd), "--chdir", str(cwd), "--die-with-parent", "--new-session"]
    for p in secret_paths():  # empty file/folder in place of secret files: they cannot be read
        out += ["--tmpfs", str(p)] if p.is_dir() else ["--ro-bind", "/dev/null", str(p)]
    if not network(ctx):
        out.append("--unshare-net")
    return out + argv, {}


def mac_profile(cwd: Path, net: bool, secrets: list[Path] | None = None) -> str:
    q = json.dumps(str(cwd))
    rules = ["(version 1)", "(allow default)", "(deny file-write*)",
             f"(allow file-write* (subpath {q}) (subpath \"/private/tmp\") (subpath \"/private/var/folders\") "
             "(literal \"/dev/null\") (literal \"/dev/tty\"))"]
    if secrets:
        rules.append("(deny file-read* " + " ".join(f"(subpath {json.dumps(str(p))})" for p in secrets) + ")")
    if not net:
        rules.append("(deny network*)")
    return "".join(rules)


# ------------------------------------------------------------------------------------------- secrets
# Key files under ~/.alice. The sandbox can read everywhere and (on Windows) has the network open: a tricked command
# could read API keys, subscription sessions or local service keys and send them out.
SECRET_NAMES = (".env", ".env.bak", "chatgpt-auth.json", "connect.json", "phone-token", "relay.json",
                "browser-bridge-token", "browser-ext-id", "cliproxy/keys.txt", "cliproxy/config.yaml", "cliproxy/auths",
                "cliproxy/auths-disabled")
# Secret-valued environment variables are not given to the sandboxed process (ALICE loads .env into its env).
_SECRET_ENV_RX = re.compile(r"(?i)(?:_API_?KEY|_KEY|_TOKEN|_SECRET|_PASSWORD|_PASSWD|_PASS|_PAT|_CREDENTIALS?)$|"
                            r"^(?:CLIPROXY_|GH_TOKEN$|GITHUB_TOKEN$|ALICE_.*(?:TOKEN|KEY|SECRET))")


def secret_paths() -> list[Path]:
    from alice.config import alice_home

    home = alice_home()
    return [p for p in (home / n for n in SECRET_NAMES) if p.exists()]


def hide_secrets() -> None:
    """Hide secret files from the sandbox. Redone on every sandbox command: files like .env are written as a temp
    file + rename, which drops the label."""
    for p in secret_paths():
        try:
            hide_from_sandbox(p)
        except Exception:  # noqa: BLE001 - if the label cannot be set, the command still runs
            pass


def scrub_env(env: dict[str, str], ctx: Any = None) -> dict[str, str]:
    """Remove keys from the environment passed to the sandboxed process: those loaded from .env and those whose
    names look like keys. Ones that are explicitly needed can be kept with [agent] sandbox_env_allow = ["AD", ...]."""
    from alice import config as cfg

    agent = getattr(getattr(ctx, "config", None), "agent", None)
    allow = {str(k).upper() for k in ((agent.get("sandbox_env_allow") or []) if isinstance(agent, dict) else [])}
    loaded = {k.upper() for k in cfg.LOADED_ENV_KEYS} | {k.upper() for k in cfg.read_env()}
    return {k: v for k, v in env.items()
            if k.upper() in allow or not (k.upper() in loaded or _SECRET_ENV_RX.search(k))}


# ------------------------------------------------------------------------------------------- Windows
def _labels_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "sandbox_dirs.json"


_EXE_FIXED: set[str] = set()


def _restore_exes(folder: Path) -> None:
    """Klasördeki .exe'leri normal (Medium) seviyeye döndür. Windows düşük etiketli bir exe'yi düşük bütünlükle
    başlatır: proje klasörü etiketlenince .venv/Scripts/python.exe de etiketleniyor, ALICE'in kendisi düşük
    yetkiyle açılıp ~/.alice'e ve Temp'e yazamıyordu ("attempt to write a readonly database"). Hata olursa sessiz
    geçilir: etiket sandbox'ın çalışmasını engellemez."""
    try:
        subprocess.run(["icacls", str(Path(folder) / "*.exe"), "/setintegritylevel", "M", "/T", "/C", "/Q"],
                       capture_output=True, text=True, timeout=300, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        pass  # icacls missing or timed out: the sandbox still works, only the executables keep the low label


def label_low(folder: Path) -> None:
    """Label the folder once so low-integrity processes can write to it (subfolders included)."""
    if not IS_WIN:
        return
    try:
        done = set(json.loads(_labels_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        done = set()
    key = str(folder).lower()
    if key in done:
        if key not in _EXE_FIXED:  # eski sürümün etiketlediği klasörde exe'ler hâlâ düşük olabilir
            _restore_exes(folder)
            _EXE_FIXED.add(key)
        return
    r = subprocess.run(["icacls", str(folder), "/setintegritylevel", "(OI)(CI)low", "/T", "/C", "/Q"],
                       capture_output=True, text=True, timeout=300, creationflags=0x08000000)
    if r.returncode != 0:
        raise RuntimeError(f"could not prepare the project folder for the sandbox (icacls): {(r.stderr or r.stdout)[:200]}")
    _restore_exes(folder)
    _EXE_FIXED.add(key)
    done.add(key)
    try:
        _labels_path().parent.mkdir(parents=True, exist_ok=True)
        _labels_path().write_text(json.dumps(sorted(done)), encoding="utf-8")
    except OSError:
        pass  # the label is set again next start


_REPAIRED: list[bool] = []


def repair_old_labels() -> list[str]:
    """Undo low-integrity labels that old versions put on folders the sandbox no longer accepts (Windows only).

    Before #201 personal folders such as Downloads could become the sandbox's writable area and were labeled
    "(OI)(CI)low". New versions never label them, but the label stayed, so a low-integrity process could still write
    there. Every folder in sandbox_dirs.json that boxable() now rejects (personal folders, home, ALICE home) is set
    back to medium and dropped from the list; project folders are left as they are. A folder whose reset fails stays
    in the list and is retried next time. Runs once per process. Returns the folders that were reset."""
    if not IS_WIN or _REPAIRED:
        return []
    _REPAIRED.append(True)
    path = _labels_path()
    try:
        done = list(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
    keep, fixed = [], []
    for key in done:
        if not boxable(Path(key)):
            keep.append(key)
            continue
        try:
            r = subprocess.run(["icacls", key, "/setintegritylevel", "(OI)(CI)M", "/T", "/C", "/Q"],
                               capture_output=True, text=True, timeout=300, creationflags=0x08000000)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        (fixed if ok else keep).append(key)
    if fixed:
        try:
            path.write_text(json.dumps(sorted(keep)), encoding="utf-8")
        except OSError:
            pass  # the labels are already reset; the list is cleaned up next time
    return fixed


def hide_from_sandbox(path: Path) -> bool:
    """Label a secret file (phone key, relay key) so low-integrity processes CANNOT read it. The Low level can read
    every file by default: a tricked sandbox command could read the phone key and, through the phone server on
    127.0.0.1, control the computer with full authority (sandbox escape). Label: Medium, no-read-up + no-write-up.
    On a folder the label also covers the files inside it (and ones created later). Outside Windows, or on failure,
    returns False (the file still works)."""
    if not IS_WIN:
        return False
    import ctypes
    import ctypes.wintypes as wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    advapi32.GetSecurityDescriptorSacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                   ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    sd = ctypes.c_void_p()
    sddl = "S:(ML;OICI;NWNR;;;ME)" if Path(path).is_dir() else "S:(ML;;NWNR;;;ME)"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
        return False
    try:
        present, defaulted, sacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorSacl(sd, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)):
            return False
        # SE_FILE_OBJECT = 1, LABEL_SECURITY_INFORMATION = 0x10
        return advapi32.SetNamedSecurityInfoW(str(path), 1, 0x10, None, None, None, sacl) == 0
    finally:
        kernel32.LocalFree(sd)


def low_temp() -> Path:
    base = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "LocalLow" / "ALICE-sandbox"
    base.mkdir(parents=True, exist_ok=True)
    return base


def lower_integrity() -> None:
    """Set this process's (and the children it starts from now on) integrity level to Low. On Windows a process can
    lower its own level but cannot raise it. On failure, OSError (so the command is not run without the sandbox)."""
    import ctypes
    import ctypes.wintypes as wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

    class SID_AND_ATTRIBUTES(ctypes.Structure):  # noqa: N801 - Windows structure name
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
