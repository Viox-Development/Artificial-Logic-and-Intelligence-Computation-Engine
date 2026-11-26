"""The workflow engine (alice/workflow): script rules, the script's process, agents, resume. Fake spawns, no models."""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from alice.workflow import engine, host
from alice.workflow.engine import AgentCall, AgentResult, launch
from alice.workflow.schema import check, schema_problems
from alice.workflow.script import ScriptError, parse

META = 'META = {"name": "t", "description": "d"}\n'


def echo(call: AgentCall, cancel, progress) -> AgentResult:
    return AgentResult(True, text=f"<{call.prompt}>", tokens=10)


def go(body: str, spawn=echo, timeout: float = 20, meta: str = META, **kw) -> engine.Run:
    run = launch(meta + body, spawn, **kw)
    assert run.wait(timeout), run.snapshot()
    return run


def all_fail(cases) -> None:
    """Each script fails with the given error (started together: most of the time is the start of the processes)."""
    runs = [(code, error, launch(META + code, echo)) for code, error in cases]
    for code, error, run in runs:
        assert run.wait(30) and run.status == "failed" and error in run.error, (code, run.status, run.error)
        assert agents(run) == [] or "schema" not in code


def value(body: str, spawn=echo, **kw):
    run = go(body, spawn, **kw)
    assert run.status == "done", run.error
    return run.value


def agents(run: engine.Run) -> list[dict]:
    return sorted((a for p in run.snapshot()["phases"] for a in p["agents"]), key=lambda a: a["n"])


def until(cond, timeout: float = 10) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def leftovers(run: engine.Run) -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith(f"workflow-{run.id}-")]


# Script rules ---------------------------------------------------------------------------------------------------

def test_meta():
    m = parse('"""doc"""\nMETA = {"name": "Über", "description": "d", "phases": [{"title": "A", "detail": "x"}, "B"], '
              '"when_to_use": "w"}\nreturn 1')
    assert (m.name, m.description, m.when_to_use) == ("Über", "d", "w")
    assert m.phases == [{"title": "A", "detail": "x"}, {"title": "B"}]


@pytest.mark.parametrize("source", [
    "", "   ", "return 1", "x = 1\n" + META, 'META = {"name": "x"}', 'META = {"name": "", "description": "d"}',
    'META = {"name": f"x", "description": "d" + "e"}', 'META = dict(name="x", description="d")', "META = [1]",
    'META = {"name": "x", "description": "d", "phases": [{"detail": "no title"}]}',
    'META = {"name": "x", "description": "d", "phases": 3}', 'META = {"name": "x", "description": "d", "phases": [1]}',
    META + "x = (", META + "x = 1\x00", META + "lambda: await agent('x')", META + "yield 1", META + "#" * 600_000,
    META + "x = " + "(" * 5000 + ")" * 5000, META + "x = " + "-" * 20000 + "1", META + 's = "\ud800"',
    META + "x = " + "+".join(["1"] * 60000),
    META + "x = (" + "\n+".join(["1"] * 9000), META + "x = f'{" + "+".join(["1"] * 60000) + "}'",
    META + "x = a" + ".b" * 60000,
    META + "x = a" + "()" * 60000, META + "x = " + "not " * 60000 + "1", META + "x = " + "lambda: " * 30000 + "1",
    META + "if a: pass\n" + "elif a: pass\n" * 5000, META + "x = " + "1 if a else " * 20000 + "1",
    META + "x = " + "[" * 5000,
    META + "x = f'{f\"{1" + "+1" * 30000 + "}\"}'", META + "x = a" + "[0]" * 60000,
    META + "x = " + "await " * 30000 + "a",
], ids=itertools.count())
def test_bad_scripts(source):
    with pytest.raises(ScriptError):
        parse(source)


@pytest.mark.parametrize("code", [
    "import os", "from os import path", "class A: pass", "def f():\n  global x\n  x = 1",
    "def f():\n  x = 1\n  def g():\n    nonlocal x", "x = __import__", "x = _private", "args.__class__",
    "x = [].__class__",
    "eval('1')", "exec('1')", "compile('1', '', 'eval')", "open('x')", "input()", "getattr(args, 'x')",
    "setattr(args, 'x', 1)",
    "delattr(args, 'x')", "vars()", "globals()", "locals()", "type(1)", "object()", "super()", "breakpoint()",
    "'{0.x}'.format(1)", "'{x}'.format_map({})", "f = str.format", "g = (x for x in [])\ng.gi_frame", "x.cr_frame",
    "x.f_back", "x.f_globals", "x.tb_frame", "x.ag_frame", "x.func_globals", "str.mro()", "x.co_code", "x.gi_code",
    "match args:\n  case str(__class__=c): pass", "match args:\n  case {**_rest}: pass",
    "match args:\n  case [*_x]: pass",
    "match args:\n  case _x: pass", "def f(_x): pass", "def _f(): pass", "lambda __x: 1", "f(__x=1)",
    "try:\n  pass\nexcept Exception as _e: pass", "with agent as x: pass", "x = ｅｖａｌ", "x.ｍｒｏ", "x = [].＿＿class＿＿",
    "for _x in []: pass", "(_y := 1)", "f'{args.__class__}'", "f'{1:{args.__class__}}'", "del __builtins__",
    "async def f():\n  async with x: pass", "def f(x=().__class__): pass", "@x.__class__\ndef f(): pass",
])
def test_sandbox_rules(code):
    with pytest.raises(ScriptError):
        parse(META + code)


