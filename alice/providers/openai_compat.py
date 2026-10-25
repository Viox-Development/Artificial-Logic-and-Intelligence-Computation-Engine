"""OpenAI uyumlu /chat/completions istemcisi.

Gemini, OpenRouter, Groq, NVIDIA NIM, Cerebras ve Ollama hepsi bu formatı konuşuyor,
o yüzden tek bir istemci yeterli. Asıl iş hataları doğru sınıflandırmak: router
hangi hatada bekleyeceğine, hangisinde modeli/sağlayıcıyı tamamen atlayacağına
buna göre karar veriyor.
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


# Hata metinlerinde (sağlayıcının yankıladığı gövde, httpx istisnası) görünebilecek anahtar kalıpları. Bunlar
# kullanıcıya gösterilen hataya ve ~/.alice/logs/providers.log'a gitmeden önce maskelenir.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)([?&](?:key|api_key|apikey|access_token|token)=)[^&\s\"']+"),
    re.compile(r"\b(?:sk-(?:or-v1-|proj-)?|gsk_|nvapi-|csk-|xai-|ghp_|github_pat_|hf_)[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
]


def redact(text: str, *secrets: str | None) -> str:
    """Metindeki API anahtarlarını maskele: verilen gerçek anahtarlar ve bilinen anahtar biçimleri."""
    if not text:
        return text
    for sec in secrets:
        if sec and len(sec) >= 6:
            text = text.replace(sec, "***")
    for rx in _SECRET_PATTERNS:
        text = rx.sub(lambda m: (m.group(1) if m.re.groups else "") + "***", text)
    return text


class ProviderError(Exception):
    """kind: rate_limit | quota | auth | not_found | too_large | bad_request | server | network | unreachable"""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None,
                 daily: bool = False):
        super().__init__(f"[{kind}] {redact(message)}")
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
# 401/403 ama sorun anahtarda değil, o modelde: "Model ... is not available on the Workers Free plan" (Cloudflare),
# "you do not have access to model ..." gibi. "auth" sayılınca sağlayıcının BÜTÜN modelleri kapanıyordu.
_MODEL_DENIED_HINTS = ("not available on the workers", "requires a paid plan", "not available on your plan",
                       "not available on the free", "do not have access to model", "does not have access to model",
                       "not have access to the model", "model is not available")
_NOT_FOUND_HINTS = ("model not found", "does not exist", "no endpoints found", "not a valid model",
                    "model_not_found", "unknown model", "is not supported", "decommissioned", "deprecated")


def _quota_window(low: str) -> tuple[str, float | None]:
    """Gemini 429 gövdesindeki kota kimliğinden pencereyi ("day", "minute" ya da "") ve bekleme süresini çıkar.
    Gemini dakikalık sınırda da "exceeded your current quota" yazıyor: kimliğe bakmadan hepsi günlük sanılıyordu
    ve 40 sn sonra açılacak model bir saat kapanıyordu."""
    window = "day" if "perday" in low else "minute" if "perminute" in low else ""
    m = re.search(r'"retrydelay":\s*"([\d.]+)s"', low) or re.search(r"retry in ([\d.]+)\s*s", low)
    return window, (float(m.group(1)) if m else None)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After başlığı: saniye ("7") ya da HTTP tarihi ("Wed, 21 Oct 2026 07:28:00 GMT"). Tarih biçimi
    önceden sessizce yok sayılıyor, sunucunun istediği bekleme yerine varsayılan süre kullanılıyordu."""
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
    """Hata gövdesini kullanıcıya gösterilebilir kısa metne çevir. Cloudflare/nginx gibi ara katmanların HTML
    hata sayfaları önceden olduğu gibi (etiketler, stil, betik) hata mesajına dökülüyordu."""
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
        # OpenRouter bazı moderasyon/quota hatalarında da 403 döner.
        if any(h in low for h in _MODEL_DENIED_HINTS):
            return ProviderError("not_found", short, status)  # sadece bu model kapanır
        if any(h in low for h in _QUOTA_HINTS):
            return ProviderError("quota", short, status, retry_after)
        return ProviderError("auth", short, status)
    if status in (404, 410):  # 410: model kullanımdan kaldırıldı
        return ProviderError("not_found", short, status)
    if status == 402:  # OpenRouter: kredi yetersiz. Önceden bad_request sayılıp 60 sn sonra yine deneniyordu.
        return ProviderError("quota", short, status, retry_after)
    if status == 408:  # sunucu tarafı istek zaman aşımı: geçici
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
        if any(h in low for h in _NOT_FOUND_HINTS):
            return ProviderError("not_found", short, status)
        if any(h in low for h in _TOO_LARGE_HINTS):
            return ProviderError("too_large", short, status)
        return ProviderError("bad_request", short, status)
    if status >= 500:
        return ProviderError("server", short, status, retry_after)
    return ProviderError("bad_request", short, status)


