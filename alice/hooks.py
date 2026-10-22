"""Hook'lar: belli anlarda kendi komutunu çalıştır (Claude Code'daki hooks gibi).

config.toml (ya da ~/.alice/hooks.toml):

  [[hooks.PostToolUse]]
  matcher = "edit_file|write_file|multi_edit"     # araç adı (regex); boş = hepsi
  command = "ruff format \"$ALICE_TOOL_PATH\""
  timeout = 30

Olaylar:
  SessionStart      ilk mesajdan önce; çıktısı (stdout) ilk mesaja bağlam olarak eklenir
  UserPromptSubmit  her kullanıcı mesajında; stdout mesaja eklenir; çıkış kodu 2 = mesajı engelle (stderr sebep)
  PreToolUse        araç çalışmadan önce; çıkış kodu 2 = aracı engelle, stderr modele gider
  PostToolUse       araç bittikten sonra; çıkış kodu 2 = stderr modele geri bildirim olarak gider (ör. lint hatası)
  Stop              ajan cevabını bitirince; çıkış kodu 2 = stderr modele gider ve ajan devam eder (ör. testler kırık)

Komut kabukta çalışır, olay bilgisi stdin'e JSON olarak gelir ({"event", "tool", "args", "result", "prompt",
"cwd"}); ortamda ALICE_EVENT, ALICE_TOOL, ALICE_TOOL_PATH (aracın yol/komut/url argümanı) ve ALICE_CWD var.
Güvenlik: hook'lar sadece senin config'inden okunur; klonladığın bir depodaki dosya komut çalıştıramaz.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
BLOCK = 2  # Claude Code ile aynı: 2 = engelle / geri bildirim ver


@dataclass
class Hook:
    event: str
    command: str
    matcher: str = ""
    timeout: float = 30.0

    def matches(self, tool: str) -> bool:
        if not self.matcher or self.matcher == "*":
            return True
        try:
            return re.fullmatch(self.matcher, tool) is not None
        except re.error:
            return self.matcher == tool


@dataclass
class HookResult:
    hook: Hook
    code: int
    stdout: str
    stderr: str

    @property
    def blocked(self) -> bool:
        return self.code == BLOCK


def _read_hooks_file() -> dict[str, Any]:
    from alice.config import alice_home

    path = alice_home() / "hooks.toml"
    if not path.is_file():
        return {}
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            import tomli as tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - bozuk dosya ALICE'i durdurmasın
        return {}
    return data.get("hooks", data) if isinstance(data, dict) else {}


class Hooks:
    def __init__(self, hooks: list[Hook], cwd: Path | None = None):
        self.hooks = hooks
        self.cwd = cwd

    @classmethod
    def from_config(cls, cfg, cwd: Path | None = None) -> "Hooks":
        data = dict(((getattr(cfg, "data", None) or {}).get("hooks")) or {})
        for event, items in _read_hooks_file().items():
            data[event] = list(data.get(event, []) or []) + list(items or [])
        hooks = []
        for event in EVENTS:
            for h in data.get(event, []) or []:
                if isinstance(h, dict) and str(h.get("command", "")).strip():
                    hooks.append(Hook(event, str(h["command"]), str(h.get("matcher", "") or ""),
                                      float(h.get("timeout", 30))))
        return cls(hooks, cwd)

    def has(self, event: str) -> bool:
        return any(h.event == event for h in self.hooks)

    def run(self, event: str, payload: dict[str, Any], tool: str = "") -> list[HookResult]:
        out = []
        for h in self.hooks:
            if h.event != event or (tool and not h.matches(tool)):
                continue
            out.append(self._run_one(h, payload, tool))
            if out[-1].blocked and event in ("PreToolUse", "UserPromptSubmit"):
                break  # engellendi: sonraki hook'lara gerek yok
        return out

    def _run_one(self, h: Hook, payload: dict[str, Any], tool: str) -> HookResult:
        from alice.permissions import primary_arg

        args = payload.get("args") or {}
        env = {**os.environ, "ALICE_EVENT": h.event, "ALICE_TOOL": tool,
               "ALICE_TOOL_PATH": primary_arg(args) if isinstance(args, dict) else "",
               "ALICE_CWD": str(self.cwd or Path.cwd())}
        data = json.dumps({"event": h.event, "tool": tool, "cwd": str(self.cwd or ""), **payload},
                          ensure_ascii=False, default=str)
        try:
            p = subprocess.run(h.command, shell=True, input=data, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=h.timeout, cwd=str(self.cwd) if self.cwd else None, env=env,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
            return HookResult(h, p.returncode, (p.stdout or "").strip()[:4000], (p.stderr or "").strip()[:4000])
        except subprocess.TimeoutExpired:
            return HookResult(h, 1, "", f"hook {h.timeout:.0f} sn içinde bitmedi: {h.command}")
        except OSError as e:
            return HookResult(h, 1, "", f"hook çalıştırılamadı: {e}")


def feedback(results: list[HookResult]) -> str:
    """Çıkış kodu 2 olan hook'ların stderr'i (modele gidecek metin)."""
    return "\n".join(r.stderr or r.stdout or f"hook engelledi: {r.hook.command}" for r in results if r.blocked)


def context(results: list[HookResult]) -> str:
    """Başarılı hook'ların stdout'u (mesaja eklenecek bağlam)."""
    return "\n".join(r.stdout for r in results if r.code == 0 and r.stdout)