def test_long_scripts_that_are_not_deep_are_fine():
    data = ", ".join('{"a": 1 + 2, "b": [f(x) for x in "y"]}' for _ in range(2000))
    prompt = "- a. b (c) [d] {{\"x\": 1.5}}; " * 500
    parse(META + f"def f(x): return x\nx = [{data}]\ny = f'{prompt}{{x}}'\n" + "log(str(1) + 'a')\n" * 2000)


def test_rules_give_the_line():
    with pytest.raises(ScriptError, match="line 3: imports are not allowed"):
        parse(META + "x = 1\nimport os")


def test_ordinary_code_is_allowed():
    assert value("""
for _ in range(2):
    pass
def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)
def gen(n):
    for i in range(n):
        yield i
async def twice(x):
    return [await agent(x), await agent(x)]
match args:
    case {"a": [first, *_]}:
        found = first
try:
    int("x")
except ValueError as e:
    caught = str(e)
args = args or {}
print("a", 1, sep="-")
return {"fib": fib(10), "gen": sum(gen(4)), "twice": await twice("q"), "found": found, "caught": "invalid" in caught,
        "re": re.sub(r"(\\d+)", lambda m: m.group(1) * 2, "a1"), "flags": bool(re.search("A", "a", re.I | re.M)),
        "json": json.loads(json.dumps({"ü": {1}})), "f": f"{3.14159:.2f}", "inf": budget.remaining() == float("inf")}
""", args={"a": [7, 8]}) == {"fib": 55, "gen": 6, "twice": ["<q>", "<q>"], "found": 7, "caught": True, "re": "a11",
                             "flags": True, "json": {"ü": [1]}, "f": "3.14", "inf": True}


def test_names_that_do_not_exist():
    all_fail((c, "Error") for c in ("json.codecs", "re.enum", "hasattr(1, 'x')", "id(1)", "dir()", "memoryview",
                                    "BaseException", "SystemExit", "exit()", "json.dumps = 1", "budget.total = 5"))


def test_the_process_cannot_reach_alice_or_secrets(monkeypatch, tmp_path):
    """Even without the rules of script.py."""
    monkeypatch.setenv("SECRET_API_KEY", "hunter2")
    code = ("import sys\nsys.path\ntry:\n  import alice\n  print('alice')\nexcept ImportError:\n  pass\n"
            "try:\n  import psutil\n  print('site')\nexcept ImportError:\n  pass\n"
            "import os\nprint('secret' if any('hunter2' in v for v in os.environ.values()) else 'clean')")
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code], env=engine._environment(), cwd=tmp_path,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "clean", out.stderr


# parallel and pipeline ------------------------------------------------------------------------------------------

def test_parallel_keeps_order_and_failing_items_are_none():
    def spawn(call, cancel, progress):
        if call.prompt == "bad":
            return AgentResult(False, error="no luck")
        if call.prompt == "boom":
            raise RuntimeError("spawn broke")
        time.sleep(0.15 if call.prompt == "slow" else 0)
        return AgentResult(True, text=call.prompt)

    run = go("""
def thunk():
    raise ValueError("inside")
return await parallel([lambda: agent("slow"), lambda: agent("bad"), lambda: agent("boom"), thunk, lambda: agent("x"),
                       lambda: 5, agent("early"), lambda: parallel([lambda: agent("in")])])
""", spawn)
    assert run.value == ["slow", None, None, None, "x", 5, "early", ["in"]]
    log = "\n".join(run.snapshot()["log"])
    assert "no luck" in log and "spawn broke" in log and "item 4 failed: ValueError: inside (line 4" in log
    assert [a["status"] for a in agents(run)] == ["done", "done", "failed", "failed", "done", "done"]


def test_parallel_is_a_barrier_and_runs_at_the_same_time():
    gate = threading.Barrier(3, timeout=10)

    def spawn(call, cancel, progress):
        gate.wait()
        return AgentResult(True, text=call.prompt)

    assert value('return await parallel([lambda: agent(x) for x in ["a", "b", "c"]])', spawn,
                 concurrency=3) == ["a", "b", "c"]


def test_pipeline_has_no_barrier():
    """The fast item finishes both stages while the slow one is still in its first."""
    fast_done = threading.Event()

    def spawn(call, cancel, progress):
        if call.prompt == "1 slow":
            assert fast_done.wait(10)
        if call.prompt == "2 <1 fast> fast 1":
            fast_done.set()
        return echo(call, cancel, progress)

    assert value("""
return await pipeline(["slow", "fast"], lambda p: agent(f"1 {p}"), lambda p, item, i: agent(f"2 {p} {item} {i}"),
                      lambda p, item: p.upper() + item)
""", spawn) == ["<2 <1 SLOW> SLOW 0>slow", "<2 <1 FAST> FAST 1>fast"]


