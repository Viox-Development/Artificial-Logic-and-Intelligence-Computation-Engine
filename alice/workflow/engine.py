"""Runs workflow scripts: the process of the script (host.py), the agents it asks for, and what is kept on disk.

A run has one thread that reads the script's messages, one that writes to it (a script that does not read must not hold
up the agents), one for its stderr, and one per agent that is working. Everything about a run is stored under
home/<run id>/: script.py, args.json, journal.jsonl and result.json.

Resume: the journal has one line per agent call in the order the results were handed to the script. A resumed run
answers call n from the journal while calls 1..n are the same as before and succeeded, and hands the results over in the
recorded order, because that order decides which call the script makes next (host.py). From the first call that differs
on, everything runs live.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home
from alice.workflow import host, schema, script
from alice.workflow.script import Meta, ScriptError, parse

__all__ = ["AgentCall", "AgentResult", "Meta", "Run", "ScriptError", "Spawn", "launch", "parse"]

MAX_TEXT = 1_000_000  # characters of one agent's answer handed to the script
RESULT_CHARS = 4000  # of the result in a snapshot
OPT_CHARS = {"phase": 200, "model": 100, "effort": 100, "isolation": 100, "agent_type": 100}  # shown with every event
LOG_LINES = 200
GRACE = 5.0  # seconds agents get to react to a stop before the run ends without them
CHANGE_EVERY = 0.1  # seconds: on_change is called at this interval at most
_OPTS = ("label", "phase", "schema", "model", "effort", "isolation", "agent_type")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_LONE = re.compile("[\ud800-\udfff]")  # half a surrogate pair: cannot be encoded, breaks every later write


@dataclass
class AgentCall:
    n: int
    prompt: str
    label: str = ""
    phase: str = ""
    schema: dict[str, Any] | None = None
    model: str | None = None
    effort: str | None = None
    isolation: str | None = None
    agent_type: str | None = None


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    value: Any = None
    tokens: int = 0
    error: str = ""


Spawn = Callable[[AgentCall, threading.Event, Callable[[dict], None]], AgentResult]


class _Agent:
    """One agent() call of a run."""

    def __init__(self, call: AgentCall, host_id: int, key: str, status: str):
        self.call, self.host_id, self.key, self.status = call, host_id, key, status
        self.cancel = threading.Event()
        self.started = self.ended = 0.0
        self.tokens = self.steps = 0
        self.last, self.model = "", call.model or ""

    def snapshot(self) -> dict[str, Any]:
        secs = (self.ended or time.monotonic()) - self.started if self.started else 0.0
        return {"n": self.call.n, "label": self.call.label, "status": self.status, "secs": round(secs, 1),
                "tokens": self.tokens, "steps": self.steps, "last": self.last, "model": self.model}


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return _LONE.sub("\ufffd", value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    return value


def _count(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) and value == value and abs(value) < 1e15 else 0


def _key(prompt: str, opts: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([prompt, opts], sort_keys=True).encode()).hexdigest()


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _read_journal(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").split("\n")  # splitlines() also splits at characters inside texts
    except (OSError, ValueError):
        return []
    for line in lines:
        try:
            entry = json.loads(line)
            if isinstance(entry["n"], int) and isinstance(entry["key"], str) and isinstance(entry["ok"], bool):
                out.append(entry)
        except (ValueError, KeyError, TypeError):
            pass  # the last line of a run that was cut off
    return out


def _environment() -> dict[str, str]:
    """What Python needs to start and nothing more: no API keys in the script's process."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH", "__PYVENV_LAUNCHER__")
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def _job(pid: int) -> int | None:
    """Windows: a job object around the script's process. It bounds the memory, forbids child processes, and ends the
    process when ALICE ends, even inside a long C call. A venv's python.exe is only a launcher, so this takes the pid
    the script's process reports, not the one that was started."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", w.LARGE_INTEGER), ("job_time", w.LARGE_INTEGER), ("flags", w.DWORD),
                    ("min_set", ctypes.c_size_t), ("max_set", ctypes.c_size_t), ("processes", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = k.OpenProcess.restype = w.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.CloseHandle.argtypes = [w.HANDLE]
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Extended()
    info.basic.flags = 0x2000 | 0x400 | 0x100 | 0x8  # kill on close, no crash dialog, memory, number of processes
    info.basic.processes = 1
    info.process_memory = host.MEMORY
    process = k.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    done = bool(process) and k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
        and k.AssignProcessToJobObject(job, process)
    if process:
        k.CloseHandle(process)
    if not done:
        k.CloseHandle(job)
        return None
    return job


def _close_job(job: int | None) -> None:
    if job:
        import ctypes
        from ctypes import wintypes

        close = ctypes.WinDLL("kernel32").CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(job)


class Run:
    """A running or finished workflow. Made by launch()."""

    def __init__(self, run_id: str, folder: Path, meta: Meta, source: str, spawn: Spawn, args: Any, budget: int | None,
                 on_change: Callable[[Run], Any] | None, on_done: Callable[[Run], Any] | None,
                 resolve: Callable[[str], str | None] | None, max_agents: int, concurrency: int,
                 journal: list[dict[str, Any]]):
        self.id, self.folder, self.meta = run_id, folder, meta
        self.name, self.description = meta.name, meta.description
        self.status, self.value, self.error = "running", None, ""  # running | done | failed | stopped
        self._result = ""
        self.budget = budget
        self._source, self._args, self._spawn_agent, self._resolve = source, args, spawn, resolve
        self._on_change, self._on_done = on_change, on_done
        self._max_agents, self._concurrency = max_agents, concurrency
        self._lock = threading.RLock()
        self._started, self._ended = time.monotonic(), 0.0
        self._phases: dict[str, dict[str, Any]] = {}  # title -> {"title", "detail", "agents": [_Agent]}
        for p in meta.phases:
            self._phases[p["title"]] = {"title": p["title"], "detail": p.get("detail", ""), "agents": []}
        self._log: collections.deque[str] = collections.deque(maxlen=LOG_LINES)
        self._agents: list[_Agent] = []
        self._queue: collections.deque[_Agent] = collections.deque()
        self._working: dict[int, threading.Thread] = {}
        self._spent = 0
        self._journaled_ok = 0
        self._over = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._job: int | None = None
        self._out: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._stderr = b""
        self._sent = 0  # results handed to the script
        self._finished = threading.Event()
        self._changed, self._closing = threading.Event(), threading.Event()
        # Replay: the longest run of calls 1..n that succeeded, and the order their results were handed over in.
        self._replay: dict[int, dict[str, Any]] = {}
        by_number = {e["n"]: e for e in journal}
        while (e := by_number.get(len(self._replay) + 1)) and e["ok"]:
            self._replay[e["n"]] = e
        self._order = collections.deque(e["n"] for e in journal if e["n"] in self._replay)
        self._held: dict[int, _Agent] = {}  # replayed calls whose turn has not come yet
        self._threads = [threading.Thread(target=f, name=f"workflow-{run_id}-{n}", daemon=True)
                         for n, f in (("run", self._main), ("notify", self._notify))]

    # For the caller ---------------------------------------------------------------------------------------------

    @property
    def spent(self) -> int:
        with self._lock:
            return self._spent + sum(a.tokens for a in self._agents if a.status == "running")

    @property
    def script_path(self) -> str:
        return str(self.folder / "script.py")

    def stop(self) -> None:
        """Ends the run: the script's process is killed and every agent is told to stop. Returns at once."""
        self._decide("stopped")

    def skip(self, n: int) -> bool:
        """Ends one agent, the script gets None for it. False: there is no such agent at work."""
        with self._lock:
            agent = next((a for a in self._agents if a.call.n == n and a.status in ("queued", "running")), None)
            if agent is None or self._over:
                return False
            agent.cancel.set()
            if agent.status == "queued":
                self._queue.remove(agent)
                self._settle(agent, "stopped", error="skipped")
        self._touch()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """True once the run has ended, its agents have ended too and on_done has returned."""
        if threading.current_thread() in self._threads:
            return self.status != "running"  # on_done itself asks
        return self._finished.wait(timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self.id, "name": self.name, "description": self.description, "status": self.status,
                    "phases": [{"title": p["title"], "detail": p["detail"],
                                "agents": [a.snapshot() for a in p["agents"]]} for p in self._phases.values()],
                    "log": list(self._log), "result": self._result, "error": self.error,
                    "spent": self.spent, "budget": self.budget,
                    "secs": round((self._ended or time.monotonic()) - self._started, 1),
                    "script_path": self.script_path,
                    "resumable": self.status != "running" and self._journaled_ok > 0}

    # The run ----------------------------------------------------------------------------------------------------

    def _start(self) -> None:
        for t in self._threads:
            t.start()

    def _result_text(self, value: Any) -> str:
        if value is None or isinstance(value, str):
            return (value or "")[:RESULT_CHARS]
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)[:RESULT_CHARS]
        except (TypeError, ValueError, RecursionError) as e:
            return f"({type(e).__name__})"

    def _touch(self) -> None:
        self._changed.set()

    def _notify(self) -> None:
        """Tells the caller about changes from a thread of its own, so a slow listener does not slow the run."""
        while not self._closing.is_set():
            self._changed.wait()
            self._changed.clear()
            self._tell(self._on_change)
            self._closing.wait(CHANGE_EVERY)
        self._tell(self._on_change)
        self._tell(self._on_done)
        self._finished.set()

    def _tell(self, listener: Callable[[Run], Any] | None) -> None:
        if listener is not None:
            try:
                listener(self)
            except Exception:  # noqa: BLE001 - a broken listener must not end the run
                pass

    def _decide(self, status: str, value: Any = None, error: str = "") -> None:
        """The outcome of the run; the first one counts."""
        with self._lock:
            if self._over:
                return
            self._over = True
            self.status, self.value, self.error = status, value, error
            self._result = self._result_text(value)  # once: every event carries it
            self._ended = time.monotonic()
            for a in self._agents:
                a.cancel.set()
                if a.status == "queued":
                    a.status = "stopped"
            self._queue.clear()
            proc, job, self._job = self._proc, self._job, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass  # it ended by itself
        _close_job(job)
        self._touch()

    def _main(self) -> None:
        helpers: list[threading.Thread] = []
        proc = None
        try:
            proc = self._open()
            with self._lock:
                self._proc = proc
                over = self._over
            if over:  # stopped while the process was starting
                proc.kill()
            helpers = [threading.Thread(target=f, args=(proc,), name=f"workflow-{self.id}-{n}", daemon=True)
                       for n, f in (("write", self._write), ("stderr", self._drain))]
            for t in helpers:
                t.start()
            self._read(proc, helpers[1])
        except Exception as e:  # noqa: BLE001 - whatever breaks here ends this run, not ALICE
            self._decide("failed", error=f"{type(e).__name__}: {e}")
        finally:
            self._decide("failed", error="the script ended without a result")  # only if nothing else was decided
            self._out.put(None)
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for t in helpers:
                t.join(GRACE)
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()
            self._join_agents()
            self._save()
            with _lock:
                _live.pop(self.folder, None)
            self._closing.set()
            self._touch()

    def _open(self) -> subprocess.Popen[bytes]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen([sys.executable, "-I", "-S", "-B", "-X", "utf8", host.__file__], cwd=str(self.folder),
                                env=_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=flags, start_new_session=os.name != "nt")

    def _write(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdin is not None
        try:
            while (line := self._out.get()) is not None:
                proc.stdin.write(line)
                proc.stdin.flush()
        except (OSError, ValueError):
            pass  # the process is gone
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _drain(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        try:
            while chunk := os.read(proc.stderr.fileno(), 65536):
                self._stderr = (self._stderr + chunk)[-4000:]
        except (OSError, ValueError):
            pass
        finally:
            proc.stderr.close()

    def _read(self, proc: subprocess.Popen[bytes], stderr: threading.Thread) -> None:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline(host.MAX_LINE + 2)
            if not line.endswith(b"\n"):
                break
            message = json.loads(line)
            self._handle(_clean(message) if b"\\ud" in line else message)
            if self._over:
                return
        if self._over:
            return
        if len(line) > host.MAX_LINE:
            self._decide("failed", error="the script sent a message that is too large")
            return
        try:
            code: int | str = proc.wait(GRACE)
        except subprocess.TimeoutExpired:
            code = "?"
        stderr.join(1)
        tail = self._stderr.decode("utf-8", "replace").strip()
        self._decide("failed", error=f"the script's process ended (exit code {code})" + (f": {tail}" if tail else ""))

    def _handle(self, m: dict[str, Any]) -> None:
        kind = m["t"]
        if kind == "agent":
            self._call(m)
        elif kind == "log":
            with self._lock:
                self._log.append(m["text"])
        elif kind == "phase":
            with self._lock:
                self._phase(m["title"])
        elif kind == "idle":
            self._idle(m["seen"])
        elif kind == "workflow":
            self._send(self._nested(m["name"]))
        elif kind == "hello":
            job = _job(m["pid"])
            with self._lock:
                if not self._over:
                    self._job, job = job, None
            _close_job(job)
            self._send({"t": "start", "source": self._source, "name": self.name, "args": self._args,
                        "budget": self.budget, "spent": 0, "ack": bool(self._replay)})
        elif kind == "done":
            self._decide("done", value=m["value"])
        elif kind == "failed":
            self._decide("failed", error=m["error"])
        if kind in ("agent", "log", "phase"):
            self._touch()

    def _send(self, message: dict[str, Any]) -> None:
        self._out.put(json.dumps(message).encode("ascii") + b"\n")

    def _nested(self, name: str) -> dict[str, Any]:
        reply = {"t": "workflow", "name": name, "source": "", "error": ""}
        try:
            source = self._resolve(name) if self._resolve is not None else None
            if source is None:
                path = script.local_path(name)
                if path.suffix != ".py" or not path.is_file() or path.stat().st_size > 4 * script.MAX_SOURCE:
                    raise ScriptError(f"there is no workflow named {name!r}")
                source = path.read_text(encoding="utf-8-sig")
            source = source.lstrip("\ufeff")
            reply["name"], reply["source"] = parse(source).name, source
        except Exception as e:  # noqa: BLE001 - becomes an exception in the script that asked
            reply["error"] = f"workflow({name!r}): {e}"
        return reply

    def _phase(self, title: str) -> dict[str, Any]:
        if title not in self._phases:
            self._phases[title] = {"title": title, "detail": "", "agents": []}
        return self._phases[title]

    # Agents -----------------------------------------------------------------------------------------------------

    def _call(self, m: dict[str, Any]) -> None:
        prompt, opts, host_id = m["prompt"], m["opts"], m["id"]
        problems = schema.schema_problems(opts["schema"]) if "schema" in opts else []
        if problems:
            self._refuse(host_id, "agent() schema: " + "; ".join(problems))
            return
        with self._lock:
            if self._over:
                return
            n = len(self._agents) + 1
            key = _key(prompt, opts)
            cached = self._replay.get(n)
            if cached is not None and cached["key"] != key:
                self._diverge(n)
                cached = None
            if n > self._max_agents:
                self._refuse(host_id, f"more than {self._max_agents} agents in one workflow")
                return
            if cached is None and self._broke():
                self._refuse(host_id, self._broke())
                return
            first = next((s.strip() for s in prompt.splitlines() if s.strip()), "")
            opts = {k: v[:OPT_CHARS[k]] if k in OPT_CHARS and isinstance(v, str) else v for k, v in opts.items()}
            call = AgentCall(n, prompt, (opts.get("label") or "").strip()[:200] or first[:60],
                             opts.get("phase") or m["at"], **{k: opts.get(k) for k in _OPTS[2:]})
            agent = _Agent(call, host_id, key, "cached" if cached else "queued")
            self._agents.append(agent)
            self._phase(call.phase)["agents"].append(agent)
            if cached:
                agent.tokens, agent.model = _count(cached.get("tokens")), str(cached.get("model") or "")
                self._held[n] = agent
                self._release()
            else:
                self._queue.append(agent)
                self._pump()

    def _broke(self) -> str:
        if self.budget is not None and self.spent >= self.budget:
            return f"the token budget is used up ({self.spent} of {self.budget})"
        return ""

    def _refuse(self, host_id: int, error: str) -> None:
        with self._lock:
            self._sent += 1
            self._send({"t": "result", "id": host_id, "error": error, "spent": self.spent})

    def _pump(self) -> None:
        """Starts waiting agents while there is room (with the lock held)."""
        while self._queue and len(self._working) < self._concurrency and not self._over:
            agent = self._queue.popleft()
            broke = self._broke()
            if broke:
                self._settle(agent, "failed", error=broke, refuse=True)
                continue
            agent.status, agent.started = "running", time.monotonic()
            thread = threading.Thread(target=self._work, args=(agent,), name=f"workflow-{self.id}-a{agent.call.n}",
                                      daemon=True)
            self._working[agent.call.n] = thread
            thread.start()

    def _work(self, agent: _Agent) -> None:
        def progress(p: dict) -> None:
            with self._lock:
                if agent.status != "running":
                    return
                if p.get("last") is not None:
                    agent.last = _clean(str(p["last"]))[:200]
                agent.steps = _count(p.get("steps")) or agent.steps
                agent.tokens = max(agent.tokens, _count(p.get("tokens")))
                agent.model = str(p.get("model") or agent.model)[:100]
            self._touch()

        value, error, tokens = None, "", 0
        try:
            result = self._spawn_agent(agent.call, agent.cancel, progress)
            tokens = _count(result.tokens)
            if not result.ok:
                error = str(result.error or "failed")
            elif agent.call.schema is not None:
                value = _clean(json.loads(json.dumps(result.value)))
                problems = schema.check(agent.call.schema, value)
                if len(json.dumps(value)) > host.MAX_LINE // 2:
                    error = "the answer is too large"
                elif problems:
                    error = "the answer does not fit the schema: " + "; ".join(problems)
            else:
                value = _clean(str(result.text))[:MAX_TEXT]
        except BaseException as e:  # noqa: BLE001 - a failing agent gives None to the script
            error = f"{type(e).__name__}: {e}"
        with self._lock:
            del self._working[agent.call.n]
            if agent.status != "running":
                return  # the run ended without waiting for it
            agent.ended = time.monotonic()
            agent.tokens = max(agent.tokens, tokens)
            self._spent += agent.tokens
            if self._over or agent.cancel.is_set():
                self._settle(agent, "stopped", error=error or "stopped")
            elif error:
                self._log.append(f"agent {agent.call.n} ({agent.call.label}) failed: {_clean(error)[:500]}")
                self._settle(agent, "failed", error=error)
            else:
                self._settle(agent, "done", value)
            self._pump()
        self._touch()

    def _settle(self, agent: _Agent, status: str, value: Any = None, error: str = "", refuse: bool = False) -> None:
        """The end of one agent: journal it and hand the result to the script (with the lock held, so the journal has
        the order of the results)."""
        agent.status = status
        if self._over:
            return
        ok = status in ("done", "cached")
        self._journal({"n": agent.call.n, "key": agent.key, "ok": ok, "value": value if ok else None,
                       "tokens": agent.tokens, "label": agent.call.label, "model": agent.model,
                       "error": _clean(error)[:1000]})
        self._journaled_ok += ok
        self._sent += 1
        self._send({"t": "result", "id": agent.host_id, "value": value if ok else None, "spent": self.spent}
                   | ({"error": error} if refuse else {}))

    def _join_agents(self) -> None:
        deadline = time.monotonic() + GRACE
        with self._lock:
            threads = list(self._working.values())
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            for a in self._agents:  # one that does not react: its thread is a daemon, the run does not wait for it
                if a.status == "running":
                    a.status, a.ended = "stopped", time.monotonic()
                    self._spent += a.tokens

    # Replay -----------------------------------------------------------------------------------------------------

    def _diverge(self, n: int) -> None:
        """Calls from n on run live. Earlier ones have all been made, so their results can be handed over now."""
        self._replay = {k: e for k, e in self._replay.items() if k < n}
        self._order = collections.deque(k for k in self._order if k < n)
        self._release()

    def _release(self) -> None:
        while self._order and self._order[0] in self._held:
            n = self._order.popleft()
            entry = self._replay[n]
            self._settle(self._held.pop(n), "cached", entry.get("value"))

    def _idle(self, seen: int) -> None:
        """The script has done all it can with the results it has. If the result that was handed over next last time
        belongs to a call it has not made, the runs have parted."""
        with self._lock:
            if not self._over and self._held and seen == self._sent:
                self._diverge(len(self._agents) + 1)

    # Disk -------------------------------------------------------------------------------------------------------

    def _journal(self, entry: dict[str, Any]) -> None:
        try:
            with open(self.folder / "journal.jsonl", "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, ValueError, RecursionError) as e:
            self._log.append(f"journal: {e}")  # the run goes on, it just cannot be resumed from here

    def _save(self) -> None:
        try:
            with self._lock:
                data = self.snapshot() | {"value": self.value}
            _write_json(self.folder / "result.json", data)
        except (OSError, ValueError, RecursionError) as e:
            with self._lock:
                self._log.append(f"result.json: {e}")


_lock = threading.Lock()
_live: dict[Path, Run] = {}  # folder -> run that has not ended


def _new_folder(home: Path, run_id: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    if run_id:
        try:
            (home / run_id).mkdir()
        except FileExistsError:
            raise ScriptError(f"there already is a workflow run {run_id}") from None
        return home / run_id
    numbers = [int(p.name[2:]) for p in home.iterdir() if re.fullmatch(r"wf[0-9]{1,9}", p.name)]
    n = max(numbers, default=0)
    while True:  # another process may take the same number
        n += 1
        try:
            (home / f"wf{n}").mkdir()
            return home / f"wf{n}"
        except FileExistsError:
            pass


def launch(source: str, spawn: Spawn, *, args: Any = None, budget: int | None = None, resume_from: str = "",
           home: Path | None = None, on_change: Callable[[Run], Any] | None = None,
           on_done: Callable[[Run], Any] | None = None, resolve: Callable[[str], str | None] | None = None,
           max_agents: int = 200, concurrency: int | None = None, run_id: str = "") -> Run:
    """Starts a workflow in the background. ScriptError: it cannot run.

    resume_from: an earlier run whose agent results are used again as far as the calls are the same; its script and
    args are used when none are given. resolve(name) gives the source of a nested workflow (None: not known; a path to
    a .py file always works). on_change(run) and on_done(run) are called from a thread of the run."""
    home = Path(home) if home is not None else alice_home() / "workflows"
    for what in (run_id, resume_from):
        if what and not _ID.fullmatch(what):
            raise ScriptError(f"not a workflow run id: {what!r}")
    journal: list[dict[str, Any]] = []
    if resume_from:
        old = home / resume_from
        if not (old / "script.py").is_file():
            raise ScriptError(f"there is no workflow run {resume_from} to resume")
        live = _live.get(old)
        if live is not None and live.status == "running":
            raise ScriptError(f"the workflow run {resume_from} is still running, stop it first")
        journal = _read_journal(old / "journal.jsonl")
        source = source or (old / "script.py").read_text(encoding="utf-8")
        if args is None:
            try:
                args = json.loads((old / "args.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                args = None
    source = source.lstrip("\ufeff") if isinstance(source, str) else source
    meta = parse(source)
    try:
        args = _clean(json.loads(json.dumps(args)))
    except (TypeError, ValueError, RecursionError) as e:
        raise ScriptError(f"args must be JSON data: {e}") from None
    if len(json.dumps(args)) > host.MAX_LINE // 2:
        raise ScriptError("args are too large")
    if not concurrency:
        concurrency = min(6, max(2, (os.cpu_count() or 4) - 2))
    with _lock:
        folder = _new_folder(home, run_id)
        (folder / "script.py").write_bytes(source.encode("utf-8"))
        _write_json(folder / "args.json", args)
        run = Run(folder.name, folder, meta, source, spawn, args, budget or None, on_change, on_done, resolve,
                  max(1, max_agents), max(1, concurrency), journal)
        _live[folder] = run
    run._start()
    return run
