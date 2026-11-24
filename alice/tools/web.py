"""Web search and page reading that need no API key."""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

from typing import Any

import httpx

from alice.tools import reader, stealth
from alice.tools.base import NETWORK, Tool, ToolContext, ToolError, ToolResult, obj, truncate

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

# Fetching the same page/search repeatedly in a session is common (the agent keeps revisiting the same doc).
# A short-lived in-memory cache cuts this from seconds to ms; the TTL is short, so content never goes stale.
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
        self.main_parts: list[str] = []  # Inside <main>/<article>: the actual content, without menus and sidebars
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
    """Convert HTML to readable text. main_only: if the page has a long enough <main>/<article>, return only that."""
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
        return f"[start={start} is past the end of the text; the text is {len(body)} characters]"
    chunk = body[start:start + limit]
    rest = len(body) - start - len(chunk)
    head = f"[karakter {start}-{start + len(chunk)} / {len(body)}]\n" if start else ""
    tail = f"\n\n... [{rest} characters cut; for the rest start={start + len(chunk)}]" if rest > 0 else ""
    return head + chunk + tail


def _extract(ctx: ToolContext, url: str, text: str, prompt: str) -> str | None:
    """Like Claude Code's WebFetch: pass the page to a small model and extract only the requested info. None if no model.
    The model gets the BM25-selected parts of the page (crawl4ai's BM25 filter), not its first 60k characters: on a
    long page the answer used to sit past the cut."""
    if ctx.router is None:
        return None
    focus = reader.bm25_select(text, prompt, budget=24000) if len(text) > 24000 else text
    msgs = [{"role": "system", "content": "You extract information from a web page. Answer only from the page content "
             "below, concisely, in the language of the request. Quote exact numbers, names and code. Links appear as "
             "text⟨n⟩ with the URLs listed at the end; cite a URL when it helps. If the page does not contain the "
             "answer, say so."},
            {"role": "user", "content": f"Request: {prompt}\n\nURL: {url}\n\nPage content:\n{focus}"}]
    try:
        return ctx.router.chat("fast", msgs, temperature=0, max_tokens=1500).text.strip() or None
    except Exception:  # noqa: BLE001 - if there is no model or it crashes, fall back to the selected text
        return None


# ------------------------------------------------------------------------------------- fetch ladder
# crawl4ai's escalation: a plain request first (fast, no browser), then a real stealth browser when the answer is a
# block page, a challenge or an empty JavaScript shell. Only an explicit raw=True skips the browser.
BOT_UA = ("ALICE/1.0 (+https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine; "
          "personal assistant fetching a page for its user)")
# Sites that refuse a faked browser UA but welcome an honest client (Wikimedia's robot policy asks for exactly this).
HONEST_UA_HOSTS = ("wikipedia.org", "wikimedia.org", "wiktionary.org", "wikidata.org", "mediawiki.org")


@dataclass
class Fetched:
    status: int
    url: str
    ctype: str
    body: bytes
    text: str
    via: str = "http"  # http | browser
    note: str = ""     # why the browser was used, shown to the model