def test_pipeline_stages():
    run = go("""
async def check(prev, item):
    if item == 2:
        raise ValueError("dropped")
    return await agent(str(prev))
return [await pipeline([1, 2, 3], lambda p: p * 10, check, str.upper, lambda *all: len(all)),
        await pipeline([1], lambda p, f="kept": f), await pipeline([], check), await pipeline([4])]
""")
    assert run.value == [[3, None, 3], ["kept"], [], [4]]
    assert len(agents(run)) == 2


def test_misuse_fails_with_a_reason():
    all_fail([
        ("await parallel([lambda: 1] * 4097)", "at most 4096"), ("await pipeline(range(4097), str)", "at most 4096"),
        ("await parallel([1])", "list of functions"), ("await pipeline([1], 5)", "must be functions"),
        ("await pipeline([1], lambda a, b, c, d: 1)", "takes"), ("await agent('')", "prompt"),
        ("await agent(5)", "prompt"),
        ("await agent('x', label=5)", "label must be a string"),
        ("await agent('x', schema=[])", "schema must be a dict"),
        ("await agent('x', nope=1)", "nope"), ("await agent('x' * 2_000_000)", "too long"), ("await 5", "await"),
        ("return agent('x')", "not awaited"), ("return [agent('x')]", "not awaited"), ("return lambda: 1", "JSON data"),
        ("return {(1, 2): 3}", "keys"), ("x = []\nx.append(x)\nreturn x", "ircular"),
        ("return 'x' * 9_000_000", "too large"),
        ("async def f():\n  return await box[0]\nbox = []\nbox.append(parallel([f]))\nawait box[0]", "never finish"),
    ])


def test_functions_made_in_loops_keep_their_values():
    assert value("""
out = []
out.append(await parallel([lambda: agent(f) for f in "ab"]))
thunks = []
for i, f in enumerate("ab"):
    prompt = f + str(i)
    async def one():
        return await agent("def " + prompt)
    if i >= 0:
        deep = prompt + "!"
        thunks.append(lambda: agent(deep))
    thunks.append(lambda: agent(prompt))
    thunks.append(one)
out.append(await parallel(thunks))
out.append(await parallel([lambda: agent(f"{a}{b}") for a in "xy" for b in "zw" if a != "y"]))
n = 0
while n < 2:
    maybe = lambda: late      # set below: must stay late
    late = n
    n += 1
out.append(maybe())
out.append([f() for f in [lambda: (lambda: k)() for k in [1, 2]]])
out.append({k: v() for k, v in {k: (lambda: k) for k in "pq"}.items()})
return out
""") == [["<a>", "<b>"], ["<a0!>", "<a0>", "<def a0>", "<b1!>", "<b1>", "<def b1>"], ["<xz>", "<xw>"], 1, [1, 2],
         {"p": "p", "q": "q"}]


def test_sets_have_a_fixed_order():
    body = """
a = {"delta", "alpha", "charlie", "bravo"}
b = set(["echo", "alpha"]) | {x + "!" for x in a}
a.add("zulu")
return [list(a), list(b), [x for x in a & b], a.pop(), "alpha" in a, len(a), list(frozenset("hgf")), sorted(a - b),
        a == {"zulu", "delta", "charlie", "bravo"}, isinstance(a, set), {"k": a}]
"""
    first = value(body)
    assert first[0] == ["alpha", "bravo", "charlie", "delta", "zulu"] and first[3:6] == ["alpha", False, 4]
    assert first[8:10] == [True, True] and first[10]["k"] == ["bravo", "charlie", "delta", "zulu"]
    assert value(body) == first


# Limits ---------------------------------------------------------------------------------------------------------

def test_concurrency_cap():
    now, peak, lock = [0], [0], threading.Lock()

    def spawn(call, cancel, progress):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.03)
        with lock:
            now[0] -= 1
        return AgentResult(True, text="x")

    assert value("return len(await parallel([lambda: agent(str(i)) for i in range(12)]))", spawn, concurrency=3) == 12
    assert peak[0] == 3


def test_agent_cap():
    run = go("""
got = await parallel([lambda: agent(str(i)) for i in range(5)])
try:
    await agent("one more")
except Exception as e:
    return [got, str(e)]
""", max_agents=3)
    assert run.value == [["<0>", "<1>", "<2>", None, None], "more than 3 agents in one workflow"]
    assert len(agents(run)) == 3


def test_a_flood_of_calls_ends():
    run = go("while True:\n  agent('x')", max_agents=2)
    assert run.status == "failed" and "more than 10000 calls" in run.error
    run = go("for i in range(50_000):\n  log(i)\n  phase(str(i))\nreturn 1")
    assert run.value == 1 and len(run.snapshot()["log"]) <= engine.LOG_LINES


def test_budget_is_a_hard_ceiling():
    run = go("""
out = [budget.total, budget.spent(), budget.remaining()]
await agent("a")
out += [budget.spent(), budget.remaining()]
await agent("b")
out.append(budget.remaining())
try:
    await agent("c")
except Exception as e:
    out.append(str(e))
return out
""", budget=15)
    assert run.value == [15, 0, 15, 10, 5, 0, "the token budget is used up (20 of 15)"]
    assert run.spent == run.snapshot()["spent"] == 20


