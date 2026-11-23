"""web_crawl: research a site across several pages and return only what is relevant to a question.

crawl4ai's deep crawling, reduced to what an agent needs:

* Best-first order (BestFirstCrawlingStrategy): every discovered link is scored and the most promising one is opened
  next. The score combines the link's anchor text and URL path matching the question (KeywordRelevanceScorer), the
  relevance of the page it was found on, and a small penalty per level of depth (PathDepthScorer).
* Stop when saturated (AdaptiveCrawler, statistical strategy): after each page, how many of the question's terms are
  covered by what we already have, and how much the last pages added. When coverage is complete and new pages stop
  adding anything, the crawl ends before max_pages.
* Same site only, robots.txt respected, non-HTML assets skipped, a few pages fetched in parallel.
* Each page goes through the same reader as web_fetch (pruning + Markdown + numbered links), then BM25 keeps only the
  chunks relevant to the question, so 15 pages come back as one compact digest instead of 15 whole pages.
"""

from __future__ import annotations

import heapq
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from alice.tools import reader
from alice.tools.base import NETWORK, Tool, ToolContext, ToolError, ToolResult, obj, truncate

DEPTH_PENALTY = 0.15
PARALLEL = 4
PER_PAGE_BUDGET = 2500   # characters of BM25-selected text kept per page
SATURATION_PAGES = 3     # stop when this many pages in a row added no new question terms (after full coverage)


@dataclass(order=True)
class _Item:
    neg_score: float
    depth: int
    url: str = field(compare=False)
    parent: str = field(compare=False, default="")


@dataclass
class _PageResult:
    url: str
    title: str
    depth: int
    score: float
    excerpt: str
    links: list[tuple[str, str]]
    terms: set[str]
    via: str = "http"
    error: str = ""


class _Robots:
    def __init__(self, fetch) -> None:
        self._fetch = fetch
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if root not in self._parsers:
                rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
                try:
                    status, text = self._fetch(root + "/robots.txt")
                    if status >= 400:
                        rp = None  # no robots.txt: everything allowed
                    else:
                        rp.parse(text.splitlines())
                except Exception:  # noqa: BLE001 - unreachable robots.txt counts as "no rules"
                    rp = None
                self._parsers[root] = rp
            rp = self._parsers[root]
        return True if rp is None else rp.can_fetch("ALICE", url)


MIN_LINK_SCORE = 0.12  # links scoring below this (no query term in anchor or URL, from a weak page) are not followed


def _link_score(url: str, text: str, query: str, parent_rel: float, depth: int) -> float:
    path = urlparse(url).path.replace("/", " ").replace("-", " ").replace("_", " ").replace(".", " ")
    own = reader.relevance(f"{text} {path}", query)
    # A link that shares nothing with the question only inherits a fraction of its page's relevance: on a docs page
    # every sidebar link ("math", "bugs") used to ride on the page's score and get opened.
    return (0.7 * own + (0.25 if own else 0.08) * parent_rel + 0.05) - DEPTH_PENALTY * (depth - 1)


def crawl(start: str, query: str, max_pages: int = 15, max_depth: int = 3, fetch_page=None,
          cancelled=lambda: False) -> dict[str, Any]:
    """Run the crawl. fetch_page(url) -> web.Fetched (injected for tests). Returns pages, stop reason and counts."""
    if fetch_page is None:
        from alice.tools.web import fetch_page as fp

        fetch_page = fp

    def robots_fetch(u: str) -> tuple[int, str]:
        f = fetch_page(u, allow_browser=False, timeout=10)
        return f.status, f.text

    robots = _Robots(robots_fetch)
    q_terms = set(reader._tokens(query))
    queue: list[_Item] = [_Item(-1.0, 0, start)]
    seen = {start}
    pages: list[_PageResult] = []
    covered: set[str] = set()
    stale = 0
    reason = "sayfa sınırı"
    skipped_robots = 0

    def visit(item: _Item) -> _PageResult:
        try:
            f = fetch_page(item.url)
        except ToolError as e:
            return _PageResult(item.url, "", item.depth, -item.neg_score, "", [], set(), error=str(e))
        if f.status >= 400 or "html" not in f.ctype:
            return _PageResult(item.url, "", item.depth, -item.neg_score, "", [], set(),
                               error=f"HTTP {f.status} {f.ctype.split(';')[0]}")
        page = reader.to_markdown(f.text, f.url)
        excerpt = reader.bm25_select(page.markdown, query, budget=PER_PAGE_BUDGET) if query else \
            page.markdown[:PER_PAGE_BUDGET]
        # Links from the whole page (navigation included: that is where a docs site keeps its map).
        links = reader.page_links(f.text, f.url)
        return _PageResult(f.url, page.title, item.depth, -item.neg_score, excerpt, links,
                           set(reader._tokens(page.markdown)) & q_terms, via=f.via)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        while queue and len(pages) < max_pages:
            if cancelled():
                reason = "durduruldu"
                break
            batch: list[_Item] = []
            while queue and len(batch) < min(PARALLEL, max_pages - len(pages)):
                item = heapq.heappop(queue)
                if item.depth and not robots.allowed(item.url):
                    skipped_robots += 1
                    continue
                batch.append(item)
            if not batch:
                break
            futures = {pool.submit(visit, it): it for it in batch}
            for fut in as_completed(futures):
                res = fut.result()
                pages.append(res)
                if res.error:
                    continue
                rel = reader.relevance(res.excerpt, query) if query else 0.5
                res.score = rel
                new = res.terms - covered
                covered |= res.terms
                stale = 0 if new else stale + 1
                if res.depth >= max_depth:
                    continue
                for url, text in res.links:
                    if url in seen or not reader.same_site(url, start) or reader.SKIP_EXT.search(url):
                        continue
                    seen.add(url)
                    s = _link_score(url, text, query, rel, res.depth + 1) if query else 0.5 - res.depth * 0.1
                    if query and s < MIN_LINK_SCORE:
                        continue
                    heapq.heappush(queue, _Item(-s, res.depth + 1, url, res.url))
            if q_terms and covered >= q_terms and stale >= SATURATION_PAGES:
                reason = "yeni bilgi gelmiyor (soru kapsandı)"
                break
            # Saturation on quality too: once we have strong pages and what is left in the queue is weak, stop.
            strong = sum(1 for p in pages if not p.error and p.score >= 0.99)
            if query and strong >= 2 and (not queue or -queue[0].neg_score < 0.35) and len(pages) >= 4:
                reason = "kalan bağlantılar zayıf (soru kapsandı)"
                break
        else:
            if not queue:
                reason = "gezilecek bağlantı kalmadı"
    return {"pages": pages, "reason": reason, "queued": len(queue), "robots_skipped": skipped_robots,
            "coverage": (len(covered & q_terms) / len(q_terms)) if q_terms else 1.0}


