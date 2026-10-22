"""Windows sandbox yardımcısı: `python -m alice.sandbox_run -- <komut...>`.

Kendi bütünlük seviyesini Low'a düşürür, sonra komutu çalıştırır (çocuk süreç Low seviyeyi devralır) ve onun çıkış
kodunu döndürür. Seviye düşürülemezse komutu ÇALIŞTIRMAZ (126): sandbox istenmişse sandbox'sız çalışmak yanlış olur.
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
        print(f"sandbox kurulamadı, komut çalıştırılmadı: {e}", file=sys.stderr)
        return 126
    return subprocess.call(argv, creationflags=0x08000000 if sys.platform == "win32" else 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
