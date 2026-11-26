"""Checks a workflow script before it runs: what it declares about itself and that it stays inside the workflow API.

    META = {"name": "review", "description": "Review every changed file", "phases": [{"title": "Review"}]}
    found = await parallel([lambda: agent(f"Review {f}", phase="Review") for f in args["files"]])
    return [x for x in found if x]

Scripts are written by the model, possibly after it read a hostile web page, so they run in a process of their own
(host.py) and this module keeps them from reaching Python's internals from there: an allowlist of syntax, and no names
or attributes that lead to frames, globals, classes or imports.
"""

from __future__ import annotations

import ast
import io
import tokenize
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from alice.workflow import host

MAX_SOURCE = 256 * 1024
MAX_DEPTH = 500

_NODES = tuple(getattr(ast, n) for n in (
    "Module Expr Assign AugAssign AnnAssign NamedExpr Delete For AsyncFor While If Break Continue Pass Return Raise "
    "Try ExceptHandler Assert FunctionDef AsyncFunctionDef Lambda arguments arg Call keyword Await Yield YieldFrom "
    "IfExp BoolOp BinOp UnaryOp Compare Subscript Slice Starred Attribute Name Constant JoinedStr FormattedValue List "
    "Tuple Set Dict ListComp SetComp DictComp GeneratorExp comprehension Match match_case MatchValue MatchSingleton "
    "MatchSequence MatchMapping MatchClass MatchStar MatchAs MatchOr operator unaryop boolop cmpop expr_context"
).split())

_NAMES = frozenset("eval exec compile open input getattr setattr delattr vars globals locals type object super "
                   "breakpoint".split())
_ATTRS = frozenset("mro im_func im_self im_class".split())
_ATTR_PREFIXES = ("gi_", "cr_", "ag_", "f_", "tb_", "co_", "func_")
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class ScriptError(Exception):
    """The script cannot run; the text says why and is meant for the model that wrote it."""


@dataclass
class Meta:
    name: str
    description: str
    phases: list[dict[str, str]] = field(default_factory=list)
    when_to_use: str = ""


def _fail(node: ast.AST, text: str) -> NoReturn:
    line = getattr(node, "lineno", 0)
    raise ScriptError(f"line {line}: {text}" if line else text)


def _name(node: ast.AST, name: str | None) -> None:
    if not name or name == "_":  # `for _ in range(3)` is harmless and everywhere
        return
    if name.startswith("_"):
        _fail(node, f"names starting with an underscore are not allowed ({name})")
    if name in _NAMES:
        _fail(node, f"{name} is not available in workflow scripts (not as a variable name either)")


def _attr(node: ast.AST, name: str) -> None:
    if name.startswith("_"):
        _fail(node, f"attributes starting with an underscore are not allowed ({name})")
    if name in ("format", "format_map"):
        _fail(node, f".{name}() is not allowed, use an f-string")
    if name in _ATTRS or name.startswith(_ATTR_PREFIXES):
        _fail(node, f"attribute {name} is not allowed")


def _check(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _fail(node, "imports are not allowed (json and re are already there; for files, the network or the clock "
                        "ask an agent)")
        if not isinstance(node, _NODES):
            _fail(node, f"{type(node).__name__} is not allowed in workflow scripts")
        if isinstance(node, ast.Name):
            _name(node, node.id)
        elif isinstance(node, ast.arg):
            _name(node, node.arg)
        elif isinstance(node, ast.Attribute):
            _attr(node, node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ExceptHandler)):
            _name(node, node.name)
        elif isinstance(node, ast.keyword):
            if node.arg and node.arg.startswith("_"):
                _attr(node.value, node.arg)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            _name(node, node.name)
        elif isinstance(node, ast.MatchMapping):
            _name(node, node.rest)
        elif isinstance(node, ast.MatchClass):
            for name in node.kwd_attrs:  # `case str(__class__=c)` reads the attribute
                _attr(node, name)
    todo = list(ast.iter_child_nodes(tree))
    while todo:  # a yield outside functions would turn the whole script into a generator
        node = todo.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            _fail(node, "yield is only allowed inside a function")
        if not isinstance(node, _FUNCS):
            todo.extend(ast.iter_child_nodes(node))


def _in_braces(text: str) -> int:
    """Operators in the {...} parts of an f-string (one token before Python 3.12)."""
    depth = count = i = 0
    while i < len(text):
        c = text[i]
        if depth == 0 and c in "{}" and text[i + 1:i + 2] == c:
            i += 1  # an escaped brace
        elif c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth and c in "+-*/%|&^~.([<>=@:":
            count += 1
        i += 1
    return count


