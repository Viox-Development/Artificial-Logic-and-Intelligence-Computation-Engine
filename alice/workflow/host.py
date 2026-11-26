"""The process a workflow script runs in.

engine.py starts this file as `python -I -S host.py` with an almost empty environment and talks to it in JSON lines:
the script's agent() calls go out on stdout, their results come back on stdin. A separate process because a script the
model wrote can loop forever or eat memory, and only a process can be killed. Standard library only: ALICE's modules
(and site-packages) are not importable here, and script.py has already refused everything that reaches around the names
below.

The scheduler is a small one of its own instead of asyncio. It handles one message from the engine at a time and runs
every ready task before it takes the next, so the order in which a script issues its agent() calls depends only on the
order in which results were delivered. Resume needs exactly that: replaying the recorded deliveries gives every call
its old number again.
"""

import ast
import builtins
import collections
import json
import os
import queue
import re
import sys
import threading
import types

MAX_LINE = 8 * 1024 * 1024  # bytes of one protocol line, both directions
MAX_PROMPT = 1_000_000  # characters
MAX_ITEMS = 4096  # parallel()/pipeline()
MAX_CALLS = 10_000  # agent()/workflow() calls issued, refused ones included: a script must not flood the engine
MAX_LOGS = 2_000
MAX_DEPTH = 200  # levels of the returned data
MEMORY = 1024 * 1024 * 1024
API = ("agent", "parallel", "pipeline", "phase", "log", "workflow", "args", "budget", "json", "re")
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SIMPLE = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.FunctionDef, ast.AsyncFunctionDef)


# Compiling ----------------------------------------------------------------------------------------------------------

def _stores(nodes, into_functions=False):
    """Names bound inside the nodes: {name: last (line, column) where it is bound}."""
    found = {}
    todo = list(nodes)
    while todo:
        n = todo.pop()
        name = None
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            name = n.id
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ExceptHandler)):
            name = n.name
        if name:
            found[name] = max(found.get(name, (0, 0)), (n.lineno, n.col_offset))
        if into_functions or not isinstance(n, _FUNCS + _COMPS):
            todo.extend(ast.iter_child_nodes(n))
    return found


def _blocks(node):
    """The statement lists inside a compound statement."""
    for part in (node, *getattr(node, "handlers", ()), *getattr(node, "cases", ())):
        for name in ("body", "orelse", "finalbody"):
            block = getattr(part, name, None)
            if isinstance(block, list):
                yield block


