"""İzin modları.

plan : sadece okuma/araştırma; dosya değiştiren, komut çalıştıran, ekrana dokunan her şey reddedilir
ask  : okuma dışındaki her şey onay ister
auto : tehlikeli komutlar (rm -r, format, registry, shutdown...), sistem klasörlerine yazma ve
       başkasına giden mesajlar (send: Discord/WhatsApp/e-posta) onay ister; geri kalan her şey
       (dosya düzenleme, komut, fare/klavye) serbest
yolo : hiçbir şey sormaz

Kalıcı kurallar (Claude Code'daki allow/deny gibi), config.toml ve projede .alice/permissions.toml:
  [permissions]
  allow = ["run_command(git status*)", "run_command(npm test*)", "edit_file(src/*)"]
  ask   = ["run_command(git push*)"]
  deny  = ["run_command(*rm -rf*)", "write_file(*.env)", "mcp__github__delete_*"]
Biçim: "araç" ya da "araç(desen)". Araç adında * olabilir. Desen (büyük/küçük harf duyarsız, fnmatch) aracın asıl
argümanına bakar: command, path, url, target, app, action ya da query (ilk bulunan). deny her modda (yolo dahil)
geçerlidir; ask yolo dışında her zaman sorar; allow sormadan izin verir (plan modunu delmez; tehlikeli bir işe
ancak desenli bir kural izin verir, "run_command" tek başına rm -r'yi serbest bırakmaz).
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, NETWORK, READ, SEND, WRITE, Tool, ToolContext

MODES = ("plan", "ask", "auto", "yolo")

SYSTEM_DIRS_WIN = [r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata\microsoft"]
SYSTEM_DIRS_POSIX = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/var/lib", "/System", "/Library"]


def is_system_path(p: Path) -> bool:
    s = str(p)
    if sys.platform == "win32":
        low = s.lower()
        return any(low == d or low.startswith(d + "\\") for d in SYSTEM_DIRS_WIN)
    return any(s == d or s.startswith(d + "/") for d in SYSTEM_DIRS_POSIX)


def _confirm_outward(ctx: ToolContext) -> bool:
    cfg = getattr(ctx, "config", None)
    agent = getattr(cfg, "agent", None) or {}
    return bool(agent.get("confirm_outward", True)) if isinstance(agent, dict) else True


PRIMARY_ARGS = ("command", "path", "url", "target", "app", "action", "query")
_project_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}


def primary_arg(args: dict[str, Any]) -> str:
    for k in PRIMARY_ARGS:
        v = args.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def parse_rule(rule: str) -> tuple[str, str | None]:
    rule = rule.strip()
    if rule.endswith(")") and "(" in rule:
        name, spec = rule[:-1].split("(", 1)
        return name.strip(), spec.strip()
    return rule, None


def rule_matches(rule: str, tool_name: str, args: dict[str, Any]) -> bool:
    name, spec = parse_rule(rule)
    if not fnmatch.fnmatchcase(tool_name, name):
        return False
    if spec is None:
        return True
    value = primary_arg(args).replace("\\", "/").lower()
    return fnmatch.fnmatchcase(value, spec.replace("\\", "/").lower())


def project_rules(cwd: Path | None) -> dict[str, list[str]]:
    """<proje>/.alice/permissions.toml ([permissions] başlıklı ya da başlıksız allow/ask/deny)."""
    if cwd is None:
        return {}
    path = Path(cwd) / ".alice" / "permissions.toml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _project_cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            import tomli as tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - bozuk dosya izinleri gevşetmesin: yok say
        data = {}
    data = data.get("permissions", data) if isinstance(data, dict) else {}
    rules = {k: [str(r) for r in data.get(k, []) or []] for k in ("allow", "ask", "deny")}
    _project_cache[str(path)] = (mtime, rules)
    return rules


def suggest_rule(tool: Tool, args: dict[str, Any], cwd: Path | None = None) -> str:
    """"Bu projede hep izin ver" denince yazılacak kural: dar ama işe yarar. Komutta ilk iki kelime ("git status*"),
    dosya araçlarında dosyanın klasörü, diğerlerinde aracın adı."""
    name = tool.name
    if "command" in args and args.get("command"):
        words = str(args["command"]).split()
        head = " ".join(words[:2]) if len(words) > 1 and not words[1].startswith(("-", "/", "\\", ".")) else words[0]
        return f"{name}({head}*)"
    if args.get("path"):
        p = Path(str(args["path"]))
        try:
            base = cwd.resolve() if cwd else None
            full = (base / p).resolve() if base and not p.is_absolute() else p.resolve()
            rel = full.parent.relative_to(base) if base else full.parent
            folder = str(rel).replace("\\", "/")
        except (ValueError, OSError):
            folder = str(p.parent).replace("\\", "/")
        return f"{name}({'' if folder in ('', '.') else folder.rstrip('/') + '/'}*)"
    return name


def persist_allow(cwd: Path, rule: str) -> Path:
    """Kuralı <proje>/.alice/permissions.toml'un allow listesine ekle (varsa tekrar eklemez)."""
    path = Path(cwd) / ".alice" / "permissions.toml"
    rules = dict(project_rules(cwd)) or {"allow": [], "ask": [], "deny": []}
    for k in ("allow", "ask", "deny"):
        rules.setdefault(k, [])
    if rule not in rules["allow"]:
        rules["allow"] = rules["allow"] + [rule]

    def arr(items: list[str]) -> str:
        import json

        return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# ALICE izin kuralları (bu proje). Biçim: alice/permissions.py\n[permissions]\n"
                    f"allow = {arr(rules['allow'])}\nask = {arr(rules['ask'])}\ndeny = {arr(rules['deny'])}\n",
                    encoding="utf-8")
    _project_cache.pop(str(path), None)
    return path


