"""A small JSON Schema validator for the structured answers of workflow agents.

Own code instead of a dependency: agents need a handful of keywords, and the problems have to read well because they go
back to the model that has to fix its answer.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from typing import Any

MAX_PROBLEMS = 20
MAX_DEPTH = 64
PATTERN_SECONDS = 3.0
_KNOWN = {"type", "properties", "required", "additionalProperties", "items", "enum", "const", "minItems", "maxItems",
          "minimum", "maximum", "minLength", "maxLength", "pattern", "anyOf", "oneOf", "allOf", "exclusiveMinimum",
          "exclusiveMaximum", "uniqueItems", "minProperties", "maxProperties",
          # say something about the value, check nothing
          "title", "description", "default", "examples", "format", "deprecated", "readOnly", "writeOnly"}
_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    for kind, cls in (("boolean", bool), ("integer", int), ("number", float), ("string", str), ("array", list),
                      ("object", dict)):  # bool first: True is an int for Python
        if isinstance(value, cls):
            return kind
    return type(value).__name__


def _is(value: Any, want: str) -> bool:
    kind = _kind(value)
    if want == "number":
        return kind == "integer" or (kind == "number" and math.isfinite(value))
    if want == "integer":
        return kind == "integer" or (kind == "number" and value.is_integer())
    return kind == want


def _same(a: Any, b: Any) -> bool:
    """JSON equality: True is not 1, 1.0 is 1."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b


def _short(value: Any, limit: int = 80) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _cost(pattern: str, length: int) -> float:
    """Rough upper bound of the steps a pattern can take on a text of this length; inf when it may be exponential."""
    try:
        from re import _parser as parser  # type: ignore[attr-defined]  # Python 3.11+
    except ImportError:
        import sre_parse as parser

    variable: float = 0
    length = max(length, 2)

    def walk(tree: Any, repeated: bool) -> float:
        """Steps of one pass; repeats multiply (nested fixed ones too: ((a{500}){500}){500} is 125 million)."""
        nonlocal variable
        steps = 1.0
        for op, arg in tree:
            if str(op) in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                if arg[0] != arg[1]:
                    variable += math.inf if repeated else 1
                steps += max(1, min(int(arg[1]), max(length, int(arg[0])))) * walk(arg[2], repeated or arg[1] > 1)
            elif str(op) == "BRANCH":
                variable += math.inf if repeated else 1
                steps += sum(walk(branch, repeated) for branch in arg[1])
            else:
                steps += 1
                for part in arg if isinstance(arg, (tuple, list)) else (arg,):
                    if isinstance(part, parser.SubPattern):
                        steps += walk(part, repeated)
        return steps

    size = walk(parser.parse(pattern), False)
    try:
        return size * float(length) ** (variable + 1)
    except OverflowError:
        return math.inf


def _python(pattern: str) -> str:
    """JSON Schema means ECMA patterns: "$" is the very end (Python's also matches before a final newline)."""
    return re.sub(r"(?<!\\)((?:\\\\)*)\$$", r"\1\\Z", pattern)


_SEARCH = ("import json, re, sys\np, s = json.loads(sys.stdin.buffer.read())\n"
           "sys.exit(0 if re.search(p, s, re.ASCII) else 3)")


def _matches(pattern: str, text: str) -> bool | None:
    """None: no answer in time. The re module holds the GIL, so a pattern such as (a+)+$ from a script would freeze all
    of ALICE; one that is not obviously cheap runs in a process that can be killed."""
    try:
        pattern = _python(pattern)
        if _cost(pattern, len(text)) <= 1e6:
            return re.search(pattern, text, re.ASCII) is not None
        done = subprocess.run([sys.executable, "-I", "-S", "-c", _SEARCH], input=json.dumps([pattern, text]).encode(),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=PATTERN_SECONDS,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, re.error, RecursionError, OverflowError):
        return None
    return {0: True, 3: False}.get(done.returncode)


class _Out(list):
    """Problems found so far. `work` is shared by all of them: nested anyOf multiplies the number of checks."""

    def __init__(self, work: list[int] | None = None):
        super().__init__()
        self.work = work if work is not None else [0]


MAX_WORK = 200_000


def check(schema: Any, value: Any) -> list[str]:
    """Problems of the value, empty when it fits the schema."""
    out = _Out()
    try:
        _check(schema, value, "", out, 0)
    except RecursionError:
        out.append("the answer is nested too deeply")
    return out[:MAX_PROBLEMS]


