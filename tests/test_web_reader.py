"""Page reader (pruning, Markdown, BM25, block detection), the web_fetch escalation ladder and web_crawl."""

from __future__ import annotations

import pytest

from alice.tools import crawl, reader, stealth, web
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config

ARTICLE = """
<html><head><title>Guide</title><meta name="description" content="A guide"></head><body>
<header class="site-header"><a href="/">Home</a> <a href="/login">Log in</a></header>
<nav class="sidebar"><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li><li><a href="/c">C</a></li></ul></nav>
<div class="cookie-banner">We use cookies. <button>Accept all</button></div>
<main class="content">
  <h1>Install the tool <a class="headerlink" href="#install">¶</a></h1>
  <p>To install the tool run the command below. It works on <strong>Linux</strong> and Windows, see the
     <a href="/docs/requirements">requirements page</a> for details about supported versions.</p>
  <pre><code class="language-bash">pip install tool</code></pre>
  <h2>Options</h2>
  <table><tr><th>Flag</th><th>Meaning</th></tr><tr><td>--all</td><td>everything</td></tr>
         <tr><td>--dir</td><td>target folder</td></tr></table>
  <ul><li>First point about options and how they combine</li><li>Second point with a
      <a href="https://example.org/x">link</a></li></ul>
</main>
<div class="share-buttons"><a href="https://twitter.com/x">Tweet</a> <a href="https://fb.com/x">Share</a></div>
<footer>© 2026 Company · <a href="/privacy">Privacy Policy</a> · <a href="/terms">Terms of use</a></footer>
<script>var tracking = 1;</script>
</body></html>"""


def test_markdown_keeps_structure_and_drops_chrome():
    page = reader.to_markdown(ARTICLE, "https://site.test/guide")
    md = page.markdown
    assert page.title == "Guide"
    assert "# Install the tool" in md and "¶" not in md
    assert "```bash\npip install tool\n```" in md
    assert "| Flag | Meaning |" in md and "| --dir | target folder |" in md
    assert "- First point about options" in md
    assert "**Linux**" in md
    for noise in ("Log in", "cookies", "Accept all", "Tweet", "Privacy Policy", "©", "tracking"):
        assert noise not in md, noise


def test_links_are_numbered_with_one_reference_list():
    page = reader.to_markdown(ARTICLE, "https://site.test/guide")
    assert "requirements page⟨1⟩" in page.markdown
    assert page.links[0] == ("https://site.test/docs/requirements", "requirements page")
    refs = reader.references(page.links)
    assert "⟨1⟩ https://site.test/docs/requirements — requirements page" in refs
    # only the numbers still present in a shown slice are listed
    assert len(page.links) >= 2
    assert reader.references(page.links, used={2}).count("⟨") == 1


def test_fit_falls_back_to_full_page_when_pruning_removes_everything():
    html = "<body><nav>" + " ".join(f"<a href='/p{i}'>Page number {i} of the site</a>" for i in range(60)) + "</nav></body>"
    assert "Page number 59" in reader.to_markdown(html, "https://s.test/").markdown


def test_sloppy_markup_still_nests():
    md = reader.to_markdown("<body><p>one<p>two<ul><li>a<li>b</ul></body>").markdown
    assert "one" in md and "two" in md and "- a" in md and "- b" in md


def test_bm25_picks_the_section_about_the_question():
    md = "\n\n".join(["# Doc", "## Install", "Run pip install. " * 30, "## Cancel a task",
                      "Call task.cancel() and catch CancelledError inside the coroutine. " * 5,
                      "## Logging", "Configure the logger with handlers. " * 30])
    out = reader.bm25_select(md, "how to cancel a task and catch CancelledError", budget=600)
    assert "task.cancel()" in out and "pip install" not in out and "logger" not in out
    assert reader.bm25_select(md, "zzzz qqqq", budget=50) == md[:50]  # no match: head of the page


def test_bm25_keeps_code_blocks_whole():
    # "# setup" inside the fence is a comment, not a heading: the code stays one chunk under "Example".
    md = "## Example\n\n```python\n# setup\nx = 1\n\n# run it\nrun(x)\n```\n\n## Other\n\nunrelated text"
    parts = reader.chunks(md)
    assert [h for h, _ in parts] == ["Example", "Other"]
    assert "# run it" in parts[0][1]