def _check_depth(source: str) -> None:
    """Refuses expressions that nest so deeply (1+1+1+... ten thousand times) that Python's own parser overflows the C
    stack, which would end ALICE. Counts on the tokens, without parsing: at most how many levels a place can have."""
    levels = [0]  # operators since the last comma, per open bracket
    elifs = 0
    words = {"not", "and", "or", "if", "else", "lambda", "await", "in", "is", "for", "yield", "from"}
    try:
        for t in tokenize.generate_tokens(io.StringIO(source).readline):
            if t.type == tokenize.NEWLINE:
                levels = [0]
            elif t.type == tokenize.OP:
                if t.string in ",;":
                    levels[-1] = 0
                elif t.string in ")]}":
                    if len(levels) > 1:
                        levels.pop()
                else:
                    levels[-1] += 1
                    if t.string in "([{":
                        levels.append(0)
            elif t.type == tokenize.NAME:
                elifs += t.string == "elif"
                levels[-1] += t.string in words
            elif t.type == tokenize.STRING and "f" in t.string[:2].lower():
                levels[-1] += _in_braces(t.string)
            if sum(levels) > MAX_DEPTH or elifs > MAX_DEPTH:
                raise ScriptError(f"line {t.start[0]}: this is nested too deeply, split it up")
    except (tokenize.TokenError, SyntaxError):
        pass  # ast.parse gives the better message


def _text(data: dict, key: str, required: bool = False) -> str:
    value = data.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ScriptError(f"META needs a {key!r} text" if required else f"META {key!r} must be a text")
    return value.strip()


def _meta(tree: ast.Module) -> Meta:
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # a docstring
    first = body[0] if body else None
    if not (isinstance(first, ast.Assign) and len(first.targets) == 1 and isinstance(first.targets[0], ast.Name)
            and first.targets[0].id == "META"):
        raise ScriptError('a workflow script starts with META = {"name": "...", "description": "..."}')
    try:
        data = ast.literal_eval(first.value)
    except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
        data = None
    if not isinstance(data, dict):
        _fail(first, "META must be a plain literal dict (no variables, calls or f-strings)")
    phases = data.get("phases") or []
    if not isinstance(phases, (list, tuple)):
        raise ScriptError("META 'phases' must be a list")
    out = []
    for p in phases:
        p = {"title": p} if isinstance(p, str) else p
        if not isinstance(p, dict) or not isinstance(p.get("title"), str) or not p["title"].strip() \
                or not isinstance(p.get("detail") or "", str):
            raise ScriptError('every entry of META "phases" is {"title": "...", "detail": "..."} (detail is optional)')
        out.append({"title": p["title"].strip()[:200]} | ({"detail": p["detail"][:500]} if p.get("detail") else {}))
    return Meta(_text(data, "name", True)[:100], _text(data, "description", True)[:2000], out[:100],
                _text(data, "when_to_use")[:2000])


def local_path(name: str) -> Path:
    """The path, unless it is on the network: opening one sends the Windows login to whoever owns the server. Checked on
    the parsed path, because "\\/server/share" becomes one too."""
    path = Path(name).expanduser()
    if any(str(p).replace("\\", "/").startswith("//") for p in (name.strip(), path)):
        raise ScriptError(f"there is no workflow named {name!r}: network paths are not opened")
    return path


def parse(source: str) -> Meta:
    """What the script declares; ScriptError when it cannot run."""
    if not isinstance(source, str) or not source.strip():
        raise ScriptError("the script is empty")
    if len(source) > MAX_SOURCE:
        raise ScriptError(f"the script is too long ({len(source) // 1024} KB, at most {MAX_SOURCE // 1024} KB)")
    try:
        source.encode("utf-8")
        _check_depth(source)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # "\d" in a plain string is not worth a line in ALICE's console
            tree = ast.parse(source)
            _check(tree)
            meta = _meta(tree)
            host.build(source, "<workflow>")  # finds what only the compiler rejects (await in a plain function...)
    except SyntaxError as e:
        line = (e.text or "").strip()
        raise ScriptError(f"line {e.lineno}: {e.msg}" + (f": {line[:200]}" if line else "")) from None
    except (ValueError, RecursionError, MemoryError) as e:
        raise ScriptError(f"the script cannot be compiled: {type(e).__name__} {e}".strip()) from None
    return meta
