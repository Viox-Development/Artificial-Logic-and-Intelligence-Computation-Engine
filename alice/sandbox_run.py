"""Windows sandbox helper: `python -m alice.sandbox_run -- <command...>`.

Lowers its own integrity level to Low, then runs the command (the child process inherits the Low level) and returns
its exit code. If the level cannot be lowered it does NOT run the command (126): when a sandbox was requested,
running without one would be wrong.
"""

from __future__ import annotations

import subprocess
import sys


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("sandbox: komut yok", file=sys.stderr)
        return 2
    try:
        from alice.sandbox import lower_integrity

        lower_integrity()
    except Exception as e:  # noqa: BLE001
        print(f"could not set up the sandbox, the command was not run: {e}", file=sys.stderr)
        return 126
    # Output is passed through explicitly: a child started with subprocess.call and no console (CREATE_NO_WINDOW)
    # did not inherit our pipe ends; the output of every command in the test sandbox (including the tests of tools
    # ALICE wrote) was lost, and the model only saw "çıkış kodu 1 (çıktı yok)" and could not know the cause of the error.
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=0x08000000 if sys.platform == "win32" else 0)

    def pump(src, dst) -> None:
        for chunk in iter(lambda: src.read1(65536) if hasattr(src, "read1") else src.read(65536), b""):
            dst.write(chunk)
            dst.flush()

    import threading

    threads = [threading.Thread(target=pump, args=(p.stdout, sys.stdout.buffer), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, sys.stderr.buffer), daemon=True)]
    for t in threads:
        t.start()
    code = p.wait()
    for t in threads:
        t.join(5)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
