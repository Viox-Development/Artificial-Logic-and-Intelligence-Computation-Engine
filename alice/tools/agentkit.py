"""Tools that exist in Claude Code but are missing from ALICE.

- notify: notifies the user (desktop/HUD card, follows the mode policy). PushNotification in Claude Code.
- enter_plan_mode: before starting a big task, ALICE puts itself into plan mode (read-only; it presents the plan for
  approval with exit_plan_mode). EnterPlanMode in Claude Code.
- mcp_resources: lists/reads the resources of connected MCP servers. ListMcpResources + ReadMcpResource.
- code_intel: code understanding: error/warning diagnostics (ruff/pyright, tsc/node, go vet, cargo, JSON/TOML), a
  file's symbols, a symbol's definition and where it is used. The counterpart of Claude Code's LSP tool that works
  without installing a language server.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from alice.tools.base import EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(argv: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=_FLAGS)
    except FileNotFoundError:
        return 127, f"{argv[0]} bulunamadı"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} {timeout} sn içinde bitmedi"
    return r.returncode, (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()


# ---------------------------------------------------------------------------------------------------- notify
class Notify(Tool):
    name = "notify"
    description = ("Sends the user a notification (a desktop notification or ALICE's on-screen card; in modes such as "
                   "game/flight it may be silenced by the mode rule and is logged).\n"
                   "- Use it when a long job finishes or something needs the user back (a step awaiting approval, red "
                   "CI, a finished download) and the user is probably busy with something else.\n"
                   "- Don't use it if the user is already chatting with you; your answer is enough. Don't notify at "
                   "every step.")
    parameters = obj({
        "title": {"type": "string", "description": "Short title, e.g. 'Tests finished'."},
        "body": {"type": "string", "description": "One or two sentences: what happened, what is expected from the "
                                                  "user."},
        "severity": {"type": "string", "enum": ["info", "warn", "critical"], "description": "Importance; default "
                                                                                            "info."},
    }, ["title"])
    risk = READ
    parallel_safe = True

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("title", ""))[:80]

    def run(self, ctx: ToolContext, title: str, body: str = "", severity: str = "info") -> ToolResult:
        from alice.core import notify as nt

        n = nt.Notification(title.strip()[:120], body.strip()[:500], severity if severity in ("info", "warn",
                            "critical") else "info", "task", kind="task")
        hub = nt.ACTIVE
        if hub is not None:
            ok = hub.send(n)
            return ToolResult("bildirim gönderildi" if ok else "şu anki mod bildirimi susturdu (kaydedildi; kullanıcı "
                              "sonra 'ben yokken ne oldu' diye görebilir)")
        if nt.desktop_notify(n):
            return ToolResult("bildirim gönderildi (masaüstü)")
        raise ToolError("bildirim gönderilemedi: çekirdek çalışmıyor ve masaüstü bildirimi başarısız")


# ---------------------------------------------------------------------------------------------------- plan mode
class EnterPlanMode(Tool):
    name = "enter_plan_mode"
    description = ("Puts you in plan mode: writing files and running commands are turned off and you only read and "
                   "research; when the plan is ready, present it for the user's approval with exit_plan_mode.\n"
                   "- Use it BEFORE starting a big job that will touch many files, needs an architectural decision or "
                   "has several reasonable ways: first study the code base, then present the plan.\n"
                   "- Don't use it for small, clear jobs (a one-file fix, an obvious bug), questions or research.")
    parameters = obj({"reason": {"type": "string", "description": "Why a plan is needed (one sentence)."}})
    risk = READ

    def run(self, ctx: ToolContext, reason: str = "") -> ToolResult:
        agent = ctx.state.get("agent")
        perms = getattr(agent, "permissions", None)
        if perms is None or getattr(agent, "is_child", False):
            raise ToolError("plan modu bu bağlamda kullanılamıyor")
        if getattr(perms, "mode", "") == "plan":
            return ToolResult("zaten plan modundasın: incele, sonra exit_plan_mode ile planı sun")
        perms.mode = "plan"
        return ToolResult("Plan moduna geçildi. Şimdi yalnız oku ve araştır (dosya yazma/komut kapalı). Plan hazır "
                          "olunca exit_plan_mode ile sun; değişen dosyaları, sırayı ve nasıl doğrulayacağını yaz."
                          + (f" Gerekçe: {reason}" if reason else ""))


# ---------------------------------------------------------------------------------------------------- MCP resources
class McpResources(Tool):
    name = "mcp_resources"
    untrusted = True  # resource content comes from outside
    description = ("Lists and reads the resources (files, documents, schemas, records) of connected MCP servers.\n"
                   "- list: the resources of all servers (or of one if server is given): uri, name, type.\n"
                   "- read: a resource's content by uri. If there are several servers, pass server.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "read"], "description": "list: list resources | read: read by "
                                                                              "uri."},
        "server": {"type": "string", "description": "Name of the MCP server (as in the config)."},
        "uri": {"type": "string", "description": "read: the resource's uri (from the list output)."},
    }, ["action"])
    risk = READ
    parallel_safe = True

    def run(self, ctx: ToolContext, action: str, server: str = "", uri: str = "") -> ToolResult:
        from alice.mcp import MCPError

        servers = [s for s in ctx.state.get("mcp_servers") or [] if not server or s.name == server]
        if not servers:
            raise ToolError("bağlı MCP sunucusu yok" + (f": {server}" if server else ""))
        if action == "list":
            out = []
            for s in servers:
                try:
                    items = s.list_resources()
                except MCPError as e:
                    out.append(f"## {s.name}: kaynak listelenemedi ({e})")
                    continue
                out.append(f"## {s.name} ({len(items)})")
                out += [f"- {r.get('uri')} — {r.get('name') or ''}"
                        + (f" [{r['mimeType']}]" if r.get("mimeType") else "")
                        + (f": {str(r.get('description'))[:160]}" if r.get("description") else "") for r in items[:200]]
            return ToolResult("\n".join(out))
        if action == "read":
            if not uri:
                raise ToolError("read için uri gerekli")
            errors = []
            for s in servers:
                try:
                    res = s.read_resource(uri)
                except MCPError as e:
                    errors.append(str(e))
                    continue
                texts, images = [], []
                for c in res.get("contents") or []:
                    if c.get("text") is not None:
                        texts.append(str(c["text"]))
                    elif c.get("blob") and str(c.get("mimeType", "")).startswith("image/"):
                        images.append(c["blob"])
                    elif c.get("blob"):
                        texts.append(f"[ikili içerik: {c.get('mimeType', '?')}, {len(c['blob'])} bayt base64]")
                body = "\n".join(texts)
                if len(body) > 60000:
                    body = body[:60000] + "\n… (kısaltıldı)"
                return ToolResult(body or "(boş kaynak)", images=images)
            raise ToolError("kaynak okunamadı: " + "; ".join(errors)[:500])
        raise ToolError(f"bilinmeyen action: {action}")


# ---------------------------------------------------------------------------------------------------- code intelligence
_DEF_PATTERNS = [
    r"^\s*(?:async\s+)?def\s+{n}\b", r"^\s*class\s+{n}\b",  # Python
    r"\bfunction\s*\*?\s*{n}\s*\(", r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+{n}\b",  # JS/TS
    r"^\s*(?:export\s+)?(?:const|let|var)\s+{n}\s*[=:]", r"^\s*(?:export\s+)?(?:interface|type|enum)\s+{n}\b",
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+{n}\b", r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+{n}\b",  # Rust
    r"^\s*func\s+(?:\([^)]*\)\s*)?{n}\s*\(", r"^\s*type\s+{n}\s+",  # Go
    r"^\s*(?:public|private|protected|internal|static|\s)*[\w<>\[\],\s]+\s+{n}\s*\([^;]*$",  # C#/Java method
]
_CALL_LINE = re.compile(r"\s*(?:return|await|new|throw|yield|else|if|elif|while|for|case|print|assert|raise|"
                        r"echo|var\s+\w+\s*=|let\s+\w+\s*=|const\s+\w+\s*=\s*\w+\()\b")
_CODE_EXT = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".cs", ".java", ".kt",
             ".c", ".h", ".cpp", ".hpp", ".lua", ".rb", ".php", ".swift"}
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".ruff_cache",
         "target", "bin", "obj", ".next", "Library"}


def _code_files(root: Path, limit: int = 20000) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if any(part in _SKIP for part in p.parts):
            continue
        if p.suffix in _CODE_EXT and p.is_file():
            out.append(p)
            if len(out) >= limit:
                break
    return out


def _search(root: Path, regex: str, word: str = "", limit: int = 120) -> list[tuple[Path, int, str]]:
    """(file, line, text). Uses ripgrep if available, otherwise Python."""
    rg = shutil.which("rg")
    hits: list[tuple[Path, int, str]] = []
    if rg:
        argv = [rg, "--no-heading", "--line-number", "--color=never", "-g", "!node_modules", "-g", "!.git",
                "-m", "50"]
        argv += ["-w", "-F", word] if word else ["-e", regex]
        _, out = _run(argv + ["."], root, timeout=60)
        for line in out.splitlines():
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if m and Path(m.group(1)).suffix in _CODE_EXT:
                hits.append((root / m.group(1), int(m.group(2)), m.group(3).strip()))
                if len(hits) >= limit:
                    break
        return hits
    rx = re.compile(rf"\b{re.escape(word)}\b" if word else regex, re.M)
    for p in _code_files(root):
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append((p, i, line.strip()))
                    if len(hits) >= limit:
                        return hits
        except OSError:
            continue
    return hits


def _py_symbols(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    out: list[str] = []

    def walk(node: ast.AST, prefix: str, depth: int) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = ", ".join(a.arg for a in ch.args.args)
                out.append(f"{'  ' * depth}{ch.lineno}: def {prefix}{ch.name}({args})")
                if depth < 1:  # only the first level of nested functions
                    walk(ch, f"{prefix}{ch.name}.", depth + 1)
            elif isinstance(ch, ast.ClassDef):
                out.append(f"{'  ' * depth}{ch.lineno}: class {prefix}{ch.name}")
                walk(ch, f"{prefix}{ch.name}.", depth + 1)
            elif isinstance(ch, ast.Assign) and depth == 0:
                for t in ch.targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        out.append(f"{ch.lineno}: {t.id} =")

    walk(tree, "", 0)
    return out


_SYM_RX = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
    r"(?P<kind>function\*?|class|interface|type|enum|struct|trait|fn|func|const|let)\s+"
    r"(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_$][\w$]*)")


def _regex_symbols(path: Path) -> list[str]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = _SYM_RX.match(line)
        if m and not (m.group("kind") in ("const", "let") and line[:1].isspace()):
            out.append(f"{i}: {m.group('kind')} {m.group('name')}")
    return out


def _diagnose(path: Path, root: Path) -> tuple[str, list[str]]:
    """(checkers used, findings). Empty list if there are no findings."""
    ext = path.suffix.lower()
    used, found = [], []
    if ext in (".py", ".pyi"):
        try:
            compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec")
        except SyntaxError as e:
            return "python", [f"{path.name}:{e.lineno}:{e.offset}: sözdizimi hatası: {e.msg}"]
        used.append("python")
        code, out = _run([sys.executable, "-m", "ruff", "check", "--output-format=concise", "--no-cache", str(path)],
                         root)
        if code != 127 and "No module named ruff" not in out:
            used.append("ruff")
            found += [x for x in out.splitlines() if re.search(r":\d+:\d+:", x)]
        pyright = shutil.which("pyright") or shutil.which("basedpyright")
        if pyright:
            used.append(Path(pyright).stem)
            _, out = _run([pyright, "--outputjson", str(path)], root, timeout=180)
            try:
                for d in json.loads(out[out.find("{"):]).get("generalDiagnostics", []):
                    if d.get("severity") in ("error", "warning"):
                        ln = d.get("range", {}).get("start", {}).get("line", 0) + 1
                        found.append(f"{path.name}:{ln}: {d['severity']}: {d.get('message', '')}")
            except (ValueError, KeyError):
                pass
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        cfg = next((d / "tsconfig.json" for d in [path.parent, *path.parents] if (d / "tsconfig.json").is_file()),
                   None)
        tsc = cfg is not None and ((cfg.parent / "node_modules" / ".bin" / "tsc").exists()
                                   or (cfg.parent / "node_modules" / ".bin" / "tsc.cmd").exists())
        if tsc and shutil.which("npx"):
            used.append("tsc")
            _, out = _run(["npx", "--no-install", "tsc", "--noEmit", "--pretty", "false", "-p", str(cfg)],
                          cfg.parent, timeout=240)
            found += [x for x in out.splitlines() if path.name in x and "error" in x]
        elif ext in (".js", ".mjs", ".cjs") and shutil.which("node"):
            used.append("node --check")
            code, out = _run(["node", "--check", str(path)], root)
            if code:
                found.append(out[:1500])
    elif ext == ".go" and shutil.which("go"):
        used.append("go vet")
        code, out = _run(["go", "vet", "."], path.parent, timeout=240)
        found += [x for x in out.splitlines() if path.name in x]
    elif ext == ".rs" and shutil.which("cargo"):
        used.append("cargo check")
        _, out = _run(["cargo", "check", "--message-format", "short", "-q"], path.parent, timeout=600)
        found += [x for x in out.splitlines() if path.name in x and ("error" in x or "warning" in x)]
    elif ext == ".json":
        used.append("json")
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            found.append(f"{path.name}: {e}")
    elif ext == ".toml":
        import tomllib

        used.append("toml")
        try:
            tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            found.append(f"{path.name}: {e}")
    return ", ".join(used), found


class CodeIntel(Tool):
    name = "code_intel"
    description = ("Understanding and checking code (no language server needed).\n"
                   "- diagnostics: errors and warnings in file(s): Python (syntax, ruff, pyright if available), TS/JS "
                   "(type errors if the project has tsc, otherwise node --check), Go (go vet), Rust (cargo check), "
                   "JSON/TOML. For a quick check after editing a file, before the tests.\n"
                   "- symbols: classes/functions/constants in a file and their lines (to find where to look in a big "
                   "file without reading all of it).\n"
                   "- definition: where(s) a symbol (function, class, type) is defined.\n"
                   "- references: where a symbol is used (to see the impact before a rename/change).")
    parameters = obj({
        "action": {"type": "string", "enum": ["diagnostics", "symbols", "definition", "references"],
                   "description": "diagnostics: errors/warnings | symbols: the file's symbols | definition: a "
                                  "symbol's definition | references: where it is used."},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "diagnostics/symbols: dosyalar "
                  "(en fazla 10)."},
        "symbol": {"type": "string", "description": "definition/references: symbol name."},
        "root": {"type": "string", "description": "definition/references: folder to search (default: the working "
                                                  "folder)."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        return EXEC if args.get("action") == "diagnostics" else READ  # runs checker programs

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('symbol') or ', '.join((args.get('paths') or [])[:2])}".strip()

    def run(self, ctx: ToolContext, action: str, paths: list[str] | None = None, symbol: str = "",
            root: str = "") -> ToolResult:
        if action in ("diagnostics", "symbols"):
            files = [ctx.resolve(str(p)) for p in (paths or [])[:10]]
            if not files:
                raise ToolError(f"{action} için paths gerekli")
            out = []
            for p in files:
                if not p.is_file():
                    out.append(f"## {p}\ndosya yok")
                    continue
                if action == "symbols":
                    try:
                        syms = _py_symbols(p) if p.suffix in (".py", ".pyi") else _regex_symbols(p)
                    except SyntaxError as e:
                        syms = [f"(ayrıştırılamadı: {e.msg}, satır {e.lineno}) "] + _regex_symbols(p)
                    out.append(f"## {p.name} ({len(syms)})\n" + ("\n".join(syms[:400]) or "(sembol yok)"))
                else:
                    used, found = _diagnose(p, ctx.cwd)
                    if not used:
                        out.append(f"## {p.name}\nbu dosya türü için denetçi yok ya da kurulu değil")
                    else:
                        out.append(f"## {p.name} ({used})\n" + ("\n".join(found[:150]) if found else "sorun yok"))
            return ToolResult("\n\n".join(out))
        if action in ("definition", "references"):
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", symbol or ""):
                raise ToolError("geçerli bir sembol adı ver (ör. MyClass, load_config)")
            base = ctx.resolve(root) if root else ctx.cwd
            if action == "definition":
                rx = "|".join(p.replace("{n}", re.escape(symbol)) for p in _DEF_PATTERNS)
                # the C#/Java pattern also matches calls like "return f(x)" (ripgrep doesn't support lookbehind): filter them out.
                hits = [h for h in _search(base, rx) if not _CALL_LINE.match(h[2])]
            else:
                hits = _search(base, "", word=symbol, limit=150)
            if not hits:
                return ToolResult(f"'{symbol}' için {'tanım' if action == 'definition' else 'kullanım'} bulunamadı "
                                  f"({base})")
            lines = []
            for p, ln, text in hits:
                try:
                    rel = p.relative_to(base)
                except ValueError:
                    rel = p
                lines.append(f"{rel}:{ln}: {text[:200]}")
            return ToolResult(f"{len(hits)} sonuç{' (ilk 150)' if len(hits) >= 150 else ''}:\n" + "\n".join(lines))
        raise ToolError(f"bilinmeyen action: {action}")


TOOLS = [Notify(), EnterPlanMode(), McpResources(), CodeIntel()]


class Advisor(Tool):
    """Consult a stronger model: it sees the whole conversation, uses no tools, and gives short, concrete advice."""

    name = "advisor"
    description = ("Consults a stronger model (the advisor). Use it for a hard decision, a bug you are stuck on, a "
                   "plan you are unsure of, or a final check before finishing a job. The advisor sees this whole "
                   "conversation, uses no tools and gives concrete advice. question: what you are asking and why you "
                   "are unsure.")
    parameters = obj({"question": {"type": "string", "description": "Question for the advisor: the situation, the "
                                                                    "options, your doubt."}},
                     ["question"])
    risk = READ

    def run(self, ctx: ToolContext, question: str) -> ToolResult:
        ref = str((ctx.config.agent or {}).get("advisor") or "").strip()
        if not ref:
            raise ToolError("danışman modeli ayarlı değil: config.toml'da [agent] advisor = \"sağlayıcı:model\" "
                            "(ör. cliproxy:claude-opus-5-5)")
        router = ctx.router
        agent = ctx.state.get("agent")
        if router is None or agent is None:
            raise ToolError("danışman bu ortamda kullanılamıyor")
        msgs = [m for m in list(agent.messages) if not m.get("_image")]
        # The last assistant message is the call to this tool: no result yet, and the provider would reject it.
        while msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
            msgs.pop()
        msgs.append({"role": "user", "content": (
            "[Danışman olarak çağrıldın. Yukarıdaki konuşmada çalışan asistan sana danışıyor. Araç kullanma; kısa, "
            "somut, uygulanabilir tavsiye ver: ne yanlış gidiyor olabilir, hangi seçenek neden daha iyi, neye dikkat "
            "etmeli. Gerekiyorsa açıkça 'bu yanlış' de.]\nSoru: " + question)})
        res = router.chat(ref, msgs, max_tokens=1500)
        return ToolResult(f"[danışman: {ref}]\n{(res.text or '(cevap yok)').strip()}")


TOOLS.append(Advisor())
