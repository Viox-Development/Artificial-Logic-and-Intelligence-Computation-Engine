"""ALICE that writes its own tool: when there is no tool for a job, it writes the tool and its test itself.

Flow (tool: make_tool, alice/tools/selftool.py):
  1. test: the model gives the plugin, its test and its manifest (what it will access). The code is checked against
     the manifest (network, folder or app access not in the manifest is rejected); the test runs in the test sandbox
     (on Windows low integrity level: it can only write to the temp folder) with a time limit.
  2. install: the user is shown in one sentence what it does and what it will access; approval is ALWAYS asked
     (DANGER in the permission system). Without approval no file goes into the plugin folder.
  3. After approval the tool lands under ~/.alice/plugins/ and is used right away in the same conversation.

Every tool it writes is listed in ~/.alice/selftools.json: when it was written, why, how many times it was used, and
the last error. A tool that errors three times switches itself off. Tools that change code, spend money, send messages
or delete (asks_each_time in the manifest) ask for approval on every use.

Limit: the manifest check is static (done by reading the code); network and file access at runtime is not restricted
at the OS level. That is why the approval screen is kept short and clear.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from alice.config import alice_home

NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
MAX_ERRORS = 3
TEST_TIMEOUT = 90.0
NET_MODULES = {"httpx", "requests", "urllib", "urllib3", "socket", "http", "aiohttp", "websockets", "ftplib", "smtplib"}
EXEC_CALLS = {"subprocess", "os.system", "os.popen", "os.startfile", "os.spawnl", "os.execv"}
BANNED_MODULES = {"ctypes", "winreg", "importlib", "marshal", "pickle"}
BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
WRITE_ATTRS = {"write_text", "write_bytes", "unlink", "rmdir", "rename", "replace", "mkdir", "touch"}
WRITE_CALLS = {"os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace", "os.makedirs", "os.mkdir",
               "shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copyfile", "shutil.copytree"}

_lock = threading.Lock()
_PENDING: dict[str, dict[str, Any]] = {}  # tools that passed the test and await approval (for the process lifetime)


# ---------------------------------------------------------------------- registry
def registry_path() -> Path:
    return alice_home() / "selftools.json"


def load_registry() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_registry(reg: dict[str, dict[str, Any]]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _update(name: str, **kw: Any) -> dict[str, Any] | None:
    with _lock:
        reg = load_registry()
        if name not in reg:
            return None
        reg[name].update(kw)
        save_registry(reg)
        return reg[name]


# ------------------------------------------------------------------- manifest
def normalize_manifest(m: Any) -> dict[str, Any]:
    m = m if isinstance(m, dict) else {}

    def lst(k: str) -> list[str]:
        v = m.get(k) or []
        return [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]

    return {"network": lst("network"), "folders": lst("folders"), "apps": lst("apps"),
            "asks_each_time": bool(m.get("asks_each_time"))}


def describe_access(m: dict[str, Any]) -> str:
    parts = []
    if m["network"]:
        parts.append("internet: " + ", ".join(m["network"]))
    if m["folders"]:
        parts.append("klasör: " + ", ".join(m["folders"]))
    if m["apps"]:
        parts.append("uygulama: " + ", ".join(m["apps"]))
    out = "; ".join(parts) or "hiçbir şeye erişmez (sadece hesap yapar)"
    return out + ("; her kullanımda ayrıca onay ister" if m["asks_each_time"] else "")


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _host(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/:?#]+)", url, re.I)
    return m.group(1).lower() if m else ""


def _norm_path(path: str) -> str:
    """For comparison: ~ is expanded, backslashes and forward slashes (including doubled ones) become a single "/",
    and case is ignored. A folder written with doubled backslashes in the code did not match the same folder in the
    manifest (Steam tool trial)."""
    return re.sub(r"[\\/]+", "/", os.path.expanduser(path)).rstrip("/").lower()


def _under(path: str, folders: list[str]) -> bool:
    p = _norm_path(path)
    return any(p == (f := _norm_path(x)) or p.startswith(f + "/") for x in folders)


def check_code(code: str, name: str, manifest: dict[str, Any]) -> list[str]:
    """Check the code against the manifest. A list of problems; empty means it passed."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"sözdizimi hatası: satır {e.lineno}: {e.msg}"]
    problems: list[str] = []
    imports: set[str] = set()
    writes = execs = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = _dotted(node.func)
            if fn in BANNED_CALLS:
                problems.append(f"{fn}() kullanılamaz")
            if fn.split(".")[0] == "subprocess" or fn in EXEC_CALLS:
                execs = True
            if fn in WRITE_CALLS or (isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_ATTRS):
                writes = True
            if fn == "open" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and \
                    any(c in str(node.args[1].value) for c in "wax+"):
                writes = True
            for kw in node.keywords:
                if fn == "open" and kw.arg == "mode" and isinstance(kw.value, ast.Constant) and \
                        any(c in str(kw.value.value) for c in "wax+"):
                    writes = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            host = _host(s)
            if host and not any(host == h.lower() or host.endswith("." + h.lower()) for h in manifest["network"]):
                problems.append(f"bildirgede olmayan adres: {host}")
            if re.match(r"^(?:[a-zA-Z]:[\\/]|~[\\/])", s) and not _under(s, manifest["folders"]):
                problems.append(f"bildirgede olmayan klasör: {s}")
        elif isinstance(node, ast.ClassDef):
            for st in node.body:
                if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "name" for t in st.targets) \
                        and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                    if not st.value.value.startswith(f"{name}_") and st.value.value != name:
                        problems.append(f"araç adı '{st.value.value}' '{name}_' ile başlamalı (çakışma olmasın)")
    for mod in sorted(imports & BANNED_MODULES):
        problems.append(f"{mod} modülü kullanılamaz")
    if imports & NET_MODULES and not manifest["network"]:
        problems.append("kod internete çıkıyor ama bildirgede network boş")
    if (execs or "subprocess" in imports) and not manifest["apps"]:
        problems.append("kod program çalıştırıyor ama bildirgede apps boş")
    if writes and not manifest["folders"]:
        problems.append("kod dosya yazıyor/siliyor ama bildirgede folders boş")
    if not re.search(r"(?m)^(PLUGIN|PLUGINS)\s*=|^def register\(", code):
        problems.append("PLUGIN = Plugin(...) tanımı yok")
    return list(dict.fromkeys(problems))