def _check(schema: Any, value: Any, path: str, out: _Out, depth: int) -> None:
    if schema is True or len(out) >= MAX_PROBLEMS:
        return
    at = path or "the answer"
    out.work[0] += 1
    if out.work[0] > MAX_WORK:
        out.append(f"{at}: the schema is too complex to check")
        return
    if not isinstance(schema, dict):
        out.append(f"{at}: no value is allowed here")
        return
    if depth > MAX_DEPTH:
        out.append(f"{at}: nested too deeply")
        return
    want = schema.get("type")
    if want is not None:
        wanted = [str(w) for w in (want if isinstance(want, list) else [want])]
        if not any(_is(value, w) for w in wanted):
            out.append(f"{at}: must be {' or '.join(wanted)}, got {_kind(value)} {_short(value)}")
            return
    if "enum" in schema and not any(_same(value, e) for e in schema["enum"] or []):
        out.append(f"{at}: must be one of {_short(schema['enum'], 300)}, got {_short(value)}")
    if "const" in schema and not _same(value, schema["const"]):
        out.append(f"{at}: must be {_short(schema['const'])}, got {_short(value)}")
    if isinstance(value, dict):
        _object(schema, value, path, out, depth)
    elif isinstance(value, list):
        _between(schema, "minItems", "maxItems", len(value), " items", at, out)
        if schema.get("uniqueItems") is True and len(value) <= 2000 and any(
                _same(a, b) for i, a in enumerate(value) for b in value[:i]):
            out.append(f"{at}: the items must be different from each other")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(schema["items"], item, f"{path}[{i}]", out, depth + 1)
    elif isinstance(value, str):
        _between(schema, "minLength", "maxLength", len(value), " characters", at, out)
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            found = _matches(pattern, value)
            if found is None:
                out.append(f"{at}: the pattern {pattern} takes too long to check")
            elif not found:
                out.append(f"{at}: must match the pattern {pattern}, got {_short(value)}")
    elif _is(value, "number"):
        _between(schema, "minimum", "maximum", value, "", at, out)
        low, high = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
        if _is(low, "number") and value <= low:
            out.append(f"{at}: must be more than {low}, got {value}")
        if _is(high, "number") and value >= high:
            out.append(f"{at}: must be less than {high}, got {value}")
    for sub in schema.get("allOf") or []:
        _check(sub, value, path, out, depth + 1)
    for key in ("anyOf", "oneOf"):
        if not schema.get(key):
            continue
        results: list[_Out] = []
        for sub in schema[key]:
            results.append(_Out(out.work))
            _check(sub, value, path, results[-1], depth + 1)
        fits = sum(not r for r in results)
        if fits == 0:
            out.append(f"{at}: fits none of the allowed forms (closest: {'; '.join(min(results, key=len)[:3])})")
        elif key == "oneOf" and fits > 1:
            out.append(f"{at}: fits {fits} of the oneOf forms, must fit exactly one")


def _between(schema: dict, low_key: str, high_key: str, n: float, unit: str, at: str, out: list) -> None:
    low, high = schema.get(low_key), schema.get(high_key)
    if _is(low, "number") and n < low:
        out.append(f"{at}: must be at least {low}{unit}, got {n}")
    if _is(high, "number") and n > high:
        out.append(f"{at}: must be at most {high}{unit}, got {n}")


def _object(schema: dict, value: dict, path: str, out: _Out, depth: int) -> None:
    _between(schema, "minProperties", "maxProperties", len(value), " fields", path or "the answer", out)
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in value:
            out.append(f"{path or 'the answer'}: {key!r} is missing")
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        sub = f"{path}.{key}" if path else str(key)
        if key in props:
            _check(props[key], item, sub, out, depth + 1)
        elif extra is False:
            out.append(f"{sub}: not allowed here (allowed: {', '.join(map(str, props)) or 'nothing'})")
        elif isinstance(extra, dict):
            _check(extra, item, sub, out, depth + 1)


def schema_problems(schema: Any) -> list[str]:
    """What is wrong with the schema itself: agent() refuses it up front instead of failing every answer later."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return ['the schema root must be {"type": "object", "properties": {...}}']
    out: list[str] = []
    _form(schema, "schema", out, 0)
    if not out:
        missing = [k for k in schema.get("required") or [] if k not in (schema.get("properties") or {})]
        if missing:
            out.append(f"schema.required: no such property: {', '.join(missing)}")
    return out[:MAX_PROBLEMS]


def _form(schema: Any, path: str, out: list[str], depth: int) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict) or depth > MAX_DEPTH:
        out.append(f"{path}: nested too deeply" if depth > MAX_DEPTH else f"{path}: must be a schema (a dict)")
        return
    if "$ref" in schema:
        out.append(f"{path}: $ref is not supported, write the schema out")
    # A keyword that is silently skipped would let the script believe the answer was checked.
    unknown = [k for k in schema if isinstance(k, str) and k not in _KNOWN and not k.startswith(("$", "x-"))]
    if unknown:
        out.append(f"{path}: not supported here: {', '.join(unknown[:5])} (say it in the prompt instead)")
    kinds = schema.get("type", [])
    for k in kinds if isinstance(kinds, list) else [kinds]:
        if k not in _TYPES:
            out.append(f"{path}.type: unknown type {_short(k)} (known: {', '.join(_TYPES)})")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        out.append(f"{path}.properties: must be a dict")
        props = {}
    for key, sub in props.items():
        _form(sub, f"{path}.properties.{key}", out, depth + 1)
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        out.append(f"{path}.required: must be a list of names")
    for key in ("items", "additionalProperties"):
        if key in schema:
            _form(schema[key], f"{path}.{key}", out, depth + 1)
    for key in ("enum", "anyOf", "oneOf", "allOf"):
        subs = schema.get(key, ())
        if key in schema and (not isinstance(subs, list) or not subs):
            out.append(f"{path}.{key}: must be a list with at least one entry")
        elif key != "enum":
            for i, sub in enumerate(subs):
                _form(sub, f"{path}.{key}[{i}]", out, depth + 1)
    for key in ("minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum",
                "exclusiveMaximum", "minProperties", "maxProperties"):
        if key in schema and not _is(schema[key], "number"):
            out.append(f"{path}.{key}: must be a number")
    if "pattern" in schema:
        try:
            if not isinstance(schema["pattern"], str) or len(schema["pattern"]) > 1000:
                raise ValueError("must be a text of at most 1000 characters")
            re.compile(_python(schema["pattern"]), re.ASCII)
        except (re.error, ValueError, RecursionError, OverflowError) as e:
            out.append(f"{path}.pattern: {e}")
