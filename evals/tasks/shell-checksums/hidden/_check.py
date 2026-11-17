"""Eval check: SHA256SUMS matches the files in data/."""

import hashlib
import sys
from pathlib import Path

want = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  data/{p.name}"
        for p in sorted(Path("data").iterdir(), key=lambda p: p.name) if p.is_file()]
try:
    got = [ln.rstrip("\r") for ln in Path("SHA256SUMS").read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
except FileNotFoundError:
    sys.exit("SHA256SUMS missing")
if got != want:
    sys.exit("mismatch:\n" + "\n".join(got) + "\n---- expected ----\n" + "\n".join(want))
print("ok")