class WebCrawl(Tool):
    name = "web_crawl"
    untrusted = True
    description = (
        "Researches a site by visiting several pages and returns only the parts relevant to the question (opens "
        "nothing on screen). Opens the most relevant link first and stops by itself once the question is covered well "
        "enough and new pages add nothing; stays on the same site, follows robots.txt, and opens pages that block it "
        "with a hidden browser.\n"
        "- For finding something in documentation/a blog/a knowledge base, or gathering a topic from several pages. "
        "For a single page web_fetch is enough.\n"
        "- The clearer the query, the fewer pages are visited.")
    parameters = obj({
        "url": {"type": "string", "description": "Start page, e.g. 'https://docs.python.org/3/library/asyncio.html'."},
        "query": {"type": "string", "description": "What you are looking for, e.g. 'how task cancellation and "
                                                   "CancelledError are handled'."},
        "max_pages": {"type": "integer", "description": "At most this many pages (default 12, at most 40)."},
        "max_depth": {"type": "integer", "description": "At most this many clicks away from the start (default 3)."},
    }, ["url", "query"])
    risk = NETWORK

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('url', '')} ({str(args.get('query', ''))[:60]})"

    def run(self, ctx: ToolContext, url: str = "", query: str = "", max_pages: int = 12, max_depth: int = 3,
            **kwargs: Any) -> ToolResult:
        if "://" not in url:
            url = "https://" + url
        max_pages = max(1, min(int(12 if max_pages is None else max_pages), 40))
        t0 = time.monotonic()
        depth_val = max(0, min(int(3 if max_depth is None else max_depth), 6))
        out = crawl(url, query, max_pages=max_pages, max_depth=depth_val,
                    cancelled=getattr(ctx, "cancelled", lambda: False))
        pages: list[_PageResult] = out["pages"]
        ok = [p for p in pages if not p.error]
        if not ok:
            errs = "; ".join(f"{p.url}: {p.error}" for p in pages[:3])
            raise ToolError(f"hiçbir sayfa okunamadı ({errs})")
        ok.sort(key=lambda p: p.score, reverse=True)
        lines = [f"{len(ok)} sayfa okundu ({time.monotonic() - t0:.0f} sn), durma nedeni: {out['reason']}; soru "
                 f"kapsamı %{out['coverage'] * 100:.0f}"
                 + (f"; robots.txt yüzünden {out['robots_skipped']} bağlantı atlandı" if out["robots_skipped"] else "")
                 + (f"; {sum(1 for p in pages if p.error)} sayfa açılamadı" if any(p.error for p in pages) else "")]
        for i, p in enumerate(ok, 1):
            via = " · tarayıcıyla" if p.via == "browser" else ""
            body = p.excerpt.strip() or "(ilgili bölüm yok)"
            # The numbered links inside an excerpt refer to that page's own list; drop the markers to avoid
            # confusing numbers across pages, the page URL is what matters here.
            body = reader.strip_link_marks(body)
            lines.append(f"\n## {i}. {p.title or p.url}\n{p.url} (alaka %{p.score * 100:.0f}{via})\n\n{body}")
        limit = int(ctx.config.agent.get("tool_output_chars", 12000)) * 2
        return ToolResult(truncate("\n".join(lines), limit))


TOOLS = [WebCrawl()]
