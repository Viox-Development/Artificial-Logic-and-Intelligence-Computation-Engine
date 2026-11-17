"""OpenAI-compatible /chat/completions client.

Gemini, OpenRouter, Groq, NVIDIA NIM, Cerebras, and Ollama all speak this format,
so a single client is enough. The main task is properly classifying errors: the router
decides based on this whether to wait on an error or completely skip the model/provider.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from alice.config import ProviderConfig


# Key patterns that may appear in error texts (provider-echoed body, httpx exception). These are
# masked before going to the user-facing error and ~/.alice/logs/providers.log.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)([?&](?:key|api_key|apikey|access_token|token)=)[^&\s\"']+"),
    re.compile(r"\b(?:sk-(?:or-v1-|proj-)?|gsk_|nvapi-|csk-|xai-|ghp_|github_pat_|hf_)[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
]


def redact(text: str, *secrets: str | None) -> str:
    """Mask API keys in text: given actual keys and known key formats."""
    if not text:
        return text
    for sec in secrets:
        if sec and len(sec) >= 6:
            text = text.replace(sec, "***")
    for rx in _SECRET_PATTERNS:
        text = rx.sub(lambda m: (m.group(1) if m.re.groups else "") + "***", text)
    return text


def redact_binary(content: Any) -> str:
    """Unknown tool results go as JSON text; images/binary data inside are replaced with a placeholder."""
    def scrub(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [scrub(x) for x in v]
        if isinstance(v, str) and (v.startswith("data:") and ";base64," in v[:100]
                                   or len(v) > 512 and re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", v)):
            return "[ikili veri atlandı]"
        return v

    return "" if content is None else json.dumps(scrub(content), ensure_ascii=False)


class ProviderError(Exception):
    """kind: rate_limit | quota | auth | not_found | too_large | bad_request | server | network | unreachable | garbled"""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None,
                 daily: bool = False):
        super().__init__(f"[{kind}] {redact(message)}")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.daily = daily  # daily quota: no requests to this model until it resets the next day


class StreamCancelled(Exception):
    """User cancelled the stream."""


@dataclass
class ChatResult:
    message: dict[str, Any]
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0
    finish: str = ""  # "length": the response hit the output limit and was cut short

    @property
    def text(self) -> str:
        c = self.message.get("content")
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return c or ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []


# "set up billing": Gemini returns 429 with this text when the free tier is exhausted; retrying after 20s is pointless.
_QUOTA_HINTS = ("per day", "daily", "quota", "rpd", "tokens per day", "free-models-per-day", "exhausted",
                "billing", "insufficient credits", "insufficient_quota", "credit balance")
_DAILY_HINTS = ("per day", "daily", "rpd", "tokens per day", "free-models-per-day")
_TOO_LARGE_HINTS = ("context length", "context_length", "maximum context", "too long", "too large",
                    "reduce the length", "request_too_large", "tokens per minute", "max_tokens")
# 401/403 but the issue is not the key, it's the model: e.g. "Model ... is not available on the Workers Free plan" (Cloudflare),
# "you do not have access to model ...", etc. Counting it as "auth" used to disable ALL models of the provider.
_MODEL_DENIED_HINTS = ("not available on the workers", "requires a paid plan", "not available on your plan",
                       "not available on the free", "do not have access to model", "does not have access to model",
                       "not have access to the model", "model is not available")
_NOT_FOUND_HINTS = ("model not found", "does not exist", "no endpoints found", "not a valid model",
                    "model_not_found", "unknown model", "is not supported", "decommissioned", "deprecated")


def _quota_window(low: str) -> tuple[str, float | None]:
    """Extract the window ("day", "minute", or "") and retry delay from the quota ID in the Gemini 429 body.
    Gemini also writes "exceeded your current quota" for minute limits: previously all were assumed daily
    regardless of the ID, and a model that would open in 40s was locked for an hour."""
    window = "day" if "perday" in low else "minute" if "perminute" in low else ""
    m = re.search(r'"retrydelay":\s*"([\d.]+)s"', low) or re.search(r"retry in ([\d.]+)\s*s", low)
    return window, (float(m.group(1)) if m else None)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After header: seconds ("7") or HTTP date ("Wed, 21 Oct 2026 07:28:00 GMT"). The date format
    was previously ignored silently, using the default duration instead of the server's requested wait time."""
    if not value:
        return None
    value = value.strip()
    try:
        secs = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        secs = (when - (now or datetime.now(timezone.utc))).total_seconds()
    if not math.isfinite(secs):
        return None
    return max(0.0, secs)


