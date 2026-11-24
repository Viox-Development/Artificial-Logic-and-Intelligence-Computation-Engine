"""ALICE diagnostic and test tools: app state, service health and test runs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from alice.tools.base import EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj, truncate
from alice.tools.shell import IS_WIN, NO_WINDOW, kill_tree


class AppState(Tool):
    name = "app_state"
    parallel_safe = True
    description = ("Diagnoses the current state of the ALICE app and agent: active mode, loaded tool groups, "
                   "background processes, session info and configuration summaries.\n"
                   "- Use it for questions like 'why does it look busy', 'which mode am I in', 'what is running in "
                   "the background'.\n"
                   "- For the system's overall CPU/RAM, use system_info or process_list instead.")
    parameters = obj({
        "section": {"type": "string", "enum": ["all", "mode", "tools", "processes", "config", "service"],
                    "description": "Which section to focus on. Default 'all'."}
    })
    risk = READ

    def run(self, ctx: ToolContext, section: str = "all") -> ToolResult:
        from alice.core.service import read_state

        out = []
        state = read_state()
        
        if section in ("all", "mode", "service"):
            mode_info = f"Mod: {state.get('mode', 'bilinmiyor')}"
            if state.get("forced"):
                mode_info += " (pinned by hand)"
            out.append(f"### Servis ve Mod\n{mode_info}")
            running = state.get("running", False)
            out.append(f"Core status: {'Running' if running else 'Stopped'}")
            if running:
                out.append(f"PID: {state.get('pid')}, Started: {state.get('start_time', 'unknown')}")

        if section in ("all", "tools"):
            enabled = ctx.state.get("tool_groups", set())
            groups_str = ", ".join(sorted(enabled)) if enabled else "(basic tools only)"
            out.append(f"### Tool groups\nLoaded: {groups_str}")
            
            fg = ctx.state.get("fg_procs", set())
            if fg:
                out.append(f"Active foreground process: {len(fg)}")

        if section in ("all", "processes"):
            bg_procs = ctx.state.get("bg_procs", {})
            if bg_procs:
                out.append("### Background processes")
                for pid, p in bg_procs.items():
                    status = "running" if p.popen.poll() is None else f"finished (code {p.popen.returncode})"
                    out.append(f"- pid={pid}: {status} :: {p.command[:100]}")
            elif section == "processes":
                out.append("No active background processes.")

        if section in ("all", "config"):
            out.append("### Configuration summary")
            cfg = ctx.config
            brain_val = getattr(cfg, "brain", None)
            if brain_val and isinstance(brain_val, (list, tuple)):
                model_str = brain_val[0] if brain_val else "default"
            else:
                roles = getattr(cfg, "roles", None)
                model_str = (roles.get("brain") if roles else None) or "default"
            out.append(f"Model: {model_str}")
            out.append(f"Working folder: {ctx.cwd}")
            out.append(f"ALICE Home: {os.environ.get('ALICE_HOME', '~/.alice')}")

        return ToolResult("\n".join(out))


class ServiceDiagnose(Tool):
    name = "service_diagnose"
    parallel_safe = True
    description = ("Diagnoses the ALICE core service and background processes, and reports errors and logs.\n"
                   "- Answers whether the core is running, when it last crashed and which errors it gave.\n"
                   "- Shows the logs of background processes (by PID). For general system processes use process_list.")
    parameters = obj({
        "service": {"type": "string", "description": "Service name to diagnose ('core', 'bg_procs') or a PID.",
                    "default": "all"},
        "tail_lines": {"type": "integer", "description": "How many log lines to show. Default 50.", "default": 50}
    })
    risk = READ

    def run(self, ctx: ToolContext, service: str = "all", tail_lines: int = 50) -> ToolResult:
        from alice.core.service import read_state
        from alice.core.store import Store

        out = []
        
        if service in ("all", "core"):
            state = read_state()
            out.append(f"### Core service diagnosis")
            if state.get("running"):
                out.append(f"Status: RUNNING (pid={state.get('pid')})")
                out.append(f"Mod: {state.get('mode')}")
            else:
                out.append("Status: STOPPED or not running.")
            
            # Fetch errors from the Store (only warning/error severities)
            own_store = False
            st = ctx.state.get("core_store")
            try:
                if st is None:
                    st = Store()
                    own_store = True
                events = st.events(limit=100)
                errors = [e for e in events if str(getattr(e, "severity", "")).lower() in ("warn", "warning", "error",
                                                                                          "critical")][:20]
                if errors:
                    out.append("\n### Recent errors/warnings")
                    for e in errors:
                        out.append(e.line())
                else:
                    out.append("\nNo critical errors recorded.")
            except Exception as e:
                out.append(f"\nError while reading the store: {e}")
            finally:
                if own_store:
                    try:
                        st.close()
                    except Exception:  # noqa: BLE001 - a close error must not block the diagnosis
                        pass

        if service in ("all", "bg_procs") or service.isdigit():
            bg_procs = ctx.state.get("bg_procs", {})
            target_pid = int(service) if service.isdigit() else None
            
            if bg_procs:
                out.append("\n### Background process logs")
                for pid, p in bg_procs.items():
                    if target_pid and pid != target_pid:
                        continue
                    status = "running" if p.popen.poll() is None else f"finished (code {p.popen.returncode})"
                    out.append(f"\n--- PID {pid} ({status}) :: {p.command[:80]} ---")
                    if p.log.exists():
                        lines = p.log.read_text(errors="replace").splitlines()
                        log_tail = "\n".join(lines[-tail_lines:])
                        out.append(log_tail or "(log empty)")
                    else:
                        out.append("(log file not found)")
            elif service != "core":
                 out.append("\nNo active background processes.")

        return ToolResult("\n".join(out))


class TestRun(Tool):
    name = "test_run"
    parallel_safe = False
    description = ("Test runner: runs pytest, unittest or a custom test command and summarizes the results.\n"
                   "- Separates failures from noise (passing tests) and reports only the real breakages and the "
                   "summary line.\n"
                   "- To run ordinary shell commands use run_command, not this; this is only for tests.")
    parameters = obj({
        "command": {"type": "string", "description": "Test command to run (default: pytest).", "default": "pytest"},
        "path": {"type": "string", "description": "File or folder path to test."},
        "filter_noise": {"type": "boolean", "description": "Filter out test noise (passing tests etc.) and show only "
                                                           "the summary and failures. Default true.", "default": True},
        "timeout": {"type": "integer", "description": "Timeout in seconds. Default 120.", "default": 120}
    })
    risk = EXEC

    def run(self, ctx: ToolContext, command: str = "pytest", path: str | None = None,
            filter_noise: bool = True, timeout: int = 120) -> ToolResult:
        full_command = command
        if path:
            full_command += f" {path}"
        
        wd = ctx.cwd
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        
        # Windows PowerShell / forced UTF-8 (similar to the logic in shell.py)
        argv = [sys.executable, "-m", "pytest"] if command == "pytest" else [command]
        if command == "pytest" and path:
            argv.append(path)
        elif command != "pytest":
            # Custom command: run it through the shell (like shell_argv)
            if IS_WIN:
                argv = ["powershell", "-NoProfile", "-Command", full_command]
            else:
                argv = ["/bin/bash", "-c", full_command]

        start_time = time.monotonic()
        try:
            pop = subprocess.Popen(argv, cwd=wd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, creationflags=NO_WINDOW if IS_WIN else 0)
            try:
                stdout, stderr = pop.communicate(timeout=timeout)
                returncode = pop.returncode
            except subprocess.TimeoutExpired:
                kill_tree(pop.pid)
                pop.communicate()
                return ToolResult(f"TEST TIMEOUT ({timeout} s): {full_command}", is_error=True)
            
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            
        except Exception as e:
            raise ToolError(f"Could not start the tests: {e}") from e

        duration = time.monotonic() - start_time
        
        # Process the results
        body = out + ("\n[stderr]\n" + err if err.strip() else "")
        
        if filter_noise:
            # Parse pytest output: both the short "FAILED x::y - msg" list and the "_____ FAIL: test _____" blocks.
            summary_match = re.search(r"^=+ (.*?(?:passed|failed|error).*?) in [\d.]+s =+$", body, re.M)
            if not summary_match:
                summary_match = re.search(r"^=+ (.*) in [\d.]+s =+$", body, re.M)
            blocks = re.findall(r"^_+ (FAILED|FAIL|ERROR) (.*?) _+$\n([\s\S]*?)(?=^_+ (?:FAILED|FAIL|ERROR) |^=+ |\Z)",
                                body, re.M)
            short_failed = re.findall(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$", body, re.M)

            filtered = [f"### Test summary: {full_command}", f"Duration: {duration:.2f}s, Code: {returncode}"]
            if summary_match:
                filtered.append(f"Durum: {summary_match.group(1).strip()}")

            if blocks:
                filtered.append("\n### Hatalar")
                for kind, name, trace in blocks[:10]:
                    filtered.append(f"\n--- {kind}: {name.strip()} ---\n{truncate(trace.strip(), 2000)}")
            elif short_failed:
                filtered.append("\n### Failed tests")
                for name, msg in short_failed[:30]:
                    filtered.append(f"- {name}" + (f" — {msg.strip()[:200]}" if msg.strip() else ""))

            total_failures = bool(blocks or short_failed)
            if not total_failures and returncode != 0:
                filtered.append("\n### Output (there are errors but they couldn't be parsed)")
                filtered.append(truncate(body, 4000))
            elif not total_failures:
                filtered.append("\nAll tests passed.")

            return ToolResult("\n".join(filtered), is_error=returncode != 0)

        return ToolResult(f"Test Sonucu ({returncode}):\n{truncate(body, 8000)}", is_error=returncode != 0)


TOOLS = [AppState(), ServiceDiagnose(), TestRun()]
