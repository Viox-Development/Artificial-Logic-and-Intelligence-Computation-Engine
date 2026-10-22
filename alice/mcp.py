"""MCP (Model Context Protocol) istemcisi, stdio taşıması.

Claude Code'daki gibi dış araç sunucularını bağlar (GitHub, veritabanı, Blender, Unity
MCP sunucuları vb.). config.toml:

    [mcp.servers.dosyalar]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/hayat/Projeler"]
    # env = { TOKEN = "${GITHUB_TOKEN}" }   # ${AD} ortamdan / .env'den doldurulur
    # enabled = false

Her sunucunun araçları ajana `mcp__<sunucu>__<araç>` adıyla eklenir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable

from alice import __version__
from alice.config import Config, alice_home
from alice.tools.base import DANGER, EXEC, READ, Tool, ToolContext, ToolError, ToolResult

PROTOCOL_VERSION = "2025-06-18"
# Geri alınamaz işler (PR birleştirme, dosya/dal/depo silme, kapatma): auto modda da onay ister.
_DANGER_NAME = re.compile(r"(^|_)(merge|delete|remove|drop|destroy|close|archive|transfer|force)", re.I)


class MCPError(Exception):
    pass


class MCPServer:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 timeout: float = 60.0):
        self.name = name
        self.command = command
        self.args = list(args or [])
        # "${AD}" değerleri ortamdan doldur: token config.toml'a değil .env'e yazılsın.
        self.env = {k: os.path.expandvars(str(v)) for k, v in (env or {}).items()}
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.server_info: dict[str, Any] = {}

    # ------------------------------------------------------------ süreç
    def _argv(self) -> list[str]:
        exe = shutil.which(self.command) or self.command
        if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", exe, *self.args]  # npx.cmd gibi batch dosyaları doğrudan başlatılamaz
        return [exe, *self.args]

    def start(self) -> None:
        alice_home().mkdir(parents=True, exist_ok=True)
        log = open(alice_home() / f"mcp-{self.name}.log", "ab")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                     env={**os.environ, **self.env}, **kwargs)
        threading.Thread(target=self._reader, daemon=True, name=f"mcp-{self.name}").start()
        res = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "alice", "version": __version__}})
        self.server_info = res.get("serverInfo", {})
        self.notify("notifications/initialized")

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self.proc.kill()

    # --------------------------------------------------------- JSON-RPC
    def _write(self, msg: dict[str, Any]) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise MCPError(f"{self.name} sunucusu çalışmıyor")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self.proc.stdin.write(data)  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:  # JSON olmayan günlük satırı (cp1254 dahil): okumayı bırakma
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    if "method" in msg and "id" in msg:  # sunucudan istek
                        if msg["method"] == "ping":
                            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                        else:
                            self._write({"jsonrpc": "2.0", "id": msg["id"],
                                         "error": {"code": -32601, "message": "desteklenmiyor"}})
                        continue
                    pending = self._pending.get(msg.get("id"))
                    if pending:
                        pending[1].update(msg)
                        pending[0].set()
                except Exception:  # noqa: BLE001 - tek bozuk mesaj okuyucuyu öldürmesin
                    continue
        finally:
            for ev, box in list(self._pending.values()):  # süreç öldü
                box.setdefault("error", {"message": "sunucu kapandı"})
                ev.set()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._id += 1
            rid = self._id
        ev = threading.Event()
        box: dict[str, Any] = {}
        self._pending[rid] = (ev, box)
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            if not ev.wait(self.timeout):
                raise MCPError(f"{self.name}: {method} zaman aşımı")
        finally:
            self._pending.pop(rid, None)
        if "error" in box:
            raise MCPError(f"{self.name}: {box['error'].get('message', box['error'])}")
        return box.get("result", {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ------------------------------------------------------------- araçlar
    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


class MCPTool(Tool):
    untrusted = True  # sunucunun döndürdüğü içerik (issue, sayfa, dosya) dışarıdan gelir

    def __init__(self, server: MCPServer, spec: dict[str, Any]):
        self.server = server
        self.remote_name = spec["name"]
        self.name = f"mcp__{_safe(server.name)}__{_safe(spec['name'])}"[:64]
        self.description = f"[MCP: {server.name}] " + (spec.get("description") or spec["name"])[:1000]
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        self.parameters = schema
        ann = spec.get("annotations") or {}
        self.risk = READ if ann.get("readOnlyHint") else DANGER if _DANGER_NAME.search(spec["name"]) else EXEC
        self.parallel_safe = self.risk == READ  # istemci thread-safe, salt okuma araçları paralel çalışabilir

    def run(self, ctx: ToolContext, **args: Any) -> ToolResult:
        try:
            res = self.server.call_tool(self.remote_name, args)
        except MCPError as e:
            raise ToolError(str(e)) from e
        texts, images = [], []
        for part in res.get("content") or []:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image" and part.get("data"):
                images.append(part["data"])
            elif part.get("type") == "resource":
                r = part.get("resource") or {}
                texts.append(r.get("text") or f"[kaynak: {r.get('uri', '')}]")
        if res.get("structuredContent") and not texts:
            texts.append(json.dumps(res["structuredContent"], ensure_ascii=False, indent=1))
        return ToolResult("\n".join(texts) or "(boş sonuç)", images=images, is_error=bool(res.get("isError")))


def load_mcp(config: Config, emit: Callable[[str], None] = lambda s: None) -> tuple[list[MCPServer], list[Tool]]:
    servers: list[MCPServer] = []
    tools: list[Tool] = []
    for name, spec in (config.data.get("mcp", {}).get("servers") or {}).items():
        if spec.get("enabled", True) is False or not spec.get("command"):
            continue
        srv = MCPServer(name, spec["command"], spec.get("args"), spec.get("env"), float(spec.get("timeout", 60)))
        try:
            srv.start()
            found = srv.list_tools()
        except (MCPError, OSError) as e:
            emit(f"MCP '{name}' başlatılamadı: {e}")
            srv.close()
            continue
        servers.append(srv)
        tools += [MCPTool(srv, t) for t in found]
        emit(f"MCP '{name}' bağlandı: {len(found)} araç")
    return servers, tools


def close_all(servers: list[MCPServer]) -> None:
    for s in servers:
        s.close()