def test_budget_stops_calls_that_already_wait():
    def spawn(call, cancel, progress):
        time.sleep(0.05)
        return echo(call, cancel, progress)

    run = go("return await parallel([lambda: agent(str(i)) for i in range(6)])", spawn, budget=25, concurrency=1)
    assert run.value == ["<0>", "<1>", "<2>", None, None, None]
    assert [a["status"] for a in agents(run)] == ["done"] * 3 + ["failed"] * 3
    assert launch(META, echo, budget=0).snapshot()["budget"] is None


def test_budget_counts_agents_at_work():
    both, seen = threading.Barrier(2, timeout=10), threading.Event()

    def spawn(call, cancel, progress):
        both.wait()
        if call.prompt == "big":
            progress({"tokens": 100, "steps": 2, "last": "bash ls", "model": "m1"})
            seen.set()
            time.sleep(0.2)
        else:
            assert seen.wait(5)
        return AgentResult(True, text="x", tokens=1)

    run = go("big = agent('big')\nsmall = agent('small')\nawait small\ntry:\n  await agent('late')\n"
             "except Exception as e:\n  "
             "return [str(e), await big]", spawn, budget=50, concurrency=2)
    assert run.value == ["the token budget is used up (101 of 50)", "x"]
    big = agents(run)[0]
    assert (big["tokens"], big["steps"], big["last"], big["model"]) == (100, 2, "bash ls", "m1")


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS does not enforce RLIMIT_AS")
def test_memory_is_bounded():
    run = go("x = []\nwhile True:\n  x.append('a' * 50_000_000)")
    assert run.status == "failed" and "MemoryError" in run.error, run.error


# Schemas --------------------------------------------------------------------------------------------------------

def test_structured_answers():
    def spawn(call, cancel, progress):
        return AgentResult(True, text="ignored",
                           value={"good": {"n": 1}, "bad": {"n": "x"}, "odd": {1, 2}}[call.prompt])

    run = go("""
s = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
return await parallel([lambda: agent(p, schema=s) for p in ["good", "bad", "odd"]])
""", spawn)
    assert run.value == [{"n": 1}, None, None]
    assert "n: must be integer, got string" in "\n".join(run.snapshot()["log"])


def test_bad_schemas_raise():
    all_fail((f"await agent('x', schema={s})", e) for s, e in [
        ('{"type": "array"}', "root must be"), ("{}", "root must be"),
        ('{"type": "object", "required": ["x"]}', "no such property: x"),
        ('{"type": "object", "properties": {"a": {"type": "str"}}}', "unknown type"),
        ('{"type": "object", "properties": {"a": {"$ref": "#"}}}', "$ref"),
        ('{"type": "object", "properties": {"a": {"pattern": "("}}}', "pattern"),
    ])


OBJ = {"type": "object", "properties": {"a": {"type": ["string", "null"]}, "b": {"items": {"type": "integer"}}},
       "required": ["a"], "additionalProperties": False}


@pytest.mark.parametrize("schema, good, bad", [
    (OBJ, [{"a": None}, {"a": "x", "b": [1, 2.0]}],
     [{}, {"a": 1}, {"a": "x", "c": 1}, {"a": "x", "b": [1, True]}, [], None]),
    ({"type": "integer"}, [1, -3, 2.0], [True, 1.5, "1", None]),
    ({"type": "number"}, [1, 1.5], [True, "1", float("nan"), float("inf")]),
    ({"type": "boolean"}, [True], [1, "true"]),
    ({"type": "null"}, [None], [0, False]),
    ({"enum": [1, "a", None, [1]]}, [1, 1.0, "a", None, [1]], [True, "b", [True]]),
    ({"const": {"x": 1}}, [{"x": 1}], [{"x": True}, {"x": 1, "y": 2}]),
    ({"minItems": 1, "maxItems": 2}, [[1], [1, 2], "text"], [[], [1, 2, 3]]),
    ({"minimum": 0, "maximum": 1}, [0, 0.5, 1, "x"], [-1, 2]),
    ({"minLength": 1, "maxLength": 2, "pattern": "^[a-zü]+$"}, ["a", "ü"], ["", "abc", "A"]),
    ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, ["a", 1], [None, 1.5]),
    ({"oneOf": [{"minimum": 0}, {"maximum": 10}]}, [-1, 11], [5]),
    ({"allOf": [{"minimum": 0}, {"maximum": 10}]}, [5], [-1, 11]),
    ({"additionalProperties": {"type": "integer"}}, [{"x": 1}], [{"x": "1"}]),
    (True, [1, None], []), (False, [], [1]), ({}, [1, {}], []),
    ({"pattern": "^[a-z]+(-[a-z]+)*$"}, ["a-b-c"], ["a--b"]),  # not obviously cheap: checked in a process
])
def test_check(schema, good, bad):
    for v in good:
        assert check(schema, v) == [], v
    for v in bad:
        assert check(schema, v), v


def test_problems_say_where():
    s = {"type": "object", "properties": {"list": {"type": "array", "items": OBJ}}}
    assert check(s, {"list": [{"a": "x"}, {"a": 5}]}) == ["list[1].a: must be string or null, got integer 5"]
    assert check(s, {"list": [{"z": "x"}]}) == ["list[0]: 'a' is missing",
                                                "list[0].z: not allowed here (allowed: a, b)"]
    deep: list = []
    for _ in range(3000):
        deep = [deep]
    loop: dict = {"items": None}
    loop["items"] = loop
    assert check(loop, deep)
    assert len(check({"items": {"type": "string"}}, [1] * 500)) <= 20
    assert schema_problems(OBJ) == []


