"""API anahtarı gerektirmeyen web arama ve sayfa okuma."""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

from typing import Any

import httpx

from alice.tools.base import NETWORK, Tool, ToolContext, ToolError, ToolResult, obj, truncate

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

# Aynı sayfayı/aramayı bir oturumda defalarca çekmek yaygın (ajan adım adım ilerlerken aynı dokümana dönüyor).
# Kısa ömürlü bellek içi önbellek bunu saniyelerden milisaniyelere indirir; TTL kısa olduğu için içerik bayatlamaz.
CACHE_TTL = 180.0
CACHE_MAX = 48
_cache: "OrderedDict[tuple, tuple[float, Any]]" = OrderedDict()
_cache_lock = threading.Lock()


def cache_get(key: tuple) -> Any:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        ts, value = hit
        if time.time() - ts > CACHE_TTL:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return value


def cache_put(key: tuple, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre", "table"}
    MAIN = {"main", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_parts: list[str] = []  # <main>/<article> içi: menü ve kenar çubuğu olmadan asıl içerik
        self._skip = 0
        self._main = 0
        self._href: str | None = None

    def _add(self, s: str) -> None:
        self.parts.append(s)
        if self._main:
            self.main_parts.append(s)

    def handle_starttag(self, tag, attrs):
        if tag in self.MAIN:
            self._main += 1
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self._add("\n")
        if tag in ("h1", "h2", "h3"):
            self._add("#" * int(tag[1]) + " ")
        if tag == "li":
            self._add("* ")
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self._add("\n")
        if tag == "a" and self._href and self._href.startswith("http"):
            self._add(f" ({self._href})")
            self._href = None
        if tag in self.MAIN and self._main:
            self._main -= 1

    def handle_data(self, data):
        if not self._skip:
            self._add(data)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def html_to_text(raw: str, main_only: bool = False) -> str:
    """HTML'i okunur metne çevir. main_only: sayfada yeterince uzun bir <main>/<article> varsa sadece onu döndür."""
    p = _TextExtractor()
    p.feed(raw)
    if main_only:
        main = _clean("".join(p.main_parts))
        if len(main) >= 200:
            return main
    return _clean("".join(p.parts))


def _paginate(body: str, start: int, limit: int) -> str:
    start = max(0, int(start or 0))
    if start >= len(body) > 0:
        return f"[start={start} metnin sonunu aşıyor; metin {len(body)} karakter]"
    chunk = body[start:start + limit]
    rest = len(body) - start - len(chunk)
    head = f"[karakter {start}-{start + len(chunk)} / {len(body)}]\n" if start else ""
    tail = f"\n\n... [{rest} karakter kesildi; devamı için start={start + len(chunk)}]" if rest > 0 else ""
    return head + chunk + tail


def _extract(ctx: ToolContext, url: str, text: str, prompt: str) -> str | None:
    """Claude Code'un WebFetch'i gibi: sayfayı küçük bir modele verip sadece sorulan bilgiyi çıkar. Model yoksa None."""
    if ctx.router is None:
        return None
    msgs = [{"role": "system", "content": "You extract information from a web page. Answer only from the page content "
             "below, concisely, in the language of the request. Quote exact numbers, names and code. If the page does "
             "not contain the answer, say so."},
            {"role": "user", "content": f"Request: {prompt}\n\nURL: {url}\n\nPage content:\n{text[:60000]}"}]
    try:
        return ctx.router.chat("fast", msgs, temperature=0, max_tokens=1500).text.strip() or None
    except Exception:  # noqa: BLE001 - model yoksa/çökerse ham metne düş
        return None


class WebFetch(Tool):
    name = "web_fetch"
    parallel_safe = True
    description = ("Bir URL'yi arka planda indirir ve okunabilir metin döndürür: HTML asıl içeriğe çevrilir, JSON "
                   "düzenlenir, PDF metne çevrilir (ekranda bir şey açmaz).\n"
                   "- prompt verirsen sayfayı küçük bir model okur ve sadece istediğin bilgiyi döndürür; uzun sayfalarda "
                   "çok daha az token.\n"
                   "- Uzun sayfa kesilirse start ile devamını oku.\n"
                   "- Kullanıcıya siteyi açmak için windows action=navigate; giriş gerektiren ya da JavaScript ile "
                   "yüklenen sayfalar (Gmail, YouTube) için browser. API çağrısı (POST, başlık) için http_request.")
    parameters = obj({
        "url": {"type": "string", "description": "Tam adres, ör. 'https://tr.wikipedia.org/wiki/Ankara'."},
        "prompt": {"type": "string", "description": "Sayfadan ne öğrenmek istediğin, ör. 'kurulum adımları' ya da "
                   "'fiyat ve stok durumu'. Verilmezse sayfanın metni döner."},
        "start": {"type": "integer", "description": "Metnin bu karakterinden itibaren göster (kesilen sayfanın "
                  "devamı için). Varsayılan 0."},
        "raw": {"type": "boolean", "description": "true: HTML'i metne çevirmeden döndür. Varsayılan false."},
    }, ["url"])
    risk = NETWORK

    def summary(self, args: dict[str, Any]) -> str:
        q = f" ({args['prompt'][:60]})" if args.get("prompt") else ""
        return f"{args.get('url', '')}{q}"

    def run(self, ctx: ToolContext, url: str, raw: bool = False, prompt: str | None = None,
            start: int = 0) -> ToolResult:
        if "://" not in url:
            url = "https://" + url
        key = ("fetch", url, raw)
        hit = cache_get(key)
        if hit is None:
            try:
                r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
            except httpx.HTTPError as e:
                raise ToolError(f"indirilemedi: {e}") from e
            ctype = r.headers.get("content-type", "").lower()
            if "pdf" in ctype or (not ctype and str(r.url).lower().endswith(".pdf")):
                body = _pdf_text(r.content)
            else:
                body = r.text
                if not raw and "html" in ctype:
                    body = html_to_text(body, main_only=True)
                elif not raw and "json" in ctype:
                    try:
                        body = json.dumps(json.loads(body), ensure_ascii=False, indent=1)
                    except ValueError:
                        pass
            hit = (r.status_code, str(r.url), body)
            if r.status_code < 400:
                cache_put(key, hit)  # sadece başarılı indirmeler; hatalı sayfa tekrar denensin
        status, final_url, body = hit
        head = f"HTTP {status} {final_url}\n\n"
        if prompt and status < 400:
            answer = _extract(ctx, final_url, body, prompt)
            if answer:
                return ToolResult(head + answer)
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        return ToolResult(head + _paginate(body, start, limit), is_error=status >= 400)


def _pdf_text(data: bytes) -> str:
    import tempfile
    from pathlib import Path

    from alice.tools.files import read_pdf

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sayfa.pdf"
        p.write_bytes(data)
        return read_pdf(p, None, 400_000)


def ddg_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": UA},
                   timeout=20, follow_redirects=True)
    r.raise_for_status()
    results = []
    blocks = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                        r.text, re.S)
    for href, title, rest in blocks:
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:
            continue  # reklam
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        results.append({"title": _strip_tags(title), "url": href, "snippet": _strip_tags(snip.group(1)) if snip else ""})
        if len(results) >= max_results:
            break
    return results


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def bing_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """DuckDuckGo bot sayfası gösterir ya da düşerse yedek: Bing'in HTML sonuç sayfası."""
    r = httpx.get("https://www.bing.com/search", params={"q": query, "setlang": "tr"}, headers={"User-Agent": UA},
                  timeout=20, follow_redirects=True)
    r.raise_for_status()
    results = []
    for block in re.findall(r'<li class="b_algo"(.*?)</li>', r.text, re.S):
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        results.append({"title": _strip_tags(a.group(2)), "url": html.unescape(a.group(1)),
                        "snippet": _strip_tags(snip.group(1)) if snip else ""})
        if len(results) >= max_results:
            break
    return results


