"""OpenAI uyumlu /chat/completions istemcisi.

Gemini, OpenRouter, Groq, NVIDIA NIM, Cerebras ve Ollama hepsi bu formatı konuşuyor,
o yüzden tek bir istemci yeterli. Asıl iş hataları doğru sınıflandırmak: router
hangi hatada bekleyeceğine, hangisinde modeli/sağlayıcıyı tamamen atlayacağına
buna göre karar veriyor.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from alice.config import ProviderConfig


class ProviderError(Exception):
    """kind: rate_limit | quota | auth | not_found | too_large | bad_request | server | network | unreachable"""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None,
                 daily: bool = False):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.daily = daily  # günlük kota: ertesi gün sıfırlanana kadar bu modele istek gitmesin


class StreamCancelled(Exception):
    """Kullanıcı akışı durdurdu."""


@dataclass
class ChatResult:
    message: dict[str, Any]
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0

    @property
    def text(self) -> str:
        c = self.message.get("content")
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return c or ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []


# "set up billing": Gemini ücretsiz katmanı dolunca bu metinle 429 döner; 20 sn sonra tekrar denemek boşuna.
_QUOTA_HINTS = ("per day", "daily", "quota", "rpd", "tokens per day", "free-models-per-day", "exhausted",
                "billing", "insufficient credits", "insufficient_quota", "credit balance")
_DAILY_HINTS = ("per day", "daily", "rpd", "tokens per day", "free-models-per-day")
_TOO_LARGE_HINTS = ("context length", "context_length", "maximum context", "too long", "too large",
                    "reduce the length", "request_too_large", "tokens per minute", "max_tokens")
_NOT_FOUND_HINTS = ("model not found", "does not exist", "no endpoints found", "not a valid model",
                    "model_not_found", "unknown model", "is not supported", "decommissioned", "deprecated")


def _quota_window(low: str) -> tuple[str, float | None]:
    """Gemini 429 gövdesindeki kota kimliğinden pencereyi ("day", "minute" ya da "") ve bekleme süresini çıkar.
    Gemini dakikalık sınırda da "exceeded your current quota" yazıyor: kimliğe bakmadan hepsi günlük sanılıyordu
    ve 40 sn sonra açılacak model bir saat kapanıyordu."""
    window = "day" if "perday" in low else "minute" if "perminute" in low else ""
    m = re.search(r'"retrydelay":\s*"([\d.]+)s"', low) or re.search(r"retry in ([\d.]+)\s*s", low)
    return window, (float(m.group(1)) if m else None)


def classify_error(status: int, body: str, headers: dict[str, str] | None = None) -> ProviderError:
    low = body.lower()
    retry_after = None
    if headers:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                retry_after = None
    short = body[:400]
    if status in (401, 403):
        # OpenRouter bazı moderasyon/quota hatalarında da 403 döner.
        if any(h in low for h in _QUOTA_HINTS):
            return ProviderError("quota", short, status, retry_after)
        return ProviderError("auth", short, status)
    if status in (404, 410):  # 410: model kullanımdan kaldırıldı
        return ProviderError("not_found", short, status)
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
        if any(h in low for h in _NOT_FOUND_HINTS):
            return ProviderError("not_found", short, status)
        if any(h in low for h in _TOO_LARGE_HINTS):
            return ProviderError("too_large", short, status)
        return ProviderError("bad_request", short, status)
    if status >= 500:
        return ProviderError("server", short, status, retry_after)
    return ProviderError("bad_request", short, status)


class OpenAICompatProvider:
    # Akış bu kadar saniye gerçek veri (metin, düşünme, araç parçası) göndermezse kesilir. Bazı sağlayıcılar
    # beklerken ": PROCESSING" gibi canlı tutma satırları yollar; bunlar okuma zaman aşımını sıfırladığı için
    # yoksa çağrı saatlerce asılı kalabiliyor.
    STREAM_IDLE = 300.0

    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: httpx.Client | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0))

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
        body = self._body(model, messages, tools, temperature, max_tokens, response_format, extra)
        if "openrouter.ai" in self.cfg.base_url:
            body.setdefault("usage", {"include": True})
        t0 = time.monotonic()
        try:
            r = self._client.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
        except httpx.TimeoutException as e:
            raise ProviderError("network", f"timeout: {e}") from e
        except httpx.ConnectError as e:
            # Sunucu hiç yok (ör. Ollama kapalı): uzun süre tekrar deneme.
            raise ProviderError("unreachable", str(e)) from e
        except httpx.HTTPError as e:
            raise ProviderError("network", str(e)) from e
        latency = time.monotonic() - t0

        if r.status_code != 200:
            raise classify_error(r.status_code, r.text, dict(r.headers))
        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise ProviderError("server", f"geçersiz JSON: {r.text[:200]}") from e

        # Bazı sağlayıcılar (OpenRouter) hatayı 200 içinde döndürür.
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = json.dumps(err)[:400]
            status = code if isinstance(code, int) else 500
            raise classify_error(status, msg)

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("server", f"boş cevap: {str(data)[:200]}")
        msg = choices[0].get("message") or {}
        if not msg.get("content") and not msg.get("tool_calls"):
            if choices[0].get("finish_reason") == "length":
                raise ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")
        return ChatResult(
            message=normalize_message(msg),
            provider=self.name,
            model=data.get("model") or model,
            usage=data.get("usage") or {},
            latency=latency,
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
    ) -> ChatResult:
        """SSE ile akışlı cevap. Metin parçaları on_delta'ya gider, tool_call parçaları birleştirilir (yazıldıkça
        on_tool'a araç adı ve argümanların o ana kadarki uzunluğu bildirilir)."""
        body = self._body(model, messages, tools, temperature, max_tokens, response_format, extra)
        body["stream"] = True
        if "openrouter.ai" in self.cfg.base_url:  # kullanım ve ücret son parçada gelsin (proje maliyet takibi)
            body.setdefault("usage", {"include": True})
        t0 = time.monotonic()
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        served_model = model
        finish = None
        think = ThinkFilter()
        last_data = time.monotonic()
        try:
            with self._client.stream("POST", f"{self.cfg.base_url}/chat/completions",
                                     headers=self._headers(), json=body) as r:
                if r.status_code != 200:
                    r.read()
                    raise classify_error(r.status_code, r.text, dict(r.headers))
                for line in r.iter_lines():
                    if cancel and cancel():
                        raise StreamCancelled()
                    if time.monotonic() - last_data > self.STREAM_IDLE:
                        raise ProviderError("network", f"akış {int(self.STREAM_IDLE)} sn'dir veri göndermiyor")
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):  # "data: null" gibi: oturumu AttributeError ile düşürmesin
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        code = err.get("code") if isinstance(err, dict) else None
                        raise classify_error(code if isinstance(code, int) else 500, json.dumps(err)[:400])
                    served_model = chunk.get("model") or served_model
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content") or delta.get("tool_calls") or delta.get("reasoning_content") \
                                or delta.get("reasoning"):
                            last_data = time.monotonic()
                        if on_first is not None and (delta.get("content") or delta.get("tool_calls")
                                                     or delta.get("reasoning_content") or delta.get("reasoning")):
                            on_first()  # ilk token süresi (model sıralaması için)
                            on_first = None
                        thought = delta.get("reasoning_content") or delta.get("reasoning")
                        if thought and on_reasoning and isinstance(thought, str):
                            on_reasoning(thought)
                        piece = delta.get("content")
                        if piece:
                            piece, thought = think.feed(piece)
                            if thought and on_reasoning:
                                on_reasoning(thought)
                        if piece:
                            text_parts.append(piece)
                            on_delta(piece)
                        for tc in delta.get("tool_calls") or []:
                            _merge_tool_delta(calls, tc)
                        if delta.get("tool_calls") and on_tool is not None:
                            cur = calls[max(calls)] if calls else {}
                            fn = cur.get("function") or {}
                            on_tool(fn.get("name") or "", len(fn.get("arguments") or ""))
                        finish = ch.get("finish_reason") or finish
        except httpx.TimeoutException as e:
            raise ProviderError("network", f"timeout: {e}") from e
        except httpx.ConnectError as e:
            raise ProviderError("unreachable", str(e)) from e
        except httpx.HTTPError as e:
            raise ProviderError("network", str(e)) from e

        text = "".join(text_parts)
        msg: dict[str, Any] = {"content": text}
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        if not text and not calls and finish == "length":
            raise ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")
        return ChatResult(normalize_message(msg), self.name, served_model, usage, time.monotonic() - t0)

    def list_models(self) -> list[dict[str, Any]]:
        r = self._client.get(f"{self.cfg.base_url}/models", headers=self._headers())
        if r.status_code != 200:
            raise classify_error(r.status_code, r.text, dict(r.headers))
        data = r.json()
        return data.get("data", data if isinstance(data, list) else [])