def test_an_explosive_pattern_does_not_freeze_alice(monkeypatch):
    from alice.workflow import schema

    monkeypatch.setattr(schema, "PATTERN_SECONDS", 0.5)
    start = time.monotonic()
    assert "too long" in check({"pattern": "^(a+)+$"}, "a" * 40 + "!")[0]
    assert "too long" in check({"pattern": "^(a|a)*$"}, "a" * 40 + "!")[0]
    assert time.monotonic() - start < 5


# Stop -----------------------------------------------------------------------------------------------------------

def test_stop_while_agents_run():
    started, cancelled = threading.Semaphore(0), []

    def spawn(call, cancel, progress):
        started.release()
        assert cancel.wait(10)
        cancelled.append(call.n)
        return AgentResult(False, tokens=7)

    run = launch(META + "return await parallel([lambda: agent(str(i)) for i in range(5)])", spawn, concurrency=2)
    assert started.acquire(timeout=10) and started.acquire(timeout=10) and until(lambda: len(agents(run)) == 5)
    proc = run._proc
    run.stop()
    assert run.status == "stopped"
    assert run.wait(10)
    assert sorted(cancelled) == [1, 2] and run.value is None and run.spent == 14
    assert [a["status"] for a in agents(run)] == ["stopped"] * 5
    assert proc.poll() is not None and leftovers(run) == []
    run.stop()  # again: nothing happens
    assert run.status == "stopped"


@pytest.mark.parametrize("code", ["while True:\n  pass", "x = 10 ** (10 ** 8)", "re.search('(a+)+$', 'a' * 60 + '!')",
                                  "while True:\n  log('x' * 100)",
                                  "while True:\n  try:\n    agent('x')\n  except:\n    pass"])
def test_stop_an_endless_script(code):
    run = launch(META + "log('go')\n" + code, echo)
    assert until(lambda: run.snapshot()["log"])
    pid = run._proc.pid
    run.stop()
    assert run.wait(10) and run.status == "stopped"
    assert run._proc.poll() is not None and leftovers(run) == []
    assert until(lambda: pid not in psutil.pids())


def test_stop_at_once_and_agents_that_ignore_it(monkeypatch):
    run = launch(META + "while True: pass", echo)
    run.stop()
    assert run.wait(10) and run.status == "stopped" and leftovers(run) == []

    monkeypatch.setattr(engine, "GRACE", 0.2)
    release = threading.Event()

    def deaf(call, cancel, progress):
        release.wait(10)
        return AgentResult(True, text="late", tokens=99)

    run = launch(META + "await agent('x')", deaf)
    assert until(lambda: agents(run) and agents(run)[0]["status"] == "running")
    run.stop()
    assert run.wait(5)
    before = run.snapshot()
    release.set()
    assert until(lambda: not leftovers(run))
    assert run.snapshot()["spent"] == before["spent"] == 0 and agents(run)[0]["status"] == "stopped"


