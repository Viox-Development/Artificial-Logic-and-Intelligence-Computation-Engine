"""Manifestten ALICE aracı üret: her bağlantı tek bir araç, eylemler `action` parametresiyle seçilir."""

from __future__ import annotations

from typing import Any

from alice.connect.manifest import Manifest
from alice.connect.runner import ConnectError, call, ready, render
from alice.tools.base import NETWORK, READ, SEND, WRITE, Tool, ToolContext, ToolError, ToolResult

TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean", "array": "array"}


class ConnectTool(Tool):
    """Bir servisin aracı. Şema manifestten üretilir, böylece model parametreleri tahmin etmez."""

    parallel_safe = True
    risk = NETWORK

    def __init__(self, m: Manifest):
        self.m = m
        self.name = m.name
        acts = m.actions
        lines = [f"- {a.name}: {a.summary or a.path}" for a in acts.values()]
        hazır, sebep = ready(m)
        self.description = (f"{m.description or m.label}\nEylemler:\n" + "\n".join(lines)
                            + ("" if hazır else f"\n(şu an kullanılamaz: {sebep})"))
        props: dict[str, Any] = {"action": {"type": "string", "enum": sorted(acts),
                                            "description": "Yapılacak iş."}}
        for a in acts.values():
            for p in a.params.values():
                prev = props.get(p.name)
                desc = f"[{a.name}] {p.description}".strip()
                if prev:  # aynı ad birden çok eylemde: açıklamaları birleştir
                    prev["description"] = (prev.get("description", "") + " " + desc).strip()
                    continue
                spec: dict[str, Any] = {"type": TYPES.get(p.type, "string"), "description": desc}
                if p.enum:
                    spec["enum"] = p.enum
                if spec["type"] == "array":
                    spec["items"] = {"type": "string"}
                props[p.name] = spec
        self.parameters = {"type": "object", "properties": props, "required": ["action"]}

    def risk_for(self, args: dict[str, Any]) -> str:
        act = self.m.actions.get(str(args.get("action", "")))
        if act is None:
            return READ
        if act.sends:
            return SEND  # başkasına gidiyor: geri alınamaz
        return WRITE if act.writes else NETWORK

    def summary(self, args: dict[str, Any]) -> str:
        rest = {k: v for k, v in args.items() if k != "action" and v not in (None, "")}
        tail = " " + " ".join(f"{k}={v}" for k, v in list(rest.items())[:3]) if rest else ""
        return f"{self.m.label} · {args.get('action', '?')}{tail}"[:160]

    def run(self, ctx: ToolContext, action: str = "", **args: Any) -> ToolResult:
        act = self.m.actions.get(action)
        if act is None:
            raise ToolError(f"{self.name}: bilinmeyen eylem {action!r}. Geçerli: {', '.join(sorted(self.m.actions))}")
        hazır, sebep = ready(self.m)
        if not hazır:
            raise ToolError(f"{self.m.label} hazır değil: {sebep}")
        limit = int(ctx.config.agent.get("tool_output_chars", 12000)) if ctx.config else 12000
        try:
            data = call(self.m, act, args)
        except ConnectError as e:
            raise ToolError(str(e)) from e
        return ToolResult(render(data, act, limit))


def connector_tools(cfg: Any = None, emit: Any = None, reserved: set[str] | None = None) -> list[ConnectTool]:
    """Bulunan tanımlardan araç üret. Yerleşik bir araçla aynı ada sahip tanım atlanır."""
    from alice.connect.manifest import discover

    from alice.tools import groups

    manifests, _errors = discover(cfg, emit)
    out = []
    for m in manifests.values():
        if reserved and m.name in reserved:
            if emit:
                emit(f"bağlantı atlandı: '{m.name}' zaten bir araç adı")
            continue
        out.append(ConnectTool(m))
    groups.register_connect(t.name for t in out)  # "connect" grubunda toplansınlar
    return out
