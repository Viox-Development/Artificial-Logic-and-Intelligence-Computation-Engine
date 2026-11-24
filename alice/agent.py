"""Main agent loop (Claude Code / Codex style).

user message -> model (with tools) -> if there are tool_calls, permission check + run ->
feed the results back -> the turn ends when the model stops calling tools.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from alice.config import Config, alice_home
from alice.decider import Decider
from alice.i18n import _
from alice.permissions import Permissions
from alice.prompts import COMPACT_PROMPT, build_system_prompt
from alice.providers.openai_compat import ChatResult, StreamCancelled
from alice.session import (ACK_SUMMARY, NUDGE_PREFIX, PLAN_NOTE, SHOT_REMOVED, STOPPED, SUMMARY_HEAD, is_summary,
                           strip_plan_note, summary_body)
from alice.providers.router import (COOLING_RX, AllProvidersFailed, Router, cache_write_tokens, cached_tokens,  # noqa: F401
                                    estimate_tokens, strip_images)
from alice.tools.base import COMPUTER, DANGER, SEND, Tool, ToolContext, ToolError, ToolResult, check_args, truncate, unknown_params

# Trace for swallowed errors: silent by default, visible with `logging.basicConfig(level=logging.DEBUG)`.
log = logging.getLogger(__name__)


class UI(Protocol):
    """Contract for how the interface (terminal or desktop app) displays agent events."""

    def stream_start(self) -> None: ...  # model reply is starting ("thinking" until the first chunk arrives)
    def stream_delta(self, text: str) -> None: ...
    # Optional: def thinking_delta(self, text: str) -> None  (the model's reasoning text)
    def stream_reset(self) -> None: ...  # model dropped mid-stream, the fallback model will rewrite from the start
    def stream_end(self, text: str, source: str) -> None: ...
    def tool_start(self, call_id: str, name: str, summary: str) -> None: ...
    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None: ...
    def ask_permission(self, name: str, summary: str, risk: str) -> str: ...  # y | n | a
    def info(self, msg: str) -> None: ...


class NullUI:
    def __init__(self, approve: str = "y"):
        self.approve = approve
        self.events: list[tuple[str, Any]] = []

    def stream_start(self):
        self.events.append(("start", None))

    def stream_delta(self, text):
        self.events.append(("delta", text))

    def stream_reset(self):
        self.events.append(("reset", None))

    def stream_end(self, text, source):
        if text:
            self.events.append(("text", text))

    def tool_start(self, call_id, name, summary):
        self.events.append(("tool", name))

    def tool_end(self, call_id, name, result):
        self.events.append(("result", result.text))

    def ask_permission(self, name, summary, risk):
        self.events.append(("ask", name))
        return self.approve

    def info(self, msg):
        self.events.append(("info", msg))


class Cancelled(Exception):
    pass


class _Sink:
    def __init__(self, ui: UI, cancel: threading.Event):
        self.ui = ui
        self.cancel = cancel
        self.dead = False  # if the turn was stopped, a model call still running in the background must no longer write to the UI
        self.parts: list[str] = []

    first_at = 0.0  # moment the first text arrived in this model call (session statistic: time to first word)

    def on_delta(self, text: str) -> None:
        if self.dead:
            return
        if not self.first_at:
            self.first_at = time.monotonic()
        self.parts.append(text)
        self.ui.stream_delta(text)

    early: Any = None  # agent function that starts a completed tool call before the reply is finished
    early_reset: Any = None

    def on_reset(self) -> None:
        if self.early_reset is not None:
            self.early_reset()  # model changed mid-stream: results of early-started calls are not used
        if self.dead:
            return
        self.parts.clear()
        self.ui.stream_reset()

    def on_call(self, call: dict[str, Any]) -> None:
        if self.early is not None and not self.dead and not self.cancel.is_set():
            self.early(call)

    def on_reasoning(self, text: str) -> None:
        if not self.first_at:
            self.first_at = time.monotonic()
        fn = getattr(self.ui, "thinking_delta", None)
        if fn is not None and not self.dead:
            fn(text)

    def on_tool(self, name: str, size: int) -> None:
        if not self.first_at:
            self.first_at = time.monotonic()
        fn = getattr(self.ui, "tool_delta", None)
        if fn is not None and not self.dead:
            fn(name, size)

    def cancelled(self) -> bool:
        return self.dead or self.cancel.is_set()


def _call_key(call: dict[str, Any]) -> str:
    return call["function"]["name"] + ":" + (call["function"].get("arguments") or "").strip()


REPEAT_FAIL_NOTE = ("\n\n[ALICE note] This call has now failed {n} times with the same arguments. Don't repeat it: read "
                    "the error, think about the root cause, try a different tool or approach; if it really can't be "
                    "done, ask the user.")
ERROR_STREAK_NOTE = ("\n\n[ALICE note] The last {n} tool calls failed. Stop and think before going on: what do the error "
                     "messages say, are your assumptions (environment, shell syntax, file path, whether the program is "
                     "installed) right? Pick a new way and say briefly why it will work.")
REPEAT_OK_NOTE = ("\n\n[ALICE note] You made the same call {n} times and the result doesn't seem to progress. Don't loop: "
                  "try something different or finish the job with what you have.")


_TOO_BIG = re.compile(r"(?:request ~(\d+) tokens, model limit|istek ~(\d+) token, model sınırı) (\d+)")


def lean_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Short form of the tool specs: first sentence of the description, parameter descriptions trimmed (narrow-context models)."""
    out = []
    for sp in specs:
        fn = dict(sp.get("function") or {})
        desc = (fn.get("description") or "").strip().split("\n")[0]
        fn["description"] = desc[:160]
        params = json.loads(json.dumps(fn.get("parameters") or {}))
        for prop in (params.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("description"):
                prop["description"] = str(prop["description"])[:60]
        fn["parameters"] = params
        out.append({**sp, "function": fn})
    return out


def fits_after_compact(errors: list[str]) -> int:
    """If models failed only because the conversation didn't fit their context, the largest context (tokens) that fits; else 0."""
    limits = [int(m.group(3)) for e in errors if (m := _TOO_BIG.search(e))]
    big = [int(m.group(1) or m.group(2)) for e in errors if (m := _TOO_BIG.search(e))]
    # If a large model is busy only briefly (a few minutes of waiting, rate limit), don't shrink the conversation
    # permanently: waiting is better. An hourly-quota wait, however, is not short.
    for e in errors:
        w = COOLING_RX.search(e)
        if (w and int(w.group(1)) < 300) or "rate_limit" in e or "429" in e:
            return 0
    if not limits or max(limits) < 4000 or (big and max(limits) >= max(big)):
        return 0
    return max(limits)


def parse_args(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is not None and not isinstance(raw, str):
        # Some providers send the arguments as a list/number: don't crash, return it to the model as an error.
        raise json.JSONDecodeError("argümanlar obje değil", str(raw), 0)
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        # Some models append extra text or a code fence at the end.
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            raise
        val = json.loads(m.group(0))
    if not isinstance(val, dict):
        raise json.JSONDecodeError("argümanlar obje değil", str(raw), 0)
    return val


def _sanitize_calls(msg: dict[str, Any]) -> None:
    """If the name or arguments in the model's tool_calls are not strings (null, list, number), convert them to strings:
    otherwise the loop crashes and the malformed call that went into the history made every later request fail with 400."""
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function")
        if not isinstance(fn, dict):
            fn = tc["function"] = {}
        if not isinstance(fn.get("name"), str):
            fn["name"] = "" if fn.get("name") is None else str(fn["name"])
        args = fn.get("arguments")
        if not isinstance(args, str):
            fn["arguments"] = "{}" if args is None else json.dumps(args, ensure_ascii=False)


# Summarization limit used when no model is known yet (auto mode without a ref).
LEGACY_COMPACT_AT = 48000


# The heading of the loadable-tools list in the system prompt (a chat saved before it became English has the old one)
_CATALOG_RX = re.compile(r"\n\n# (?:Loadable tools|Yüklenebilir araçlar)\n.*?(?=\n\n# |\n\n<|\Z)", re.S)
# Denial counter: the turn stops after this many denied calls in a row, or this many in total in one turn. An unattended
# agent (KAIROS, PR watching, tasks) or plan mode kept retrying the same thing in another form after each denial.
DENIAL_LIMITS = (3, 20)
# What ALICE tells the user when a turn is cut short. English here; shown through _() in the interface language.
DENIAL_HALT = ("I stopped because I kept trying actions that aren't allowed: this can't be done in the current permission "
               "mode. If you change the permission mode, or tell me what you want me to do and write \"continue\", I'll "
               "carry on.")
BUDGET_HALT = ("I stopped because I used up the token budget for this turn ({used:,} / {budget:,}). The job is half done; "
               "if you write \"continue\" I'll carry on from where I was. (Limit: [agent] turn_token_budget)")
MAX_CUTOFFS = 3  # a reply cut off at the output limit is continued at most this many times
CUTOFF_NOTE = ("Your answer hit the output limit and was cut off. Continue exactly where you stopped, without "
               "repeating the earlier part and without apologizing.")
KEEP_OUTPUTS = 6  # the last this-many tool outputs stay in full
OLD_OUTPUT_CHARS = 1200  # long older ones are shortened to this many characters
# Changing a message in the history invalidates the provider's prompt cache from that point on. Old outputs are
# trimmed only once the cache has already gone cold (this many seconds after the last request); while it is warm the
# history is never rewritten (the earlier "batch of 8 once past half the threshold" rule rewrote nearly the whole
# conversation into the cache every ~8 steps). A growing conversation is handled by summarization instead.
CACHE_COLD_S = 300
CACHE_LONG_S = 55 * 60  # cliproxy writes a 1-hour cache; leave a small margin before it expires
IMAGE_SLACK = 3  # screenshots are pruned in bulk once there are keep_images + this many
# "auto" summarization: at most this many tokens, even on 1M-window models. Every step resends the whole conversation;
# at 80% of a 1M window (800K; cliproxy is configured with max_input_tokens = 1000000) a single step of a long chat
# cost as much as a whole short chat and burned the subscription quota. [agent] compact_max_tokens, 0 = no cap.
AUTO_COMPACT_MAX = 120_000
# "auto" summarization threshold as a share of the model's context window.
AUTO_COMPACT_RATIO = 0.6
# Tools whose definitions are sent at once even with a warm cache (plan mode must always be able to exit).
ALWAYS_SEND = frozenset({"exit_plan_mode", "todo_write"})
# If the end of the reply says "I will do it" ("yapacağım"), the work is unfinished.
_PROMISE_RX = re.compile(
    r"(yapaca[ğg][ıi]m|edece[ğg]im|ba[şs]l[ıi]yorum|bakaca[ğg][ıi]m|deneyece[ğg]im|ekleyece[ğg]im|d[üu]zeltece[ğg]im|"
    r"[çc]al[ıi][şs]t[ıi]raca[ğg][ıi]m|kontrol edece[ğg]im|yazaca[ğg][ıi]m|kuraca[ğg][ıi]m|indirece[ğg]im|"
    r"\blet me\b|\bi'll\b|\bi will\b|\bnext,? i\b|\bnow i\b)[^.!?]{0,60}[.:…]*\s*$", re.I)
# "I'll write when it's done" ("Bitince yazacağım"): the work was handed to the background (project, long command),
# and the promise depends on its completion. This is not unfinished work; on 11 November, when this reply was
# answered with "you stopped without doing it" ("yapmadan durdun"), ALICE waited 30 min for the background project and burned the quota.
_DEFERRED_RX = re.compile(
    r"\b(bitince|bitti[ğg]inde|tamamlan[ıi]nca|tamamland[ıi][ğg][ıi]nda|haz[ıi]r olunca|sonu[çc]lan[ıi]nca|"
    r"arka planda|once (it|they)('s| is| are)? (done|finished)|when (it|they) (finish|complete)\w*)\b", re.I)


def _valid_summary(text: str) -> bool:
    """Is the summary really a summary? Text that is empty/too short, in tool-call form, or a copy of the transcript is not."""
    t = text.strip()
    if len(t) < 8:
        return False
    low = t[:400].lower()
    if re.match(r"\s*(<\s*(tool|function|invoke)|\{\s*\"(name|tool|function)\"|->\s*\w+\(|\[(assistant|tool|user)\])", low):
        return False
    echoed = len(re.findall(r"(?m)^\[(assistant|tool|user)\]|<parameter name=|<tool call|->\s*\w+\(\{", t))
    return echoed < 4


def _is_request(m: dict[str, Any]) -> bool:
    """A message the user really wrote (not a reminder, a summary or a screenshot note)."""
    c = m.get("_display") or m.get("content")
    if isinstance(c, list):
        c = next((p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"), "")
    return (m.get("role") == "user" and not m.get("_nudge") and not m.get("_image") and isinstance(c, str)
            and bool(c.strip()) and not is_summary(c))


RUNNING_HEAD = "## Ongoing work: the user's latest request (verbatim)"
OLD_RUNNING_HEAD = "## Süren iş: kullanıcının son isteği (birebir)"


def _last_request(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        c = m.get("content")
        if m.get("role") == "user" and is_summary(c):
            for head in (RUNNING_HEAD, OLD_RUNNING_HEAD):
                if head in c:
                    return c.split(head, 1)[1].strip()  # an earlier summary already carried it: carry it on
        if _is_request(m):
            c = m.get("_display") or m.get("content")
            if isinstance(c, list):
                c = next((p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"), "")
            return str(c).strip()
    return ""


def _fallback_summary(old: list[dict[str, Any]]) -> str:
    """Summary without a model: what the user wrote in order (shortened), touched files and the last assistant reply."""
    asks, files, last = [], [], ""
    for m in old:
        c = m.get("content")
        if m.get("role") == "user" and not m.get("_nudge") and isinstance(c, str) and c.strip():
            if is_summary(c):
                asks.append("(earlier summary) " + _clip(summary_body(c), 1500))
            else:
                asks.append(_clip(m.get("_display") or c, 600))
        elif m.get("role") == "assistant" and not m.get("_error"):
            if isinstance(c, str) and c.strip():
                last = c
            for tc in m.get("tool_calls") or []:
                if tc["function"]["name"] in ("edit_file", "write_file", "multi_edit", "apply_patch"):
                    hit = re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', tc["function"].get("arguments") or "")
                    if hit and hit.group(1) not in files:
                        files.append(hit.group(1))
    parts = ["## What the user wrote (in order)"] + [f"{i}. {a}" for i, a in enumerate(asks[-25:], 1)]
    if files:
        parts += ["", "## Changed files"] + [f"- {f}" for f in files[-40:]]
    if last:
        parts += ["", "## My last answer", _clip(last, 2500)]
    parts += ["", "(This summary was extracted automatically; if you need details, re-read the files and the git state with the tools.)"]
    return "\n".join(parts)


# Reply that reports a result: the evidence check runs only on these (so there is no extra model call on every reply)
_CLAIM_RX = re.compile(
    r"(yapt[ıi]m|kurdum|kuruldu|indirdim|indirildi|ba[şs]latt[ıi]m|a[çc]t[ıi]m|a[çc][ıi]ld[ıi]|g[öo]nderdim|"
    r"g[öo]nderildi|tamamlad[ıi]m|tamamland[ıi]|bitti|ayarlad[ıi]m|kaydettim|kaydedildi|sildim|silindi|"
    r"y[üu]klendi|[çc]al[ıi][şs][ıi]yor|haz[ıi]r|\byok\b|bulunamad[ıi]|bulamad[ıi]m|mevcut de[ğg]il|"
    r"\bdone\b|\binstalled\b|\bdownloaded\b|\bnot found\b)", re.I)


def _clip(text: str, limit: int) -> str:
    """Keep the head and tail of a long tool output (the result is usually at the end), shorten the middle with '…'."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + "\n…\n" + text[-(limit - head):]


class Agent:
    def __init__(
        self,
        config: Config,
        router: Router,
        tools: list[Tool],
        ctx: ToolContext,
        permissions: Permissions,
        ui: UI | None = None,
        decider: Decider | None = None,
        system_prompt: str | None = None,
        is_child: bool = False,
    ):
        self.config = config
        self.router = router
        self.tools = {t.name: t for t in tools if not (is_child and t.name in ("task", "subagents"))}
        self.ctx = ctx
        self.permissions = permissions
        self.ui = ui or NullUI()
        self.decider = decider
        self.is_child = is_child
        self.role = "brain"  # the voice assistant uses "voice": models whose first word arrives fastest
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt or build_system_prompt(ctx.cwd)}]
        a = config.agent
        self.max_steps = int(a.get("max_steps", 80))
        self._compact_cfg = a.get("compact_at_tokens", "auto")
        # Upper limit (0 = none). Set for project agents and the council: on a 1M-context model every step sent
        # hundreds of thousands of tokens (one project ~100M tokens).
        self.compact_cap = 0
        # Context size right after the last summarization (-1 = measure at the next check). If summarizing could not
        # bring the context under the threshold (system prompt + tool specs alone near it), it re-summarized on EVERY
        # step: each time a summary call plus a ~40K-token cache rewrite (Nov 22, quota burned in minutes).
        self._post_compact = 0
        self._last_prompt: tuple[int, int, Any] | None = None  # (tokens counted by the provider, message count, last message)
        self.output_limit = int(a.get("tool_output_chars", 12000))
        self.keep_images = int(a.get("keep_images", 2))
        self._last_call_at = 0.0  # moment the last model call finished (monotonic): has the cache gone cold
        # Prompt cache TTL for the Claude API, latched for the whole session: flipping between 5m and 1h mid-chat
        # also rewrites the cache (Claude Code latches it for the same reason). Decided on the first model call.
        self._cache_ttl_latched: str | None = None
        from alice.providers.prompt_cache import CacheBreakDetector

        self.cache_breaks = CacheBreakDetector(log_dir=None if is_child else alice_home() / "logs")
        self.stream = bool(a.get("stream", True))
        # If, while working with tools, it stops before the job is done (saying "şimdi testleri çalıştıracağım" ("now I'll
        # run the tests") and going silent, leaving an unfinished step in the todo list), it is reminded to continue at
        # most this many times. 0 = off.
        self.finish_nudges = int(a.get("finish_nudges", 2))
        try:
            from alice.hooks import Hooks

            self.hooks = Hooks.from_config(config, ctx.cwd)  # the user's hooks (config.toml / hooks.toml)
        except Exception:  # noqa: BLE001 - a broken hook setting must not make the agent unable to start
            from alice.hooks import Hooks

            self.hooks = Hooks([], ctx.cwd)
        self.cancel = threading.Event()
        # If the stop request is also kept outside (team member): run_turn checks this after clearing the flag.
        # That way a Stop that lands between the check and the start of the turn is not lost.
        self.stop_check: Any = None
        self.busy = False
        # User messages that arrive while working (steer): they enter the conversation before the next model step.
        self._inbox: list[tuple[str, list[str]]] = []
        self._inbox_lock = threading.Lock()
        self._steering = False  # is the turn at a point where it can take the inbox (closed in finally)
        self._call_counts: dict[str, int] = {}
        self._fail_counts: dict[str, int] = {}
        self._error_streak = 0
        # Session statistics (panels in the status bar): turns, steps, model and tool time, time to first word, tokens.
        self.stats: dict[str, float] = {"turns": 0, "steps": 0, "llm_s": 0.0, "tool_s": 0.0, "ttft_s": 0.0,
                                        "ttft_n": 0, "gen_s": 0.0, "gen_tokens": 0, "input": 0, "cached": 0, "written": 0,
                                        "output": 0}
        # Usage breakdown: which model the tokens went to, which tools' outputs the context went to
        self.usage_models: dict[str, dict[str, float]] = {}
        self.usage_tools: dict[str, dict[str, float]] = {}
        # Called after every tool step (to save the session): so no trace is lost if the turn hangs or crashes.
        self.on_step: Any = None
        self.on_compact: Any = None  # messages dropped from the context during summarization are passed here (the app writes them to the archive)
        self.on_usage: Any = None  # usage info of every model reply (tokens, cost if any) is also reported here
        # Long-term memory (alice.memory.LongMemory): whoever sets up the session attaches it; None in tests and unattended tasks.
        self.memory: Any = None
        self._memory_surfaced: set[str] = set()  # notes already added to a message in this chat (so they are not added again)
        self.memory_channel = ""
        self._memory_version: Any = -1
        # For rewind: index of the user message -> the old state of the files first changed in that turn.
        self.checkpoints: dict[int, dict[str, bytes | None]] = {}
        if not is_child:
            ctx.state["agent"] = self

    # ------------------------------------------------------------------ public
    def tool_specs(self) -> list[dict[str, Any]]:
        """Tools sent to the model: core tools + groups opened in this session (see tools/groups.py)."""
        if str(self.config.agent.get("tool_groups", "auto")) == "all":
            return [t.spec() for t in self.tools.values()]
        from alice.tools.groups import visible_tools

        tools = visible_tools(self.tools, self.ctx.state.setdefault("tool_groups", set()))
        # The order is fixed: tools sent earlier stay in place, tools of a newly opened group are appended at the end.
        # The tool list is at the start of the request; when a tool entered the middle, the prompt cache was wasted
        # for everything after that point in the list.
        order: list[str] = self.__dict__.setdefault("_spec_order", [])
        by = {t.name: t for t in tools}
        new = [n for n in by if n not in order]
        if new and order and not self.__dict__.get("_flush_tools") and self._defer_new_tools():
            # Warm cache: the tool list is at the very start of the request (in Claude even before the system prompt), so a
            # single new tool made the whole conversation be rewritten to the cache. Newly opened tools join the list in a
            # batch once the cache has gone cold or after compaction; until then they work when called by name, and their
            # definitions reach the model in the load_tools result (at the end of the conversation, without breaking the cache).
            keep = [n for n in new if n in ALWAYS_SEND]
            self.__dict__["_deferred_tools"] = {n for n in new if n not in keep}
            order.extend(keep)
        else:
            order.extend(new)
            self.__dict__["_deferred_tools"] = set()
            self.__dict__.pop("_flush_tools", None)
        return [by[n].spec() for n in order if n in by]

    DEFER_TOOLS_TOKENS = 20000  # in a conversation shorter than this, rewriting the cache is cheap: the definition is sent right away

    def _defer_new_tools(self) -> bool:
        """Should newly opened tools wait before their definitions join the list? Yes if the cache is warm and the
        conversation has started and is large. Not right after compaction: the conversation is being rewritten anyway."""
        if not self._cache_warm() or self._cache_cold():
            return False
        lp = self._last_prompt
        size = lp[0] if lp and lp[0] > 0 else estimate_tokens(self.messages)
        return size >= int(self.config.agent.get("defer_tools_tokens", self.DEFER_TOOLS_TOKENS) or 0)

    def deferred_tools(self) -> set[str]:
        """Tools that are open but whose definitions are not in the tool list yet (held back while the cache is warm)."""
        self.tool_specs()
        return set(self.__dict__.get("_deferred_tools") or ())

    def _cache_warm(self) -> bool:
        """If the tool list changes, is the cached conversation wasted? After the first request of a chat, yes: the tool
        list is at the very start of the request (in Claude, tools come even before the system prompt), and Gemini and
        OpenAI also match the cache from the start of the prompt. When a tool was added, the system prompt and the whole
        conversation dropped out of the cache (measured: every new group ~11-35K tokens rewritten). Setting:
        agent.cache_stable_tools = false gives the old behavior."""
        if self.is_child or str(self.config.agent.get("tool_groups", "auto")) == "all":
            return False
        if not self.config.agent.get("cache_stable_tools", True):
            return False
        return any(m.get("role") == "assistant" for m in self.messages[1:-1])

    def _enable_groups_for(self, text: str = "", tool_names: tuple[str, ...] = ()) -> None:
        from alice.tools.groups import group_of, select_groups

        enabled: set[str] = self.ctx.state.setdefault("tool_groups", set())
        self.ctx.state["tool_names"] = tuple(self.tools)
        guessed = set()
        if text:
            # Keywords in the message, filtered by the context: code-only tools open from words like "düzelt" (fix)
            # only inside a code project, never by voice.
            guessed = select_groups(text, cwd=getattr(self.ctx, "cwd", None), mode=getattr(self, "role", ""),
                                    tool_names=self.tools) - enabled
        if guessed and self._cache_warm():
            # A group guessed from a word may not really be needed (broad patterns like "aç" (open), "token", "ekran"
            # (screen)). Adding it to the tool list in a long chat made the whole conversation get rewritten into the
            # cache; instead a note is added to the message, and the model opens it itself with load_tools if needed.
            self._hint_groups(guessed)
        else:
            enabled |= guessed
        enabled |= {g for g in map(group_of, tool_names) if g}
        if getattr(getattr(self, "permissions", None), "mode", "") == "plan" and not getattr(self, "is_child", False):
            # In plan mode exit_plan_mode is always ready (only it and the todo list, not all 17 plan tools).
            enabled.update(("exit_plan_mode", "todo_write"))

    def _reopen_recent_tools(self) -> None:
        """Keep the tools called in the latest messages loaded. The loaded set lives only in memory, so a saved chat
        reopened after a restart started with the core alone and the model lost the tools it had just been using."""
        from alice.tools.groups import recent_tools

        enabled: set[str] = self.ctx.state.setdefault("tool_groups", set())
        enabled |= {n for n in recent_tools(self.messages[:-1]) if n in self.tools} - enabled

    def _autoload(self, name: str) -> None:
        """A real tool called by name runs even if its group was not loaded (the model knows it from the catalog).
        Open its group too, so the full definition (parameters, rules) is sent from the next request on; the model
        no longer has to spend a turn on load_tools first."""
        from alice.tools.groups import CORE, group_of

        if name in CORE:
            return
        enabled: set[str] = self.ctx.state.setdefault("tool_groups", set())
        g = group_of(name)
        key = g or name
        if key not in enabled:
            enabled.add(key)

    def _hint_groups(self, names: set[str]) -> None:
        """Add tool groups that are not loaded but may be needed to the last user message as a note."""
        from alice.tools.groups import all_groups

        if not self.messages or self.messages[-1].get("role") != "user":
            return
        groups = all_groups()
        parts = []
        for n in sorted(names):
            desc = groups[n][1] if n in groups else ("MCP server" if n.startswith("mcp:") else "tool")
            parts.append(f"{n} ({desc[:90]})")
        note = ("[Tool note: these tools are available for this task: " + "; ".join(parts) +
                ". You can load the ones you need with load_tools.]")
        msg = self.messages[-1]
        content = msg.get("content")
        if isinstance(content, str):
            msg.setdefault("_display", content)
            msg["content"] = content + "\n\n" + note
        elif isinstance(content, list):
            msg.setdefault("_display", next((p.get("text", "") for p in content if isinstance(p, dict)
                                              and p.get("type") == "text"), ""))
            msg["content"] = content + [{"type": "text", "text": note}]

    def spawn_child(self) -> "Agent":
        return Agent(self.config, self.router, list(self.tools.values()), self.ctx, self.permissions, self.ui,
                     self.decider, system_prompt=self.messages[0]["content"], is_child=True)

    def refresh_memory(self, force: bool = False) -> None:
        """If there is new/changed information in memory, update the profile section of the system prompt."""
        mem = self.memory
        if mem is None or self.is_child:
            return
        try:
            from alice import projects

            # The system prompt is at the very start of the request: if it changes, the prompt cache is rebuilt from
            # scratch. Previously the profile was rewritten every time a turn was saved (mem.version), resetting the
            # cache on every new message. It is refreshed when notes and projects change before the chat starts. After
            # the chat has started, only when the user edits memory by hand (deleting/editing a note), or the folder or
            # the day changes: the end-of-turn automatic note extraction and project scan changed the system prompt in
            # the middle of the chat and made the whole conversation get rewritten into the cache. Notes newly created
            # in this chat are already inside the conversation, and a relevant missing note is added to the message
            # by recall.
            key = (str(self.ctx.cwd), getattr(mem, "user_edits", 0), time.strftime("%Y-%m-%d"))
            if len(self.messages) > 1 and isinstance(self._memory_version, tuple) and not force:
                if self._memory_version[0] == key:
                    return
                version: Any = (key, None, None)
            else:
                scanned = projects.load()[0] if projects.enabled(self.config) else 0.0
                version = (key, scanned, mem.notes_stamp(self.ctx.cwd))
            if force or version != self._memory_version:
                from alice.prompts import with_memory

                profile = "\n\n".join(b for b in (mem.profile(cwd=self.ctx.cwd), projects.brief(self.config))
                                       if b)
                new = with_memory(self.messages[0]["content"], profile)
                if new != self.messages[0]["content"]:
                    self.cache_breaks.expect("hafıza bölümü yenilendi")
                self.messages[0]["content"] = new
                self._memory_version = version
        except Exception:  # noqa: BLE001 - a memory error must not stop the conversation
            log.debug("hafıza profili yenilenemedi", exc_info=True)

    def _recall(self, user_text: str) -> str:
        if self.memory is None or self.is_child:
            return ""
        try:
            voice = str(getattr(self, "memory_channel", "") or "").startswith(("ses", "voice", "live"))
            return self.memory.recall(user_text, cwd=self.ctx.cwd, surfaced=self._memory_surfaced,
                                      use_model=False if voice or self.role == "voice" else None,
                                      context=self._recent_chat_text())
        except Exception:  # noqa: BLE001
            log.debug("hafızadan hatırlama başarısız", exc_info=True)
            return ""

    def _recent_chat_text(self, turns: int = 3, chars: int = 1500) -> str:
        """The last few user/assistant texts of THIS chat, for recall: "o" or "bizimki" two messages after a person
        was named still points to that person's note. Tool results, nudges and images are skipped."""
        out: list[str] = []
        users = 0
        for m in reversed(self.messages[1:]):
            if m.get("_nudge") or m.get("_image"):
                continue
            role, content = m.get("role"), m.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            out.append(f"{'Kullanıcı' if role == 'user' else 'ALICE'}: {' '.join(content.split())[:500]}")
            if role == "user":
                users += 1
                if users >= turns:
                    break
        return "\n".join(reversed(out))[-chars:]

    def _remember_turn(self, user_text: str, reply: str) -> None:
        if self.memory is None or self.is_child:
            return
        try:
            wrote = bool(self.ctx.state.pop("memory_wrote", False))
            self.memory.record(user_text, reply, self.memory_channel,
                               sid=str(self.ctx.state.get("session_id") or ""))
            self.memory.learn(user_text, reply, cwd=self.ctx.cwd, wrote=wrote)
        except Exception:  # noqa: BLE001
            log.debug("tur hafızaya yazılamadı", exc_info=True)
        self._feed_buddy(user_text, reply)
        self._update_magic_docs()

    def _update_magic_docs(self) -> None:
        """Update the magic docs (# MAGIC DOC:) read in this chat in the background according to the conversation."""
        if self.is_child or not self.ctx.state.get("magic_docs"):
            return
        import threading

        from alice import magicdocs

        msgs = list(self.messages)
        threading.Thread(target=lambda: magicdocs.update(self.ctx.state, self.router, msgs), daemon=True,
                         name="alice-magicdocs").start()

    def _feed_buddy(self, user_text: str, reply: str) -> None:
        """BUDDY gets fed by the conversation; occasionally makes a short comment (in the background, doesn't hold up the turn)."""
        import threading

        from alice import buddy

        quiet = self.is_child or self.role == "voice" or self.memory_channel in ("ses", "telefon", "voice", "call")
        if not buddy.settings(self.config).get("enabled", True) or quiet:
            return  # the buddy doesn't comment in voice conversations and sub-agents (unnecessary model call)

        def work() -> None:
            try:
                buddy.on_turn(self.config, self.router, user_text, reply)
            except Exception:  # noqa: BLE001
                log.debug("buddy güncellenemedi", exc_info=True)

        threading.Thread(target=work, daemon=True, name="alice-buddy").start()

    def sync_catalog(self) -> None:
        """Refresh the 'Loadable tools' list at the end of the system instructions with the
        current tools (MCP and plugin tools can be added after the agent is set up)."""
        if str(self.config.agent.get("tool_groups", "auto")) == "all":
            return
        from alice.tools.groups import catalog

        system = self.messages[0]["content"]
        if _CATALOG_RX.search(system) and self._defer_new_tools():
            return  # the system prompt is cached: the list (e.g. a newly connected MCP server) is updated once the cache goes cold
        text = catalog(self.tools)
        body = _CATALOG_RX.sub("", system)
        new = body + ("\n\n" + text if text else "")
        if new != system:
            self.messages[0]["content"] = new

    def run_turn(self, user_text: str, max_steps: int | None = None, images: list[str] | None = None) -> str:
        # The stop flag is cleared before everything else: a Stop pressed during recall and setup must not be lost
        # (it used to be cleared inside _run_turn; a stop request arriving in between was erased and the turn went on).
        self.cancel.clear()
        if self.stop_check is not None and self.stop_check():
            self.cancel.set()
        self.refresh_memory()
        self.sync_catalog()
        recall = self._recall(user_text)
        old_role = self.role
        routed = self._route_role(user_text)
        if routed:
            self.role = routed
        self._turn_blocked = False
        self._turn_error = False
        try:
            final = self._run_turn(user_text, max_steps, images, recall)
        finally:
            self._last_turn_role = self.role  # so the next turn's routing can stay on the model with the warm cache
            self.role = old_role
        if not self._turn_blocked:  # a message blocked by a hook must not be written to memory as an empty turn
            # A provider error text is not ALICE's answer: memory keeps only what the user said.
            self._remember_turn(user_text, "" if self._turn_error else final)
        return final

    ROUTE_ROLES = {"strong": "route-strong", "light": "route-light"}

    def _route_role(self, user_text: str) -> str | None:
        """Model tier for this turn via Jev (alice/providers/jev_route.py). Only on the main brain turn; left alone
        when a model was picked by hand from the menu (pinning) or [routing] is off. None = today's chain."""
        rc = self.config.data.get("routing", {}) or {}
        router = self.router
        if (not rc.get("enabled") or self.is_child or self.role != "brain"
                or "brain" in getattr(router, "role_overrides", {"brain": 1})):
            return None
        from alice.providers import jev_route

        sticky = self._sticky_role(rc)
        log = getattr(router, "on_event", None) or (lambda s: None)
        if sticky is not None and self._sticky_skips_jev(rc, sticky):
            # The cache is warm and we won't drop to the light tier; heavy tiers go to the same model: no need to ask Jev.
            log(_("routing: the cache is warm, staying on {model} (Jev wasn't asked)", model=sticky))
            return None if sticky == "brain" else sticky
        prev = ""
        for m in reversed(self.messages[1:]):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip() \
                    and not m.get("_error"):
                prev = m["content"]
                break
        r = jev_route.route(self.config, user_text, prev, log=log)
        try:
            log(_("routing: {tier} ({reason}) {signals} {secs} s", tier=r.tier, reason=r.reason, signals=r.signals, secs=f"{r.latency:.2f}"))
        except Exception:  # noqa: BLE001
            pass
        if r.tier == "light" and sticky is not None:
            # The long chat's cache is warm on the heavy model: switching to the light model makes the whole context be
            # paid for there without cache, and on return the heavy model's cache is rewritten. The easy message also
            # stays on the same model.
            log(_("routing: the cache is warm, staying on {model} instead of the light model", model=sticky))
            return None if sticky == "brain" else sticky
        role = self.ROUTE_ROLES.get(r.tier)
        if role is None:
            return None
        try:
            chain = jev_route.tier_chain(router, self.config, r.tier)
        except Exception:  # noqa: BLE001 - if the chain can't be built, today's behavior
            return None
        if not chain:
            return None
        router.role_overrides[role] = chain
        return role

    STICKY_TOKENS = 6000  # if the context is shorter than this, the cache cost of switching models is negligible

    def _sticky_role(self, rc: dict[str, Any]) -> str | None:
        """The previous turn's role if it ran on a heavy model (brain or strong tier) and its cache is still warm;
        else None. [routing] cache_sticky = false restores the old behavior (easy messages always go to the light model)."""
        prev = getattr(self, "_last_turn_role", None)
        if not rc.get("cache_sticky", True) or prev not in ("brain", "route-strong"):
            return None
        last = getattr(self, "_last_call_at", 0.0)
        if not last:
            return None
        chain = self.router.role_overrides.get(prev) if prev != "brain" else self.router.chain("brain")
        if not chain:
            return None
        ttl = float(rc["cache_sticky_minutes"]) * 60 if rc.get("cache_sticky_minutes") else \
            self._cache_ttl(str(chain[0])) - 30
        if time.monotonic() - last > ttl:
            return None
        try:
            if self.context_tokens() < int(rc.get("cache_sticky_tokens", self.STICKY_TOKENS)):
                return None
        except Exception:  # noqa: BLE001 - if we can't measure, old behavior
            return None
        return prev

    def _sticky_skips_jev(self, rc: dict[str, Any], sticky: str) -> bool:
        """If we stay on the cache, would Jev's answer change the result? The light tier is already blocked; if the
        strong tier also starts with the same model, the Jev call (network latency) is wasted."""
        from alice.providers import jev_route

        try:
            strong = jev_route.tier_chain(self.router, self.config, "strong")
            current = (self.router.role_overrides.get(sticky) if sticky != "brain" else self.router.chain("brain"))
        except Exception:  # noqa: BLE001
            return False
        return bool(strong and current) and strong[0] == current[0]

    def _run_turn(self, user_text: str, max_steps: int | None, images: list[str] | None, recall: str) -> str:
        from alice.mentions import expand_mentions

        content_text = expand_mentions(user_text, self.ctx.cwd)
        if recall:
            content_text += "\n\n" + recall
        if self.hooks.hooks and not self.is_child:
            from alice.hooks import context, feedback

            extra = []
            if len(self.messages) == 1 and self.hooks.has("SessionStart"):
                extra.append(context(self.hooks.run("SessionStart", {})))
            if self.hooks.has("UserPromptSubmit"):
                res = self.hooks.run("UserPromptSubmit", {"prompt": user_text})
                blocked = feedback(res)
                if blocked:
                    self.ui.info(_("a hook blocked the message: {reason}", reason=blocked))
                    self._turn_blocked = True
                    return ""
                extra.append(context(res))
            extra = [e for e in extra if e]
            if extra:
                content_text += "\n\n[hook context]\n" + "\n".join(extra)
        if not self.is_child:
            from alice import guard

            guard.clear(self.ctx)  # the user's new message: external content read in the previous turn does not restrict this turn
        if self.permissions.mode == "plan" and not self.is_child:
            content_text = PLAN_NOTE + content_text
        guess_text = strip_plan_note(content_text)
        if not self.is_child and self.role != "voice" and self.config.agent.get("message_clock", True):
            # The time used to sit in the system prompt and changed it in every new chat (prompt cache rewritten);
            # now it rides at the end of the user's message, which is new anyway. Not used for tool guessing:
            # the word "saat" opened the automation group.
            content_text += time.strftime("\n\n[saat %H:%M]")
        content: Any = content_text
        if images:
            content = [{"type": "text", "text": content_text}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        if not self.is_child:
            self.checkpoints[len(self.messages)] = {}
            self.ctx.state["checkpoint"] = self.checkpoints[len(self.messages)]
        msg: dict[str, Any] = {"role": "user", "content": content}
        if content_text != user_text:
            msg["_display"] = user_text
        if images:
            msg["_user_images"] = len(images)
        self.messages.append(msg)
        self._reopen_recent_tools()
        # Guess tools from what the user wrote, not from ALICE's own plan-mode note ("adım adım", "ekrana dokunan"
        # in it opened the plan tools and the computer tool on every plan-mode turn).
        self._enable_groups_for(guess_text)
        final = ""
        steps = max_steps or self.max_steps
        self.busy = True
        self._call_counts.clear()
        self._fail_counts.clear()
        self._error_streak = 0
        sink = _Sink(self.ui, self.cancel)
        sink.early, sink.early_reset = self._early_start, self._early_clear
        self._turn_tools: set[str] = set()
        nudges = 0
        verified = False
        answered = ""  # the answer given before the evidence-check reminder
        self._turn_start = len(self.messages) - 1
        self.stats["turns"] += 1
        stop_hooks = 3  # the Stop hook can send the agent back at most this many times (no endless loop)
        cutoffs, cut_text = 0, ""  # earlier parts of an answer cut off at the output limit
        self._denials = [0, 0]  # refused calls (in a row, total)
        self._turn_tokens = 0  # tokens spent this turn (input not from cache + output): for the turn budget
        self._steering = not self.is_child
        try:
            for _step in range(steps):
                if self.cancel.is_set():
                    raise Cancelled
                self._drain_inbox()
                self._early_clear()
                self._maybe_compact()
                self._prune_images()
                self._shrink_old_outputs()
                sink.parts.clear()
                self.ui.stream_start()
                sent = len(self.messages)
                sink.first_at = 0.0
                t_call = time.monotonic()
                specs = self._specs()
                self._cache_before(specs)
                try:
                    res = self._interruptible(self.router.chat, self.role, list(self.messages), tools=specs,
                                              stream=sink if self.stream else None, cache=self._cache_hint())
                except AllProvidersFailed as e:
                    # If the remaining models' context is too small for the conversation: compact enough to fit that model and try once more.
                    room = fits_after_compact(e.errors)
                    if room and estimate_tokens(self.messages[:1], self.tool_specs()) > room * 0.45:
                        self._lean = True  # even the tool definitions fill the narrow model: send short definitions
                    if not room or not self._maybe_compact(force=True, limit=room):
                        raise
                    self.ui.stream_start()
                    sent = len(self.messages)
                    specs = self._specs()
                    self._cache_before(specs)
                    res = self._interruptible(self.router.chat, self.role, list(self.messages), tools=specs,
                                              stream=sink if self.stream else None, cache=self._cache_hint())
                self._count_call(res, t_call, sink.first_at)
                self._cache_after(res)
                msg = res.message
                _sanitize_calls(msg)
                if getattr(self, "_lean", False) and self._roomy(res):
                    self._lean = False  # a wide-context model is back: return to the full tool definitions
                if self.on_usage is not None:
                    try:
                        self.on_usage(res.usage or {})
                    except Exception:  # noqa: BLE001 - a counter error must not break the turn
                        log.debug("on_usage hata verdi", exc_info=True)
                pt = int((res.usage or {}).get("prompt_tokens") or 0)
                self._last_prompt = (pt, sent, self.messages[sent - 1]) if pt else None
                if not self.stream and msg.get("content"):
                    self.ui.stream_delta(msg["content"])
                self.messages.append(msg)
                sink.parts.clear()  # the text is in the history now: if interrupted, don't add it again as "[durduruldu]" (stopped)
                self.ui.stream_end(msg.get("content") or "", f"{res.provider}:{res.model}")
                if msg.get("content"):
                    final = cut_text + msg["content"]
                if not msg.get("tool_calls") and getattr(res, "finish", "") == "length" and cutoffs < MAX_CUTOFFS:
                    # The answer was cut off mid-text: don't count the partial text as the final answer, have it continue from there.
                    cutoffs += 1
                    cut_text = final
                    self.messages.append({"role": "user", "content": NUDGE_PREFIX + CUTOFF_NOTE, "_nudge": True})
                    continue
                if not msg.get("tool_calls"):
                    if self._drain_inbox():
                        continue  # the user said something new while the answer was being written: handle it before the turn ends
                    if stop_hooks and self.hooks.has("Stop") and not self.is_child:
                        from alice.hooks import feedback

                        back = feedback(self.hooks.run("Stop", {"reply": final}))
                        if back:
                            stop_hooks -= 1
                            self.messages.append({"role": "user", "content": NUDGE_PREFIX + "The Stop hook didn't "
                                                  "consider the work finished; continue:\n" + back, "_nudge": True})
                            continue
                    nudge = self._unfinished(msg.get("content") or "") if nudges < self.finish_nudges else ""
                    if not nudge and not verified:
                        verified = True  # once per turn: the check itself must not loop
                        nudge = self._unverified(msg.get("content") or "")
                        if nudge:
                            answered = final  # the original answer stays on screen: the correction does not replace it, it is appended
                    if not nudge:
                        # When the correction written after the evidence check was logged on its own, the original answer was lost,
                        # leaving broken turns in memory like "Merhaba" -> "Haklısın, doğrulama başarısız oldu" (you're right, verification failed).
                        if answered and final != answered:
                            final = answered + "\n\n" + final
                        return final
                    nudges += 1
                    self.messages.append({"role": "user", "content": nudge, "_nudge": True})
                    continue
                self._turn_tools.update(c["function"]["name"] for c in msg["tool_calls"])
                t_tools = time.monotonic()
                self._run_tools(msg["tool_calls"])
                self.stats["tool_s"] += time.monotonic() - t_tools
                if self._denials[0] >= DENIAL_LIMITS[0] or self._denials[1] >= DENIAL_LIMITS[1]:
                    return self._halt(_(DENIAL_HALT)) or final
                budget = self._turn_budget()
                if budget and self._turn_tokens >= budget:
                    # Long unattended tool chains (hundreds of calls) must not grow the bill without anyone noticing.
                    return self._halt(_(BUDGET_HALT, used=self._turn_tokens, budget=budget)) or final
                if self.on_step is not None:
                    try:
                        self.on_step()
                    except Exception:  # noqa: BLE001 - a logging error must not stop the work
                        log.debug("on_step hata verdi", exc_info=True)
            return self._halt(_("I reached the step limit ({steps}) and the job is half done. Write \"continue\" and I'll "
                               "carry on from where I was.", steps=steps)) or final
        except (KeyboardInterrupt, Cancelled, StreamCancelled):
            sink.dead = True
            partial = "".join(sink.parts)
            if partial and not (self.messages[-1]["role"] == "assistant" and self.messages[-1].get("content") == partial):
                self.messages.append({"role": "assistant", "content": partial + " [" + _("stopped") + "]"})
                self.ui.stream_end(partial, "")
            self._repair_after_interrupt()
            self.ui.info(_("stopped"))
            return final
        except AllProvidersFailed as e:
            self.ui.stream_end("", "")
            self._repair_after_interrupt()
            self.ui.info(str(e))
            # The turn used to end here silently: a small info line on screen, nothing in the chat log.
            # The user didn't notice the work had stopped and waited, then had to type "devam et" (continue).
            why = "; ".join(" ".join(x.split())[:140] for x in e.errors[:4])
            head = (_("The model you chose couldn't answer and the job is half done (I didn't switch to another model "
                      "because you picked it by hand).")
                    if getattr(e, "pinned", False) else _("None of the next models could answer and the job is half done."))
            # This text is only for the user: it does not enter the history sent to the model or memory as an ALICE answer
            # (_error). It used to go to the model with every later request, take up space and mislead it into "I errored".
            self._turn_error = True
            return self._halt(f"{head}\n\n" + _("Why: {why}", why=why) + "\n\n"
                              + _("If you pick another model from the model menu, or wait a bit, and write \"continue\", "
                                  "I'll carry on from where I was."), error=True) or final
        except Exception:
            # Unexpected error: if unanswered tool_calls stay in the history, every later turn gets a 400 from the provider.
            sink.dead = True
            self._repair_after_interrupt()
            raise
        finally:
            with self._inbox_lock:
                self._steering = False  # a message arriving after this becomes a new turn (the rest are picked up with take_inbox)
            self.busy = False
            self._release_desktop()

    def _turn_budget(self) -> int:
        """Tokens a turn may spend (0 = unlimited). Subagents take their own limit from [agent] sub_turn_token_budget;
        otherwise the main limit applies. Input read from the cache is not counted: it is cheap and repeats every step."""
        try:
            if self.is_child and self.config.agent.get("sub_turn_token_budget") is not None:
                return max(0, int(self.config.agent.get("sub_turn_token_budget") or 0))
            return max(0, int(self.config.agent.get("turn_token_budget", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _count_call(self, res: ChatResult, started: float, first: float) -> None:
        """Add a model call to the session statistics."""
        st = self.stats
        now = time.monotonic()
        self._last_call_at = now
        st["steps"] += 1
        st["llm_s"] += now - started
        if first:
            st["ttft_s"] += first - started
            st["ttft_n"] += 1
        u = res.usage or {}
        out = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        fresh = max(0, int(u.get("prompt_tokens") or u.get("input_tokens") or 0) - cached_tokens(u))
        self.__dict__["_turn_tokens"] = self.__dict__.get("_turn_tokens", 0) + fresh + out
        st["input"] += int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        st["cached"] += cached_tokens(u)
        st["written"] = st.get("written", 0) + cache_write_tokens(u)
        st["output"] += out
        if first and out and now - first > 0.2:  # streaming speed: only for answers that really streamed
            st["gen_s"] += now - first
            st["gen_tokens"] += out
        m = self.usage_models.setdefault(f"{res.provider}:{res.model}", {"calls": 0, "input": 0, "cached": 0,
                                                                          "output": 0, "seconds": 0.0})
        m["calls"] += 1
        m["input"] += int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        m["cached"] += cached_tokens(u)
        m["written"] = m.get("written", 0) + cache_write_tokens(u)
        m["output"] += out
        m["seconds"] += now - started

    def stats_view(self) -> dict[str, Any]:
        """Status bar panels: session statistics, token usage and how the context is split."""
        st = self.stats
        system = estimate_tokens(self.messages[:1])
        tools = estimate_tokens([], self._specs())
        total = self.context_tokens()
        return {
            "turns": int(st["turns"]), "steps": int(st["steps"]), "llm_s": round(st["llm_s"], 1),
            "tool_s": round(st["tool_s"], 1),
            "ttft": round(st["ttft_s"] / st["ttft_n"], 2) if st["ttft_n"] else 0.0,
            "tps": round(st["gen_tokens"] / st["gen_s"]) if st["gen_s"] else 0,
            "input": int(st["input"]), "cached": int(st["cached"]),
            "written": int(st.get("written", 0)), "output": int(st["output"]),
            "ctx": {"system": system, "tools": tools, "messages": max(0, total - system - tools), "total": total},
        }

    def export_stats(self) -> dict[str, Any]:
        """Statistics and usage breakdown written to the session file (so they are not reset when reopened)."""
        return {"stats": dict(self.stats), "usage_models": self.usage_models, "usage_tools": self.usage_tools}

    def import_stats(self, data: dict[str, Any]) -> None:
        for k, v in (data.get("stats") or {}).items():
            if k in self.stats and isinstance(v, (int, float)):
                self.stats[k] = v
        self.usage_models = {str(k): dict(v) for k, v in (data.get("usage_models") or {}).items() if isinstance(v, dict)}
        self.usage_tools = {str(k): dict(v) for k, v in (data.get("usage_tools") or {}).items() if isinstance(v, dict)}

    def usage_breakdown(self) -> dict[str, Any]:
        """"Where did my tokens go": calls/input/cache/output per model, output added to the context per tool, and how
        the context is split right now. Used by the usage breakdown window and the usage tool."""
        models = sorted(({"model": k, **{x: (round(y, 1) if isinstance(y, float) else y) for x, y in v.items()}}
                         for k, v in self.usage_models.items()), key=lambda r: -(r["input"] + r["output"]))
        tools = sorted(({"tool": k, **v} for k, v in self.usage_tools.items()), key=lambda r: -r["tokens"])
        return {**self.stats_view(), "models": models, "tools": tools}

    def steer(self, text: str, images: list[str] | None = None) -> bool:
        """Add a message to the running turn. True if the turn can take it (the message goes to the model at the next
        step); otherwise False, and the caller should run the message as a new turn."""
        with self._inbox_lock:
            if not (self.busy and self._steering) or self.cancel.is_set():
                return False
            self._inbox.append((text, list(images or [])))
            return True

    def take_inbox(self) -> list[tuple[str, list[str]]]:
        """Messages the turn could not take (the turn ended/was stopped): the caller runs them as a new turn."""
        with self._inbox_lock:
            out, self._inbox = self._inbox, []
            return out

    def _drain_inbox(self) -> bool:
        """Add pending user messages to the conversation. Called only after tool results (no message goes between
        tool_calls and their results). True if anything was added."""
        with self._inbox_lock:
            items, self._inbox = self._inbox, []
        for text, images in items:
            body = f"[The user wrote this while you were working; take it into account and carry on]\n{text}"
            content: Any = body
            if images:
                content = [{"type": "text", "text": body}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
            msg: dict[str, Any] = {"role": "user", "content": content, "_display": text, "_steer": True}
            if images:
                msg["_user_images"] = len(images)
            self.messages.append(msg)
            self._enable_groups_for(text)
        return bool(items)

    def _halt(self, text: str, error: bool = False) -> str:
        """When a turn ends without an answer (no model, step limit), write that into the chat as an assistant message:
        it shows on screen and in the log, and in the next turn the model knows the work was left unfinished. Only an
        info line for subagents and voice. error=True: a provider error; shown on screen but not sent to the model
        (_error, see router.clean_messages)."""
        if self.is_child or self.role == "voice":
            self.ui.info(text)
            return ""
        try:
            msg: dict[str, Any] = {"role": "assistant", "content": text, "_notice": True}
            if error:
                msg["_error"] = True
            self.messages.append(msg)
            self.ui.stream_start()
            self.ui.stream_delta(text)
            self.ui.stream_end(text, "")
            if self.on_step is not None:
                self.on_step()
        except Exception:  # noqa: BLE001 - failing to write the notice must not fail the turn
            log.debug("durma bildirimi yazılamadı", exc_info=True)
        return text

    def _unfinished(self, text: str) -> str:
        """If the turn answered and stopped while working with tools and the work looks unfinished, the reminder to
        send to the model (empty: done). Not in the voice role: the second answer would be read aloud too."""
        used = getattr(self, "_turn_tools", set())
        if not used or self.role == "voice" or self.permissions.mode == "plan":
            return ""  # in plan mode "şunu yapacağım" (I will do this) is the expected answer
        tail = text.strip()[-240:]
        if tail.endswith("?"):
            return ""  # it is asking the user a question: waiting is right
        todos = self.ctx.state.get("todos") or []
        left = [t.get("content", "") for t in todos if isinstance(t, dict) and t.get("status") != "done"]
        if "todo_write" in used and left:
            return (NUDGE_PREFIX + "There are unfinished steps in the todo list: " + "; ".join(left[:6]) + ". If nothing "
                    "is needed from the user, carry on with the tools from where you left off. If it is really done, "
                    "update the list and report the result; if you got stuck, say plainly why. Write in the user's language.")
        if _PROMISE_RX.search(tail) and not _DEFERRED_RX.search(tail):
            return (NUDGE_PREFIX + "You said you would do something but stopped without doing it. If nothing is needed "
                    "from the user, do it now with the tools and report the result when done; if something is needed, "
                    "ask plainly. Write in the user's language.")
        return ""

    def _unverified(self, text: str) -> str:
        """Is an answer that states a result ("yaptım" (I did it), "kuruldu" (installed), "indirildi" (downloaded), "yok"
        (there is none)) backed by this turn's tool results? If a claim is unsupported, the reminder to send to the model
        (empty: fine). A fast model compares them.

        While installing Sonora, ALICE said "indirmeyi başlattım" (I started the download) before any file came down, and
        "Windows sürümü yok" (there is no Windows version) while the release page had a Setup.exe: it filled in the screen/
        tool results with its assumptions. This is not specific to one tool; it can happen in any task."""
        used = getattr(self, "_turn_tools", set())
        if (not used or self.role == "voice" or self.permissions.mode == "plan" or self.is_child
                or not self.config.agent.get("verify_claims", True) or not _CLAIM_RX.search(text)):
            return ""
        evidence: list[str] = []
        for m in self.messages[getattr(self, "_turn_start", 0):]:
            if m.get("role") == "tool":
                evidence.append(f"[{m.get('name')}] {_clip(str(m.get('content') or ''), 3000)}")
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                evidence.append("calls: " + "; ".join(
                    f"{c['function']['name']}({str(c['function'].get('arguments') or '')[:200]})"
                    for c in m["tool_calls"]))
        ask = next((str(m.get("content")) for m in reversed(self.messages[:getattr(self, "_turn_start", 0) + 1])
                    if m.get("role") == "user"), "")
        prompt = (
            "Compare an assistant's answer with the RESULTS of the tools it used in this turn. List only these two kinds "
            "of claims:\n"
            "1. Work the assistant says it did THIS TURN but with no tool call behind it (set up, downloaded, sent, "
            "deleted, saved, opened and the like).\n"
            "2. A claim that CLEARLY contradicts the tool results (saying 'it worked' when the result says error, "
            "'it isn't there' when the result has it, giving a different number or state).\n"
            "Do NOT list: things known from earlier turns or from the conversation, general knowledge, explanations, "
            "comments, guesses or things presented as 'I haven't verified', intentions, suggestions, questions. Tool "
            "outputs may be shortened here (with '…'): don't count a detail that may be in the shortened part as "
            "unsupported. If you're not sure, leave it out of the list.\n"
            'Write only JSON: {"unsupported": ["short claim", ...]}\n\n'
            f"USER REQUEST:\n{ask[:1500]}\n\nTOOL RESULTS:\n" + "\n".join(evidence)[-24000:]
            + f"\n\nANSWER:\n{text[:4000]}")
        # With the answer already on screen, the turn stayed open until this check finished and the UI showed "Çalışıyor"
        # (working). A stuck model in the fast queue (step-5-preview, 180 s timeout) left a finished ALICE "working" for
        # minutes: the check is time-limited, and if it doesn't make it the answer stays as is (its result is dropped later).
        box: dict[str, Any] = {}
        give_up = threading.Event()

        class _Quiet:
            """A receiver that shows the stream to no one but can be cancelled: when time runs out the provider request is
            cut too, instead of running on in the background using quota and a connection (the router checks this while
            streaming and between models)."""

            def on_delta(self, text: str) -> None:
                pass

            def on_reset(self) -> None:
                pass

            def cancelled(self) -> bool:
                return give_up.is_set()

        def check() -> None:
            try:
                # Thinking models (Cerebras qwen, step) spent the 400 tokens reasoning, so the reply came back empty.
                box["res"] = self.router.chat(str(self.config.agent.get("verify_role", "fast")),
                                              [{"role": "user", "content": prompt}], temperature=0, max_tokens=1200,
                                              stream=_Quiet(), cache={"mode": "none"})
            except Exception as e:  # noqa: BLE001 - includes cancellation (StreamCancelled)
                box["err"] = e

        worker = threading.Thread(target=check, daemon=True, name="alice-verify")
        worker.start()
        deadline = time.monotonic() + float(self.config.agent.get("verify_seconds", 25))
        while worker.is_alive() and time.monotonic() < deadline and not self.cancel.is_set():
            worker.join(0.2)
        if "res" not in box:
            give_up.set()
            if worker.is_alive():
                log.debug("iddia kontrolü süre sınırını aştı; istek iptal edildi, cevap olduğu gibi bırakıldı")
            return ""
        try:
            res = box["res"]
            m = re.search(r"\{.*\}", res.text or "", re.S)
            bad = [str(x)[:160] for x in (json.loads(m.group(0)).get("unsupported") or [])] if m else []
        except Exception:  # noqa: BLE001 - if verification fails, do not block the reply
            return ""
        if not bad:
            return ""
        return (NUDGE_PREFIX + "These claims in your answer have no basis in this turn's tool results: "
                + "; ".join(bad[:5]) + ". Either really verify them with a tool, or tell the user plainly that you did not "
                "verify them. Don't say 'done/downloaded/not there' without checking.\n"
                "The user does NOT see this note and didn't write it; your previous answer is still on screen. So: don't "
                "write as if answering the note (don't start with 'You're right' or 'I checked' or 'this turn', don't "
                "mention this check) and don't repeat the previous answer from the start. Write only a short addition, "
                "in the user's language: if you verified, the result in a sentence or two; if you couldn't, start "
                "with the word for 'Correction:' and say which part is not certain.")

    def stop(self) -> None:
        """Stop the current turn from another thread (e.g. the Durdur (Stop) button in the app). Does not wait
        for a model or tool reply; running commands are terminated too."""
        self.cancel.set()
        from alice.tools.browser import abort_browser
        from alice.tools.shell import kill_foreground

        kill_foreground(self.ctx)
        abort_browser(self.ctx, "stop")  # a stuck browser call (only if one is running)

    def _interruptible(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Run fn in a separate thread and check for a stop request while waiting for it to finish. If stopped,
        raises Cancelled without waiting; the background call finishes on its own and its result is dropped."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["res"] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 - re-raised in the calling thread
                box["err"] = e
            finally:
                done.set()

        threading.Thread(target=work, daemon=True, name="alice-call").start()
        while not done.wait(0.1):
            if self.cancel.is_set():
                raise Cancelled
        if "err" in box:
            raise box["err"]
        return box["res"]

    def rewind(self, msg_index: int) -> tuple[str, list[str]]:
        """Rewind the conversation to just before the user message at msg_index and restore the files that
        ALICE changed with its file tools since then. (Changes made by shell commands cannot be tracked.)
        Returns the message text and the restored files."""
        if self.busy:
            raise RuntimeError(_("can't rewind while it is working"))
        if not 0 < msg_index < len(self.messages) or self.messages[msg_index].get("role") != "user":
            raise ValueError(_("invalid message"))
        original: dict[str, bytes | None] = {}
        for idx in sorted(k for k in self.checkpoints if k >= msg_index):
            for path, data in self.checkpoints[idx].items():
                original.setdefault(path, data)
        restored = []
        for path, data in original.items():
            p = Path(path)
            try:
                if data is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                restored.append(path)
            except OSError as e:
                self.ui.info(_("could not restore: {path} ({error})", path=path, error=e))
        m = self.messages[msg_index]
        text = m.get("_display") or (m["content"] if isinstance(m["content"], str) else
                                     next((p.get("text", "") for p in m["content"] if p.get("type") == "text"), ""))
        text = strip_plan_note(text)
        self.messages = self.messages[:msg_index]
        for k in [k for k in self.checkpoints if k >= msg_index]:
            del self.checkpoints[k]
        return text, restored

    # ------------------------------------------------------------------- tools
    def _run_tools(self, calls: list[dict[str, Any]]) -> None:
        # A tool from an unsent group was called (the model knows it from the system prompt): keep the group open.
        self._enable_groups_for(tool_names=tuple(c["function"]["name"] for c in calls))
        results = self._execute_calls(calls)
        pending_images: list[tuple[str, list[str]]] = []
        for call, result in zip(calls, results, strict=True):
            name = call["function"]["name"]
            from alice import tool_results

            text = tool_results.shape(result.text, self.output_limit, str(call.get("id") or ""), name)
            if result.is_error:
                text = f"HATA: {text}"
            text += self._loop_note(call, result)
            if result.images:
                if self._brain_sees_images():
                    pending_images.append((name, result.images))
                else:
                    text += "\n\n[image description - vision model]\n" + self._describe_images(result.images)
            tool_msg: dict[str, Any] = {"role": "tool", "tool_call_id": call["id"], "name": name, "content": text}
            tu = self.usage_tools.setdefault(name, {"calls": 0, "tokens": 0, "errors": 0})
            tu["calls"] += 1
            tu["tokens"] += max(1, len(text) // 4)  # approximate tokens the result adds to the context
            tu["errors"] += 1 if result.is_error else 0
            if result.files:  # for the UI (not sent to the model): the file card still shows when the chat is reopened
                tool_msg["_files"] = result.files[:10]
            if result.card:
                tool_msg["_card"] = result.card
            self.messages.append(tool_msg)
        for name, images in pending_images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": f"[image returned by the {name} tool]"}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
            self.messages.append({"role": "user", "content": parts, "_image": True})

    def _loop_note(self, call: dict[str, Any], result: ToolResult) -> str:
        """Weak models get stuck repeating the same faulty call; notice this and tell the model."""
        if self.cancel.is_set():
            return ""
        key = _call_key(call)
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        if result.is_error:
            self._error_streak += 1
            self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            if self._fail_counts[key] >= 2:
                return REPEAT_FAIL_NOTE.format(n=self._fail_counts[key])
            if self._error_streak >= 3 and self._error_streak % 3 == 0:
                return ERROR_STREAK_NOTE.format(n=self._error_streak)
            return ""
        self._error_streak = 0
        n = self._call_counts[key]
        if n >= 3 and call["function"]["name"] not in ("todo_write",):
            return REPEAT_OK_NOTE.format(n=n)
        return ""

    def _execute_calls(self, calls: list[dict[str, Any]]) -> list[ToolResult]:
        """Process calls in order, but run consecutive calls that need no permission and are parallel-safe
        (read-only) at the same time. UI events always go out from this thread, in call order."""
        results: list[ToolResult | None] = [None] * len(calls)
        i = 0
        while i < len(calls):
            if self.cancel.is_set():
                results[i] = ToolResult(STOPPED, is_error=True)
                i += 1
                continue
            early = self._take_early(calls[i])
            if early is not None:  # started while the reply was still streaming: result is ready (or nearly so)
                tool, args, summary, fut = early
                self.ui.tool_start(calls[i]["id"], tool.name, summary)
                res = self._await(fut)
                self._note_read(tool, args, res)
                self.ui.tool_end(calls[i]["id"], tool.name, res)
                results[i] = self._fence(tool, res)
                i += 1
                continue
            prep = self._prepare(calls[i])
            if isinstance(prep, ToolResult):
                results[i] = prep
                i += 1
                continue
            batch = [(i, prep)]
            j = i + 1
            if self._parallel_ok(prep):
                busy = {prep[0].exclusive_key(prep[1])} - {""}
                while j < len(calls) and not self.cancel.is_set():
                    nxt = self._prepare(calls[j], quiet=True)
                    if isinstance(nxt, ToolResult) or not self._parallel_ok(nxt):
                        break
                    key = nxt[0].exclusive_key(nxt[1])
                    if key and key in busy:
                        break  # second command on the same repo/package manager: runs once the earlier ones finish
                    busy.add(key)
                    batch.append((j, nxt))
                    j += 1
            if len(batch) == 1:
                results[i] = self._execute(calls[i], *prep)
            else:
                for idx, (tool, _args, summary) in batch:
                    self.ui.tool_start(calls[idx]["id"], tool.name, summary)
                pool = ThreadPoolExecutor(max_workers=min(8, len(batch)))
                try:
                    futures = {idx: pool.submit(self._run_tool, tool, args) for idx, (tool, args, _summary) in batch}
                    for idx, (tool, args, _summary) in batch:
                        results[idx] = self._await(futures[idx])
                        self._note_read(tool, args, results[idx])
                        self.ui.tool_end(calls[idx]["id"], tool.name, results[idx])
                        results[idx] = self._fence(tool, results[idx])
                finally:
                    # If stopped, read-only tools still running in the background are not waited for (the old
                    # with block used shutdown(wait=True) and waited for all of them to finish).
                    pool.shutdown(wait=False)
            i = j
        return [r if r is not None else ToolResult("not run", is_error=True) for r in results]

    def _await(self, fut: Any) -> ToolResult:
        """Wait for the result of a parallel or early-started tool; if Durdur (Stop) is pressed, return
        without waiting. Previously fut.result() waited forever: a long grep or a slow web_fetch kept the
        stopped turn open."""
        from concurrent.futures import TimeoutError as FutTimeout

        while True:
            try:
                return fut.result(timeout=0.1)
            except FutTimeout:
                if self.cancel.is_set():
                    return ToolResult(STOPPED, is_error=True)
            except Exception as e:  # noqa: BLE001
                return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

    def _early_start(self, call: dict[str, Any]) -> None:
        """A tool call that completes while the stream is still running: if it is parallel-safe (read-only,
        needs no permission, no hooks, does not use the screen), start it in the background right away. In jobs
        with hundreds of tool calls, waiting for the model to finish writing wasted minutes. The result is used
        if the same call arrives once the reply is done."""
        try:
            prep = self._prepare(call, quiet=True)
            if isinstance(prep, ToolResult) or not self._parallel_ok(prep):
                return
            tool, args, summary = prep
            if getattr(tool, "risk", "") == COMPUTER:
                return
            pool = self.__dict__.get("_early_pool")
            if pool is None:
                pool = self._early_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="alice-early")
            fut = pool.submit(self._run_tool, tool, args)
            self.__dict__.setdefault("_early", {})[call["id"]] = (args, tool, summary, fut)
        except Exception:  # noqa: BLE001 - early start only speeds things up; if it fails, the normal path runs
            log.debug("araç erken başlatılamadı", exc_info=True)

    def _early_clear(self) -> None:
        self.__dict__["_early"] = {}

    def _take_early(self, call: dict[str, Any]) -> tuple[Tool, dict[str, Any], str, Any] | None:
        e = self.__dict__.get("_early", {}).pop(call.get("id"), None)
        if e is None:
            return None
        args, tool, summary, fut = e
        try:
            final = parse_args(call["function"].get("arguments"))
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if final != args or call["function"].get("name") != tool.name:
            return None  # final form differs: runs again on the normal path
        return tool, args, summary, fut

    def _parallel_ok(self, prep: tuple[Tool, dict[str, Any], str]) -> bool:
        tool, args, _summary = prep
        if self.hooks.has("PreToolUse") or self.hooks.has("PostToolUse"):
            return False  # hooks must run in order for every call (the parallel path skips _execute)
        try:
            return bool(tool.parallel_for(self.ctx, args)) and self.permissions.decide(tool, args, self.ctx) == "allow"
        except Exception:  # noqa: BLE001 - bad argument: the sequential path reports it to the model as an error
            return False

    def _prepare(self, call: dict[str, Any], quiet: bool = False) -> ToolResult | tuple[Tool, dict[str, Any], str]:
        """Find the tool, parse the arguments and get permission. Returns a ToolResult if there is an error
        or a refusal. With quiet=True, permission is not asked (when checking whether a call can join a
        parallel group)."""
        name = call["function"]["name"]
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"unknown tool '{name}'. Available tools: {', '.join(self.tools)}", is_error=True)
        if not quiet:
            self._autoload(name)
        try:
            args = parse_args(call["function"].get("arguments"))
        except json.JSONDecodeError as e:
            return ToolResult(f"the arguments are not valid JSON ({e}). Try again.", is_error=True)
        if not isinstance(args, dict):
            return ToolResult("the arguments must be a JSON object ({\"name\": value}). Try again.", is_error=True)
        bad = check_args(tool, args)
        if bad:
            return ToolResult(bad + " Fix it and call again.", is_error=True)
        hook_verdict = ""
        if not quiet and self.hooks.has("PreToolUse"):
            # Before permission: a hook may change the arguments or decide on permission (see alice.hooks.decision).
            from alice.hooks import decision, feedback

            results = self.hooks.run("PreToolUse", {"args": args}, tool.name)
            blocked = feedback(results)
            if blocked:
                res = ToolResult(f"The user's hook blocked this call: {blocked}", is_error=True)
                self.ui.tool_start(call["id"], name, name)
                self.ui.tool_end(call["id"], name, res)
                return res
            d = decision(results)
            if "updatedInput" in d:
                args = d["updatedInput"]
                bad = check_args(tool, args)
                if bad:
                    return ToolResult(f"The arguments the hook changed are invalid: {bad}", is_error=True)
            hook_verdict = str(d.get("permissionDecision") or "")
            if hook_verdict == "deny":
                why = d.get("reason") or "no reason given"
                res = ToolResult(f"The user's hook did not allow this call: {why}", is_error=True)
                self.ui.tool_start(call["id"], name, name)
                self.ui.tool_end(call["id"], name, res)
                self._note_denial()
                return res
            if d.get("additionalContext"):
                self.__dict__.setdefault("_hook_notes", {})[call["id"]] = d["additionalContext"]
        try:
            summary = tool.summary(args)
            if quiet:
                return tool, args, summary
            verdict = self.permissions.decide(tool, args, self.ctx)
            if hook_verdict == "allow" and verdict == "ask":
                verdict = "allow"  # hook approved; deny rules and plan mode still apply
            elif hook_verdict == "ask" and verdict == "allow":
                verdict = "ask"
            risk = tool.risk_for(args) if verdict == "ask" else ""
        except Exception as e:  # noqa: BLE001 - weak model's wrong-typed arg (list, null) must not break turn/history
            return ToolResult(f"invalid argument: {type(e).__name__}: {e}. Fix the parameter types and try again.",
                              is_error=True)
        if verdict == "deny":
            rule = self.permissions.matching_rule(tool, args, self.ctx, "deny")
            self._log_outward(tool, args, summary, "blocked", f"permission rule {rule}" if rule else
                              "plan mode or unattended run")
            if rule:
                res = ToolResult(f"The user's permission rule forbids this ({rule}). Don't try the same thing in another form "
                                 "either; if it is needed, tell the user why.", is_error=True)
            else:
                res = ToolResult("You are in plan mode: this tool changes things and is off right now. Write your plan first; "
                                 "you can carry it out once the user approves and changes the mode.", is_error=True)
            self.ui.tool_start(call["id"], name, summary)
            self.ui.tool_end(call["id"], name, res)
            self._note_denial()
            return res
        if verdict == "ask":
            from alice import guard

            shown = (_("[after reading outside content: {source}]", source=guard.tainted(self.ctx)) + f" {summary}"
                     if guard.tainted(self.ctx) else summary)
            ans = self.ui.ask_permission(name, shown, risk)
            try:  # so a "don't ask again" suggestion can be made for actions that are always approved (permissions action=suggest)
                from alice.permissions import record_answer

                record_answer(tool, args, self.ctx.cwd, risk, ans)
            except Exception:  # noqa: BLE001 - failing to write the log must not stop the work
                log.debug("izin cevabı kaydedilemedi", exc_info=True)
            if ans == "a":
                self.permissions.always_allow.add(name)
            elif ans == "p" and risk in (DANGER, SEND):
                # No permanent permission is written for irreversible or outward actions: it counts as yes for this time only.
                self.ui.info(_("permanent permission is not given for dangerous or outgoing actions; allowed just this once"))
            elif ans == "p":  # always in this project: written as a permanent rule, not asked again in later sessions
                from alice.permissions import persist_allow, suggest_rule

                rule = suggest_rule(tool, args, self.ctx.cwd)
                try:
                    where = persist_allow(self.ctx.cwd, rule)
                    self.ui.info(_("permanent permission added: {rule} ({where})", rule=rule, where=where))
                except OSError as e:
                    self.ui.info(_("could not write the permanent permission ({error}); allowed for this session", error=e))
                    self.permissions.always_allow.add(name)
            elif ans not in ("y", "p"):
                self._log_outward(tool, args, summary, "rejected")
                self._note_denial()
                return ToolResult("The user denied this call. Don't try the same thing again; find another way or ask the user.",
                                  is_error=True)
        if hasattr(self, "_denials"):
            self._denials[0] = 0  # an allowed call resets the in-a-row count
        return tool, args, summary

    def _note_denial(self) -> None:
        d = self.__dict__.setdefault("_denials", [0, 0])
        d[0] += 1
        d[1] += 1

    def _execute(self, call: dict[str, Any], tool: Tool, args: dict[str, Any], summary: str) -> ToolResult:
        self.ui.tool_start(call["id"], tool.name, summary)
        watch = self._watch(tool, args)
        result = self._run_tool(tool, args)
        self._note_read(tool, args, result)
        note =self.__dict__.get("_hook_notes", {}).pop(call["id"], "")
        if note:
            from dataclasses import replace

            result = replace(result, text=result.text + "\n\n[hook context]\n" + note)
        if watch is not None and not self.cancel.is_set():
            from dataclasses import replace

            seen = watch.note()
            if seen:  # what really happened on screen: a new window, an error box, UAC, a program that didn't open
                result = replace(result, text=result.text + seen)
        if self.hooks.has("PostToolUse"):
            from dataclasses import replace

            from alice.hooks import decision, feedback

            post = self.hooks.run("PostToolUse", {"args": args, "result": result.text[:200_000],
                                                  "is_error": result.is_error}, tool.name)
            verdict = decision(post)
            if "updatedToolOutput" in verdict:  # a hook changed the output (e.g. hid secrets)
                result = replace(result, text=verdict["updatedToolOutput"][:200_000])
            back = "\n".join(x for x in (feedback(post), verdict.get("additionalContext", "")) if x)
            if back:
                result = replace(result, text=result.text + "\n\n[hook geri bildirimi]\n" + back)
        self._log_outward(tool, args, summary, "failed" if result.is_error else "sent", result.text)
        self.ui.tool_end(call["id"], tool.name, result)
        return self._fence(tool, result)

    def _note_read(self, tool: Tool, args: dict[str, Any], result: ToolResult) -> None:
        """Register a magic doc that was read (# MAGIC DOC:). Called on the sequential, parallel and early-start paths
        alike: since read_file is parallel-safe it mostly goes through those, and the doc was never registered."""
        if tool.name != "read_file" or result.is_error or not args.get("path"):
            return
        try:
            from alice import magicdocs

            magicdocs.note_read(self.ctx.state, self.ctx.resolve(str(args["path"])))
        except Exception:  # noqa: BLE001 - registering a magic doc must not break the tool
            pass

    def _watch(self, tool: Tool, args: dict[str, Any]):
        """Before an action that affects the screen (click, key, opening a program, command), note the windows; what
        changed is then added to the tool result (see alice.tools.outcome). None for read-only actions or when the setting
        is off."""
        if not self.config.agent.get("observe_outcomes", True):
            return None
        try:
            if tool.name != "run_command" and tool.risk_for(args) != COMPUTER:
                return None
        except Exception:  # noqa: BLE001
            return None
        if str(args.get("action") or "") in ("screenshot", "list", "snapshot", "text", "tabs", "downloads",
                                              "cursor_position", "wait"):
            return None
        from alice.tools import outcome

        if not outcome.IS_WIN:
            return None
        return outcome.Watch(opening=tool.name in ("windows", "app") and args.get("action") in ("open", "launch"))

    def _fence(self, tool: Tool, result: ToolResult) -> ToolResult:
        """Mark the result of a tool that brings in outside content as "untrusted data" and count the turn as tainted
        (see guard)."""
        if not getattr(tool, "untrusted", False) or result.is_error:
            return result
        from dataclasses import replace

        from alice import guard

        if not guard.enabled(self.ctx):
            return result
        guard.mark(self.ctx, tool.name)
        return replace(result, text=guard.wrap(tool.name, result.text))

    @staticmethod
    def _log_outward(tool: Tool, args: dict[str, Any], summary: str, outcome: str, detail: str = "") -> None:
        """Write an outward action (send risk) to the action log: sent, refused and blocked ones alike."""
        try:
            if tool.risk_for(args) != SEND:
                return
        except Exception:  # noqa: BLE001 - if the risk can't be computed, logging must not break the turn
            return
        from alice import actionlog

        actionlog.log(tool.name, summary, outcome, args, detail)

    def _run_tool(self, tool: Tool, args: dict[str, Any]) -> ToolResult:
        # Tools run in this thread (Playwright and UI Automation objects are bound to their thread). When stopped,
        # stop() kills the commands the tools started, so a waiting tool returns right away too.
        if not self._take_desktop(tool):
            return ToolResult(STOPPED, is_error=True)
        t0 = time.monotonic()
        try:
            res = tool.run(self.ctx, **args)
        except ToolError as e:
            res = ToolResult(str(e), is_error=True)
        except TypeError as e:
            res = ToolResult(unknown_params(tool, args) or f"invalid parameter: {e}", is_error=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - a tool error must not crash the agent; return it to the model
            res = ToolResult(f"{type(e).__name__}: {e}", is_error=True)
        dur = time.monotonic() - t0
        self._log_tool_call(tool.name, args, res, dur)
        return res

    def _log_tool_call(self, name: str, args: dict[str, Any], res: ToolResult, dur: float) -> None:
        """Log tool execution for diagnostics and telemetry."""
        try:
            from alice.config import alice_home
            log_dir = alice_home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "tools.log"
            summary_args = {}
            for k, v in args.items():
                s = str(v)
                summary_args[k] = s if len(s) < 120 else s[:117] + "..."
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": name,
                "args": summary_args,
                "dur": round(dur, 3),
                "ok": not res.is_error,
                "len": len(res.text or "")
            }
            if res.is_error:
                entry["err"] = (res.text or "")[:150]
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # the tool log must not break a turn

    def _take_desktop(self, tool: Tool) -> bool:
        """Several chats can run at once in the app; a tool that drives the screen/browser is used one at a time (see
        DesktopLock). Does nothing when there is no lock (CLI, voice, core)."""
        lock = self.ctx.state.get("desktop_lock")
        owner = self.ctx.state.get("lock_owner") or self.ctx  # a subagent holds it on behalf of the main chat
        if lock is None or getattr(tool, "risk", "") != COMPUTER or lock.held_by(owner):
            return True
        return lock.acquire(owner, self.cancel.is_set, on_wait=lambda: self.ui.info(
            _("another chat is using the screen/browser; I'll carry on when its turn is done")))

    def _release_desktop(self) -> None:
        lock = self.ctx.state.get("desktop_lock")
        if lock is not None and not self.is_child:  # alt ajan bitince ana sohbet hâlâ ekranda olabilir
            lock.release(self.ctx)

    # ----------------------------------------------------------------- vision
    def _brain_sees_images(self) -> bool:
        ref = self.router.first_available(self.role)
        return bool(ref and self.router.supports_vision(ref))

    def _describe_images(self, images: list[str]) -> str:
        task = next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        parts: list[dict[str, Any]] = [{"type": "text", "text": (
            "You are the eyes of a computer-use agent. Describe this screenshot precisely for the task below: "
            "visible windows and their titles, focused element, all buttons/links/fields/menus with their exact text "
            "and approximate pixel coordinates (x,y of center in this image), dialogs, errors, and anything relevant. "
            f"Task: {task[:800]}")}]
        parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        try:
            res = self.router.chat("vision", [{"role": "user", "content": parts}], max_tokens=1500,
                                   cache={"mode": "none"})  # one-shot: a cache write would never be read
            return res.text
        except (AllProvidersFailed, KeyError) as e:
            return f"(could not describe the image: {str(e)[:200]})"

    def _cache_cold(self) -> bool:
        return bool(self._last_call_at) and time.monotonic() - self._last_call_at > self._cache_ttl()

    def _cache_ttl_choice(self) -> str:
        """Claude API cache TTL for this session: "5m" or "1h", latched on first use.
        [agent] cache_ttl = "auto" (default): 1h for the main conversation (a personal assistant often waits more than
        5 minutes between messages; one cache hit after a break pays back the 2x write), 5m for sub-agents, whose steps
        follow each other within seconds. "5m"/"1h" force it."""
        if self._cache_ttl_latched is None:
            v = str(self.config.agent.get("cache_ttl", "auto") or "auto").lower()
            self._cache_ttl_latched = v if v in ("5m", "1h") else ("5m" if self.is_child else "1h")
        return self._cache_ttl_latched

    def _cache_hint(self, mode: str = "write") -> dict[str, Any]:
        hint: dict[str, Any] = {"ttl": self._cache_ttl_choice(), "mode": mode}
        if mode == "write" and self.config.agent.get("cache_sticky_effort", True):
            hint["latch"] = self._effort_latch()
        ce = self.config.agent.get("context_editing", False)
        if mode == "write" and ce:
            # [agent] context_editing = true (or a table with trigger/keep/clear_at_least/exclude_tools): old tool
            # results are cleared on the Claude API server instead of ALICE rewriting its history.
            hint["context_edit"] = dict(ce) if isinstance(ce, dict) else {}
        return hint

    def _effort_latch(self) -> dict[str, dict[str, Any]]:
        """Per-model thinking/effort settings first sent in this conversation (router keeps resending them, see
        Router.chat). Reset when the user picks another level, so the new level takes effect at once; also reset by
        compaction, which rewrites the prefix anyway."""
        try:
            levels = (dict(self.router.thinking_overrides), dict(self.config.data.get("thinking", {}) or {}))
        except Exception:  # noqa: BLE001
            levels = None
        if levels != self.__dict__.get("_effort_levels"):
            self.__dict__["_effort_levels"] = levels
            self.__dict__["_effort_latch_map"] = {}
        return self.__dict__.setdefault("_effort_latch_map", {})

    def _reset_effort_latch(self) -> None:
        self.__dict__["_effort_latch_map"] = {}

    def _cache_ttl(self, ref: str | None = None) -> float:
        """How many seconds the last model's prompt cache lives. cliproxy writes a 1-hour cache to Claude; on the Claude
        API it is the session's TTL (cache_ttl); others ~5 min. Trimming the history before the cache really went cold
        (at minute 5) wasted a cache that was still warm."""
        if ref is None:
            try:
                ref = self.router.last_ref.get(self.role) or ""
            except Exception:  # noqa: BLE001
                ref = ""
        # Measured on 22 November: in a request to Claude through cliproxy, an entry marked for 5 minutes was still
        # cached after 6 min 40 s (on a subscription account the cache lives 1 hour). Other cliproxy models were not
        # measured. Can be set by hand with [agent] cache_ttl_minutes.
        try:
            m = float((getattr(self.config, "agent", None) or {}).get("cache_ttl_minutes") or 0)
        except (TypeError, ValueError, AttributeError):
            m = 0.0
        if m > 0:
            return m * 60
        ref = str(ref)
        if ref.startswith("cliproxy:"):
            return CACHE_LONG_S
        # (cliproxy-claude: kind = "anthropic", so the session's latched TTL below decides.)
        try:
            pc = self.router.providers_cfg.get(ref.split(":", 1)[0])
        except Exception:  # noqa: BLE001
            pc = None
        if pc is not None and getattr(pc, "kind", "") == "anthropic" and self._cache_ttl_choice() == "1h":
            return CACHE_LONG_S
        return CACHE_COLD_S

    def _on_claude_api(self) -> bool:
        try:
            ref = self.router.last_ref.get(self.role) or self.router.first_available(self.role) or ""
            pc = self.router.providers_cfg.get(str(ref).split(":", 1)[0])
        except Exception:  # noqa: BLE001
            return False
        return pc is not None and getattr(pc, "kind", "") == "anthropic"

    # ---------------------------------------------------------- cache break detection
    def _cache_before(self, specs: list[dict[str, Any]] | None = None) -> None:
        try:
            ref = self.router.last_ref.get(self.role) or ""
            settings = {"s": self.router.last_settings.get(self.role), "ttl": self._cache_ttl_latched}
            self.cache_breaks.before(self.messages, self._specs() if specs is None else specs, ref, settings)
        except Exception:  # noqa: BLE001 - diagnostics must never break a turn
            log.debug("cache snapshot alınamadı", exc_info=True)

    def _cache_after(self, res: ChatResult) -> None:
        try:
            ref = f"{res.provider}:{res.model}"
            # The snapshot was taken before the call, when last_settings still held the previous call's values:
            # refresh the settings part with what this call really sent.
            if self.cache_breaks.pending is not None:
                from alice.providers.prompt_cache import _sha

                self.cache_breaks.pending.settings = _sha({"s": self.router.last_settings.get(self.role),
                                                           "ttl": self._cache_ttl_latched})
            cleared = int((res.usage or {}).get("context_cleared_tokens") or 0)
            if cleared:
                self.cache_breaks.expect(f"sunucu eski araç sonuçlarını temizledi ({cleared:,} token)")
                self.stats["context_cleared"] = self.stats.get("context_cleared", 0) + cleared
            brk = self.cache_breaks.after(cached_tokens(res.usage or {}), ref, self._cache_ttl(ref))
            if brk is not None and not brk.expected:
                self.stats["cache_breaks"] = self.stats.get("cache_breaks", 0) + 1
                if self.config.agent.get("cache_break_notice", False):
                    self.ui.info(brk.text())
        except Exception:  # noqa: BLE001
            log.debug("cache karşılaştırması yapılamadı", exc_info=True)

    def _prune_images(self) -> None:
        idx = [i for i, m in enumerate(self.messages) if m.get("_image")]
        if len(idx) <= self.keep_images + IMAGE_SLACK and not (self._cache_cold() and len(idx) > self.keep_images):
            return  # pruning on every new screenshot broke the cache at every step: prune in a batch
        self.cache_breaks.expect("eski ekran görüntüleri budandı")
        for i in idx[:-self.keep_images] if self.keep_images else idx:
            self.messages[i] = {"role": "user", "content": SHOT_REMOVED, "_image": False}

    def _shrink_old_outputs(self, force: bool = False) -> None:
        """Shorten old tool outputs: the last KEEP_OUTPUTS results stay as they are, long earlier ones are cut to their
        start and end. Only once the cache has already gone cold, all at once: with a warm cache, changing a message in the
        middle of the history rewrites everything after it to the cache (measured 22 November: with a warm cache, the whole
        conversation from the 3rd message on, about every 8 steps). Compaction takes care of a growing conversation."""
        if not force and not self._cache_cold():
            return
        idx = [i for i, m in enumerate(self.messages) if m["role"] == "tool" and isinstance(m.get("content"), str)]
        todo = [i for i in idx[:-KEEP_OUTPUTS] if len(self.messages[i]["content"]) > OLD_OUTPUT_CHARS * 1.5]
        if not todo:
            return
        if self.config.agent.get("context_editing", False) and self._on_claude_api():
            return  # the Claude API server clears old tool results itself (context editing): history stays intact
        self.cache_breaks.expect("eski araç çıktıları kısaltıldı")
        for i in todo:
            c = self.messages[i]["content"]
            if len(c) > OLD_OUTPUT_CHARS * 1.5:
                self.messages[i] = {**self.messages[i], "content": "[old output, shortened to save space; "
                                    "call the tool again if needed]\n" + truncate(c, OLD_OUTPUT_CHARS)}

    # -------------------------------------------------------------- context
    @property
    def compact_at(self) -> int:
        """The compaction limit. "auto": 60% of the current model's context window (AUTO_COMPACT_RATIO), but at most
        AUTO_COMPACT_MAX ([agent] compact_max_tokens; the provider's limit on narrow tiers like Groq). A number is used as
        given."""
        v = self._compact_cfg
        cap = self.compact_cap or 10**9
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v.strip())
        if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) > 0:
            # A number written by hand is respected. 48000 used to be treated as "the old default" and ignored: for a user
            # with compact_at_tokens = 48000 in config.toml, the conversation was not compacted until 80% of a 1M window
            # (800K), and ~233K tokens went out per request (measured 22 November).
            return min(int(v), cap)
        try:
            auto_max = int(self.config.agent.get("compact_max_tokens", AUTO_COMPACT_MAX))
        except (TypeError, ValueError):
            auto_max = AUTO_COMPACT_MAX
        if auto_max > 0:
            cap = min(cap, max(16000, auto_max))
        try:
            ref = self.router.last_ref.get(self.role) or self.router.first_available(self.role)
        except KeyError:
            ref = None
        if not ref:
            return min(LEGACY_COMPACT_AT, cap)
        return min(max(16000, int(self.router.context_limit(ref) * AUTO_COMPACT_RATIO)), cap)

    @compact_at.setter
    def compact_at(self, value: int) -> None:
        self._compact_cfg = int(value)

    def context_tokens(self) -> int:
        """Number of tokens in the context. The provider's own count from the last request (tool definitions and
        system prompt included) is the base, and messages added after it are estimated; otherwise a full estimate."""
        est = estimate_tokens(self.messages, self.tool_specs())
        lp = self._last_prompt
        if lp and lp[0] > 0 and 0 < lp[1] <= len(self.messages) and self.messages[lp[1] - 1] is lp[2]:
            real = lp[0] + (estimate_tokens(self.messages[lp[1]:]) if lp[1] < len(self.messages) else 0)
            # If it is 4x smaller than the estimate, the provider's count is not reliable (e.g. an endpoint that skips the cached part): keep the estimate.
            if real * 4 >= est:
                return real
        return est

    def _maybe_compact(self, force: bool = False, limit: int = 0) -> bool:
        """Compact the conversation. limit: the model context the conversation must fit in (e.g. when only small
        models are available)."""
        if not force:
            ctx, at = self.context_tokens(), self.compact_at
            post = getattr(self, "_post_compact", 0)
            if post < 0:
                self._post_compact = ctx  # first real size after summarizing: the floor summarizing can reach
                return False
            if ctx < at or (post and ctx < post + max(8000, at // 3)):
                return False  # summarizing again would not shrink it: wait until the conversation really grows
        window = min(limit, self.compact_at) if limit else self.compact_at
        budget = int(window * 0.35)
        summary_tokens = 2000
        if limit:
            # Narrow model: the system prompt and tool definitions take fixed space; the summary and the rest of the conversation must fit in what is left.
            fixed = estimate_tokens(self.messages[:1], self._specs())
            summary_tokens = max(300, min(2000, limit // 8))
            budget = int(limit * 0.85) - fixed - summary_tokens
            if budget < 500:
                return False
        # Cut points: user messages and the starts of assistant steps (a tool call and its result are never split).
        # Agents run long tool chains from a single task message, so cutting at assistant steps must be possible too.
        users = [i for i, m in enumerate(self.messages) if i > 1 and m["role"] in ("user", "assistant")
                 and not m.get("_image")]
        if not users:
            return False
        cut = users[-1]
        for i in reversed(users):
            if estimate_tokens(self.messages[i:]) <= budget:
                cut = i
            else:
                break
        old = self.messages[1:cut]
        if not old:
            # It grew too big even within a single turn: shorten the large tool outputs.
            changed = False
            for m in self.messages[cut:]:
                if m["role"] == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 3000:
                    m["content"] = truncate(m["content"], 3000)
                    changed = True
            return changed and bool(limit)
        self.ui.info(_("the context grew, summarizing..."))
        # First try: ask for the summary on top of the conversation itself (same system, tools and messages as the
        # main thread, the instruction appended at the end). The whole history is then read from the prompt cache
        # instead of being sent again as one fresh transcript at full input price. Narrow models (limit) keep the
        # transcript path: the full history does not fit them anyway.
        summary = "" if limit else self._shared_summary(cut, summary_tokens)
        if not summary:
            transcript = []
            for m in strip_images(old):
                if m.get("_error"):
                    continue  # provider error text is not part of the conversation
                c = m.get("content") or ""
                if m.get("tool_calls"):
                    c += "\n" + "\n".join(f"-> {tc['function']['name']}({tc['function']['arguments'][:400]})" for tc in m["tool_calls"])
                # The previous summary is not trimmed: when it was cut to 2500 characters, most of the old context was lost at every compaction.
                keep = 12000 if is_summary(c) else 2500
                transcript.append(f"[{m['role']}] {truncate(c, keep)}")
            try:
                res: ChatResult = self.router.chat("brain", [
                    {"role": "system", "content": COMPACT_PROMPT},
                    # The summary request must fit the model too: in a narrow context the end of the transcript is sent (a character ≈ 3 tokens).
                    {"role": "user", "content": "\n\n".join(transcript)[-(int(limit * 2.4) if limit else
                                                                           max(120000, self.compact_at * 2)):]},
                ], max_tokens=summary_tokens, cache={"mode": "none"})
            except AllProvidersFailed as e:
                self.ui.info(_("summarizing failed: {error}", error=e))
                return False
            summary = (res.text or "").strip()
        self.cache_breaks.expect("konuşma özetlendi")
        self._reset_effort_latch()  # the prefix is rewritten anyway: the current level can take over
        if not _valid_summary(summary):
            # The model returned tool-call text or a copy of the transcript instead of a summary (11 November: "<tool
            # calls>…" and "-> git({…}) [tool] …" were accepted as the summary, and ALICE forgot what the task was). Such a
            # "summary" must not replace the conversation: the user's requests and the last answer are kept verbatim.
            self.ui.info(_("the model could not produce a valid summary; the user's requests and the latest state were kept verbatim"))
            summary = _clip(_fallback_summary(old), summary_tokens * 3)  # fit the narrow model's share for the summary
        if self.on_compact is not None:
            try:
                self.on_compact(old)  # let the UI keep showing the old messages (session archive)
            except Exception:  # noqa: BLE001 - failing to write the archive must not stop compaction
                log.debug("on_compact hata verdi", exc_info=True)
        ask = _last_request(old) if not any(_is_request(m) for m in self.messages[cut:]) else ""
        if ask:
            # The cut was made at an assistant step (a long tool chain): the user's ongoing request survived only in the
            # summary, and the model worked from the summary's interpretation of it. It is added verbatim.
            summary += f"\n\n{RUNNING_HEAD}\n{_clip(ask, 4000)}"
        head = [self.messages[0], {"role": "user", "content": f"{SUMMARY_HEAD}\n{summary}"}]
        if self.messages[cut]["role"] == "user":  # keep the roles alternating
            head.append({"role": "assistant", "content": ACK_SUMMARY})
        self.messages = head + self.messages[cut:]
        self._post_compact = -1
        self.__dict__["_flush_tools"] = True  # the conversation will be rewritten to the cache anyway: let the waiting tools in now
        self._shrink_old_outputs(force=True)  # for the same reason: the remaining old outputs are trimmed now, with no separate cache break
        self.checkpoints.clear()  # message indexes changed; old rewind points are invalid
        if self.on_step is not None:
            # Save right away: if messages written to the archive also stayed in the session file (the turn errors here
            # or ALICE restarts), they were reloaded at startup, compacted once more and written to the archive a second time.
            try:
                self.on_step()
            except Exception:  # noqa: BLE001 - a save error must not undo the compaction
                log.debug("on_step hata verdi", exc_info=True)
        return True

    def _shared_summary(self, cut: int, summary_tokens: int) -> str:
        """Summary request that shares the main thread's cached prefix (see _maybe_compact). "" if it is turned off
        ([agent] compact_shared_cache = false), fails, or the model answers with tool calls instead of a summary."""
        if not self.config.agent.get("compact_shared_cache", True):
            return ""
        keep = self.messages[cut]
        head = str(keep.get("_display") or keep.get("content") or "")[:160].replace("\n", " ")
        ask = (COMPACT_PROMPT + "\n\nThis is an internal context-management step, not a user request: do NOT call any "
               "tools, answer only with the summary text. Cover everything above up to the "
               f"{keep['role']} message that begins with: \"{head}\" — that message and everything after it is kept "
               "verbatim, so summarize only what comes before it.")
        msgs = list(self.messages) + [{"role": "user", "content": ask}]
        hint = self._cache_hint("read_only")
        if self.config.agent.get("cache_sticky_effort", True):
            hint["latch"] = self._effort_latch()  # same effort as the main thread, or its cache is not read
        # Side call: it must not move the main thread's routing/settings bookkeeping used by the break detector.
        saved = (dict(self.router.last_settings),)
        t0 = time.monotonic()
        try:
            res: ChatResult = self._interruptible(self.router.chat, self.role, msgs, tools=self._specs(),
                                                  max_tokens=max(summary_tokens, 4000), cache=hint)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - falls back to the transcript summary
            log.debug("önbellekli özetleme başarısız: %s", e)
            return ""
        finally:
            self.router.last_settings.clear()
            self.router.last_settings.update(saved[0])
        self._count_call(res, t0, 0.0)
        text = (res.text or "").strip()
        if res.message.get("tool_calls") and not _valid_summary(text):
            return ""
        return text if _valid_summary(text) else ""

    def _roomy(self, res: ChatResult) -> bool:
        try:
            limit = self.router.context_limit(f"{res.provider}:{res.model}")
        except Exception:  # noqa: BLE001
            return False
        return limit > estimate_tokens(self.messages[:1], self.tool_specs()) * 3

    def _specs(self) -> list[dict[str, Any]]:
        specs = self.tool_specs()
        return lean_specs(specs) if getattr(self, "_lean", False) else specs

    def compact(self) -> bool:
        return self._maybe_compact(force=True)

    def _repair_after_interrupt(self) -> None:
        """Add placeholder results for unfinished tool_calls, otherwise the next request gets a 400."""
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = {x.get("tool_call_id") for x in self.messages[i + 1:] if x["role"] == "tool"}
                # Results must go into the tool block right after the call: if a partial text or image message got in
                # between, the appended result is orphaned and the provider rejected the whole conversation.
                at = i + 1
                while at < len(self.messages) and self.messages[at]["role"] == "tool":
                    at += 1
                missing = [{"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"],
                            "content": STOPPED}
                           for tc in m["tool_calls"] if tc["id"] not in answered]
                self.messages[at:at] = missing
                break

    def reset(self) -> None:
        self.messages = self.messages[:1]
        self._post_compact = 0
        self._last_turn_role = None
        self._reset_effort_latch()
        self.cache_breaks.reset()