# ---------------------------------------------------------------------- trial
def staging_dir(name: str) -> Path:
    from alice import sandbox

    base = sandbox.low_temp() if sandbox.IS_WIN else Path(alice_home()) / "selftools-staging"
    d = base / "selftools" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_text(name: str, code: str, manifest: dict[str, Any], need: str) -> str:
    # The "from __future__" lines must be at the top of the file (right after the docstring): when they came after
    # the header, the written tool raised SyntaxError (Steam tool trial, 2026-11-03).
    lines = code.strip().splitlines()
    future = [ln for ln in lines if ln.strip().startswith("from __future__ import")]
    body = "\n".join(ln for ln in lines if ln not in future)
    head = (f'"""ALICE\'in kendi yazdığı araç: {name}. İhtiyaç: {need.strip()[:200]}\n'
            f'Yazıldığı an: {time.strftime("%Y-%m-%d %H:%M")}. Kayıt: ~/.alice/selftools.json"""\n\n'
            + ("\n".join(future) + "\n\n" if future else "")
            + f"MANIFEST = {manifest!r}\n\n")
    return head + body.strip() + "\n"


def run_tests(name: str, source: str, test: str, ctx: Any, timeout: float = TEST_TIMEOUT) -> tuple[bool, str]:
    """Write the plugin and its test to a temp folder and run the test in the test sandbox. (passed?, output)."""
    from alice import sandbox

    d = staging_dir(name)
    for old in d.glob("*.py"):
        old.unlink()
    (d / f"{name}.py").write_text(source, encoding="utf-8")
    (d / f"test_{name}.py").write_text(test, encoding="utf-8")
    # The temp folder sits inside the test's own folder and its real path is resolved: when the user folder appeared
    # under two names (kresu -> Hayat), pytest's tmp_path crashed with "normalized and relative path değil".
    basetemp = (d / "tmp").resolve()
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={basetemp}",
            f"test_{name}.py"]
    try:
        argv, env_extra = sandbox.wrap(argv, d, ctx)
    except RuntimeError as e:
        return False, f"kum havuzu kurulamadı, test çalıştırılmadı: {e}"
    root = str(Path(__file__).resolve().parent.parent)
    env = {**sandbox.scrub_env(dict(os.environ), ctx), **env_extra, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join([str(d), root, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    try:
        r = subprocess.run(argv, cwd=str(d), env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
    except subprocess.TimeoutExpired:
        return False, f"test {timeout:.0f} sn'de bitmedi"
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out[-3000:]


def prepare(name: str, need: str, description: str, manifest: Any, code: str, test: str, ctx: Any,
            existing: set[str] | None = None) -> tuple[bool, str]:
    """Step 1: check, try in the test sandbox; if it passes, put it in the awaiting-approval list."""
    name = (name or "").strip().lower()
    if not NAME.match(name):
        return False, "ad küçük harf, rakam ve _ olmalı, harfle başlamalı (ör. steam_recent)"
    if existing and (name in existing or any(t.startswith(f"{name}_") for t in existing)):
        return False, f"'{name}' adında bir araç ya da eklenti zaten var: önce onu kullan"
    if not description.strip() or not test.strip():
        return False, "description (ne yaptığı, tek cümle) ve test zorunlu"
    m = normalize_manifest(manifest)
    problems = check_code(code, name, m)
    if problems:
        return False, "Bildirge denetimi geçmedi, araç yazılmadı:\n- " + "\n- ".join(problems)
    source = file_text(name, code, m, need)
    ok, out = run_tests(name, source, test, ctx)
    if not ok:
        return False, f"Test kum havuzunda geçmedi, araç yazılmadı:\n{out}"
    _PENDING[name] = {"name": name, "need": need.strip(), "description": description.strip(), "manifest": m,
                      "source": source, "test": test, "at": time.time()}
    return True, (f"Test kum havuzunda geçti.\n{out.splitlines()[-1] if out else ''}\n"
                  f"Kullanıcıya bir cümleyle sor: \"{approval_text(name)}\" Onaylarsa make_tool action=install "
                  f"name={name}.")


def approval_text(name: str) -> str:
    p = _PENDING.get(name)
    if p is None:
        return f"{name}: onay bekleyen araç yok (önce action=test)"
    return f"'{name}' aracını ekleyeyim mi? {p['description']} Erişim: {describe_access(p['manifest'])}."


def install(name: str) -> tuple[Path, list[Any]]:
    """Step 2 (after approval): write to the plugin folder, add to the registry, load. (file, tools)."""
    from alice import plugins

    p = _PENDING.pop(name, None)
    if p is None:
        raise ValueError("onay bekleyen böyle bir araç yok: önce action=test")
    d = plugins.plugin_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.py"
    if path.exists():
        raise ValueError(f"{path.name} zaten var; ALICE başkasının eklentisinin üstüne yazmaz")
    path.write_text(p["source"], encoding="utf-8")
    (d / "_tests").mkdir(exist_ok=True)
    (d / "_tests" / f"test_{name}.py").write_text(p["test"], encoding="utf-8")
    with _lock:
        reg = load_registry()
        reg[name] = {"created": time.strftime("%Y-%m-%d %H:%M"), "need": p["need"], "description": p["description"],
                     "manifest": p["manifest"], "uses": 0, "errors": 0, "last_error": "", "last_used": "",
                     "disabled": False, "file": str(path)}
        save_registry(reg)
    try:
        loaded = plugins._load_file(path)
    except Exception:
        path.unlink(missing_ok=True)
        with _lock:
            reg = load_registry()
            reg.pop(name, None)
            save_registry(reg)
        raise
    tools = [t for pl in instrument(loaded) for t in pl.tools]
    return path, tools


# ------------------------------------------------------------------ monitoring
def instrument(loaded: list[Any]) -> list[Any]:
    """Mark the plugins it wrote: drop the disabled ones, count uses and errors, and ask for approval on every use
    if needed. plugins.load_plugins calls this on every load."""
    reg = load_registry()
    out = []
    for pl in loaded:
        info = reg.get(pl.name)
        if info is None:
            out.append(pl)
            continue
        if info.get("disabled"):
            continue
        for t in pl.tools:
            _wrap(pl.name, t, bool((info.get("manifest") or {}).get("asks_each_time")))
        out.append(pl)
    return out


def _wrap(plugin: str, tool: Any, ask: bool) -> None:
    from alice.tools.base import DANGER, ToolError

    if getattr(tool, "_selftool", False):
        return
    if ask:
        tool.risk = DANGER
        tool.risk_for = lambda args: DANGER  # type: ignore[method-assign]
    orig = tool.run

    def run(ctx, **kw):
        # Disabling (3 errors) and make_tool remove only took effect on the next load: the tool stayed in the list
        # and kept running in the same session. The registry is checked on every call.
        if (load_registry().get(plugin) or {}).get("disabled"):
            raise ToolError(f"{plugin} aracı kapatıldı ya da kaldırıldı (make_tool action=list); yeniden yazmadan "
                            "kullanılamaz")
        try:
            res = orig(ctx, **kw)
        except ToolError:
            raise  # misuse: not counted as the tool's error
        except Exception as e:
            record(plugin, error=f"{type(e).__name__}: {e}")
            raise
        if getattr(res, "is_error", False):
            record(plugin, error=str(getattr(res, "text", ""))[:200])
        else:
            record(plugin)
        return res

    tool.run = run
    tool._selftool = True


def record(name: str, error: str = "") -> None:
    """Record a use or an error; after MAX_ERRORS errors the tool is switched off (the night loop suggests a fix)."""
    with _lock:
        reg = load_registry()
        info = reg.get(name)
        if info is None:
            return
        info["last_used"] = time.strftime("%Y-%m-%d %H:%M")
        if error:
            info["errors"] = int(info.get("errors", 0)) + 1
            info["last_error"] = error[:300]
            if info["errors"] >= MAX_ERRORS:
                info["disabled"] = True
        else:
            info["uses"] = int(info.get("uses", 0)) + 1
        save_registry(reg)


def listing() -> str:
    reg = load_registry()
    if not reg:
        return "Henüz kendi yazdığım bir araç yok."
    lines = []
    for name, i in sorted(reg.items(), key=lambda kv: kv[1].get("created", "")):
        state = "kapalı (3 hata)" if i.get("disabled") else "açık"
        err = f", son hata: {i['last_error'][:80]}" if i.get("last_error") else ""
        lines.append(f"- {name} ({i.get('created', '?')}, {state}): {i.get('description', '')} "
                     f"Kullanım {i.get('uses', 0)}, hata {i.get('errors', 0)}{err}. "
                     f"Erişim: {describe_access(normalize_manifest(i.get('manifest')))}.")
    return "\n".join(lines)


def remove(name: str) -> str:
    """Does not delete the plugin: moves it under plugins/_kaldirilan/ (it can be restored if wanted)."""
    from alice import plugins

    reg = load_registry()
    if name not in reg:
        return f"{name}: kendi yazdığım araçlar arasında yok"
    src = plugins.plugin_dir() / f"{name}.py"
    if src.exists():
        dst = plugins.plugin_dir() / "_kaldirilan"
        dst.mkdir(exist_ok=True)
        src.replace(dst / f"{name}-{time.strftime('%Y%m%d%H%M%S')}.py")
    _update(name, disabled=True, removed=time.strftime("%Y-%m-%d %H:%M"))
    return f"{name} kaldırıldı (dosya _kaldirilan klasöründe)"
