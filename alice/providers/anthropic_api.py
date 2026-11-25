"""Official Claude API (console.anthropic.com key) — via the official `anthropic` package.

The rest of ALICE works with messages in OpenAI format (role/content/tool_calls/tool). This client converts those
messages to Claude's Messages format and converts the answer back; it gives the router the same interface as the
other providers (chat, chat_stream, list_models). Retries, cooldowns and key switching are done in the router and the
key pool, so the package's own retry is turned off.

Notes:
- Thinking is always adaptive; its summary flows to on_reasoning. Thinking blocks are kept on the message (THINKING_KEY)
  and sent back unchanged on later steps. They used to be dropped: in a long tool loop the model lost its plan at every
  step and worked it out again (25 November: 39 steps, 135K output tokens, only ~15K of them visible, no file edited).
  A block is valid only after the exact conversation that produced it, and ALICE shortens and rewrites the history, so
  each block carries a fingerprint of that conversation: blocks from before an edit are left out (replayable).
- If the safety classifier refuses the request, the API continues the same request with another Claude model
  (fallbacks="default"); if that one refuses too, the error goes to the router, which moves on to the next model.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, Callable

from alice.config import ProviderConfig
from alice.providers.openai_compat import (ChatResult, ProviderError, StreamCancelled, classify_error, redact,
                                           redact_binary)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_EDIT_BETA = "context-management-2025-06-27"
# If a replayed block still does not match its conversation, the API drops it instead of failing the request.
BINDING_BETA = "thinking-binding-controls-2026-08-01"
THINKING_KEY = "_claude_thinking"  # internal message key: {"prefix": fingerprint, "blocks": [...]} (other providers never see it)
# Models that support server-side fallback (not Haiku).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Type of an in-stream "error" event -> its HTTP equivalent (for classification).
_STREAM_ERROR_STATUS = {"overloaded_error": 529, "rate_limit_error": 429, "request_too_large": 413,
                        "authentication_error": 401, "permission_error": 403, "not_found_error": 404,
                        "billing_error": 402, "invalid_request_error": 400, "api_error": 500}


def _sdk() -> Any:
    try:
        import anthropic
    except ImportError as e:
        raise ProviderError("auth", "the Claude API needs the anthropic package: pip install anthropic") from e
    return anthropic


# ------------------------------------------------------------------------------------------- message conversion
def _image_block(url: str) -> dict[str, Any] | None:
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media = head[5:].split(";")[0] or "image/png"
        if ";base64" not in head:
            data = base64.b64encode(data.encode("utf-8")).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out: list[dict[str, Any]] = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            iu = p.get("image_url")
            block = _image_block(iu.get("url", "") if isinstance(iu, dict) else str(iu or ""))
            if block:
                out.append(block)
    return out


def _tool_result_content(content: Any) -> str | list[dict[str, Any]]:
    """Tool result: text is passed as is; a list of parts (e.g. a screenshot) is converted to text and image blocks.
    It used to be dumped as JSON text: the base64 of an image went to the model as thousands of text tokens."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "image" and isinstance(p.get("data"), str):  # MCP format
                blocks.append({"type": "image", "source": {"type": "base64", "data": p["data"],
                                                           "media_type": p.get("mimeType") or "image/png"}})
            else:
                blocks.extend(_user_content([p]))
        if blocks:
            return blocks
    return redact_binary(content)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def _tool_id(raw: Any) -> str:
    """Claude accepts only letters, digits, _ and - in a tool call id. Earlier steps of the conversation may have been
    made by another model: ids like "functions.read_file:0" from providers such as Kimi made the whole request fail
    with 400. The same id is converted the same way in both tool_use and tool_result, so the match is not broken."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(raw or "")) or "call"


# --------------------------------------------------------------------------------------------- thinking replay
def _tools_digest(tools: list[dict[str, Any]] | None) -> str:
    specs = sorted(json.dumps(t, sort_keys=True, ensure_ascii=False, default=str) for t in tools or [])
    return hashlib.sha1("\n".join(specs).encode("utf-8", "replace"), usedforsecurity=False).hexdigest()


def prefix_digests(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> list[str]:
    """digests[i]: fingerprint of the tools and messages[:i], i.e. what the model saw before message i. The thinking
    itself is not part of it (the API does not count earlier thinking blocks either)."""
    cur = _tools_digest(tools)
    out = [cur]
    for m in messages:
        body = {k: m.get(k) for k in ("role", "content", "tool_calls", "tool_call_id")}
        data = cur + json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
        cur = hashlib.sha1(data.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()
        out.append(cur)
    return out


def thinking_layout(content: Any) -> list[dict[str, Any]]:
    """Claude answer → its blocks in order. Thinking is kept whole; text (as a length) and tool calls (as an id) are
    only referred to, because the message already holds them. Empty if there is nothing to send back, or if the answer
    has a block we could not rebuild exactly (e.g. the switch marker of a server-side fallback)."""
    out: list[dict[str, Any]] = []
    for b in content or []:
        if b.type == "thinking":
            if not getattr(b, "signature", None):
                return []
            out.append({"type": "thinking", "thinking": b.thinking or "", "signature": b.signature})
        elif b.type == "redacted_thinking":
            out.append({"type": "redacted_thinking", "data": b.data})
        elif b.type == "text":
            out.append({"type": "text", "len": len(b.text)})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id})
        else:
            return []
    return out if any(b["type"].endswith("thinking") for b in out) else []


def _with_thinking(layout: list[dict[str, Any]], text: str, calls: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Rebuild the answer's blocks in their original order; None if the message was changed since."""
    by_id = {c["id"]: c for c in calls}
    out: list[dict[str, Any]] = []
    pos = used = 0
    for b in layout:
        if b["type"] == "text":
            piece = text[pos:pos + b["len"]]
            pos += b["len"]
            if piece.strip():
                out.append({"type": "text", "text": piece})
        elif b["type"] == "tool_use":
            call = by_id.get(_tool_id(b["id"]))
            if call is None:
                return None
            used += 1
            out.append(call)
        else:
            out.append(dict(b))
    return out if pos == len(text) and used == len(calls) else None


