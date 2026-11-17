import hashlib
from pathlib import Path

lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  data/{p.name}"
         for p in sorted(Path("data").iterdir(), key=lambda p: p.name)]
Path("SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