_HTML_RX = re.compile(r"^\s*(<!doctype|<html|<head|<body|<\?xml)", re.I)


def summarize_body(body: str, limit: int = 400) -> str:
    """Convert an error body into short text that can be shown to the user. Previously the HTML error pages of
    intermediate layers such as Cloudflare/nginx were dumped into the error message as-is (tags, styles, scripts)."""
    body = body or ""
    if _HTML_RX.match(body) or "<html" in body[:500].lower():
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        text = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", body, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(text.split())
        head = " ".join(title.group(1).split()) if title else ""
        if head and text.startswith(head):
            text = text[len(head):].strip()
        if head:
            out = f"HTML hata sayfası: {head}" + (f" - {text}" if text else "")
        else:
            out = f"HTML hata sayfası: {text}"
        return out[:limit]
    return " ".join(body.split())[:limit] if body.strip() else "(boş gövde)"


def classify_error(status: int, body: str, headers: dict[str, str] | None = None) -> ProviderError:
    body = body or ""
    low = body.lower()
    retry_after = None
    if headers:
        retry_after = parse_retry_after(headers.get("retry-after") or headers.get("Retry-After"))
    short = summarize_body(body)
    if status in (401, 403):
        # OpenRouter also returns 403 for some moderation/quota errors.
        if any(h in low for h in _MODEL_DENIED_HINTS):
            return ProviderError("not_found", short, status)  # only this model is turned off
        if any(h in low for h in _QUOTA_HINTS):
            return ProviderError("quota", short, status, retry_after)
        return ProviderError("auth", short, status)
    if status in (404, 410):  # 410: model has been removed
        return ProviderError("not_found", short, status)
    if status == 402:  # OpenRouter: insufficient credits. Previously counted as bad_request and retried after 60 s.
        return ProviderError("quota", short, status, retry_after)
    if status == 408:  # server-side request timeout: transient
        return ProviderError("network", short, status, retry_after)
    if status == 413:
        return ProviderError("too_large", short, status)
    if status == 429:
        if any(h in low for h in _TOO_LARGE_HINTS) and "request" in low and "too large" in low:
            return ProviderError("too_large", short, status)
        window, wait = _quota_window(low)
        if window == "day":
            return ProviderError("quota", short, status, retry_after, daily=True)
        if window == "minute":
            return ProviderError("rate_limit", short, status, retry_after or wait)
        if any(h in low for h in _DAILY_HINTS):
            return ProviderError("quota", short, status, retry_after, daily=True)
        if any(h in low for h in _QUOTA_HINTS):
            return ProviderError("quota", short, status, retry_after)
        return ProviderError("rate_limit", short, status, retry_after)
    if status in (400, 422):
        if any(h in low for h in ("credit balance", "insufficient credits", "insufficient_quota")):
            return ProviderError("quota", short, status)  # Claude API: "Your credit balance is too low" (400)
        if any(h in low for h in _NOT_FOUND_HINTS):
            return ProviderError("not_found", short, status)
        if any(h in low for h in _TOO_LARGE_HINTS):
            return ProviderError("too_large", short, status)
        return ProviderError("bad_request", short, status)
    if status >= 500:
        return ProviderError("server", short, status, retry_after)
    return ProviderError("bad_request", short, status)


