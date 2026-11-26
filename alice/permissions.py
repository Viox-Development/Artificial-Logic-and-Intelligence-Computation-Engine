"""Permission modes.

plan : read/research only; anything that changes files, runs commands or touches the screen is denied
ask  : everything except reading asks for confirmation
auto : dangerous commands (rm -r, format, registry, shutdown...), writes to system folders and messages
       sent to others (send: Discord/WhatsApp/e-mail) ask for confirmation; everything else
       (file editing, commands, mouse/keyboard) is allowed
yolo : never asks

Permanent rules (like allow/deny in Claude Code), in config.toml and in the project's .alice/permissions.toml:
  [permissions]
  allow = ["run_command(git status*)", "run_command(npm test*)", "edit_file(src/*)"]
  ask   = ["run_command(git push*)"]
  deny  = ["run_command(*rm -rf*)", "write_file(*.env)", "mcp__github__delete_*"]
Format: "tool" or "tool(pattern)". The tool name may contain *. The pattern (case-insensitive, fnmatch) is checked
against the tool's main argument: command, path, url, target, app, action or query (the first one found). deny applies
in every mode (yolo included); ask always asks except in yolo; allow grants permission without asking (it does not
override plan mode). A dangerous action is only allowed by a rule with a pattern: a bare "run_command" does not let
rm -r through by itself. And when external content has been read in the current turn, a rule without a pattern does not
pass command execution, writes outside the project or sending messages without asking either.
"""

from __future__ import annotations

import fnmatch
import sys
import threading
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, NETWORK, READ, SEND, WRITE, Tool, ToolContext

MODES = ("plan", "ask", "auto", "yolo")

SYSTEM_DIRS_WIN = [r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata\microsoft"]
# macOS: /etc and /var are links into /private, and callers check resolved paths, so "/etc/hosts" arrives as
# "/private/etc/hosts" there (it was not recognized as a system path and auto mode allowed writing it).
SYSTEM_DIRS_POSIX = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/var/lib", "/System", "/Library",
                     "/private/etc", "/private/var/db", "/var/db"]


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


def _path_forms(raw: str, cwd: Path | None) -> list[str]:
    """Forms of a path argument to compare against a rule: the resolved absolute path and (if inside the project)
    the path relative to the project. The raw text is added only if it does not contain "..". Previously only the raw
    text was checked: the "edit_file(gizli/*)" ban was bypassed with "./gizli/a" or an absolute path, and the
    "edit_file(src/*)" allow permitted writing to "src/../../dışarı"; a relative rule written with "bu projede hep
    izin ver" (always allow in this project) also never matched a call that came with an absolute path."""
    out = []
    if ".." not in Path(raw).parts:
        out.append(raw)
    if cwd is not None:
        try:
            base = Path(cwd).resolve()
            p = Path(raw).expanduser()
            full = (p if p.is_absolute() else base / p).resolve()
            out.append(str(full))
            if full != base and _inside(full, base):
                out.append(str(full.relative_to(base)))
        except (OSError, ValueError, RuntimeError):
            pass  # a path that can't be resolved is skipped
    return out


def rule_matches(rule: str, tool_name: str, args: dict[str, Any], cwd: Path | None = None) -> bool:
    name, spec = parse_rule(rule)
    if not fnmatch.fnmatchcase(tool_name, name):
        return False
    if spec is None:
        return True
    raw = primary_arg(args)
    is_path = args.get("command") in (None, "") and args.get("path") not in (None, "")  # is the main argument a path?
    values = _path_forms(raw, cwd) if is_path else [raw]
    pattern = spec.replace("\\", "/").lower()
    return any(fnmatch.fnmatchcase(v.replace("\\", "/").lower(), pattern) for v in values)


