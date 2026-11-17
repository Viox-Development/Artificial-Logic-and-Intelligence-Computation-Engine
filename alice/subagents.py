"""Subagents: agents started by the `task` tool that work with their own clean context (like the Agent tool in
Claude Code).

Before, a subagent locked the chat and ran one at a time, wrote to the main chat's screen, and was forgotten when done:
- Parallel: multiple `task` calls in the same response run at the same time.
- Background: starts with background=true and the chat goes on; when it finishes, its result arrives in the chat as an
  "[Alt ajan ...]" message.
- Resume: send a new message to a finished subagent by its id; its conversation (what it read and found) is kept.
- Visibility: each subagent's status, step and last tool show live in the side panel's "Agents" tab; from there
  it can be stopped or sent a follow-up message.

Each subagent has its own tool context (a shallow copy of the main chat's state dictionary): pressing Stop in the main
chat must not stop a background subagent's tools, and a subagent's Stop must not affect the main chat. Permission
questions go to the main chat's interface with the subagent's name (one at a time).
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from alice.tools.base import ToolContext

KEEP = 30  # subagents remembered per chat
LIVE_EVERY = 0.4  # seconds: live status is published at this interval at most
_perm_lock = threading.Lock()  # keeps parallel subagents' permission questions from overlapping
_state_lock = threading.Lock()  # registry and status changes (parallel task calls, two follow-ups to the same agent)


@dataclass
class Sub:
    id: str
    label: str
    prompt: str
    kind: str = ""  # name of the custom agent definition; empty: general
    background: bool = False
    max_steps: int = 40
    status: str = "running"  # running | done | failed | stopped
    result: str = ""
    steps: int = 0
    last: str = ""
    turns: int = 1
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    child: Any = None
    thread: threading.Thread | None = None
    history: list[dict[str, str]] = field(default_factory=list)  # [{"q": request, "a": result}]
    history_messages: list[dict[str, Any]] = field(default_factory=list)  # from disk: conversation (excluding system)

    def snapshot(self) -> dict[str, Any]:
        now = self.ended or time.time()
        return {"id": self.id, "label": self.label, "kind": self.kind, "background": self.background,
                "status": self.status, "steps": self.steps, "last": self.last, "turns": self.turns,
                "prompt": self.prompt[:600], "result": self.result[:4000], "secs": int(now - self.started),
                "history": [{"q": h["q"][:300], "a": h["a"][:600]} for h in self.history[-5:]]}


class SubUI:
    """The subagent's interface: model and tool events are not written to the main screen, only the status is updated.
    Permission questions go to the main chat's interface under the subagent's name."""

    def __init__(self, sub: Sub, parent_ui: Any, publish: Callable[[dict[str, Any]], Any] | None):
        self.sub, self.parent_ui, self.publish = sub, parent_ui, publish
        self._last = 0.0

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def changed(self, force: bool = False) -> None:
        now = time.monotonic()
        if self.publish is None or (not force and now - self._last < LIVE_EVERY):
            return
        self._last = now
        try:
            self.publish({"type": "subagent", **self.sub.snapshot()})
        except Exception:  # noqa: BLE001 - let the work continue even without an interface
            pass

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.sub.steps += 1
        self.sub.last = f"{name} {summary}".strip()[:160]
        self.changed()

    def info(self, msg: str) -> None:
        if "adım limiti" in msg or "hiçbir model" in msg:
            self.sub.last = msg.splitlines()[0][:160]
            self.changed()

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        ask = getattr(self.parent_ui, "ask_permission", None)
        if ask is None:
            return "n"
        text = f"[alt ajan {self.sub.id} · {self.sub.label}] {summary}"
        child = self.sub.child
        with _perm_lock:
            try:  # the subagent's own Stop closes its question, not the main chat's (refused in a stopped turn)
                ans = ask(name, text, risk, cancelled=child.cancel.is_set if child is not None else None)
            except TypeError:  # interface without cancelled (terminal, tests)
                ans = ask(name, text, risk)
        return "y" if ans == "a" else ans  # "always" stays with the main session


def registry(ctx: ToolContext) -> dict[str, Sub]:
    return ctx.state.setdefault("subagents", {})


def _next_id(ctx: ToolContext) -> str:
    counter = ctx.state.setdefault("subagent_ids", itertools.count(1))
    return f"a{next(counter)}"


def child_context(parent_ctx: ToolContext) -> ToolContext:
    """The subagent's tool context: same folder and config, a shallow copy of the state (browser, processes and publish
    hooks are shared), but `agent` becomes the subagent itself so that Stop does not mix the two up."""
    state = dict(parent_ctx.state)
    # The screen/browser lock belongs to the main chat. If a separate context counted as a separate owner, the subagent
    # would wait on the main chat and deadlock, or never release a lock it took (a subagent never releases it).
    state["lock_owner"] = parent_ctx.state.get("lock_owner") or parent_ctx
    # Own records: foreground commands (killed on Stop, 'Durdur') and browser busy state are kept separate.
    for k in ("subagents", "subagent_ids", "fg_procs", "browser_busy"):
        state.pop(k, None)
    return ToolContext(cwd=parent_ctx.cwd, config=parent_ctx.config, router=parent_ctx.router,
                       decider=parent_ctx.decider, state=state, emit=lambda s: None)


def build_child(parent: Any, kind: str, ui: Any, ctx: ToolContext) -> tuple[Any, int]:
    """(subagent, default step limit). kind: name of the custom agent definition; empty: a copy of the main agent."""
    from alice.agent import Agent

    if kind:
        from alice.skills import load_agents, spawn

        adef = load_agents(parent.ctx.cwd).get(kind.strip().lower())
        if adef is None:
            from alice.tools.base import ToolError

            names = ", ".join(load_agents(parent.ctx.cwd)) or "(yok)"
            raise ToolError(f"'{kind}' adında alt ajan yok. Tanımlı olanlar: {names}")
        child = spawn(parent, adef, ctx=ctx, ui=ui)
        steps = adef.max_steps
    else:
        child = Agent(parent.config, parent.router, list(parent.tools.values()), ctx, parent.permissions, ui,
                      getattr(parent, "decider", None), system_prompt=parent.messages[0]["content"], is_child=True)
        steps = 40
    ctx.state["agent"] = child
    return child, steps


def _make_ui(sub: Sub, parent: Any, ctx: ToolContext) -> SubUI:
    """Live events go to the chat that started the subagent, even if the main chat later switches to another chat."""
    parent_ui = getattr(parent, "ui", None)
    bus, sid = getattr(parent_ui, "bus", None), str(ctx.state.get("session_id") or "")
    if bus is not None and sid:
        def publish(ev: dict[str, Any]) -> None:
            bus.publish({**ev, "sid": sid})
    else:
        publish = getattr(parent_ui, "publish", None)
    return SubUI(sub, parent_ui, publish)


def start(parent: Any, ctx: ToolContext, prompt: str, kind: str = "", label: str = "", background: bool = False,
          max_steps: int | None = None) -> Sub:
    with _state_lock:
        sub_id = _next_id(ctx)
    sub = Sub(id=sub_id, label=(label or kind or prompt.split("\n")[0])[:48].strip() or "alt ajan",
              prompt=prompt, kind=kind, background=background)
    sub.child, steps = build_child(parent, kind, _make_ui(sub, parent, ctx), child_context(ctx))
    sub.max_steps = int(max_steps or steps)
    with _state_lock:
        subs = registry(ctx)
        subs[sub.id] = sub
        for old in [k for k, s in list(subs.items()) if s.status != "running"][:-KEEP]:
            subs.pop(old, None)
    _go(ctx, sub, prompt)
    return sub


def resume(ctx: ToolContext, sub_id: str, text: str, background: bool = False) -> Sub:
    from alice.tools.base import ToolError

    with _state_lock:  # check and "running" mark together: two follow-ups must not run one agent twice
        sub = registry(ctx).get(sub_id.strip())
        if sub is None:
            raise ToolError(f"böyle bir alt ajan yok: {sub_id}. Bu sohbettekiler: "
                            f"{', '.join(registry(ctx)) or '(yok)'}")
        if sub.status == "running":
            raise ToolError(f"{sub.id} hâlâ çalışıyor; bitmesini bekle ya da durdur")
        prev_status, sub.status = sub.status, "running"
    if sub.child is None:  # app was reopened: the subagent is re-created with its conversation from the record
        parent = ctx.state.get("agent")
        try:
            if parent is None:
                raise ToolError("alt ajan bu bağlamda sürdürülemiyor")
            sub.child, _ = build_child(parent, sub.kind, _make_ui(sub, parent, ctx), child_context(ctx))
        except Exception:
            sub.status = prev_status
            raise
        sub.child.messages = sub.child.messages[:1] + sub.history_messages
        sub.history_messages = []
    sub.turns += 1
    sub.result, sub.ended, sub.background = "", 0.0, background
    sub.started = time.time()
    _go(ctx, sub, text)
    return sub


def _go(ctx: ToolContext, sub: Sub, text: str) -> None:
    ui: SubUI = sub.child.ui
    ui.changed(force=True)
    sid = str(ctx.state.get("session_id") or "")
    to_sid = ctx.state.get("deliver_to")  # (chat, text): to the chat that started it, even if the main chat moved on
    deliver = (lambda t: to_sid(sid, t)) if callable(to_sid) and sid else ctx.state.get("deliver")
    parent = ctx.state.get("agent")

    def run() -> None:
        try:
            out = sub.child.run_turn(text, max_steps=sub.max_steps)
            sub.result = out or "(alt ajan boş cevap döndürdü)"
            sub.status = "stopped" if sub.child.cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001 - the subagent's error must not bring down the chat
            sub.result, sub.status = f"alt ajan hata verdi: {e}", "failed"
        sub.ended = time.time()
        sub.history.append({"q": text, "a": sub.result})
        ui.changed(force=True)
        # It may have taken the screen lock in the main chat's name: release it if the main chat is not running now
        # (if it is running, it releases the lock itself when its turn ends).
        state = getattr(getattr(sub.child, "ctx", None), "state", None) or {}
        lock, owner = state.get("desktop_lock"), state.get("lock_owner")
        if lock is not None and owner is not None and not getattr(parent, "busy", False):
            lock.release(owner)
        if sub.background and sub.status != "stopped" and callable(deliver):
            deliver(f"[Alt ajan {sub.id} · {sub.label} {'bitti' if sub.status == 'done' else 'başarısız'}]\n"
                    f"{sub.result[:6000]}\n\n(Devam ettirmek için: task id={sub.id} prompt=...)")

    sub.thread = threading.Thread(target=run, daemon=True, name=f"alice-sub-{sub.id}")
    sub.thread.start()


def wait(sub: Sub, cancelled: Callable[[], bool]) -> None:
    """Wait for a subagent running in the foreground; if Stop is pressed in the main chat, stop the subagent too."""
    while sub.thread is not None and sub.thread.is_alive():
        if cancelled():
            stop(sub)
        sub.thread.join(0.25)


def stop(sub: Sub) -> bool:
    if sub.status != "running" or sub.child is None:
        return False
    sub.child.stop()
    return True


MAX_SAVED_MESSAGES = 200


def export(ctx: ToolContext) -> list[dict[str, Any]]:
    """Subagents to write to the session file: status, result and conversation (so they can resume after reopening)."""
    from alice.providers.router import strip_images

    out = []
    for s in list(registry(ctx).values()):
        msgs = list(s.child.messages[1:]) if s.child is not None else s.history_messages
        out.append({**s.snapshot(), "prompt": s.prompt, "result": s.result, "max_steps": s.max_steps,
                    "started": s.started, "ended": s.ended, "history": s.history[-10:],
                    "messages": strip_images(msgs[-MAX_SAVED_MESSAGES:])})
    return out


def restore(ctx: ToolContext, rows: list[dict[str, Any]]) -> None:
    """Re-create the saved subagents. Those that were closed while running count as "stopped" (durduruldu); all of them
    can be resumed (their conversation is loaded into the subagent re-created by the follow-up message)."""
    subs = registry(ctx)
    top = 0
    for d in rows:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        sub = Sub(id=str(d["id"]), label=str(d.get("label") or "alt ajan"), prompt=str(d.get("prompt") or ""),
                  kind=str(d.get("kind") or ""), background=bool(d.get("background")),
                  max_steps=int(d.get("max_steps") or 40),
                  status=d.get("status") if d.get("status") in ("done", "failed", "stopped") else "stopped",
                  result=str(d.get("result") or ""), steps=int(d.get("steps") or 0), last=str(d.get("last") or ""),
                  turns=int(d.get("turns") or 1), started=float(d.get("started") or 0),
                  ended=float(d.get("ended") or d.get("started") or 0), history=list(d.get("history") or []))
        sub.history_messages = list(d.get("messages") or [])
        subs[sub.id] = sub
        try:
            top = max(top, int(sub.id.lstrip("a")))
        except ValueError:
            pass
    if top:
        ctx.state["subagent_ids"] = itertools.count(top + 1)


def snapshots(ctx: ToolContext) -> list[dict[str, Any]]:
    return [s.snapshot() for s in reversed(list(registry(ctx).values()))]