def _inline_error(err: Any) -> ProviderError:
    """200 cevabın (ya da SSE parçasının) içindeki {"error": ...} nesnesini sınıflandır. code bazen "429" gibi
    metin geliyor; önceden bu durumda hata 500 (server) sayılıp günlük kota 30 sn sonra yeniden deneniyordu."""
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
    """httpx istisnasını sınıflandır. ReadTimeout gibi istisnaların metni çoğu zaman boş: önceden kullanıcı
    sadece "timeout: " görüyordu."""
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


# httpx.InvalidURL ve StreamError HTTPError'dan türemiyor: önceden router'ı ProviderError dışı istisnayla düşürüyordu.
_TRANSPORT_ERRORS = (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError)


class OpenAICompatProvider:
    # Akış bu kadar saniye gerçek veri (metin, düşünme, araç parçası) göndermezse kesilir. Bazı sağlayıcılar
    # beklerken ": PROCESSING" gibi canlı tutma satırları yollar; bunlar okuma zaman aşımını sıfırladığı için
    # yoksa çağrı saatlerce asılı kalabiliyor.
    STREAM_IDLE = 300.0

    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: httpx.Client | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0))

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - kapanışta hata önemsiz
            pass

    def _scrub(self, e: ProviderError) -> ProviderError:
        """Hata metninde bu sağlayıcının gerçek anahtarı geçiyorsa (sağlayıcı yankılamış) maskele."""
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
            # ConnectError: sunucu hiç yok (ör. Ollama kapalı), uzun süre tekrar deneme.
            raise _transport_error(e) from e
        latency = time.monotonic() - t0

        if r.status_code != 200:
            raise classify_error(r.status_code, r.text, dict(r.headers))
        try:
            data = r.json()
        except ValueError as e:  # JSONDecodeError ya da bozuk kodlama
            raise ProviderError("server", f"geçersiz JSON: {summarize_body(r.text, 200)}", 200) from e

        # Bazı sağlayıcılar (OpenRouter) hatayı 200 içinde döndürür.
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
        try:
            return self._chat_stream(model, messages, on_delta, tools, temperature, max_tokens, response_format,
                                     extra, cancel, on_reasoning, on_first, on_tool)
        except ProviderError as e:
            raise self._scrub(e) from e.__cause__

    def _chat_stream(self, model, messages, on_delta, tools, temperature, max_tokens, response_format, extra,
                     cancel, on_reasoning, on_first, on_tool) -> ChatResult:
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
        chunks = 0  # çözülebilen SSE parçası sayısı
        done = False
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
                        done = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):  # "data: null" gibi: oturumu AttributeError ile düşürmesin
                        continue
                    chunks += 1
                    if chunk.get("error"):
                        raise _inline_error(chunk["error"])
                    if isinstance(chunk.get("model"), str) and chunk["model"]:
                        served_model = chunk["model"]
                    if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                        usage = chunk["usage"]
                    choices = chunk.get("choices")
                    for ch in choices if isinstance(choices, list) else []:
                        if not isinstance(ch, dict):  # bozuk parça: AttributeError ile oturumu düşürmesin
                            continue
                        delta = ch.get("delta")
                        if not isinstance(delta, dict):
                            delta = {}
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
                        if not isinstance(piece, str):  # bazı sağlayıcılar liste biçiminde yolluyor
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
                        if calls and delta.get("tool_calls") and on_tool is not None:
                            cur = calls[max(calls)] if calls else {}
                            fn = cur.get("function") or {}
                            on_tool(fn.get("name") or "", len(fn.get("arguments") or ""))
                        finish = ch.get("finish_reason") or finish
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(e) from e
        if not chunks and not done:
            # 200 geldi ama tek bir SSE parçası yok (HTML ara sayfa, boş gövde, akış açılır açılmaz kapandı):
            # önceden boş cevap başarılı sayılıp kullanıcıya hiçbir şey gösterilmiyordu.
            raise ProviderError("server", "akış veri göndermeden kapandı (geçerli SSE parçası yok)", 200)

        text = "".join(text_parts)
        msg: dict[str, Any] = {"content": text}
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        if not text and not calls and finish == "length":
            raise ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")
        return ChatResult(normalize_message(msg), self.name, served_model, usage, time.monotonic() - t0)

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
        # Bazı sağlayıcılar listeyi doğrudan döndürüyor: önceden list.get AttributeError ile düşüyordu.
        items = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
        return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _text_of(content: Any) -> str:
    """Akışta liste biçiminde gelen içerik parçalarından metni çıkar ([{"type": "text", "text": "..."}])."""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


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