def _inline_error(err: Any) -> ProviderError:
    """Classify the {"error": ...} object inside a 200 response (or an SSE chunk). Sometimes code arrives as text
    such as "429"; previously the error was then counted as 500 (server) and the daily quota was retried after 30 s."""
    code = err.get("code") if isinstance(err, dict) else None
    if isinstance(code, str) and code.strip().isdigit():
        code = int(code.strip())
    status = code if isinstance(code, int) and 100 <= code <= 599 else 500
    try:
        msg = json.dumps(err, ensure_ascii=False)[:400]
    except (TypeError, ValueError):
        msg = str(err)[:400]
    return classify_error(status, msg)


def _transport_error(e: Exception) -> ProviderError:
    """Classify an httpx exception. The text of exceptions like ReadTimeout is often empty: previously the user
    only saw "timeout: "."""
    detail = str(e).strip()
    label = type(e).__name__ + (f": {detail}" if detail else "")
    if isinstance(e, httpx.ConnectTimeout):
        return ProviderError("network", f"bağlantı zaman aşımı ({label})")
    if isinstance(e, httpx.TimeoutException):
        return ProviderError("network", f"zaman aşımı ({label})")
    if isinstance(e, httpx.ConnectError):
        return ProviderError("unreachable", f"bağlanılamadı ({label})")
    if isinstance(e, httpx.RemoteProtocolError):
        return ProviderError("network", f"sunucu bağlantıyı yarıda kesti ({label})")
    if isinstance(e, httpx.InvalidURL):
        return ProviderError("unreachable", f"geçersiz sağlayıcı adresi ({label})")
    return ProviderError("network", label)


# httpx.InvalidURL and StreamError don't subclass HTTPError; they used to escape the router as non-ProviderError.
_TRANSPORT_ERRORS = (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError)