def _trust_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "trusted-permissions.json"


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trusted(path: Path, text: str) -> bool:
    import json

    try:
        data = json.loads(_trust_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get(str(path.resolve())) == _digest(text)


def _trust(path: Path, text: str) -> None:
    """This content was written by ALICE itself (with the user's approval): its allow rules are valid."""
    import json

    tp = _trust_path()
    try:
        data = json.loads(tp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[str(path.resolve())] = _digest(text)
    try:
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass  # trust is asked again next time


def project_rules(cwd: Path | None, include_untrusted: bool = False) -> dict[str, list[str]]:
    """<project>/.alice/permissions.toml (allow/ask/deny, with or without a [permissions] header).

    The file lives inside the project: a cloned repo, or a command in the sandbox, could write `allow = ["*(*)"]`
    and bypass all confirmations. So allow rules are valid only while the file is in the form ALICE wrote it (the one
    the user approved with "bu projede hep izin ver" (always allow in this project)). A digest of the content is kept
    in ~/.alice/trusted-permissions.json. The allow rules of a file changed by hand or from outside are ignored
    (listed with include_untrusted). ask and deny are always valid since they only tighten."""
    if cwd is None:
        return {}
    path = Path(cwd) / ".alice" / "permissions.toml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _project_cache.get(str(path))
    if hit and hit[0] == mtime:
        rules, trusted = hit[1], hit[2]
    else:
        try:
            try:
                import tomllib
            except ImportError:  # pragma: no cover
                import tomli as tomllib
            text = path.read_text(encoding="utf-8-sig")
            data = tomllib.loads(text)
        except Exception:  # noqa: BLE001 - a broken file must not loosen permissions: ignore it
            text, data = "", {}
        data = data.get("permissions", data) if isinstance(data, dict) else {}
        rules = {k: [str(r) for r in data.get(k, []) or []] for k in ("allow", "ask", "deny")}
        trusted = bool(text) and _trusted(path, text)
        _project_cache[str(path)] = (mtime, rules, trusted)
    if trusted or include_untrusted:
        return rules
    return {**rules, "allow": []}


def project_rules_trusted(cwd: Path | None) -> bool:
    """Are the allow rules in the project valid? (True if there is no file, or if ALICE wrote it.)"""
    rules = project_rules(cwd, include_untrusted=True)
    return not rules.get("allow") or project_rules(cwd).get("allow") == rules.get("allow")


def _write_project_rules(cwd: Path, rules: dict[str, list[str]]) -> Path:
    import json

    def arr(items: list[str]) -> str:
        return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"

    path = Path(cwd) / ".alice" / "permissions.toml"
    text = ("# ALICE izin kuralları (bu proje). Biçim: alice/permissions.py\n"
            "# Elle değiştirilirse allow kuralları ALICE'te tekrar onaylanana kadar yok sayılır.\n[permissions]\n"
            f"allow = {arr(rules.get('allow', []))}\nask = {arr(rules.get('ask', []))}\n"
            f"deny = {arr(rules.get('deny', []))}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _trust(path, path.read_text(encoding="utf-8-sig"))
    _project_cache.pop(str(path), None)
    return path


def trust_project_rules(cwd: Path) -> bool:
    """The user approved the rules in the project (written by hand or from outside): treat them as valid as they are."""
    rules = project_rules(cwd, include_untrusted=True)
    if not rules:
        return False
    _write_project_rules(cwd, rules)
    return True


def suggest_rule(tool: Tool, args: dict[str, Any], cwd: Path | None = None) -> str:
    """The rule written when the user picks "bu projede hep izin ver" (always allow in this project): narrow but useful.
    For a command, the first two words ("git status*"); for file tools, the file's folder;
    for others, the tool's name."""
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
    """Add the rule to the allow list of <project>/.alice/permissions.toml (not added again if already there)."""
    # Unapproved (external) allow rules are dropped here too: a single user approval must not launder them.
    rules = dict(project_rules(cwd)) or {"allow": [], "ask": [], "deny": []}
    for k in ("allow", "ask", "deny"):
        rules.setdefault(k, [])
    if rule not in rules["allow"]:
        rules["allow"] = rules["allow"] + [rule]
    return _write_project_rules(cwd, rules)


HISTORY_MAX = 2000  # lines: the permission-answer history is capped at this many
_HISTORY_LOCK = threading.Lock()


def history_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "permission-history.jsonl"


def record_answer(tool: Tool, args: dict[str, Any], cwd: Path | None, risk: str, answer: str) -> None:
    """Record the permission asked and the answer: the "artık sorma" (stop asking) suggestion
    for always-approved tasks comes from this."""
    import json
    import time

    row = {"ts": round(time.time()), "tool": tool.name, "rule": suggest_rule(tool, args, cwd), "risk": risk,
           "answer": answer, "cwd": str(cwd or "")}
    p = history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # When two chats wrote answers at the same time, read-modify-write deleted one of the lines: lock + os.replace
    # from a temp file (a half-written file is never read).
    with _HISTORY_LOCK:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()[-(HISTORY_MAX - 1):]
        except OSError:
            lines = []
        _atomic_write(p, "\n".join(lines + [json.dumps(row, ensure_ascii=False)]) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    import os
    import tempfile
    import time

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: the target is open in another process at that moment
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def suggestions(cwd: Path | None = None, min_yes: int = 3) -> list[dict[str, Any]]:
    """Suggestions from the history: rules approved at least min_yes times, never rejected, not dangerous or sent to
    others, and not yet allowed. If cwd is given, only those asked in that project."""
    import json

    try:
        text = history_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stats: dict[str, dict[str, Any]] = {}
    for ln in text.splitlines():
        # A broken or half-written line, or a record without a "rule": skip it, don't drop all suggestions.
        try:
            r = json.loads(ln) if ln.strip() else None
            if not isinstance(r, dict) or not isinstance(r.get("rule"), str) or not r["rule"]:
                continue
            if cwd is not None and r.get("cwd") and Path(str(r["cwd"])) != Path(cwd):
                continue
            ts = int(r.get("ts") or 0)
        except (ValueError, TypeError):
            continue
        s = stats.setdefault(r["rule"], {"rule": r["rule"], "tool": str(r.get("tool") or ""), "yes": 0, "no": 0,
                                         "risky": False, "last": 0})
        s["yes" if r.get("answer") in ("y", "a", "p") else "no"] += 1
        s["risky"] = s["risky"] or r.get("risk") in (DANGER, SEND)
        s["last"] = max(s["last"], ts)
    have = set(project_rules(cwd).get("allow", [])) if cwd is not None else set()
    out = [s for s in stats.values() if s["yes"] >= min_yes and not s["no"] and not s["risky"] and s["rule"] not in have]
    return sorted(out, key=lambda s: (-s["yes"], -s["last"]))


def remove_project_rule(cwd: Path, kind: str, rule: str) -> bool:
    """Delete a rule from the project's .alice/permissions.toml (the "Asistan" tab in the app)."""
    trusted = project_rules_trusted(cwd)
    rules = dict(project_rules(cwd, include_untrusted=True))
    if kind not in ("allow", "ask", "deny") or rule not in (rules.get(kind) or []):
        return False
    rules[kind] = [r for r in rules[kind] if r != rule]
    if not trusted:
        rules["allow"] = []  # deleting must not make unapproved allow rules valid
    _write_project_rules(cwd, rules)
    return True


class Permissions:
    def __init__(self, mode: str = "auto", rules: dict[str, list[str]] | None = None):
        if mode not in MODES:
            raise ValueError(f"invalid permission mode: {mode}")
        self.before_plan: str | None = None  # the mode before plan mode: exit_plan_mode returns to it after approval
        self._mode = mode
        self.always_allow: set[str] = set()
        self.rules = rules  # None: config.toml [permissions] + the project's .alice/permissions.toml

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        if value == "plan" and self._mode != "plan":
            self.before_plan = self._mode
        self._mode = value

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
        """Actions that need confirmation in a turn where external content was read: sending messages, running
        commands, writing outside the working folder."""
        if tool.name == "run_command":
            from alice import sandbox

            return not sandbox.usable(ctx, args)  # sandboxed: cannot write outside the project
        if risk == SEND or tool.name == "process_kill" or "command" in args:
            return True
        if risk == WRITE:
            # The "path" and the files in the patch text (apply_patch has no "path": it was never checked before).
            # A prefix comparison counted C:\proje2 as inside C:\proje; the check now uses the folder boundary.
            try:
                from alice.prompts import scratch_dir

                roots = (Path(ctx.cwd).resolve(), scratch_dir().resolve())  # the scratch folder is the model's own
                return any(not any(_inside(ctx.resolve(p), r) for r in roots) for p in _write_targets(args))
            except (OSError, ValueError, AttributeError, TypeError):
                return True
        return False

    def matching_rule(self, tool: Tool, args: dict[str, Any], ctx: ToolContext, kind: str) -> str | None:
        for rule in self.rules_for(ctx).get(kind, []):
            try:
                if rule_matches(rule, tool.name, args, getattr(ctx, "cwd", None)):
                    return rule
            except Exception:  # noqa: BLE001 - a weird argument must not break the rule
                continue
        return None

    def decide(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> str:
        """Returns 'allow', 'ask' or 'deny'."""
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
        from alice import guard

        allowed = self.matching_rule(tool, args, ctx, "allow")
        if allowed and (risk != DANGER or parse_rule(allowed)[1] is not None):
            # A rule without a pattern ("run_command" alone) does not let command execution, writing outside the
            # project or sending messages through without asking in a tainted turn: an instruction from external content
            # would have opened every use of that tool. Rules with a pattern ("run_command(git status*)") stay valid
            # because they are narrow.
            if parse_rule(allowed)[1] is not None or not (
                    guard.tainted(ctx) and self._risky_after_untrusted(tool, args, risk, ctx)):
                return "allow"
        # External content was read in this turn: the instruction may have come from there. A session-long
        # "hep izin ver" (always_allow) does not override this: the user did not give that approval with injection in
        # mind (pattern allow rules above stay valid).
        if guard.tainted(ctx) and self._risky_after_untrusted(tool, args, risk, ctx):
            return "ask"
        if tool.name in self.always_allow and risk != DANGER:
            return "allow"
        if self.mode == "ask":
            return "ask"
        # auto
        if risk == DANGER:
            return "ask"
        if risk == SEND and _confirm_outward(ctx):
            return "ask"
        if risk == WRITE:
            try:
                if any(is_system_path(ctx.resolve(p)) for p in _write_targets(args)):
                    return "ask"
            except (OSError, ValueError):
                return "ask"
        return "allow"


def _inside(target: Path, root: Path) -> bool:
    """Is target inside the root folder? (Case-insensitive on Windows.)"""
    t, r = str(target), str(root)
    if sys.platform == "win32":
        t, r = t.lower(), r.lower()
    sep = "\\" if sys.platform == "win32" else "/"
    return t == r or t.startswith(r.rstrip("\\/") + sep)


def _write_targets(args: dict[str, Any]) -> list[str]:
    """Paths a write tool will touch: "path" and the files in the patch text (apply_patch used to never ask
    when writing to a system folder)."""
    out = [str(args["path"])] if "path" in args else []
    patch = args.get("patch")
    if isinstance(patch, str) and patch:
        from alice.tools.base import ToolError
        from alice.tools.patch import parse_patch

        try:
            ops = parse_patch(patch)
        except ToolError:
            return out  # broken patch: the tool itself errors and writes nothing
        for op in ops:
            out += [p for p in (op.path, op.move_to) if p]
    return out