def replayable(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> set[int]:
    """Indexes of the messages whose thinking can go back. Blocks may only be removed from the front, so everything up
    to the last block whose conversation changed (compaction, shortened output, new tools...) is left out."""
    marked = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get(THINKING_KEY)]
    if not marked:
        return set()
    digests = prefix_digests(messages, tools)
    stale = [i for i in marked if messages[i][THINKING_KEY].get("prefix") != digests[i]]
    last = stale[-1] if stale else -1
    return {i for i in marked if i > last}


def to_messages(messages: list[dict[str, Any]], replay: set[int] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI format → (system, Claude messages). Leading system messages go to system; those in the middle go to
    the user turn as marked text. Consecutive tool results are collected into a single user turn. `replay`: indexes of
    the messages that are sent with their thinking blocks."""
    system: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            t = _text(m.get("content"))
            if not out:
                system.append(t)
            elif t:
                push("user", [{"type": "text", "text": f"[Sistem notu]\n{t}"}])
        elif role == "user":
            push("user", _user_content(m.get("content")))
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            t = _text(m.get("content"))
            if t.strip():
                blocks.append({"type": "text", "text": t})
            calls: list[dict[str, Any]] = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls.append({"type": "tool_use", "id": _tool_id(tc.get("id")), "name": fn.get("name") or "",
                              "input": args if isinstance(args, dict) else {"value": args}})
            blocks.extend(calls)
            if replay and i in replay:
                blocks = _with_thinking(m[THINKING_KEY]["blocks"], t, calls) or blocks
            push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": _tool_id(m.get("tool_call_id")),
                           "content": _tool_result_content(m.get("content"))}])
    if out and out[0]["role"] != "user":  # the first turn must come from the user
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(devam)"}]})
    return "\n\n".join(s for s in system if s), out


def to_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        out.append({"name": fn["name"], "description": fn.get("description") or "", "input_schema": schema})
    return out


_CACHEABLE = {"text", "tool_result", "image", "document"}
CACHE_MODES = ("write", "read_only", "none")


def _markable(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in _CACHEABLE and (
        block.get("type") != "text" or bool(str(block.get("text") or "").strip()))


def _mark_message(m: dict[str, Any], eph: dict[str, Any]) -> bool:
    content = m.get("content")
    if isinstance(content, str):
        if content.strip():
            m["content"] = [{"type": "text", "text": content, "cache_control": eph}]
            return True
        return False
    if isinstance(content, list) and content and _markable(content[-1]):
        content[-1] = {**content[-1], "cache_control": eph}
        return True
    return False


def mark_cache(kw: dict[str, Any], ttl: str = "5m", mode: str = "write") -> None:
    """Prompt cache markers (at most 4 per request; we use up to 3).

    - system: split at DYNAMIC_HEADING into a static and a dynamic block, a marker on each. Tools come before system
      in Claude's prefix, so the static marker covers the tool list too. When only the dynamic tail (date, cwd,
      memory) changes, the static part is still read from the cache.
    - messages: exactly one marker. mode="write": on the last block (the next step reads everything up to here).
      mode="read_only": on the second-to-last message, so a one-off side call (a fork, a compaction summary) reads the
      shared prefix but does not pay to write its own tail. mode="none": no markers at all (tiny one-shot calls).
    ttl: "5m" (default, writes cost 1.25x input) or "1h" (writes 2x, survives breaks of up to an hour).
    """
    from alice.providers.prompt_cache import split_system

    if mode == "none":
        if isinstance(kw.get("system"), str) and not kw["system"].strip():
            kw.pop("system")
        return
    eph: dict[str, Any] = {"type": "ephemeral"}
    if ttl == "1h":
        eph["ttl"] = "1h"
    system = kw.get("system")
    if isinstance(system, str) and system.strip():
        static, dynamic = split_system(system)
        blocks = [{"type": "text", "text": static, "cache_control": dict(eph)}] if static.strip() else []
        if dynamic.strip():
            blocks.append({"type": "text", "text": dynamic, "cache_control": dict(eph)})
        kw["system"] = blocks
    msgs = kw.get("messages") or []
    if not msgs:
        return
    if mode == "read_only":
        for m in reversed(msgs[:-1]):
            if _mark_message(m, dict(eph)):
                return
        return
    _mark_message(msgs[-1], dict(eph))


def from_message(msg: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Claude answer → (message in OpenAI format, usage)."""
    texts, calls = [], []
    for b in msg.content or []:
        if b.type == "text":
            texts.append(b.text)
        elif b.type == "tool_use":
            calls.append({"id": b.id, "type": "function",
                          "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)}})
    out: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if calls:
        out["tool_calls"] = calls
    u = msg.usage
    written = getattr(u, "cache_creation_input_tokens", 0) or 0
    prompt = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + written
    # Tokens written to the cache are reported separately: the router's usage counter (cache_write_tokens) reads this
    # name. They used to be dropped, so cache writes (more expensive than normal input) always showed 0 on the Claude API.
    usage = {"prompt_tokens": prompt, "completion_tokens": u.output_tokens or 0,
             "cached_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
             "cache_creation_input_tokens": written}
    # 1-hour writes cost 2x input, 5-minute writes 1.25x: kept apart so the cost estimate stays right.
    cc = getattr(u, "cache_creation", None)
    w1h = (cc.get("ephemeral_1h_input_tokens") if isinstance(cc, dict)
           else getattr(cc, "ephemeral_1h_input_tokens", 0)) if cc is not None else 0
    if isinstance(w1h, (int, float)) and w1h:
        usage["cache_write_1h_tokens"] = int(w1h)
    # Server-side context editing: how many tokens of old tool results the server cleared on this request.
    cm = getattr(msg, "context_management", None)
    applied = (cm.get("applied_edits") if isinstance(cm, dict) else getattr(cm, "applied_edits", None)) if cm else None
    cleared = 0
    for e in applied or []:
        v = e.get("cleared_input_tokens") if isinstance(e, dict) else getattr(e, "cleared_input_tokens", 0)
        cleared += int(v or 0) if isinstance(v, (int, float)) else 0
    if cleared:
        usage["context_cleared_tokens"] = cleared
    return out, usage


class AnthropicProvider:
    STREAM_READ = 300.0  # sec: the stream is cut if no data arrives for this long (even long thinking sends summaries and pings)
    # In a non-streaming call the answer arrives in one piece, only when it is finished: a long thinking answer easily
    # exceeds 300 sec and the read timeout cut it. Like the package's own suggestion, the time scales with max_tokens
    # (128k tokens ~ 1 hr).
    NONSTREAM_MIN = 900.0
    NONSTREAM_MAX = 3600.0

    @classmethod
    def nonstream_timeout(cls, max_tokens: int) -> float:
        return min(cls.NONSTREAM_MAX, max(cls.NONSTREAM_MIN, 3600.0 * max_tokens / 128_000))

    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: Any = None):
        self.cfg = cfg
        self.name = cfg.name
        self.timeout = timeout
        self._client = client
        self._stale: set[str] = set()  # signatures the API rejected: never sent again
        self._binding = True  # the endpoint accepts thinking.block_binding

    @property
    def client(self) -> Any:
        if self._client is None:
            sdk = _sdk()
            base = self.cfg.base_url.rstrip("/")
            self._client = sdk.Anthropic(api_key=self.cfg.api_key or "", max_retries=0,
                                         base_url=base if base and base != "https://api.anthropic.com" else None,
                                         default_headers=self.cfg.headers or None,
                                         timeout=sdk.Timeout(self.timeout, connect=15.0, read=self.STREAM_READ))
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - errors on close are unimportant
                pass

    # ------------------------------------------------------------------------------------ request body
    def _replay(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> set[int]:
        ok = replayable(messages, tools)
        bad = [i for i in ok if any(b.get("signature", b.get("data")) in self._stale
                                    for b in messages[i][THINKING_KEY]["blocks"] if b["type"].endswith("thinking"))]
        return {i for i in ok if not bad or i > max(bad)}

    def _recover(self, err: ProviderError, kw: dict[str, Any]) -> bool:
        """The request was rejected because of the thinking blocks sent back: change what caused it, True if it is worth
        sending again. Handled here: the router takes a bad_request for a rejected effort setting and turns it off."""
        low = str(err).lower()
        if err.kind != "bad_request":
            return False
        if self._binding and ("block_binding" in low or BINDING_BETA in low) and "bound to" not in low:
            self._binding = False
            return True
        sent = {b.get("signature", b.get("data")) for m in kw["messages"] if m["role"] == "assistant"
                for b in m["content"] if b["type"] in ("thinking", "redacted_thinking")}
        if sent and ("thinking" in low or "signature" in low):
            self._stale |= sent
            return True
        return False

    @staticmethod
    def _keep_thinking(out: dict[str, Any], msg: Any, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None) -> None:
        layout = thinking_layout(msg.content) if tools else []  # one-off calls without tools are not continued
        if layout:
            out[THINKING_KEY] = {"prefix": prefix_digests(messages, tools)[-1], "blocks": layout}

    def _params(self, model, messages, tools, max_tokens, response_format, extra, stream: bool) -> dict[str, Any]:
        system, msgs = to_messages(messages, self._replay(messages, tools))
        kw: dict[str, Any] = {"model": model, "messages": msgs,
                              "max_tokens": int(max_tokens) if max_tokens else (32000 if stream else 16000)}
        out_cfg: dict[str, Any] = {}
        if response_format and response_format.get("type") == "json_schema":
            schema = (response_format.get("json_schema") or {}).get("schema")
            if schema:
                out_cfg["format"] = {"type": "json_schema", "schema": schema}
        elif response_format and response_format.get("type") == "json_object":
            system = (system + "\n\n" if system else "") + "Give the answer only as a valid JSON object."
        if system:
            kw["system"] = system
        t = to_tools(tools)
        if t:
            kw["tools"] = t
        ttl = str((extra or {}).get("_cache_ttl") or "5m")
        mode = str((extra or {}).get("_cache_mode") or "write")
        mark_cache(kw, ttl=ttl if ttl in ("5m", "1h") else "5m", mode=mode if mode in CACHE_MODES else "write")
        if not model.startswith(("claude-3", "claude-haiku-4")):
            kw["thinking"] = {"type": "adaptive", "display": "summarized"}
            if self._binding and any(b["type"].endswith("thinking") for m in msgs if m["role"] == "assistant"
                                     for b in m["content"]):
                kw["thinking"]["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
        effort = (extra or {}).get("reasoning_effort") or ((extra or {}).get("output_config") or {}).get("effort")
        if effort in EFFORTS:
            out_cfg["effort"] = effort
        if out_cfg:
            kw["output_config"] = out_cfg
        betas: list[str] = []
        if "block_binding" in kw.get("thinking", {}):
            betas.append(BINDING_BETA)
        if model in FALLBACK_MODELS:
            betas.append(FALLBACK_BETA)
            kw["fallbacks"] = "default"
        edit = (extra or {}).get("_context_edit")
        if isinstance(edit, dict) and tools:
            # Server-side context editing (public counterpart of Claude Code's internal cache_edits): old tool results
            # are cleared on the server, ALICE's local history stays as it is. clear_at_least keeps a clearing big
            # enough to be worth the cache rewrite it causes.
            betas.append(CONTEXT_EDIT_BETA)
            kw["context_management"] = {"edits": [{
                "type": "clear_tool_uses_20250919",
                "trigger": {"type": "input_tokens", "value": int(edit.get("trigger", 100_000))},
                "keep": {"type": "tool_uses", "value": int(edit.get("keep", 6))},
                "clear_at_least": {"type": "input_tokens", "value": int(edit.get("clear_at_least", 20_000))},
                **({"exclude_tools": list(edit["exclude_tools"])} if edit.get("exclude_tools") else {}),
            }]}
        if betas:
            kw["betas"] = betas
        return kw

    def _error(self, e: Exception) -> ProviderError:
        sdk = _sdk()
        if isinstance(e, sdk.APITimeoutError):
            err = ProviderError("network", "timed out: the Claude API did not answer")
        elif isinstance(e, sdk.APIConnectionError):
            err = ProviderError("network", f"could not connect to the Claude API: {e}")
        elif isinstance(e, sdk.APIStatusError):
            body = ""
            try:
                body = e.response.text
            except Exception:  # noqa: BLE001
                body = str(e)
            status = e.status_code
            if status < 400:
                # An "error" event after the stream opened with 200 (overloaded_error, api_error...): the package
                # makes it a 200 APIStatusError, and classify_error called 200 "bad_request", so a temporary overload
                # was handled as a permanent request error. The status code is now taken from the error type.
                raw = getattr(e, "body", None)
                etype = ((raw.get("error") or {}).get("type") if isinstance(raw, dict)
                         and isinstance(raw.get("error"), dict) else None) or ""
                text = f"{e} {body}"
                status = _STREAM_ERROR_STATUS.get(etype) or next(
                    (code for name, code in _STREAM_ERROR_STATUS.items() if name in text), 500)
            err = classify_error(status, body or str(e), dict(e.response.headers))
        else:
            err = ProviderError("server", str(e))
        err.args = tuple(redact(a, self.cfg.api_key) if isinstance(a, str) else a for a in err.args)
        return err

    @staticmethod
    def _check(msg: Any) -> None:
        if msg.stop_reason == "refusal":
            det = getattr(msg, "stop_details", None)
            cat = getattr(det, "category", None) if det else None
            raise ProviderError("bad_request", "Claude refused the request" + (f" ({cat})" if cat else ""))
        if msg.stop_reason == "max_tokens" and not any(b.type in ("text", "tool_use") and
                                                       (b.type == "tool_use" or b.text) for b in msg.content):
            raise ProviderError("too_large", "the answer hit max_tokens, no content")

    # ---------------------------------------------------------------------------------------- calls
    def chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             response_format: dict[str, Any] | None = None, extra: dict[str, Any] | None = None,
             _retries: int = 2) -> ChatResult:
        kw = self._params(model, messages, tools, max_tokens, response_format, extra, stream=False)
        t0 = time.monotonic()
        secs = self.nonstream_timeout(kw["max_tokens"])
        try:
            timeout: Any = _sdk().Timeout(secs, connect=15.0)
        except ProviderError:
            timeout = secs  # if the package is missing (fake client), a plain timeout
        try:
            msg = self.client.beta.messages.create(**kw, timeout=timeout)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - package errors are turned into provider errors
            err = self._error(e)
            if _retries and self._recover(err, kw):
                return self.chat(model, messages, tools, temperature, max_tokens, response_format, extra, _retries - 1)
            raise err from e
        self._check(msg)
        out, usage = from_message(msg)
        self._keep_thinking(out, msg, messages, tools)
        return ChatResult(out, self.name, getattr(msg, "model", None) or model, usage, time.monotonic() - t0,
                          finish="length" if getattr(msg, "stop_reason", "") == "max_tokens" else "")

    def chat_stream(self, model: str, messages: list[dict[str, Any]], on_delta: Callable[[str], None],
                    tools: list[dict[str, Any]] | None = None, temperature: float | None = None,
                    max_tokens: int | None = None, response_format: dict[str, Any] | None = None,
                    extra: dict[str, Any] | None = None, cancel: Callable[[], bool] | None = None,
                    on_reasoning: Callable[[str], None] | None = None, on_first: Callable[[], None] | None = None,
                    on_tool: Callable[[str, int], None] | None = None,
                    on_call: Callable[[dict[str, Any]], None] | None = None, _retries: int = 2) -> ChatResult:
        kw = self._params(model, messages, tools, max_tokens, response_format, extra, stream=True)
        t0 = time.monotonic()
        tool_name, tool_len = "", 0
        started = False
        try:
            with self.client.beta.messages.stream(**kw) as stream:
                for ev in stream:
                    if cancel and cancel():
                        raise StreamCancelled()
                    if ev.type == "content_block_start":
                        started = True
                        if ev.content_block.type == "tool_use":
                            tool_name, tool_len = ev.content_block.name, 0
                    elif ev.type == "content_block_delta":
                        d = ev.delta
                        if on_first is not None and d.type in ("text_delta", "thinking_delta", "input_json_delta"):
                            on_first()
                            on_first = None
                        if d.type == "text_delta" and d.text:
                            on_delta(d.text)
                        elif d.type == "thinking_delta" and d.thinking and on_reasoning:
                            on_reasoning(d.thinking)
                        elif d.type == "input_json_delta" and on_tool is not None:
                            tool_len += len(d.partial_json or "")
                            on_tool(tool_name, tool_len)
                    elif ev.type == "content_block_stop" and on_call is not None:
                        block = getattr(ev, "content_block", None)
                        if getattr(block, "type", "") == "tool_use":
                            try:  # tool block finished: the agent can start a safe tool before the answer is complete
                                on_call({"id": block.id, "type": "function", "function": {
                                    "name": block.name, "arguments": json.dumps(block.input or {}, ensure_ascii=False)}})
                            except Exception:  # noqa: BLE001
                                pass
                msg = stream.get_final_message()
        except (ProviderError, StreamCancelled):
            raise
        except Exception as e:  # noqa: BLE001
            err = self._error(e)
            if _retries and not started and self._recover(err, kw):  # rejected before any output
                return self.chat_stream(model, messages, on_delta, tools, temperature, max_tokens, response_format, extra,
                                        cancel, on_reasoning, on_first, on_tool, on_call, _retries - 1)
            raise err from e
        self._check(msg)
        out, usage = from_message(msg)
        self._keep_thinking(out, msg, messages, tools)
        return ChatResult(out, self.name, getattr(msg, "model", None) or model, usage, time.monotonic() - t0,
                          finish="length" if getattr(msg, "stop_reason", "") == "max_tokens" else "")

    def list_models(self) -> list[dict[str, Any]]:
        try:
            return [{"id": m.id, "context_length": getattr(m, "max_input_tokens", None)}
                    for m in self.client.models.list()]
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._error(e) from e