def _headers(url: str) -> dict[str, str]:
    host = (urlparse(url).hostname or "").lower()
    ua = BOT_UA if host.endswith(HONEST_UA_HOSTS) else UA
    return {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"}


def fetch_page(url: str, allow_browser: bool = True, timeout: float = 30) -> Fetched:
    """Plain HTTP; if the response is a block/challenge/empty JS shell, render it in the stealth browser."""
    try:
        r = httpx.get(url, headers=_headers(url), follow_redirects=True, timeout=timeout)
    except httpx.HTTPError as e:
        # A dead host or refused connection will not work in a browser either; only a reset/timeout (often a WAF
        # dropping non-browser clients) is worth a browser attempt.
        if not allow_browser or isinstance(e, (httpx.ConnectError, httpx.InvalidURL, httpx.UnsupportedProtocol)):
            raise ToolError(f"indirilemedi: {e}") from e
        r = None
        why = f"indirilemedi: {e}"
    if r is not None:
        ctype = r.headers.get("content-type", "").lower()
        f = Fetched(r.status_code, str(r.url), ctype, r.content, r.text if _looks_textual(ctype, r.content) else "")
        if "html" not in ctype or not allow_browser:
            return f
        why = reader.detect_block(r.status_code, f.text)
        if not why:
            return f
    if not stealth.available():
        if r is None:
            raise ToolError(why)
        return f
    try:
        out = stealth.render(url)
    except stealth.RenderError as e:
        if r is None:
            raise ToolError(f"{why}; it couldn't be opened with the browser either ({e})") from e
        f.note = f"[plain request: {why}; opening with the browser failed too: {e}]"
        return f
    html_text = out["html"]
    # The browser's own error page ("can't reach this page") is not the site.
    if out["url"].startswith(("chrome-error:", "edge-error:", "about:")):
        if r is None:
            raise ToolError(f"{why}; the browser couldn't reach the site either")
        return f
    return Fetched(out["status"], out["url"], "text/html; charset=utf-8", html_text.encode("utf-8", "replace"),
                   html_text, via="browser", note=f"[the page was opened with a real browser: {why}]")


class WebFetch(Tool):
    name = "web_fetch"
    untrusted = True
    parallel_safe = True
    description = ("Fetches a URL in the background and returns readable Markdown: menus, ads, and footers are stripped, "
                   "headings, lists, tables, and code are preserved, and links are numbered as text⟨n⟩ and listed at the end. "
                   "JSON is pretty-printed and PDFs are converted to text (nothing opens on screen). If a site blocks plain "
                   "requests or renders via JavaScript, it automatically falls back to a stealth browser.\n"
                   "- Pass prompt to select relevant sections and extract only the requested info with a small model, "
                   "using far fewer tokens on long pages.\n"
                   "- If a long page is truncated, pass start to read the rest. To explore a site across multiple pages, "
                   "use web_crawl.\n"
                   "- For pages requiring login, clicking, or forms, use browser. For API calls (POST, custom headers), "
                   "use http_request.")
    parameters = obj({
        "url": {"type": "string", "description": "Full URL, e.g. 'https://tr.wikipedia.org/wiki/Ankara'."},
        "prompt": {"type": "string", "description": "What you want to learn from the page, e.g. 'installation steps' or "
                   "'price and availability'. If omitted, returns the page text."},
        "start": {"type": "integer", "description": "Character offset to start reading from (to continue a truncated page). "
                  "Default 0."},
        "raw": {"type": "boolean", "description": "true: return raw HTML without converting to text. Default false."},
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
            f = fetch_page(url, allow_browser=not raw)
            ctype = f.ctype
            refs = ""
            if "pdf" in ctype or (not ctype and f.url.lower().endswith(".pdf")):
                body = _pdf_text(f.body)
            elif not f.text and not _looks_textual(ctype, f.body):
                # Images, zip, exe...: r.text used to return a pile of meaningless characters and flood the context.
                body = (f"[binary content: {ctype or 'type not given'}, {len(f.body)} bytes - can't be shown "
                        "as text. To download it use run_command (e.g. Invoke-WebRequest -OutFile).]")
            else:
                body = f.text
                if not raw and "html" in ctype:
                    page = reader.to_markdown(body, f.url)
                    title = f"# {page.title}\n\n" if page.title and not page.markdown.lstrip().startswith("# ") else ""
                    body = title + (page.markdown or html_to_text(f.text, main_only=True))
                    refs = reader.references(page.links)
                    if f.note:
                        body = f.note + "\n\n" + body
                elif not raw and "json" in ctype:
                    try:
                        body = json.dumps(json.loads(body), ensure_ascii=False, indent=1)
                    except ValueError:
                        pass  # not valid JSON: return the body as is
            hit = (f.status, f.url, body, refs)
            if f.status < 400:
                cache_put(key, hit)  # only successful downloads; failed pages may be retried
        status, final_url, body, refs = hit
        head = f"HTTP {status} {final_url}\n\n"
        if prompt and status < 400:
            answer = _extract(ctx, final_url, body + refs, prompt)
            if answer:
                return ToolResult(head + answer)
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        shown = _paginate(body, start, limit)
        # The link list belongs to what is shown: only the numbers that appear in this slice of the page.
        tail = reader.references([(u, t) if i in reader.used_links(shown) else ("", "")
                                  for i, (u, t) in enumerate(_links_of(refs), 1)], reader.used_links(shown)) if refs else ""
        return ToolResult(head + shown + tail, is_error=status >= 400)


def _links_of(refs: str) -> list[tuple[str, str]]:
    """Parse a references() block back into (url, text) by number (the cache stores the rendered block)."""
    out: dict[int, tuple[str, str]] = {}
    for m in re.finditer(r"^⟨(\d+)⟩ (\S+)(?: — (.*))?$", refs, re.M):
        out[int(m.group(1))] = (m.group(2), m.group(3) or "")
    return [out.get(i, ("", "")) for i in range(1, max(out, default=0) + 1)]


_TEXT_CTYPE = ("text/", "json", "xml", "javascript", "ecmascript", "html", "yaml", "toml", "csv",
               "x-www-form-urlencoded", "graphql", "sql")


def _looks_textual(ctype: str, data: bytes) -> bool:
    """If a text type is declared it is text. Otherwise (octet-stream, empty type) check the first bytes:
    if there is no NUL and they decode as UTF-8, call it text."""
    if any(t in ctype for t in _TEXT_CTYPE):
        return True
    head = data[:4096]
    if b"\0" in head:
        return False
    import codecs

    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


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
            continue  # ad
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        results.append({"title": _strip_tags(title), "url": href, "snippet": _strip_tags(snip.group(1)) if snip else ""})
        if len(results) >= max_results:
            break
    return results


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def bing_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """Fallback when DuckDuckGo shows a bot page or fails: Bing's HTML results page."""
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
    untrusted = True
    parallel_safe = True
    description = ("Searches the web and returns result titles, URLs, and snippets without opening anything on screen. "
                   "Use for current information, news, prices, or unfamiliar topics; follow up with web_fetch (with prompt) "
                   "for details.\n"
                   "- Restrict results to specific sites with allowed_domains.\n"
                   "- If the user wants to view the search in their browser, use windows action=navigate.")
    parameters = obj({
        "query": {"type": "string", "description": "Search query, e.g. 'Ankara weather tomorrow'."},
        "max_results": {"type": "integer", "description": "Maximum number of results to return. Default 8."},
        "allowed_domains": {"type": "array", "items": {"type": "string", "description": "Domain name."},
                            "description": "Only include results from these sites, e.g. [\"docs.python.org\"]."},
        "blocked_domains": {"type": "array", "items": {"type": "string", "description": "Domain name."},
                            "description": "Exclude results from these sites."},
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
                pass  # quota/error: classic engines
        q = query + (f" site:{allowed[0]}" if len(allowed) == 1 and "site:" not in query else "")
        want = max_results * 3 if allowed or blocked else max_results
        key = os.environ.get("BRAVE_SEARCH_API_KEY") or ctx.config.agent.get("brave_api_key")
        engines = ([("brave", lambda: brave_search(q, key, want))] if key else []) + [
            ("duckduckgo", lambda: ddg_search(q, want)), ("bing", lambda: bing_search(q, want))]
        res: list[dict[str, str]] = []
        errors = []
        for name, fn in engines:
            ckey = ("search", name, q, want)  # note: `key` above is the Brave key
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
                raise ToolError("search failed: " + "; ".join(errors))
            return ToolResult("no results")
        return ToolResult("\n\n".join(f"{i}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(res, 1)))


# Google search (Gemini grounding): the model searches Google, reads the results and writes a sourced summary.
# It is much more current and complete than scraping DuckDuckGo/Bing pages. If the quota runs out or it errors,
# it is not tried for a while and classic engines are used instead.
GROUNDING_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROUNDING_PROMPT = ("Search Google for this question and write what you find as a short, factual summary: at most 8 points, "
                    "keep dates, numbers, versions and names exactly; say when you're unsure; add no commentary.\n"
                    "Search: {query}")
_grounding = {"down_until": 0.0}


def grounding_model(ctx: ToolContext) -> str:
    return str(ctx.config.agent.get("grounding_model", "gemini-flash-latest"))


def gemini_search(query: str, key: str, model: str, timeout: float = 25.0) -> dict[str, Any]:
    """{'text': summary, 'sources': [{'title', 'url'}], 'queries': [...]}; quota/error raises ValueError."""
    if time.monotonic() < _grounding["down_until"]:
        raise ValueError("Google search is temporarily off (quota/error)")
    try:
        r = httpx.post(GROUNDING_URL.format(model=model), headers={"x-goog-api-key": key},
                       json={"contents": [{"role": "user", "parts": [{"text": GROUNDING_PROMPT.format(query=query)}]}],
                             "tools": [{"google_search": {}}]}, timeout=timeout)
    except httpx.HTTPError as e:
        _grounding["down_until"] = time.monotonic() + 300
        raise ValueError(f"connection: {e}") from e
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
        raise ValueError("Google search returned no results")
    return {"text": text, "sources": sources[:8], "queries": meta.get("webSearchQueries") or []}


def format_grounded(res: dict[str, Any]) -> str:
    src = "\n".join(f"{i}. {s['title']}\n{s['url']}" for i, s in enumerate(res["sources"], 1))
    return (f"Google search summary (Gemini, with sources):\n{res['text']}\n\nSources (web_fetch for details; "
            f"the title is the site's name):\n{src}")


_ENV_RX = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(s: str) -> str:
    """'${env:GITHUB_TOKEN}' -> environment variable; the key goes into the header without the model ever seeing it."""
    def sub(m: re.Match) -> str:
        if m.group(1) not in os.environ:
            raise ToolError(f"no such environment variable: {m.group(1)}")
        return os.environ[m.group(1)]
    return _ENV_RX.sub(sub, s)


def _json_arg(name: str, v: Any) -> Any:
    """Object parameters are JSON text in the schema (Gemini and some providers reject free-form objects); if
    the model still sends an object, accept it too."""
    if v is None or v == "" or not isinstance(v, str):
        return v if v != "" else None
    try:
        return json.loads(v)
    except ValueError as e:
        raise ToolError(f"{name} is not valid JSON: {e}") from e


class HttpRequest(Tool):
    name = "http_request"
    untrusted = True
    description = ("Makes an HTTP request (GET/POST/PUT/PATCH/DELETE) for calling REST APIs, triggering webhooks, sending JSON, "
                   "or downloading files. Use web_fetch instead when you only need to read a web page.\n"
                   "- Do not hardcode secrets; use '${env:NAME}' in headers or the URL and ALICE will expand it from environment variables.\n"
                   "- Pass save_to to save the response body directly to a file (file download).")
    parameters = obj({
        "url": {"type": "string", "description": "Full URL, e.g. 'https://api.github.com/repos/a/b'."},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                   "description": "HTTP method. Default GET."},
        "headers": {"type": "string", "description": "Request headers as a JSON object string, e.g. "
                    "'{\"Authorization\": \"Bearer ${env:TOKEN}\"}'."},
        "params": {"type": "string", "description": "URL query parameters as a JSON object string, e.g. '{\"q\": \"ankara\"}'."},
        "json": {"type": "string", "description": "JSON request body string, e.g. '{\"name\": \"x\"}' (sets Content-Type to "
                 "application/json automatically)."},
        "body": {"type": "string", "description": "Raw text request body (used instead of json)."},
        "save_to": {"type": "string", "description": "Save the response body to this file path (absolute or relative to the "
                    "working directory), e.g. 'downloads/setup.exe'."},
        "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."},
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
        # Requests that send data leave a trace outside: they are asked about in ask mode and disabled in plan mode.
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
        # If the URL has ${env:..}, show the template, not the expanded r.url, so the key does not reach the model.
        where = (lambda r: url) if _ENV_RX.search(url) else (lambda r: r.url)
        try:
            if save_to:
                dest = ctx.resolve(save_to)
                dest.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with httpx.stream(method, _expand_env(url), **kw) as r:
                    if r.status_code >= 400:
                        r.read()
                        return ToolResult(f"HTTP {r.status_code} {where(r)}\n\n{truncate(r.text, 2000)}", is_error=True)
                    # Write to a side file first. Opening the target with "wb" up front deleted the existing file
                    # if a download was cut off (network error, Durdur/Stop) it left a half-written file behind.
                    tmp = dest.with_name(dest.name + ".part")
                    try:
                        with open(tmp, "wb") as f:
                            for part in r.iter_bytes():
                                if ctx.cancelled():
                                    raise ToolError("stopped by the user; the file was not saved")
                                f.write(part)
                                size += len(part)
                        tmp.replace(dest)
                    except BaseException:
                        tmp.unlink(missing_ok=True)
                        raise
                return ToolResult(f"HTTP {r.status_code}: {size} byte kaydedildi -> {dest}")
            r = httpx.request(method, _expand_env(url), **kw)
        except httpx.HTTPError as e:
            raise ToolError(f"request failed: {type(e).__name__}: {e}") from e
        keep = ("content-type", "location", "retry-after", "x-ratelimit-remaining", "link", "etag")
        shown = "\n".join(f"{k}: {v}" for k, v in r.headers.items() if k.lower() in keep)
        text = r.text
        if "json" in r.headers.get("content-type", ""):
            try:
                text = jsonlib.dumps(r.json(), ensure_ascii=False, indent=1)
            except ValueError:
                pass  # not valid JSON: return the text as is
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        return ToolResult(truncate(f"HTTP {r.status_code} {where(r)}\n{shown}\n\n{text}", limit),
                          is_error=r.status_code >= 400)


TOOLS = [WebFetch(), WebSearch(), HttpRequest()]