def remove_project_rule(cwd: Path, kind: str, rule: str) -> bool:
    """Projedeki .alice/permissions.toml'dan bir kuralı sil (uygulamadaki Asistan sekmesi)."""
    rules = dict(project_rules(cwd))
    if kind not in ("allow", "ask", "deny") or rule not in (rules.get(kind) or []):
        return False
    rules[kind] = [r for r in rules[kind] if r != rule]
    path = Path(cwd) / ".alice" / "permissions.toml"
    import json

    arr = {k: "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in rules.get(k, [])) + "]"
           for k in ("allow", "ask", "deny")}
    path.write_text("# ALICE izin kuralları (bu proje). Biçim: alice/permissions.py\n[permissions]\n"
                    f"allow = {arr['allow']}\nask = {arr['ask']}\ndeny = {arr['deny']}\n", encoding="utf-8")
    _project_cache.pop(str(path), None)
    return True


class Permissions:
    def __init__(self, mode: str = "auto", rules: dict[str, list[str]] | None = None):
        if mode not in MODES:
            raise ValueError(f"geçersiz izin modu: {mode}")
        self.mode = mode
        self.always_allow: set[str] = set()
        self.rules = rules  # None: config.toml [permissions] + projedeki .alice/permissions.toml

    def rules_for(self, ctx: ToolContext) -> dict[str, list[str]]:
        if self.rules is not None:
            return self.rules
        cfg = getattr(ctx, "config", None)
        data = getattr(cfg, "data", None)
        base = (data.get("permissions") if isinstance(data, dict) else None) or {}
        proj = project_rules(getattr(ctx, "cwd", None))
        return {k: [str(r) for r in base.get(k, []) or []] + proj.get(k, []) for k in ("allow", "ask", "deny")}

    @staticmethod
    def _risky_after_untrusted(tool: Tool, args: dict[str, Any], risk: str, ctx: ToolContext) -> bool:
        """Dış içerik okunmuş bir turda onaya bağlanan işler: mesaj göndermek, komut çalıştırmak, çalışma klasörü
        dışına yazmak."""
        if tool.name == "run_command":
            from alice import sandbox

            return not sandbox.usable(ctx, args)  # sandbox'ta çalışacaksa sormaya gerek yok: proje dışına yazamaz
        if risk == SEND or tool.name == "process_kill" or "command" in args:
            return True
        if risk == WRITE and args.get("path"):
            try:
                target = ctx.resolve(str(args["path"]))
                return not str(target).startswith(str(Path(ctx.cwd).resolve()))
            except (OSError, ValueError, AttributeError):
                return True
        return False

    def matching_rule(self, tool: Tool, args: dict[str, Any], ctx: ToolContext, kind: str) -> str | None:
        for rule in self.rules_for(ctx).get(kind, []):
            try:
                if rule_matches(rule, tool.name, args):
                    return rule
            except Exception:  # noqa: BLE001 - garip argüman kuralı bozmasın
                continue
        return None

    def decide(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> str:
        """'allow', 'ask' ya da 'deny' döndürür."""
        if self.matching_rule(tool, args, ctx, "deny"):
            return "deny"
        if self.mode == "yolo":
            return "allow"
        if self.matching_rule(tool, args, ctx, "ask"):
            return "ask"
        risk = tool.risk_for(args)
        if risk == READ:
            return "allow"
        if self.mode == "plan":
            return "allow" if risk == NETWORK else "deny"
        allowed = self.matching_rule(tool, args, ctx, "allow")
        if allowed and (risk != DANGER or parse_rule(allowed)[1] is not None):
            return "allow"
        if tool.name in self.always_allow and risk != DANGER:
            return "allow"
        if self.mode == "ask":
            return "ask"
        # auto
        if risk == DANGER:
            return "ask"
        from alice import guard

        if guard.tainted(ctx) and self._risky_after_untrusted(tool, args, risk, ctx):
            return "ask"  # bu turda dış içerik okundu: talimat oradan gelmiş olabilir
        if risk == SEND and _confirm_outward(ctx):
            return "ask"
        if risk == WRITE and "path" in args:
            try:
                if is_system_path(ctx.resolve(str(args["path"]))):
                    return "ask"
            except (OSError, ValueError):
                return "ask"
        return "allow"