def _merge_tool_delta(calls: dict[int, dict[str, Any]], tc: dict[str, Any]) -> None:
    """Akışta parça parça gelen tool_call'ları birleştir. Gemini index göndermeyebiliyor."""
    idx = tc.get("index")
    if idx is None:
        tid = tc.get("id")
        existing = [i for i, c in calls.items() if tid and c.get("id") == tid]
        idx = existing[0] if existing else (len(calls) if tid or not calls else max(calls))
    cur = calls.setdefault(idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
    if tc.get("id"):
        cur["id"] = tc["id"]
    if tc.get("extra_content"):  # Gemini: thought_signature geri gönderilmezse sonraki istek 400 alıyor
        cur["extra_content"] = tc["extra_content"]
    fn = tc.get("function") or {}
    if fn.get("name") and not cur["function"]["name"]:
        cur["function"]["name"] = fn["name"]
    args = fn.get("arguments")
    if isinstance(args, dict):
        cur["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
    elif args:
        cur["function"]["arguments"] += args


class ThinkFilter:
    """Bazı modeller (Qwen, GLM, Nemotron, DeepSeek...) düşünmesini içeriğin içinde <think>...</think> olarak
    gönderir. Bu metin ekrana yazılmasın, sesli asistan da okumasın: akıştan ayıklanıp düşünce olarak döner.
    Etiket parçalar arasında bölünebilir; emin olunana kadar şüpheli kuyruk bekletilir."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False
        self.started = False  # görünür metin başladı mı (etiket sadece başta beklenir)

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
            if self.OPEN.startswith(stripped):  # "<thi" gibi: devamı gelsin
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
    if "</think>" in text:  # açılış etiketi olmadan gelen düşünce
        text = text.split("</think>", 1)[1]
    return text.strip()


def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Sağlayıcıya özel alanları atıp geçmişte güvenle tutulabilecek bir assistant mesajı üret."""
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
            call["_extra_content"] = tc["extra_content"]  # sadece Gemini'ye geri gönderilir
        calls.append(call)
    if calls:
        out["tool_calls"] = calls
    return out