class OpenAICompatProvider:
    # The stream is cut off if it sends no real data (text, thinking, tool fragments) for this many seconds. Some
    # providers send keep-alive lines like ": PROCESSING" while waiting; these reset the read timeout, so without
    # this limit a call can hang for hours.
    STREAM_IDLE = 300.0
    # If no first real data (text, thinking, tool) arrives within this many seconds, the model is considered stuck
    # and the next one is tried. glm-5.3 on NVIDIA kept the connection open without answering for over 5 minutes.
    FIRST_DATA = 90.0

    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: httpx.Client | None = None):
        self.cfg = cfg
        self.name = cfg.name
        # A provider that rejects usage reporting in streams (stream_options.include_usage) with a 400 once
        # is not asked again.
        self._stream_usage = True
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0))

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - errors on close don't matter
            pass

    def _scrub(self, e: ProviderError) -> ProviderError:
        """Mask this provider's real key if it appears in the error text (the provider echoed it back)."""
        e.args = tuple(redact(a, self.cfg.api_key) if isinstance(a, str) else a for a in e.args)
        return e

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.cfg.headers}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    @staticmethod
    def _body(model, messages, tools, temperature, max_tokens, response_format, extra) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        if extra:
            body.update(extra)
        return body

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        try:
            return self._chat(model, messages, tools, temperature, max_tokens, response_format, extra)
        except ProviderError as e:
            raise self._scrub(e) from e.__cause__

    def _chat(self, model, messages, tools, temperature, max_tokens, response_format, extra) -> ChatResult:
        body = self._body(model, messages, tools, temperature, max_tokens, response_format, extra)
        if "openrouter.ai" in self.cfg.base_url:
            body.setdefault("usage", {"include": True})
        t0 = time.monotonic()
        try:
            r = self._client.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
        except _TRANSPORT_ERRORS as e:
            # ConnectError: no server at all (e.g. Ollama is off), so don't retry for a long time.
            raise _transport_error(e) from e
        latency = time.monotonic() - t0

        if r.status_code != 200:
            raise classify_error(r.status_code, r.text, dict(r.headers))
        try:
            data = r.json()
        except ValueError as e:  # JSONDecodeError or broken encoding
            raise ProviderError("server", f"geçersiz JSON: {summarize_body(r.text, 200)}", 200) from e

        # Some providers (OpenRouter) return errors inside a 200 response.
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):
            raise _inline_error(data["error"])
        if not isinstance(data, dict):
            raise ProviderError("server", f"beklenmeyen cevap biçimi: {str(data)[:200]}", 200)

        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("server", f"boş cevap: {str(data)[:200]}", 200)
        msg = choices[0].get("message") or {}
        if not isinstance(msg, dict):
            raise ProviderError("server", f"beklenmeyen mesaj biçimi: {str(msg)[:200]}", 200)
        if not msg.get("content") and not msg.get("tool_calls"):
            if choices[0].get("finish_reason") == "length":
                raise ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")
        return ChatResult(
            message=normalize_message(msg),
            provider=self.name,
            model=data.get("model") or model,
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
            latency=latency,
            finish=str(choices[0].get("finish_reason") or ""),
        )

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        on_delta: Callable[[str], None],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        cancel: Callable[[], bool] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_first: Callable[[], None] | None = None,
        on_tool: Callable[[str, int], None] | None = None,
        on_call: Callable[[dict[str, Any]], None] | None = None,
    ) -> ChatResult:
        """Streamed response over SSE. on_call: a tool call is reported right away once it completes (when the next call
        starts), so the agent can run safe tools before the response ends. Text pieces go to on_delta; tool_call pieces
        are merged (as they are written, on_tool gets the tool name and the length of the arguments so far)."""
        try:
            return self._chat_stream(model, messages, on_delta, tools, temperature, max_tokens, response_format,
                                     extra, cancel, on_reasoning, on_first, on_tool, on_call)
        except ProviderError as e:
            raise self._scrub(e) from e.__cause__

    def _chat_stream(self, model, messages, on_delta, tools, temperature, max_tokens, response_format, extra,
                     cancel, on_reasoning, on_first, on_tool, on_call=None) -> ChatResult:
        body = self._body(model, messages, tools, temperature, max_tokens, response_format, extra)
        body["stream"] = True
        # Get full usage at the end of the stream (including cached tokens). Without it, some providers (cliproxy)
        # sent only the incomplete usage from the first chunk: output 0, no cache info, and ALICE's cache hit rate
        # always showed 0%.
        if self._stream_usage and "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}
        if "openrouter.ai" in self.cfg.base_url:  # usage and cost arrive in the last chunk (project cost tracking)
            body.setdefault("usage", {"include": True})
        t0 = time.monotonic()
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        announced: set[int] = set()  # calls (completed) already reported via on_call
        usage: dict[str, Any] = {}
        served_model = model
        finish = None
        think = ThinkFilter()
        last_data = time.monotonic()
        got_data = False
        chunks = 0  # number of decodable SSE chunks
        done = False
        retry = False
        try:
            with self._client.stream("POST", f"{self.cfg.base_url}/chat/completions", headers=self._headers(),
                                     json=body, timeout=httpx.Timeout(self.FIRST_DATA, connect=15.0)) as r:
                if r.status_code != 200:
                    r.read()
                    if r.status_code in (400, 422) and "stream_options" in r.text and \
                            body.get("stream_options") and self._stream_usage:
                        self._stream_usage = False
                        retry = True
                    else:
                        raise classify_error(r.status_code, r.text, dict(r.headers))
                for line in ([] if retry else r.iter_lines()):
                    if cancel and cancel():
                        raise StreamCancelled()
                    if not got_data and time.monotonic() - t0 > self.FIRST_DATA:
                        raise ProviderError("network", f"zaman aşımı: model {int(self.FIRST_DATA)} sn'dir ilk "
                                                       "cevabı vermedi")
                    if time.monotonic() - last_data > self.STREAM_IDLE:
                        raise ProviderError("network", f"akış {int(self.STREAM_IDLE)} sn'dir veri göndermiyor")
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):  # e.g. "data: null": don't crash the session with AttributeError
                        continue
                    chunks += 1
                    if chunk.get("error"):
                        raise _inline_error(chunk["error"])
                    if isinstance(chunk.get("model"), str) and chunk["model"]:
                        served_model = chunk["model"]
                    if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                        usage = merge_usage(usage, chunk["usage"])
                    choices = chunk.get("choices")
                    for ch in choices if isinstance(choices, list) else []:
                        if not isinstance(ch, dict):  # malformed chunk: don't crash the session with AttributeError
                            continue
                        delta = ch.get("delta")
                        if not isinstance(delta, dict):
                            delta = {}
                        if delta.get("content") or delta.get("tool_calls") or delta.get("reasoning_content") \
                                or delta.get("reasoning"):
                            last_data = time.monotonic()
                            got_data = True
                        if on_first is not None and (delta.get("content") or delta.get("tool_calls")
                                                     or delta.get("reasoning_content") or delta.get("reasoning")):
                            on_first()  # time to first token (for model ranking)
                            on_first = None
                        thought = delta.get("reasoning_content") or delta.get("reasoning")
                        if thought and on_reasoning and isinstance(thought, str):
                            on_reasoning(thought)
                        piece = delta.get("content")
                        if not isinstance(piece, str):  # some providers send it as a list
                            piece = _text_of(piece)
                        if piece:
                            piece, thought = think.feed(piece)
                            if thought and on_reasoning:
                                on_reasoning(thought)
                        if piece:
                            text_parts.append(piece)
                            on_delta(piece)
                        tcs = delta.get("tool_calls")
                        for tc in tcs if isinstance(tcs, list) else []:
                            if isinstance(tc, dict):
                                _merge_tool_delta(calls, tc)
                        if on_call is not None and len(calls) > 1:
                            _announce_done(calls, announced, on_call)
                        if calls and delta.get("tool_calls") and on_tool is not None:
                            cur = calls[max(calls)] if calls else {}
                            fn = cur.get("function") or {}
                            on_tool(fn.get("name") or "", len(fn.get("arguments") or ""))
                        finish = ch.get("finish_reason") or finish
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(e) from e
        if retry:
            return self._chat_stream(model, messages, on_delta, tools, temperature, max_tokens, response_format,
                                     extra, cancel, on_reasoning, on_first, on_tool, on_call)
        if not chunks and not done:
            # A 200 arrived but not a single SSE chunk (HTML interstitial page, empty body, or the stream closed
            # right after opening). Previously an empty reply was counted as success and nothing was shown.
            raise ProviderError("server", "akış veri göndermeden kapandı (geçerli SSE parçası yok)", 200)

        text = "".join(text_parts)
        msg: dict[str, Any] = {"content": text}
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        if not text and not calls and finish == "length":
            raise ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")
        return ChatResult(normalize_message(msg), self.name, served_model, usage, time.monotonic() - t0,
                          finish=str(finish or ""))

    def list_models(self) -> list[dict[str, Any]]:
        try:
            try:
                r = self._client.get(f"{self.cfg.base_url}/models", headers=self._headers())
            except _TRANSPORT_ERRORS as e:
                raise _transport_error(e) from e
            if r.status_code != 200:
                raise classify_error(r.status_code, r.text, dict(r.headers))
            try:
                data = r.json()
            except ValueError as e:
                raise ProviderError("server", f"geçersiz JSON: {summarize_body(r.text, 200)}", 200) from e
        except ProviderError as e:
            raise self._scrub(e) from e.__cause__
        # Some providers return the list directly: previously list.get raised AttributeError.
        items = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
        return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def merge_usage(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge usage split across several chunks of a stream: filled fields of a later chunk overwrite earlier ones,
    while empty/zero fields never erase earlier values (the first chunk may carry input tokens; the last, output
    and cache)."""
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_usage(out[k], v)
        elif v or k not in out:
            out[k] = v
    return out


def _text_of(content: Any) -> str:
    """Extract text from content parts that arrive as a list in a stream ([{"type": "text", "text": "..."}])."""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _announce_done(calls: dict[int, dict[str, Any]], announced: set[int],
                   on_call: Callable[[dict[str, Any]], None]) -> None:
    """Calls before the last one are complete (the model has moved on): report once each for those that have an id
    and valid JSON arguments. A notification error does not break the stream."""
    last = max(calls)
    for i in sorted(calls):
        if i >= last or i in announced:
            continue
        announced.add(i)
        c = calls[i]
        fn = c.get("function") or {}
        if not c.get("id") or not fn.get("name"):
            continue
        try:
            json.loads(fn.get("arguments") or "{}")
        except ValueError:
            continue
        try:
            on_call({"id": c["id"], "type": "function",
                     "function": {"name": fn["name"], "arguments": fn.get("arguments") or "{}"}})
        except Exception:  # noqa: BLE001
            pass


def _merge_tool_delta(calls: dict[int, dict[str, Any]], tc: dict[str, Any]) -> None:
    """Merge tool_call pieces arriving piece by piece in a stream. Gemini may not send an index."""
    idx = tc.get("index")
    if idx is None:
        tid = tc.get("id")
        existing = [i for i, c in calls.items() if tid and c.get("id") == tid]
        idx = existing[0] if existing else (len(calls) if tid or not calls else max(calls))
    cur = calls.setdefault(idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
    if tc.get("id"):
        cur["id"] = tc["id"]
    if tc.get("extra_content"):  # Gemini: if thought_signature is not sent back, the next request gets a 400
        cur["extra_content"] = tc["extra_content"]
    fn = tc.get("function")
    if not isinstance(fn, dict):
        fn = {}
    if isinstance(fn.get("name"), str) and fn["name"] and not cur["function"]["name"]:
        cur["function"]["name"] = fn["name"]
    args = fn.get("arguments")
    if isinstance(args, dict):
        cur["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
    elif isinstance(args, str) and args:
        cur["function"]["arguments"] += args


class ThinkFilter:
    """Some models (Qwen, GLM, Nemotron, DeepSeek...) send their thinking inside the content as <think>...</think>.
    This text should not be printed on screen or read by the voice assistant: it is extracted from the stream
    and returned as thought. The tag can be split across chunks; the suspicious tail is held until sure."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False
        self.started = False  # whether visible text has started (the tag is only expected at the start)

    def feed(self, piece: str) -> tuple[str, str]:
        self.buf += piece
        shown, thought = [], []
        while self.buf:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    keep = _partial_suffix(self.buf, self.CLOSE)
                    thought.append(self.buf[: len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                thought.append(self.buf[:i])
                self.buf = self.buf[i + len(self.CLOSE):].lstrip()
                self.inside = False
                continue
            if self.started:
                shown.append(self.buf)
                self.buf = ""
                break
            stripped = self.buf.lstrip()
            if stripped.startswith(self.OPEN):
                self.buf = stripped[len(self.OPEN):]
                self.inside = True
                continue
            if self.OPEN.startswith(stripped):  # like "<thi": wait for more to come
                break
            self.started = True
        return "".join(shown), "".join(thought)


def _partial_suffix(text: str, tag: str) -> int:
    for n in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:n]):
            return n
    return 0


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S)
    if "</think>" in text:  # thought coming without an opening tag
        text = text.split("</think>", 1)[1]
    return text.strip()


def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Drop provider-specific fields and produce an assistant message that can be safely kept in history."""
    content = msg.get("content") or ""
    if isinstance(content, str) and "think>" in content:
        content = strip_think(content)
    out: dict[str, Any] = {"role": "assistant", "content": content}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        call = {
            "id": tc.get("id") or f"call_{int(time.time() * 1000)}_{i}",
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args or "{}"},
        }
        if tc.get("extra_content"):
            call["_extra_content"] = tc["extra_content"]  # only sent back to Gemini
        calls.append(call)
    if calls:
        out["tool_calls"] = calls
    return out