def _direct(stmt):
    """Names a plain statement certainly sets: its own targets, not a walrus inside an expression (it may sit behind an
    `and`), not an annotation without a value."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {stmt.name}
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    if isinstance(stmt, ast.AnnAssign) and stmt.value is None:
        return set()
    return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


class _Body:
    """What a loop body sets, worked out once: asking the whole body again for every function in it took minutes for a
    script of 40 KB."""

    def __init__(self, body):
        self.last = _stores(body)
        self.first = {}  # name -> where it has certainly been set
        self.inner = []  # compound statements: (start, end, their blocks)
        gone = set()
        for stmt in body:
            end = (stmt.end_lineno, stmt.end_col_offset)
            if isinstance(stmt, ast.Delete):
                gone |= {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
            elif isinstance(stmt, _SIMPLE):
                for name in _direct(stmt):
                    self.first.setdefault(name, end)
            blocks = [] if isinstance(stmt, _FUNCS) else [_Body(b) for b in _blocks(stmt)]
            if blocks:
                gone.update(*(b.gone for b in blocks))
                self.inner.append(((stmt.lineno, stmt.col_offset), end, blocks))
        self.gone = gone  # deleted somewhere in it: never certain

    def set_before(self, here):
        """Names certainly set by the time execution reaches `here` in this block."""
        names = {n for n, at in self.first.items() if at <= here}
        for start, end, blocks in self.inner:
            if start <= here < end:
                for b in blocks:
                    names |= b.set_before(here)
        return names - self.gone


class _Prepare(ast.NodeTransformer):
    """Two changes to what the model wrote:

    - A function made inside a loop or comprehension keeps the loop's values of that moment. Python binds late, so in
      parallel([lambda: agent(f) for f in files]) every agent would get the last file; nobody means that.
    - Set literals and the results of set operators (dict views give plain sets) become the Set below, which iterates in a fixed order (string hashes differ per process, and an
      order that changes between runs would make resume useless)."""

    def __init__(self):
        self.frames = []  # loops around the current node within the current function: (targets, loop body)

    def _as_set(self, items, node):
        name = ast.copy_location(ast.Name("_wf_set", ast.Load()), node)
        return ast.copy_location(ast.Call(name, [items], []), node)

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if not isinstance(node.op, (ast.Sub, ast.BitAnd, ast.BitOr, ast.BitXor)):
            return node
        name = ast.copy_location(ast.Name("_wf_fix", ast.Load()), node)
        return ast.copy_location(ast.Call(name, [node], []), node)

    def visit_Set(self, node):
        self.generic_visit(node)
        return self._as_set(ast.copy_location(ast.List(node.elts, ast.Load()), node), node)

    def _comprehension(self, node):
        first = node.generators[0]
        first.iter = self.visit(first.iter)
        self.frames.append((set(_stores([g.target for g in node.generators])), _Body([])))
        for g in node.generators:
            if g is not first:
                g.iter = self.visit(g.iter)
            g.ifs = [self.visit(c) for c in g.ifs]
        for name in ("elt", "key", "value"):
            if hasattr(node, name):
                setattr(node, name, self.visit(getattr(node, name)))
        self.frames.pop()
        if isinstance(node, ast.SetComp):
            return self._as_set(ast.copy_location(ast.ListComp(node.elt, node.generators), node), node)
        return node

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def _loop(self, node):
        targets = set()
        if isinstance(node, ast.While):
            node.test = self.visit(node.test)
        else:
            node.iter = self.visit(node.iter)
            targets = set(_stores([node.target]))
        self.frames.append((targets, _Body(node.body)))
        node.body = [self.visit(s) for s in node.body]
        self.frames.pop()
        node.orelse = [self.visit(s) for s in node.orelse]
        return node

    visit_For = visit_AsyncFor = visit_While = _loop

    def _kept(self, node):
        """Names of the loops around this function that it should keep: targets, and what the loop's body certainly
        set before it and does not set again after it."""
        here = (node.lineno, node.col_offset)
        kept = set()
        for targets, body in self.frames:
            last = body.last
            kept |= {n for n in targets | body.set_before(here) if last.get(n, (0, 0)) <= here}
            kept -= {n for n, place in last.items() if place > here}
        return kept

    def _function(self, node):
        kept = self._kept(node) if self.frames else set()
        body = node.body if isinstance(node.body, list) else [node.body]
        if kept:
            a = node.args
            params = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if p is not None}
            used = {n.id for b in body for n in ast.walk(b) if isinstance(n, ast.Name)}
            own = {getattr(node, "name", None), *params, *_stores(body, into_functions=True)}
            for name in sorted((kept & used) - own):
                a.kwonlyargs.append(ast.copy_location(ast.arg(name, None), node))
                a.kw_defaults.append(ast.copy_location(ast.Name(name, ast.Load()), node))
        outer, self.frames = self.frames, []
        body = [self.visit(s) for s in body]
        node.body = body if isinstance(node.body, list) else body[0]
        self.frames = outer
        node.args = self.visit(node.args)
        if not isinstance(node, ast.Lambda):
            node.decorator_list = [self.visit(d) for d in node.decorator_list]
        return node

    visit_Lambda = visit_FunctionDef = visit_AsyncFunctionDef = _function


def build(source, filename):
    """The script as a code object that defines _wf_main. The API names are its parameters: `args = args or {}` at the
    top of a script would otherwise make `args` an unbound local."""
    tree = ast.parse(source, filename)
    shell = ast.parse("async def _wf_main(" + ", ".join(API) + "): pass", filename)
    body = [_Prepare().visit(s) for s in tree.body]
    if body:
        shell.body[0].body = body
    return compile(ast.fix_missing_locations(shell), filename, "exec", dont_inherit=True)


# What scripts see ---------------------------------------------------------------------------------------------------

def _stable(value):
    return type(value).__name__, repr(value)


def _fixed_order(base):
    """A set type that iterates in a fixed order."""
    def wrapped(name):
        method = getattr(base, name)

        def call(self, *others):
            out = method(self, *others)
            return out if out is NotImplemented else cls(out)
        return call

    def pop(self):
        for v in self:
            base.discard(self, v)
            return v
        raise KeyError("pop from an empty set")

    members = {"__slots__": (), "__iter__": lambda self: iter(sorted(base.__iter__(self), key=_stable))}
    for name in ("union", "intersection", "difference", "symmetric_difference", "copy", "__or__", "__and__", "__sub__",
                 "__xor__", "__ror__", "__rand__", "__rsub__", "__rxor__"):
        members[name] = wrapped(name)
    if base is set:
        members["pop"] = pop
    cls = type(base.__name__, (base,), members)
    return cls


Set, FrozenSet = _fixed_order(set), _fixed_order(frozenset)


def _fix(value):
    return Set(value) if type(value) is set else value


class WorkflowError(RuntimeError):
    """agent()/workflow() refused the call: wrong options, agent limit, token budget."""


class _Names:
    """A few functions under one name (json, re), without the module behind them."""
    __slots__ = ("_names",)

    def __init__(self, names):
        object.__setattr__(self, "_names", names)

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_names")[name]
        except KeyError:
            raise AttributeError(f"{name} is not available in workflow scripts") from None

    def __setattr__(self, name, value):
        raise AttributeError("read-only")


def _dumps(obj, *, indent=None, ensure_ascii=False, sort_keys=False, separators=None, default=None):
    def plain(v):
        if isinstance(v, (set, frozenset)):
            return list(v)
        if default is not None:
            return default(v)
        raise TypeError(f"{type(v).__name__} is not JSON data")
    return json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys, separators=separators,
                      default=plain)


JSON = _Names({"dumps": _dumps, "loads": lambda s: json.loads(s)})
RE = _Names({**{n: getattr(re, n) for n in ("compile", "search", "match", "fullmatch", "findall", "finditer", "sub",
                                            "subn", "split", "escape")},
             **{n: int(getattr(re, n)) for n in ("I", "IGNORECASE", "M", "MULTILINE", "S", "DOTALL", "X", "VERBOSE",
                                                 "A", "ASCII")}})
BUILTINS = {n: getattr(builtins, n) for n in (
    "abs all any ascii bin bool bytes callable chr dict divmod enumerate filter float hex int isinstance issubclass "
    "iter len list map max min next oct ord pow range repr reversed round slice sorted str sum tuple zip "
    "Exception ArithmeticError AssertionError AttributeError IndexError KeyError LookupError NameError "
    "NotImplementedError OverflowError RecursionError RuntimeError StopAsyncIteration StopIteration TimeoutError "
    "TypeError UnicodeError ValueError ZeroDivisionError").split()}
BUILTINS.update(set=Set, frozenset=FrozenSet)


def _depth(value):
    level, deep = [value], 0
    while level:
        deep += 1
        level = [v for x in level if isinstance(x, (dict, list)) for v in (x.values() if isinstance(x, dict) else x)]
    return deep


# Scheduler ----------------------------------------------------------------------------------------------------------

class _Future:
    """Something a script can await. agent() and the others start their work when called, as promises do."""
    __slots__ = ("_done", "_value", "_error", "_waiters", "_name", "_host")

    def __init__(self, host, name):
        self._host, self._name = host, name
        self._done, self._value, self._error, self._waiters = False, None, None, []

    def __await__(self):
        if not self._done:
            yield self
        if self._error is not None:
            raise self._error
        return self._value

    def __repr__(self):
        return f"<{self._name}: not awaited>"

    def _settle(self, value=None, error=None):
        self._done, self._value, self._error = True, value, error
        self._host.ready.extend(self._waiters)
        self._waiters = []


class _Task(_Future):
    __slots__ = ("_coro",)

    def __init__(self, host, coro, name):
        super().__init__(host, name)
        self._coro = coro
        host.ready.append(self)

    def _step(self):
        try:
            wanted = self._coro.send(None)
        except StopIteration as e:
            self._settle(e.value)
        except BaseException as e:  # noqa: BLE001 - whatever the script raised belongs to whoever awaits it
            self._settle(error=e)
        else:
            if not isinstance(wanted, _Future):
                self._settle(error=TypeError("only agent(), parallel(), pipeline(), workflow() and your own async "
                                             "functions can be awaited"))
            elif wanted._done:
                self._host.ready.append(self)
            else:
                wanted._waiters.append(self)


def _awaitable(value):
    return isinstance(value, (_Future, types.CoroutineType))


class _Budget:
    __slots__ = ("_host",)

    def __init__(self, host):
        self._host = host

    @property
    def total(self):
        return self._host.budget

    def spent(self):
        return self._host.spent

    def remaining(self):
        return float("inf") if self._host.budget is None else max(0, self._host.budget - self._host.spent)


def _stage_args(stage):
    """How many of (prev, item, index) a pipeline stage takes. Parameters with a default are the script's own
    (`lambda prev, f=f: ...`), they are not filled in."""
    if not callable(stage):
        raise TypeError(f"pipeline() stages must be functions, got {type(stage).__name__}")
    code = getattr(stage, "__code__", None)
    if code is None:
        return 1  # a builtin such as str or len
    if code.co_flags & 0x04:  # *args
        return 3
    needed = code.co_argcount - len(stage.__defaults__ or ())
    if needed > 3:
        raise TypeError("a pipeline() stage takes (prev), (prev, item) or (prev, item, index)")
    return max(needed, min(1, code.co_argcount))


class Host:
    def __init__(self, out):
        self.out = out
        self.inbox = queue.SimpleQueue()
        self.held = collections.deque()  # messages that arrived while waiting for a workflow's source
        self.ready = collections.deque()
        self.calls = {}  # id -> _Future
        self.ids = self.notes = self.seen = 0
        self.budget, self.spent, self.ack = None, 0, False
        self.sources = {}  # file name in tracebacks -> (what to call it, source lines)

    def send(self, message):
        line = json.dumps(message).encode("ascii") + b"\n"
        if len(line) > MAX_LINE:
            raise ValueError(f"too large ({len(line) >> 20} MB, at most {MAX_LINE >> 20} MB)")
        self.out.write(line)
        self.out.flush()

    def describe(self, e):
        """The exception with the script line it came from."""
        text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        if isinstance(e, MemoryError):
            text = f"MemoryError: the script used more than {MEMORY >> 20} MB"
        where, tb = "", e.__traceback__
        while tb is not None:
            known = self.sources.get(tb.tb_frame.f_code.co_filename)
            if known:
                name, lines = known
                code = lines[tb.tb_lineno - 1].strip()[:200] if 0 < tb.tb_lineno <= len(lines) else ""
                where = f" ({name}line {tb.tb_lineno}: {code})"
            tb = tb.tb_next
        return text[:4000] + where

    def note(self, message):
        self.notes += 1
        if self.notes <= MAX_LOGS:  # the rest is dropped: a loop that logs must not flood ALICE
            self.send(message)

    def log(self, message):
        self.note({"t": "log", "text": str(message)[:2000]})

    def new_id(self):
        self.ids += 1
        if self.ids > MAX_CALLS:
            raise WorkflowError(f"more than {MAX_CALLS} calls in one workflow")
        return self.ids

    def load(self, source, name, nested, phase=""):
        """The main function of a script, with the API bound to it (each script has its own current phase)."""
        filename = f"<workflow {name}>" if nested else "<workflow>"
        self.sources[filename] = (f"workflow {name}, " if nested else "", source.splitlines())
        space = {"__builtins__": dict(BUILTINS), "__name__": "workflow", "_wf_set": Set, "_wf_fix": _fix}
        space["__builtins__"]["print"] = lambda *a, sep=" ", **_: self.log(sep.join(map(str, a)))
        exec(build(source, filename), space)
        now = {"phase": phase}

        def agent(prompt, *, label=None, phase=None, schema=None, model=None, effort=None, isolation=None,
                  agent_type=None):
            if not isinstance(prompt, str) or not prompt.strip():
                raise TypeError("agent() needs a prompt (a non-empty string)")
            if len(prompt) > MAX_PROMPT:
                raise ValueError(f"the prompt is too long ({len(prompt)} characters, at most {MAX_PROMPT})")
            opts = {"label": label, "phase": phase, "schema": schema, "model": model, "effort": effort,
                    "isolation": isolation, "agent_type": agent_type}
            for key, value in opts.items():
                if key != "schema" and value is not None and not isinstance(value, str):
                    raise TypeError(f"agent() {key} must be a string, got {type(value).__name__}")
            if schema is not None and not isinstance(schema, dict):
                raise TypeError(f"agent() schema must be a dict (JSON Schema), got {type(schema).__name__}")
            call = self.new_id()
            self.send({"t": "agent", "id": call, "prompt": prompt, "at": now["phase"],
                       "opts": {k: v for k, v in opts.items() if v is not None}})
            self.calls[call] = future = _Future(self, f"agent() call {call}")
            return future

        async def guarded(what, run):
            try:
                value = run()
                return await value if _awaitable(value) else value
            except Exception as e:  # noqa: BLE001 - a failing item gives None, the others go on
                self.log(f"{what} failed: {self.describe(e)}")
                return None

        async def gather(tasks):
            return [await t for t in tasks]

        def checked(items, what):
            items = list(items)
            if len(items) > MAX_ITEMS:
                raise ValueError(f"{what}() takes at most {MAX_ITEMS} items, got {len(items)}")
            return items

        def parallel(thunks):
            thunks = checked(thunks, "parallel")
            for t in thunks:
                if not callable(t) and not _awaitable(t):
                    raise TypeError("parallel() takes a list of functions, parallel([lambda: agent(...), ...]), got "
                                    + type(t).__name__)
            tasks = [_Task(self, guarded(f"parallel() item {i + 1}", t if callable(t) else lambda t=t: t), "item")
                     for i, t in enumerate(thunks)]
            return _Task(self, gather(tasks), "parallel()")

        def pipeline(items, *stages):
            items = checked(items, "pipeline")
            takes = [(s, _stage_args(s)) for s in stages]

            async def one(item, index):
                prev = item
                for stage, n in takes:
                    prev = stage(*(prev, item, index)[:n])
                    if _awaitable(prev):
                        prev = await prev
                return prev

            tasks = [_Task(self, guarded(f"pipeline() item {i + 1}", lambda it=it, i=i: one(it, i)), "item")
                     for i, it in enumerate(items)]
            return _Task(self, gather(tasks), "pipeline()")

        def phase_(title):
            title = str(title)[:200]
            if title != now["phase"]:
                now["phase"] = title
                self.note({"t": "phase", "title": title})

        def workflow(name, args=None):
            if nested:
                raise WorkflowError("a nested workflow cannot start another one (one level only)")
            if not isinstance(name, str) or not name.strip():
                raise TypeError("workflow() needs the name or path of a workflow")
            child_args = json.loads(json.dumps(args))
            self.send({"t": "workflow", "id": self.new_id(), "name": name})
            # Waits without running anything else, so the order of calls cannot depend on when the answer comes.
            while True:
                reply = self.inbox.get()
                if reply["t"] == "workflow":
                    break
                self.held.append(reply)
            if reply["error"]:
                raise WorkflowError(reply["error"])
            main = self.load(reply["source"], reply["name"], True, now["phase"] or reply["name"])
            return _Task(self, main(child_args), f"workflow({name!r})")

        def main(args):
            return space["_wf_main"](agent, parallel, pipeline, phase_, self.log, workflow, args, _Budget(self), JSON,
                                     RE)
        return main

    def handle(self, message):
        self.seen += 1
        self.spent = message["spent"]
        future = self.calls.pop(message["id"], None)
        error = message.get("error")
        if future is not None:
            future._settle(message.get("value"), WorkflowError(error) if error else None)

    def finish(self, task):
        if task._error is not None:
            return {"t": "failed", "error": self.describe(task._error)}

        def plain(v):
            if isinstance(v, (set, frozenset)):
                return list(v)
            if _awaitable(v):
                raise TypeError("the returned data holds a call that was not awaited: use await agent(...)")
            raise TypeError(f"a workflow must return JSON data, not {type(v).__name__}")
        try:
            value = json.loads(json.dumps(task._value, default=plain))
            if _depth(value) > MAX_DEPTH:
                raise ValueError(f"the returned data is nested more than {MAX_DEPTH} levels")
            return {"t": "done", "value": value}
        except (TypeError, ValueError, RecursionError) as e:
            return {"t": "failed", "error": f"{type(e).__name__}: {e}"}

    def run(self):
        start = self.inbox.get()
        self.budget, self.spent, self.ack = start["budget"], start["spent"], start["ack"]
        try:
            task = _Task(self, self.load(start["source"], start["name"], False)(start["args"]), "workflow")
        except Exception as e:  # noqa: BLE001
            self.send({"t": "failed", "error": self.describe(e)})
            return
        while True:
            while self.ready:
                self.ready.popleft()._step()
                if task._done:  # what is still going on does not count any more
                    break
            if task._done:
                try:
                    self.send(self.finish(task))
                except ValueError as e:
                    self.send({"t": "failed", "error": f"the returned data is {e}"})
                return
            if self.ack:
                self.send({"t": "idle", "seen": self.seen})
            if not self.calls:
                self.send({"t": "failed", "error": "the script waits for something that can never finish"})
                return
            self.handle(self.held.popleft() if self.held else self.inbox.get())


def _limit():
    """Bounds memory (on Windows the engine's job object does) and forbids new processes and files. Best effort."""
    try:
        import resource
    except ImportError:
        return
    for name, value in (("RLIMIT_AS", MEMORY), ("RLIMIT_CORE", 0), ("RLIMIT_FSIZE", 0), ("RLIMIT_NPROC", 0)):
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except (AttributeError, ValueError, OSError):
            pass


def main():
    host = Host(sys.stdout.buffer)
    sys.stdout = sys.stderr  # re.compile(..., re.DEBUG) prints, and stdout carries the protocol
    stdin = sys.stdin.buffer

    def read():
        while True:
            line = stdin.readline(2 * MAX_LINE)  # what the engine sends is bounded, but by characters
            if not line.endswith(b"\n"):
                os._exit(0)  # ALICE is gone; also ends a script that never stops by itself
            host.inbox.put(json.loads(line))

    threading.Thread(target=read, daemon=True).start()
    _limit()
    host.send({"t": "hello", "pid": os.getpid()})
    code = 0
    try:
        host.run()
    except BrokenPipeError:
        pass
    except BaseException as e:  # noqa: BLE001 - the engine shows stderr when the process ends without a result
        code = 1
        try:
            sys.stderr.write(f"{type(e).__name__}: {e}\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


if __name__ == "__main__":
    main()