@pytest.mark.parametrize("status,html,blocked", [
    (403, "<html><title>Just a moment...</title><script>window._cf_chl_opt={}</script></html>", True),
    (200, "<html><body><div id='px-captcha'></div></body></html>", True),
    (429, "slow down", True),
    (403, "<html><body>Forbidden</body></html>", True),
    (200, "<html><body><div id='root'></div><script src='app.js'></script></body></html>", True),
    (200, "<html><head><title>Reddit</title><script>document.forms[0].submit()</script></head>" + " " * 6000 + "</html>", True),
    (403, '{"error": "forbidden"}', False),  # an API answer, not a block page
    (200, "<html><body>" + "<p>Real article text here.</p>" * 200 + "</body></html>", False),
])
def test_detect_block(status, html, blocked):
    assert bool(reader.detect_block(status, html)) is blocked


def test_detect_block_ignores_challenge_loader_on_a_real_page():
    html = "<html><script src='/cdn-cgi/challenge-platform/x.js'></script><body>" + "<p>text</p>" * 10 + "</body></html>"
    assert reader.detect_block(200, html, text_len=5000) == ""


# ------------------------------------------------------------------------------------------ fetch ladder
class _Resp:
    def __init__(self, url, text, status=200, ctype="text/html; charset=utf-8"):
        self.url, self.text, self.status_code = url, text, status
        self.content = text.encode()
        self.headers = {"content-type": ctype}


def test_blocked_page_is_rendered_in_the_stealth_browser(monkeypatch, workdir):
    web._cache.clear()
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _Resp(url, "<title>Just a moment...</title>", 403))
    monkeypatch.setattr(stealth, "available", lambda: True)
    seen = []
    monkeypatch.setattr(stealth, "render", lambda url, **kw: seen.append(url) or
                        {"status": 200, "url": url, "title": "Q", "html": ARTICLE})
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = web.WebFetch().run(ctx, url="https://blocked.test/q").text
    assert seen == ["https://blocked.test/q"]
    assert out.startswith("HTTP 200") and "opened with a real browser" in out and "pip install tool" in out


def test_raw_never_starts_a_browser(monkeypatch, workdir):
    web._cache.clear()
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _Resp(url, "<title>Just a moment...</title>", 403))
    monkeypatch.setattr(stealth, "available", lambda: True)
    monkeypatch.setattr(stealth, "render", lambda *a, **k: pytest.fail("raw must not render"))
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    assert "Just a moment" in web.WebFetch().run(ctx, url="https://blocked.test/r", raw=True).text


def test_browser_error_page_is_not_returned_as_the_site(monkeypatch, workdir):
    web._cache.clear()
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _Resp(url, "", 200))
    monkeypatch.setattr(stealth, "available", lambda: True)
    monkeypatch.setattr(stealth, "render", lambda url, **kw: {"status": 200, "url": "chrome-error://chromewebdata/",
                                                             "title": "", "html": "<p>can't reach this page</p>"})
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    assert "can't reach" not in web.WebFetch().run(ctx, url="https://empty.test/").text


def test_dead_host_does_not_try_the_browser(monkeypatch):
    def boom(url, **kw):
        raise web.httpx.ConnectError("refused")

    monkeypatch.setattr(web.httpx, "get", boom)
    monkeypatch.setattr(stealth, "available", lambda: True)
    monkeypatch.setattr(stealth, "render", lambda *a, **k: pytest.fail("no browser for a dead host"))
    with pytest.raises(ToolError, match="refused"):
        web.fetch_page("https://nowhere.test/")


def test_wikimedia_gets_the_honest_user_agent():
    assert web._headers("https://en.wikipedia.org/wiki/X")["User-Agent"].startswith("ALICE/")
    assert web._headers("https://example.com/")["User-Agent"] == web.UA