def brave_search(query: str, key: str, max_results: int = 8) -> list[dict[str, str]]:
    r = httpx.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": max_results},
                  headers={"Accept": "application/json", "X-Subscription-Token": key}, timeout=20)
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": _strip_tags(x.get("description", ""))}
            for x in (r.json().get("web") or {}).get("results", [])[:max_results]]


def _domain_ok(url: str, allowed: list[str], blocked: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    match = lambda d: host == d.lower().lstrip(".") or host.endswith("." + d.lower().lstrip("."))  # noqa: E731
    if allowed and not any(match(d) for d in allowed):
        return False
    return not any(match(d) for d in blocked)


class WebSearch(Tool):
    name = "web_search"
    parallel_safe = True
    description = ("İnternette arar ve sonuçların başlık, URL ve özetini döndürür; ekranda bir şey açmaz. "
                   "Güncel bilgi, haber, fiyat ya da bilmediğin bir şey sorulunca kullan; ayrıntı için sonra web_fetch "
                   "(prompt ile). Bir siteyle sınırlamak için allowed_domains. Kullanıcı aramayı tarayıcıda görmek "
                   "isterse windows action=navigate kullan.")
    parameters = obj({
        "query": {"type": "string", "description": "Arama ifadesi, ör. 'Ankara hava durumu yarın'."},
        "max_results": {"type": "integer", "description": "En fazla sonuç. Varsayılan 8."},
        "allowed_domains": {"type": "array", "items": {"type": "string", "description": "Alan adı."},
                            "description": "Sadece bu sitelerden sonuç, ör. [\"docs.python.org\"]."},
        "blocked_domains": {"type": "array", "items": {"type": "string", "description": "Alan adı."},
                            "description": "Bu sitelerden gelen sonuçları çıkar."},
    }, ["query"])

    risk = NETWORK

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("query", ""))

    def run(self, ctx: ToolContext, query: str, max_results: int = 8, allowed_domains: list[str] | None = None,
            blocked_domains: list[str] | None = None) -> ToolResult:
        allowed = [d for d in (allowed_domains or []) if d]
        blocked = [d for d in (blocked_domains or []) if d]
        gkey = os.environ.get("GEMINI_API_KEY", "").strip()
        if gkey and not allowed and not blocked and ctx.config.agent.get("search_grounding", True):
            ckey = ("search", "gemini", query)
            try:
                res_g = cache_get(ckey)
                if res_g is None:
                    res_g = gemini_search(query, gkey, grounding_model(ctx))
                    cache_put(ckey, res_g)
                return ToolResult(format_grounded(res_g))
            except ValueError:
                pass  # kota/hata: klasik motorlar
        q = query + (f" site:{allowed[0]}" if len(allowed) == 1 and "site:" not in query else "")
        want = max_results * 3 if allowed or blocked else max_results
        key = os.environ.get("BRAVE_SEARCH_API_KEY") or ctx.config.agent.get("brave_api_key")
        engines = ([("brave", lambda: brave_search(q, key, want))] if key else []) + [
            ("duckduckgo", lambda: ddg_search(q, want)), ("bing", lambda: bing_search(q, want))]
        res: list[dict[str, str]] = []
        errors = []
        for name, fn in engines:
            ckey = ("search", name, q, want)  # dikkat: `key` yukarıda Brave anahtarı
            try:
                raw_res = cache_get(ckey)
                if raw_res is None:
                    raw_res = fn()
                    cache_put(ckey, raw_res)
                res = [x for x in raw_res if _domain_ok(x["url"], allowed, blocked)][:max_results]
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{name}: {e}")
                continue
            if res:
                break
        if not res:
            if len(errors) == len(engines):
                raise ToolError("arama başarısız: " + "; ".join(errors))
            return ToolResult("sonuç yok")
        return ToolResult("\n\n".join(f"{i}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(res, 1)))


# Google araması (Gemini grounding): model aramayı Google'da yapar, sonuçları okuyup kaynaklı özet yazar.
# DuckDuckGo/Bing sayfası kazımaktan çok daha güncel ve dolu; kota dolarsa ya da hata verirse bir süre denenmez,
# klasik motorlara düşülür.
GROUNDING_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROUNDING_PROMPT = ("Bu soruyu Google'da ara ve bulduklarını kısa, olgusal bir özet olarak yaz: Türkçe, en fazla 8 madde, "
                    "tarihleri, sayıları, sürümleri ve adları aynen koru; emin olmadığını belirt; yorum ekleme.\n"
                    "Arama: {query}")
_grounding = {"down_until": 0.0}


def grounding_model(ctx: ToolContext) -> str:
    return str(ctx.config.agent.get("grounding_model", "gemini-flash-latest"))


def gemini_search(query: str, key: str, model: str, timeout: float = 25.0) -> dict[str, Any]:
    """{'text': özet, 'sources': [{'title', 'url'}], 'queries': [...]}; kota/hata ValueError."""
    if time.monotonic() < _grounding["down_until"]:
        raise ValueError("Google araması geçici olarak kapalı (kota/hata)")
    try:
        r = httpx.post(GROUNDING_URL.format(model=model), headers={"x-goog-api-key": key},
                       json={"contents": [{"role": "user", "parts": [{"text": GROUNDING_PROMPT.format(query=query)}]}],
                             "tools": [{"google_search": {}}]}, timeout=timeout)
    except httpx.HTTPError as e:
        _grounding["down_until"] = time.monotonic() + 300
        raise ValueError(f"bağlantı: {e}") from e
    if r.status_code >= 400:
        _grounding["down_until"] = time.monotonic() + (3600 if r.status_code == 429 else 900)
        raise ValueError(f"HTTP {r.status_code}: {r.text[:160]}")
    try:
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
        meta = cand.get("groundingMetadata") or {}
    except (ValueError, AttributeError) as e:
        raise ValueError(f"bozuk cevap: {e}") from e
    sources = [{"title": (c.get("web") or {}).get("title", ""), "url": (c.get("web") or {}).get("uri", "")}
               for c in meta.get("groundingChunks") or [] if c.get("web")]
    if not text or not sources:
        raise ValueError("Google araması sonuç döndürmedi")
    return {"text": text, "sources": sources[:8], "queries": meta.get("webSearchQueries") or []}


def format_grounded(res: dict[str, Any]) -> str:
    src = "\n".join(f"{i}. {s['title']}\n{s['url']}" for i, s in enumerate(res["sources"], 1))
    return (f"Google araması özeti (Gemini, kaynaklı):\n{res['text']}\n\nKaynaklar (ayrıntı için web_fetch; "
            f"başlık sitenin adıdır):\n{src}")


_ENV_RX = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(s: str) -> str:
    """'${env:GITHUB_TOKEN}' -> ortam değişkeni; anahtar modele hiç görünmeden başlığa konur."""
    def sub(m: re.Match) -> str:
        if m.group(1) not in os.environ:
            raise ToolError(f"ortam değişkeni yok: {m.group(1)}")
        return os.environ[m.group(1)]
    return _ENV_RX.sub(sub, s)


def _json_arg(name: str, v: Any) -> Any:
    """Nesne parametreleri şemada JSON metni (Gemini ve bazı sağlayıcılar serbest nesne kabul etmiyor); model
    yine de nesne gönderirse onu da kabul et."""
    if v is None or v == "" or not isinstance(v, str):
        return v if v != "" else None
    try:
        return json.loads(v)
    except ValueError as e:
        raise ToolError(f"{name} geçerli JSON değil: {e}") from e


class HttpRequest(Tool):
    name = "http_request"
    description = ("Herhangi bir HTTP isteği yapar (GET/POST/PUT/PATCH/DELETE): REST API çağırma, webhook, JSON gönderme, "
                   "dosya indirme. Sadece bir sayfayı okumak için web_fetch daha iyi.\n"
                   "- Gizli anahtarları yazma; başlıkta '${env:AD}' kullan, ALICE ortam değişkeninden doldurur.\n"
                   "- save_to verilirse yanıt gövdesi o dosyaya kaydedilir (dosya indirme).")
    parameters = obj({
        "url": {"type": "string", "description": "Tam adres, ör. 'https://api.github.com/repos/a/b'."},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                   "description": "HTTP yöntemi. Varsayılan GET."},
        "headers": {"type": "string", "description": "İstek başlıkları JSON nesnesi olarak, ör. "
                    "'{\"Authorization\": \"Bearer ${env:TOKEN}\"}'."},
        "params": {"type": "string", "description": "URL sorgu parametreleri JSON nesnesi olarak, ör. '{\"q\": \"ankara\"}'."},
        "json": {"type": "string", "description": "JSON gövde metni, ör. '{\"ad\": \"x\"}' (Content-Type otomatik "
                 "application/json)."},
        "body": {"type": "string", "description": "Ham metin gövde (json yerine)."},
        "save_to": {"type": "string", "description": "Yanıtı bu dosyaya kaydet, ör. 'indirilenler/kurulum.exe'."},
        "timeout": {"type": "integer", "description": "Saniye cinsinden zaman aşımı. Varsayılan 30."},
    }, ["url"])
    risk = NETWORK

    def risk_for(self, args: dict[str, Any]) -> str:
        from alice.tools.base import DANGER, EXEC, WRITE

        if args.get("save_to"):
            from pathlib import Path

            from alice.permissions import is_system_path

            try:
                if is_system_path(Path(str(args["save_to"])).expanduser().resolve()):
                    return DANGER
            except (OSError, ValueError):
                return DANGER
            return WRITE
        # Veri gönderen istekler dışarıda iz bırakır: ask modunda sorulur, plan modunda kapalı.
        return NETWORK if str(args.get("method") or "GET").upper() in ("GET", "HEAD") else EXEC

    def summary(self, args: dict[str, Any]) -> str:
        to = f" -> {args['save_to']}" if args.get("save_to") else ""
        return f"{str(args.get('method') or 'GET').upper()} {args.get('url', '')}{to}"[:160]

    def run(self, ctx: ToolContext, url: str, method: str = "GET", headers: Any = None, params: Any = None,
            json: Any = None, body: str | None = None, save_to: str | None = None, timeout: int = 30) -> ToolResult:
        import json as jsonlib

        headers, params, json = (_json_arg(n, v) for n, v in (("headers", headers), ("params", params), ("json", json)))
        if "://" not in url:
            url = "https://" + url
        hdrs = {"User-Agent": "ALICE/1.0"}
        hdrs.update({str(k): _expand_env(str(v)) for k, v in (headers or {}).items()})
        kw: dict[str, Any] = {"headers": hdrs, "params": params or None, "timeout": max(1, min(int(timeout or 30), 600)),
                              "follow_redirects": True}
        if json is not None:
            kw["json"] = json
        elif body is not None:
            kw["content"] = body.encode("utf-8")
        method = method.upper()
        try:
            if save_to:
                dest = ctx.resolve(save_to)
                dest.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with httpx.stream(method, _expand_env(url), **kw) as r:
                    if r.status_code >= 400:
                        r.read()
                        return ToolResult(f"HTTP {r.status_code} {r.url}\n\n{truncate(r.text, 2000)}", is_error=True)
                    with open(dest, "wb") as f:
                        for part in r.iter_bytes():
                            if ctx.cancelled():
                                raise ToolError("kullanıcı durdurdu; dosya yarım kaldı")
                            f.write(part)
                            size += len(part)
                return ToolResult(f"HTTP {r.status_code}: {size} byte kaydedildi -> {dest}")
            r = httpx.request(method, _expand_env(url), **kw)
        except httpx.HTTPError as e:
            raise ToolError(f"istek başarısız: {type(e).__name__}: {e}") from e
        keep = ("content-type", "location", "retry-after", "x-ratelimit-remaining", "link", "etag")
        shown = "\n".join(f"{k}: {v}" for k, v in r.headers.items() if k.lower() in keep)
        text = r.text
        if "json" in r.headers.get("content-type", ""):
            try:
                text = jsonlib.dumps(r.json(), ensure_ascii=False, indent=1)
            except ValueError:
                pass
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        return ToolResult(truncate(f"HTTP {r.status_code} {r.url}\n{shown}\n\n{text}", limit),
                          is_error=r.status_code >= 400)


TOOLS = [WebFetch(), WebSearch(), HttpRequest()]
