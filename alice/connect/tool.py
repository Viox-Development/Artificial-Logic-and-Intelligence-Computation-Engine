"""Generate ALICE tool from manifest: each connection is a single tool, actions are selected via `action` parameter."""

from __future__ import annotations

from typing import Any

from alice.connect import verify
from alice.connect.manifest import Manifest
from alice.connect.runner import ConnectError, call, ready, render
from alice.tools.base import NETWORK, READ, SEND, WRITE, Tool, ToolContext, ToolError, ToolResult

TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean", "array": "array"}


class ConnectTool(Tool):
    """Tool for a service. Schema is generated from manifest, so the model does not guess parameters."""

    parallel_safe = True
    risk = NETWORK
    untrusted = True  # message/title/description from service may be text written by someone else

    def __init__(self, m: Manifest):
        self.m = m
        self.name = m.name
        acts = m.actions
        lines = [f"- {a.name}: {a.summary or a.path}" for a in acts.values()]
        hazır, sebep = ready(m)
        self.description = (f"{m.description or m.label}\nActions:\n" + "\n".join(lines)
                            + ("" if hazır else f"\n(not available right now: {sebep})"))
        props: dict[str, Any] = {"action": {"type": "string", "enum": sorted(acts),
                                            "description": "The action to run."}}
        for a in acts.values():
            for p in a.params.values():
                prev = props.get(p.name)
                desc = f"[{a.name}] {p.description}".strip()
                if prev:  # same name in multiple actions: merge descriptions
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
            return SEND  # going to someone else: irreversible
        return WRITE if act.writes else NETWORK

    def summary(self, args: dict[str, Any]) -> str:
        rest = {k: v for k, v in args.items() if k != "action" and v not in (None, "")}
        tail = " " + " ".join(f"{k}={v}" for k, v in list(rest.items())[:3]) if rest else ""
        return f"{self.m.label} · {args.get('action', '?')}{tail}"[:160]

    def run(self, ctx: ToolContext, action: str = "", **args: Any) -> ToolResult:
        act = self.m.actions.get(action)
        if act is None:
            raise ToolError(f"{self.name}: unknown action {action!r}. Valid: {', '.join(sorted(self.m.actions))}")
        hazır, sebep = ready(self.m)
        if not hazır:
            raise ToolError(f"{self.m.label} is not ready: {sebep}")
        limit = int(ctx.config.agent.get("tool_output_chars", 12000)) if ctx.config else 12000
        try:
            data = call(self.m, act, args)
        except ConnectError as e:
            fixed = verify.recover(self.m, act, args, e)
            if fixed:
                return ToolResult(fixed, is_error=fixed.startswith("UYARI"))
            raise ToolError(str(e)) from e
        text = render(data, act, limit)
        note = verify.after(self.m, act, args, data)
        return ToolResult(f"{text}\n{note}" if note else text)


def connector_tools(cfg: Any = None, emit: Any = None, reserved: set[str] | None = None) -> list[ConnectTool]:
    """Generate tools from discovered definitions. Definitions sharing a name with a builtin tool are skipped."""
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
    groups.register_connect(t.name for t in out)  # collect them under the "connect" group
    return out