def test_prompt_sends_bm25_selection_not_the_page_head(monkeypatch, workdir):
    web._cache.clear()
    filler = "<p>" + "Unrelated filler about other topics. " * 40 + "</p>"
    html = "<body><main>" + "<h2>Intro</h2>" + filler * 40 + "<h2>Refund policy</h2><p>Refunds take 14 days.</p></main></body>"
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _Resp(url, html))
    sent = {}

    class Router:
        def chat(self, role, msgs, **kw):
            sent["content"] = msgs[-1]["content"]
            return type("R", (), {"text": "14 days"})()

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), router=Router())
    assert web.WebFetch().run(ctx, url="https://shop.test/", prompt="refund policy days").text.endswith("14 days")
    assert "Refunds take 14 days" in sent["content"] and len(sent["content"]) < 30000


# ---------------------------------------------------------------------------------------------- crawl
def _site(pages: dict[str, str]):
    def fetch(url, allow_browser=True, timeout=30):
        if url.endswith("/robots.txt"):
            body = pages.get(url, "")
            return web.Fetched(200 if body else 404, url, "text/plain", body.encode(), body)
        html = pages.get(url)
        if html is None:
            return web.Fetched(404, url, "text/html", b"", "")
        return web.Fetched(200, url, "text/html", html.encode(), html)
    return fetch


def _doc(title, body, links):
    return (f"<html><title>{title}</title><body><nav>" + "".join(f"<a href='{u}'>{t}</a>" for u, t in links)
            + f"</nav><main><h1>{title}</h1><p>{body}</p></main></body></html>")


SITE = {
    "https://d.test/": _doc("Docs home", "Welcome to the documentation of the tool, start anywhere you like.",
                            [("/install", "Installation"), ("/cancel", "Cancelling tasks"), ("/blog", "Our blog"),
                             ("https://other.test/x", "External"), ("/logo.png", "Logo"), ("/private/a", "Cancel private")]),
    "https://d.test/install": _doc("Installation", "Run pip install tool on any platform you use. " * 5, [("/", "Home")]),
    "https://d.test/cancel": _doc("Cancelling tasks", "Call task.cancel() and handle CancelledError. " * 5,
                                  [("/cancel/shield", "Shield a task from cancel")]),
    "https://d.test/cancel/shield": _doc("Shield", "Use shield to protect a task from cancel and CancelledError. " * 4, []),
    "https://d.test/blog": _doc("Blog", "Company news and announcements about hiring. " * 5, []),
    "https://d.test/robots.txt": "User-agent: *\nDisallow: /private/\n",
}


def test_crawl_goes_best_first_and_stays_on_site():
    out = crawl.crawl("https://d.test/", "cancel a task CancelledError", max_pages=10, fetch_page=_site(SITE))
    urls = [p.url for p in out["pages"]]
    assert urls[0] == "https://d.test/"
    assert "https://d.test/cancel" in urls[:3]
    assert not any("other.test" in u or u.endswith(".png") for u in urls)
    assert "https://d.test/private/a" not in urls and out["robots_skipped"] == 1
    assert "https://d.test/blog" not in urls  # nothing in its anchor or URL matches the question
    best = max((p for p in out["pages"] if not p.error), key=lambda p: p.score)
    assert "CancelledError" in best.excerpt


def _with_site(monkeypatch, pages):
    real = crawl.crawl
    monkeypatch.setattr(crawl, "crawl", lambda *a, **k: real(*a, **{**k, "fetch_page": _site(pages)}))


def test_crawl_tool_formats_a_digest(monkeypatch, workdir):
    _with_site(monkeypatch, SITE)
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = crawl.WebCrawl().run(ctx, url="d.test/", query="cancel a task CancelledError").text
    assert "pages read" in out and "https://d.test/cancel" in out and "⟨" not in out


def test_crawl_max_depth_zero_only_visits_root(monkeypatch, workdir):
    _with_site(monkeypatch, SITE)
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = crawl.WebCrawl().run(ctx, url="https://d.test/", query="cancel", max_depth=0).text
    assert "1 pages read" in out


def test_crawl_reports_when_nothing_could_be_read(monkeypatch, workdir):
    _with_site(monkeypatch, {})
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    with pytest.raises(ToolError, match="no page could be read"):
        crawl.WebCrawl().run(ctx, url="https://d.test/", query="x")