def test_the_script_ends_when_alice_is_gone():
    """The process ends by itself when its stdin closes, also inside an endless loop."""
    start = {"t": "start", "source": META + "while True: pass", "name": "t", "args": None, "budget": None, "spent": 0,
             "ack": False}
    proc = subprocess.Popen([sys.executable, "-I", "-S", host.__file__], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert json.loads(proc.stdout.readline())["t"] == "hello"
        proc.stdin.write(json.dumps(start).encode() + b"\n")
        proc.stdin.flush()
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(0.2)
        proc.stdin.close()
        assert proc.wait(10) == 0
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()


def test_agents_nobody_waits_for_end_with_the_script():
    def spawn(call, cancel, progress):
        assert cancel.wait(10)
        return AgentResult(False)

    run = go("agent('x')\nreturn 'early'", spawn)
    assert run.value == "early" and agents(run)[0]["status"] == "stopped" and leftovers(run) == []


def test_skip_one_agent():
    def spawn(call, cancel, progress):
        if call.prompt == "stuck":
            assert cancel.wait(10)
        return AgentResult(True, text=call.prompt)

    run = launch(META + "return await parallel([lambda: agent('stuck'), lambda: agent('a'), lambda: agent('b')])",
                 spawn,
                 concurrency=1)
    assert until(lambda: len(agents(run)) == 3)
    assert run.skip(3) and not run.skip(3) and not run.skip(9)
    assert run.skip(1)
    assert run.wait(10) and run.value == [None, "a", None]
    assert [a["status"] for a in agents(run)] == ["stopped", "done", "stopped"]


def test_a_slow_listener_does_not_slow_the_run():
    calls = []
    run = launch(META + "for i in range(30):\n  await agent(str(i))\nreturn 1", echo,
                 on_change=lambda r: (time.sleep(0.3), calls.append(r.id)), on_done=lambda r: calls.append("done"))
    assert until(lambda: run.status == "done", 5)
    assert run.wait(10) and calls[-1] == "done" and run.id in calls


def test_broken_listeners_and_wait_inside_on_done():
    seen = []

    def done(run):
        seen.append((run.wait(), run.status))
        raise RuntimeError("listener")

    run = go("return 1", on_change=lambda r: 1 / 0, on_done=done)
    assert seen == [(True, "done")] and run.value == 1


# Errors ---------------------------------------------------------------------------------------------------------

def test_a_crash_names_the_script_line():
    run = go('data = {}\nasync def deep():\n    return data["missing"]\nawait deep()')
    assert run.status == "failed"
    assert run.error == "KeyError: 'missing' (line 4: return data[\"missing\"])"
    run = go("x = [1, 2]\nreturn f(x)")
    assert run.error == "NameError: name 'f' is not defined (line 3: return f(x))"
    run = go("def f(n):\n  return f(n + 1)\nf(0)")
    assert run.error.startswith("RecursionError") and "line 3" in run.error


def test_the_process_dying_is_reported(monkeypatch, tmp_path):
    fake = tmp_path / "fake_host.py"
    fake.write_text("import sys\nsys.stderr.write('no such thing: ü\\n')\nsys.exit(3)", encoding="utf-8")
    monkeypatch.setattr(host, "__file__", str(fake))
    run = go("return 1")
    assert run.status == "failed" and "exit code 3" in run.error and "no such thing" in run.error
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nothing-here"))
    run = go("return 1")
    assert run.status == "failed" and run.error


def test_the_engine_survives_garbage_from_the_process(monkeypatch, tmp_path):
    fake = tmp_path / "fake_host.py"
    for code in ("print('not json')", "print('[]')", "print('{\"t\": \"agent\"}')",
                 "print('x' * (9 * 1024 * 1024))\nimport time\ntime.sleep(30)"):
        fake.write_text(code, encoding="utf-8")
        monkeypatch.setattr(host, "__file__", str(fake))
        run = go("return 1", timeout=10)
        assert run.status == "failed" and run.error and leftovers(run) == []


# Resume ---------------------------------------------------------------------------------------------------------

class Counting:
    def __init__(self, fail=()):
        self.prompts, self.fail, self.lock = [], set(fail), threading.Lock()

    def __call__(self, call, cancel, progress):
        with self.lock:
            self.prompts.append(call.prompt)
        if call.prompt in self.fail:
            return AgentResult(False, error="failed")
        time.sleep(hash(call.prompt) % 5 / 100)  # finishes in any order
        return AgentResult(True, text=f"<{call.prompt}>", value={"p": call.prompt}, tokens=5)


FLOW = """
async def look(item):
    return await agent(f"look {item}")
first = await pipeline(["a", "b", "c", "d"], look, lambda p, i: agent(f"check {i} {p}", schema={"type": "object"}))
last = await agent("sum up " + json.dumps(first))
return [first, last]
"""


def test_resume_replays_everything():
    spawn = Counting()
    one = go(FLOW, spawn)
    assert one.status == "done" and len(spawn.prompts) == 9 and one.snapshot()["resumable"]
    old = one
    for _ in range(2):
        again = Counting()
        old = two = go(FLOW, again, resume_from=old.id)
        assert two.value == one.value and again.prompts == []
        assert {a["status"] for a in agents(two)} == {"cached"}
        assert two.spent == 0 and [a["label"] for a in agents(two)] == [a["label"] for a in agents(one)]


def test_resume_is_stable_when_results_came_in_odd_order():
    """Second stages are numbered by who finished first; the replay must give every call the number it had."""
    for _ in range(3):
        order = {"look a": 0.12, "look b": 0.0, "look c": 0.08, "look d": 0.04}

        def slow(call, cancel, progress, order=order):
            time.sleep(order.get(call.prompt, 0.01))
            return AgentResult(True, text=call.prompt, value={})

        one = go(FLOW, slow, concurrency=4)
        labels = [a["label"] for a in agents(one)]
        assert labels.index("check b look b") < labels.index("check a look a")
        again = Counting()
        two = go(FLOW, again, resume_from=one.id)
        assert again.prompts == [] and two.value == one.value


def test_resume_after_a_changed_call():
    body = 'out = []\nfor p in ["a", "b", "c"]:\n  out.append(await agent(p))\nreturn out'
    one = go(body, Counting())
    again = Counting()
    two = go(body.replace('"b"', '"B"'), again, resume_from=one.id)
    assert again.prompts == ["B", "c"] and two.value == ["<a>", "<B>", "<c>"]
    assert [a["status"] for a in agents(two)] == ["cached", "done", "done"]
    again = Counting()  # an option changed
    go(body.replace("agent(p)", 'agent(p, model="m")'), again, resume_from=one.id)
    assert again.prompts == ["a", "b", "c"]
    again = Counting()  # fewer and more calls
    assert go(body.replace('"c"]', '"c", "d"]'), again, resume_from=one.id).value[3] == "<d>" and again.prompts == ["d"]


def test_resume_after_a_failed_call():
    body = 'return await parallel([lambda: agent(p) for p in ["a", "b", "c"]])'
    one = go(body, Counting(fail=["b"]))
    assert one.value == ["<a>", None, "<c>"]
    again = Counting()
    two = go(body, again, resume_from=one.id)
    assert sorted(again.prompts) == ["b", "c"] and two.value == ["<a>", "<b>", "<c>"]


def test_resume_when_the_recorded_order_no_longer_fits():
    """The result that came first belongs to a call the new script makes later: nothing may wait for it forever."""
    def slow_first(call, cancel, progress):
        time.sleep(0.15 if call.prompt == "a" else 0)
        return AgentResult(True, text=call.prompt)

    one = go('return await parallel([lambda: agent("a"), lambda: agent("b")])', slow_first)
    again = Counting()
    two = go('x = await agent("a")\nreturn [x, await agent("b")]', again, resume_from=one.id, timeout=10)
    assert two.value == ["a", "<b>"] and again.prompts == ["b"]


def test_resume_a_stopped_run_with_its_own_script_and_args(tmp_path):
    reached = threading.Event()

    def spawn(call, cancel, progress):
        if call.prompt == "c":
            reached.set()
            cancel.wait(10)
            return AgentResult(False)
        return AgentResult(True, text=call.prompt)

    run = launch(META + "return [await agent(p) for p in args]", spawn, args=["a", "b", "c"], home=tmp_path)
    assert reached.wait(10)
    with pytest.raises(ScriptError, match="still running"):
        launch("", echo, resume_from=run.id, home=tmp_path)
    run.stop()
    again = Counting()
    two = launch("", again, resume_from=run.id, home=tmp_path)  # at once, without waiting for the first to end
    assert two.wait(10) and two.value == ["a", "b", "<c>"] and again.prompts == ["c"]
    assert run.wait(10) and run.snapshot()["resumable"]


def test_resume_survives_a_damaged_journal(tmp_path):
    one = go("return [await agent('a'), await agent('b')]", Counting(), home=tmp_path)
    journal = tmp_path / one.id / "journal.jsonl"
    lines = journal.read_text(encoding="utf-8").splitlines()
    journal.write_text(lines[0] + "\n[]\n{}\nnull\n" + lines[1][:20], encoding="utf-8")
    again = Counting()
    two = go("return [await agent('a'), await agent('b')]", again, home=tmp_path, resume_from=one.id)
    assert two.value == ["<a>", "<b>"] and again.prompts == ["b"]
    (tmp_path / "wf1" / "args.json").unlink()
    assert go("return args", home=tmp_path, resume_from="wf1").value is None


def test_resume_with_a_budget_spends_nothing_on_cached_calls():
    body = "return [await agent(str(i)) for i in range(3)]"
    one = go(body, Counting())
    assert go(body, Counting(), resume_from=one.id, budget=1).value == ["<0>", "<1>", "<2>"]


# Nested workflows -----------------------------------------------------------------------------------------------

CHILD = 'META = {"name": "child", "description": "d"}\nphase("inner")\nlog("hi")\n' \
        'return [args, await agent("c", phase="P")]'


def test_nested_workflow_shares_the_run():
    sources = {"child": CHILD}
    body = ('phase("outer")\na = await agent("a")\nb = await workflow("child", {"x": "ü"})\n'
            'return [a, b, await agent("d")]')
    run = go(body, resolve=sources.get, max_agents=3, budget=1000)
    assert run.value == ["<a>", [{"x": "ü"}, "<c>"], "<d>"]
    assert [a["n"] for a in agents(run)] == [1, 2, 3] and run.spent == 30
    phases = {p["title"]: [a["label"] for a in p["agents"]] for p in run.snapshot()["phases"]}
    assert phases == {"outer": ["a", "d"], "inner": [], "P": ["c"]}  # the child's phase() does not move the parent's
    again = Counting()
    assert go(body, again, resolve=sources.get, resume_from=run.id).value == run.value and again.prompts == []
    run = go('return await parallel([lambda: workflow("child", i) for i in range(3)])', resolve=sources.get)
    assert run.value == [[i, "<c>"] for i in range(3)]


def test_nested_workflow_errors(tmp_path):
    sources = {"deep": META + 'return await workflow("deep")', "bad": META + "import os",
               "crash": META + "x = 1\nreturn 1 / 0"}
    for name, error in (("deep", "one level only"), ("bad", "imports are not allowed"),
                        ("nope", "no workflow named 'nope'"),
                        ("crash", "ZeroDivisionError: division by zero (workflow t, line 3: return 1 / 0)"),
                        ("nope.py", "no workflow named"), ("//host/share/x.py", "no workflow named")):
        run = go(f"await workflow({name!r})", resolve=sources.get)
        assert run.status == "failed" and error in run.error, run.error

    def broken(name):
        raise OSError("disk boom")

    assert "disk boom" in go("await workflow('x')", resolve=broken).error
    assert value("try:\n  await workflow('nope')\nexcept Exception:\n  return 'caught'",
                 resolve=sources.get) == "caught"
    path = tmp_path / "child ü.py"
    path.write_text("\ufeff" + CHILD, encoding="utf-8")
    assert value("return await workflow(args)", args=str(path)) == [None, "<c>"]


def test_agents_that_finish_during_a_nested_lookup():
    """Results that arrive while the script waits for a workflow's source are not lost."""
    def slow(name):
        time.sleep(0.2)
        return CHILD

    assert value("a = [agent(str(i)) for i in range(3)]\nb = await workflow('c')\nreturn [await x for x in a] + [b]",
                 resolve=slow) == ["<0>", "<1>", "<2>", [None, "<c>"]]


# Snapshot and storage -------------------------------------------------------------------------------------------

def test_snapshot_and_files(tmp_path):
    meta = 'META = {"name": "n", "description": "d", "phases": [{"title": "One", "detail": "x"}, {"title": "Two"}]}\n'
    seen = []

    def spawn(call, cancel, progress):
        seen.append(call)
        return AgentResult(True, text="r", tokens=3)

    run = go('phase("One")\nawait agent("line one\\nline two")\nawait agent("q", label="L", phase="New", model="m", '
             'effort="high", isolation="worktree", agent_type="kind")\nlog("note")\nreturn "x" * 5000',
             spawn, meta=meta, home=tmp_path, args={"k": 1}, budget=100)
    s = run.snapshot()
    assert set(s) == {"id", "name", "description", "status", "phases", "log", "result", "error", "spent", "budget",
                      "secs",
                      "script_path", "resumable"}
    assert (s["id"], s["name"], s["description"], s["status"], s["log"], s["error"], s["spent"], s["budget"]) == \
        ("wf1", "n", "d", "done", ["note"], "", 6, 100)
    assert len(s["result"]) == 4000 and len(run.value) == 5000 and s["resumable"] is True
    assert [(p["title"], p["detail"], len(p["agents"])) for p in s["phases"]] == [("One", "x", 1), ("Two", "", 0),
                                                                                  ("New", "", 1)]
    a = s["phases"][0]["agents"][0]
    assert set(a) == {"n", "label", "status", "secs", "tokens", "steps", "last", "model"}
    assert (a["n"], a["label"], a["status"], a["tokens"]) == (1, "line one", "done", 3)
    assert seen[0] == AgentCall(1, "line one\nline two", "line one", "One")
    assert seen[1] == AgentCall(2, "q", "L", "New", None, "m", "high", "worktree", "kind")
    folder = tmp_path / "wf1"
    assert Path(s["script_path"]) == folder / "script.py"
    assert (folder / "script.py").read_text("utf-8").startswith(meta)
    assert json.loads((folder / "args.json").read_text("utf-8")) == {"k": 1}
    entries = [json.loads(x) for x in (folder / "journal.jsonl").read_text("utf-8").splitlines()]
    assert [(e["n"], e["ok"], e["value"]) for e in entries] == [(1, True, "r"), (2, True, "r")]
    saved = json.loads((folder / "result.json").read_text("utf-8"))
    assert saved["value"] == run.value and saved["status"] == "done" and saved["phases"] == s["phases"]
    assert json.dumps(s) and sorted(os.listdir(folder)) == ["args.json", "journal.jsonl", "result.json", "script.py"]


def test_ids_and_home(isolated_home, tmp_path):
    assert [go("return 1").id for _ in range(2)] == ["wf1", "wf2"]
    assert (isolated_home / "workflows" / "wf2" / "result.json").exists()
    (isolated_home / "workflows" / "wf10").mkdir()
    assert go("return 1").id == "wf11" and go("return 1", run_id="mine").id == "mine"
    for kw in ({"run_id": "mine"}, {"run_id": "../x"}, {"resume_from": "../x"}, {"resume_from": "wf99"},
               {"args": {1, 2}},
               {"args": object()}):
        with pytest.raises(ScriptError):
            launch(META, echo, **kw)
    with pytest.raises(ScriptError):
        launch("import os", echo)
    assert not (isolated_home / "workflows" / "wf12").exists()


def test_empty_script_and_launch_returns_at_once():
    start = time.monotonic()
    run = launch(META, echo)
    assert time.monotonic() - start < 2
    assert run.wait(10) and run.status == "done" and run.value is None and run.snapshot()["result"] == ""
    assert run.snapshot()["resumable"] is False


def test_unicode_both_ways():
    text = "Şöğüş 🦉 \u2028 \x85 \r\n 漢字 \x00 \ud800"

    def spawn(call, cancel, progress):
        progress({"last": text, "steps": "x", "tokens": None, "model": None})
        return AgentResult(True, text=call.prompt + text, tokens="many")

    body = 'log("ğ🦉\\ud800")\nphase("Aşama 🦉")\nreturn [await agent("İş: 🦉\\u2028"), args, "\\ud83d"]'
    run = go(body, spawn, args=["ü\u2028🦉"])
    clean = text.replace("\ud800", "\ufffd")
    assert run.value == ["İş: 🦉\u2028" + clean, ["ü\u2028🦉"], "\ufffd"]
    s = run.snapshot()
    assert s["log"] == ["ğ🦉\ufffd"] and s["phases"][0]["title"] == "Aşama 🦉" and json.dumps(s,
                                                                                            ensure_ascii=False).encode()
    again = Counting()
    assert go(body, again, args=["ü\u2028🦉"], resume_from=run.id).value == run.value and again.prompts == []


def test_large_answers_fit():
    def spawn(call, cancel, progress):
        return AgentResult(True, text="🦉" * 1_200_000)

    assert value("a = await agent('x')\nreturn [len(a), a[0]]", spawn) == [engine.MAX_TEXT, "🦉"]


@pytest.mark.skipif(sys.platform != "win32", reason="pythonw is Windows only")
def test_works_under_pythonw(monkeypatch):
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.exists():
        pytest.skip("no pythonw next to this Python")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert value("return await agent('x')") == "<x>"
