"""alice/tools/web.py için birim testleri: httpx.get/post sahte fonksiyonlarla değiştirilir, ağa çıkılmaz."""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest

from alice.tools import web
from alice.tools.base import NETWORK, ToolContext, ToolError
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def _resp(url, text, status=200, ctype="text/html; charset=utf-8", method="GET"):
    return httpx.Response(status, headers={"content-type": ctype} if ctype else {}, text=text,
                          request=httpx.Request(method, url))


# ------------------------------------------------------------------ html_to_text


def test_html_to_text_structure():
    raw = ("<html><head><title>T</title><style>x{}</style></head><body><h1>Başlık</h1><h3>Alt</h3>"
           "<p>a &amp; b</p><ul><li>bir</li><li>iki</li></ul>"
           "<a href=\"https://x.com\">link</a> <a href=\"/rel\">rel</a>"
           "<script>alert(1)</script><nav>menü</nav><footer>alt bilgi</footer></body></html>")
    out = web.html_to_text(raw)
    assert out.startswith("# Başlık")
    assert "### Alt" in out
    assert "a & b" in out
    assert "* bir" in out and "* iki" in out
    assert "link (https://x.com)" in out
    assert "(/rel)" not in out  # göreli linkler eklenmez
    for hidden in ("T", "x{}", "alert", "menü", "alt bilgi"):
        assert hidden not in out.split()


def test_html_to_text_whitespace_collapse():
    assert web.html_to_text("<p>a   \t b</p>\n\n\n\n<p>c</p>") == "a b\n\nc"
    assert web.html_to_text("   ") == ""


def test_html_to_text_nested_skip_and_stray_end_tag():
    # iç içe atlanan etiketler sayaçla takip edilir; eşleşmeyen kapanış sayaç negatife düşürmez
    assert web.html_to_text("<head><script>a</script>b</head>c") == "c"
    assert web.html_to_text("</script>görünür") == "görünür"


def test_html_to_text_link_with_nested_markup():
    assert web.html_to_text('<a href="https://a.b">x<b>y</b></a>') == "xy (https://a.b)"


# ------------------------------------------------------------------ WebFetch


def test_web_fetch_meta():
    t = web.WebFetch()
    assert t.risk == NETWORK and t.parallel_safe
    assert t.summary({"url": "https://a"}) == "https://a"
    assert t.summary({}) == ""


def test_web_fetch_adds_scheme_and_converts_html(ctx, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, follow_redirects=False, timeout=None):
        seen.update(url=url, headers=headers, follow_redirects=follow_redirects, timeout=timeout)
        return _resp(url, "<html><body><h1>Merhaba</h1><script>x()</script></body></html>")

    monkeypatch.setattr(web.httpx, "get", fake_get)
    r = web.WebFetch().run(ctx, url="example.com/a")
    assert seen["url"] == "https://example.com/a"
    assert seen["headers"]["User-Agent"] == web.UA and seen["follow_redirects"] is True
    assert r.text == "HTTP 200 https://example.com/a\n\n# Merhaba"
    assert not r.is_error


def test_web_fetch_keeps_explicit_scheme_and_raw(ctx, monkeypatch):
    html_body = "<p>ham</p>"
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, html_body))
    r = web.WebFetch().run(ctx, url="http://x.org", raw=True)
    assert r.text.startswith("HTTP 200 http://x.org")
    assert r.text.endswith(html_body)


def test_web_fetch_json_pretty_not_html_converted(ctx, monkeypatch):
    body = '{"a": "<b>"}'
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, body, ctype="application/json"))
    assert web.WebFetch().run(ctx, url="https://api").text.endswith('{\n "a": "<b>"\n}')


def test_web_fetch_missing_content_type(ctx, monkeypatch):
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "<p>x</p>", ctype=None))
    assert web.WebFetch().run(ctx, url="https://a").text.endswith("<p>x</p>")


def test_web_fetch_http_error_status(ctx, monkeypatch):
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "yok", status=404, ctype="text/plain"))
    r = web.WebFetch().run(ctx, url="https://a/b")
    assert r.is_error and r.text.startswith("HTTP 404")


def test_web_fetch_network_error_becomes_tool_error(ctx, monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("bağlantı reddedildi")

    monkeypatch.setattr(web.httpx, "get", boom)
    with pytest.raises(ToolError, match="indirilemedi: bağlantı reddedildi"):
        web.WebFetch().run(ctx, url="https://a")


def test_web_fetch_truncates_to_config_limit(workdir, monkeypatch):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, tool_output_chars=200))
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "A" * 5000, ctype="text/plain"))
    out = web.WebFetch().run(ctx, url="https://a").text
    assert "karakter kesildi" in out
    assert len(out) < 400


# ------------------------------------------------------------------ ddg_search / WebSearch


def _result(href, title, snippet=None):
    s = f'<a rel="nofollow" class="result__a" href="{href}">{title}</a>'
    if snippet is not None:
        s += f'<div><a class="result__snippet" href="{href}">{snippet}</a></div>'
    return s


DDG_HTML = "<html><body>" + "".join([
    _result("//duckduckgo.com/l/?uddg=" + quote("https://real.example/a?b=1&c=2", safe="") + "&rut=abc",
            "<b>Gerçek</b> &amp; Sonuç", "Bir <b>özet</b> &quot;tırnak&quot;"),
    _result("https://duckduckgo.com/y.js?ad_provider=x", "Reklam", "reklam metni"),
    _result("https://direct.example/", "Doğrudan"),
    _result("https://third.example/", "Üçüncü", "üç"),
]) + "</body></html>"


@pytest.fixture
def ddg(monkeypatch):
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None, follow_redirects=False):
        seen.update(url=url, data=data, headers=headers)
        return _resp(url, seen.get("html", DDG_HTML), status=seen.get("status", 200), method="POST")

    monkeypatch.setattr(web.httpx, "post", fake_post)
    return seen


def test_ddg_search_parses_results(ddg):
    res = web.ddg_search("python test")
    assert ddg["data"] == {"q": "python test"} and ddg["url"].startswith("https://html.duckduckgo.com")
    assert res == [
        {"title": "Gerçek & Sonuç", "url": "https://real.example/a?b=1&c=2", "snippet": 'Bir özet "tırnak"'},
        {"title": "Doğrudan", "url": "https://direct.example/", "snippet": ""},
        {"title": "Üçüncü", "url": "https://third.example/", "snippet": "üç"},
    ]


def test_ddg_search_max_results(ddg):
    assert [r["title"] for r in web.ddg_search("q", max_results=1)] == ["Gerçek & Sonuç"]


def test_ddg_search_empty_page(ddg):
    ddg["html"] = "<html>sonuç yok</html>"
    assert web.ddg_search("q") == []


def test_ddg_search_http_status_raises(ddg):
    ddg["status"] = 503
    with pytest.raises(httpx.HTTPStatusError):
        web.ddg_search("q")


def test_web_search_formats(ctx, ddg):
    t = web.WebSearch()
    assert t.summary({"query": "abc"}) == "abc"
    out = t.run(ctx, query="q", max_results=2).text
    assert out == ("1. Gerçek & Sonuç\nhttps://real.example/a?b=1&c=2\nBir özet \"tırnak\"\n\n"
                   "2. Doğrudan\nhttps://direct.example/\n")


def test_web_search_no_results(ctx, ddg, monkeypatch):
    ddg["html"] = ""
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "<html></html>"))
    assert web.WebSearch().run(ctx, query="q").text == "sonuç yok"


def test_web_search_errors_become_tool_error(ctx, ddg, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    ddg["status"] = 429
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "", status=503))
    with pytest.raises(ToolError, match="arama başarısız"):
        web.WebSearch().run(ctx, query="q")

    def boom(*a, **k):
        raise httpx.ReadTimeout("zaman aşımı")

    monkeypatch.setattr(web.httpx, "post", boom)
    monkeypatch.setattr(web.httpx, "get", boom)
    with pytest.raises(ToolError, match="zaman aşımı"):
        web.WebSearch().run(ctx, query="q")


# ------------------------------------------------------------------ yeni: arama yedekleri, alan filtresi

BING_HTML = ('<ol><li class="b_algo"><h2><a href="https://bing.example/x?a=1&amp;b=2">Bing <b>Bir</b></a></h2>'
             '<div class="b_caption"><p>özet <b>bir</b></p></div></li>'
             '<li class="b_algo"><h2><a href="https://docs.python.org/3/">Python</a></h2><p>doküman</p></li></ol>')


def test_bing_search_parses():
    res = None

    def fake_get(url, params=None, **kw):
        return _resp(url, BING_HTML)

    orig = web.httpx.get
    web.httpx.get = fake_get
    try:
        res = web.bing_search("q")
    finally:
        web.httpx.get = orig
    assert res[0] == {"title": "Bing Bir", "url": "https://bing.example/x?a=1&b=2", "snippet": "özet bir"}
    assert res[1]["url"] == "https://docs.python.org/3/"


def test_web_search_falls_back_to_bing_when_ddg_blocks(ctx, ddg, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    ddg["status"] = 202
    ddg["html"] = "<html>bot kontrolü</html>"
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, BING_HTML))
    out = web.WebSearch().run(ctx, query="q").text
    assert out.startswith("1. Bing Bir")


def test_web_search_domain_filters(ctx, ddg, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "<html></html>"))
    assert "sonuç yok" == web.WebSearch().run(ctx, query="q", allowed_domains=["eal.example"]).text  # parça değil
    out = web.WebSearch().run(ctx, query="q", allowed_domains=["real.example"]).text
    assert ddg["data"] == {"q": "q site:real.example"}
    assert "real.example" in out and "direct.example" not in out
    out = web.WebSearch().run(ctx, query="q", blocked_domains=["real.example"]).text
    assert "real.example" not in out and "direct.example" in out


def test_web_search_uses_brave_when_key_set(ctx, ddg, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    seen = {}

    def fake_get(url, params=None, headers=None, **kw):
        seen.update(url=url, headers=headers)
        return _resp(url, '{"web": {"results": [{"title": "B", "url": "https://b.example", "description": "<b>d</b>"}]}}',
                     ctype="application/json")

    monkeypatch.setattr(web.httpx, "get", fake_get)
    assert web.WebSearch().run(ctx, query="q").text == "1. B\nhttps://b.example\nd"
    assert seen["headers"]["X-Subscription-Token"] == "k"


# ------------------------------------------------------------------ yeni: web_fetch asıl içerik, sayfalama, prompt


def test_html_to_text_main_only_prefers_article():
    raw = "<nav>menü</nav><div>kenar çubuğu</div><article><h1>Yazı</h1><p>" + "içerik " * 50 + "</p></article>"
    out = web.html_to_text(raw, main_only=True)
    assert out.startswith("# Yazı") and "kenar" not in out
    assert "kenar çubuğu" in web.html_to_text(raw)
    assert web.html_to_text("<main>kısa</main><p>diğer</p>", main_only=True) == "kısa\ndiğer"


def test_web_fetch_paginates_with_start(workdir, monkeypatch):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, tool_output_chars=100))
    body = "".join(f"{i:03d}" for i in range(100))  # 300 karakter
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, body, ctype="text/plain"))
    first = web.WebFetch().run(ctx, url="https://a").text
    assert "start=100" in first and body[:100] in first
    second = web.WebFetch().run(ctx, url="https://a", start=100).text
    assert body[100:200] in second and "start=200" in second and "[karakter 100-200 / 300]" in second
    assert "sonunu aşıyor" in web.WebFetch().run(ctx, url="https://a", start=999).text


class _FakeRouter:
    def __init__(self, answer="", fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def chat(self, role, messages, **kw):
        self.calls.append((role, messages))
        if self.fail:
            raise RuntimeError("model yok")
        return type("R", (), {"text": self.answer})()


def test_web_fetch_prompt_uses_fast_model(ctx, monkeypatch):
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "<p>Fiyat 42 TL</p>"))
    ctx.router = _FakeRouter("42 TL")
    out = web.WebFetch().run(ctx, url="https://a", prompt="fiyat ne").text
    assert out == "HTTP 200 https://a\n\n42 TL"
    role, msgs = ctx.router.calls[0]
    assert role == "fast" and "fiyat ne" in msgs[1]["content"] and "Fiyat 42 TL" in msgs[1]["content"]


def test_web_fetch_prompt_falls_back_to_text(ctx, monkeypatch):
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "<p>Fiyat 42 TL</p>"))
    ctx.router = _FakeRouter(fail=True)
    assert web.WebFetch().run(ctx, url="https://a", prompt="fiyat").text.endswith("Fiyat 42 TL")


# ------------------------------------------------------------------ yeni: http_request


def test_http_request_risk_by_method_and_save(ctx):
    from alice.tools.base import DANGER, EXEC, WRITE

    t = web.HttpRequest()
    assert t.risk_for({"url": "x"}) == NETWORK
    assert t.risk_for({"url": "x", "method": "post"}) == EXEC
    assert t.risk_for({"url": "x", "save_to": "a.bin"}) == WRITE
    sys_dir = "C:\\Windows\\x.dll" if __import__("sys").platform == "win32" else "/etc/x"
    assert t.risk_for({"url": "x", "save_to": sys_dir}) == DANGER
    assert t.summary({"url": "https://a", "method": "post"}) == "POST https://a"


def test_http_request_sends_json_and_env_header(ctx, monkeypatch):
    monkeypatch.setenv("ALICE_TEST_TOKEN", "gizli")
    seen = {}

    def fake_request(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return httpx.Response(201, headers={"content-type": "application/json", "x-other": "z"},
                              text='{"ok":true}', request=httpx.Request(method, url))

    monkeypatch.setattr(web.httpx, "request", fake_request)
    r = web.HttpRequest().run(ctx, url="https://api.x/v1", method="post",
                              headers='{"Authorization": "Bearer ${env:ALICE_TEST_TOKEN}"}',
                              json='{"a": 1}', params={"q": "z"})
    assert seen["method"] == "POST" and seen["json"] == {"a": 1} and seen["params"] == {"q": "z"}
    assert seen["headers"]["Authorization"] == "Bearer gizli"
    assert r.text.startswith("HTTP 201 https://api.x/v1\ncontent-type: application/json")
    assert "x-other" not in r.text and r.text.endswith('{\n "ok": true\n}')
    assert "gizli" not in r.text and not r.is_error


def test_http_request_bad_json_and_missing_env(ctx):
    with pytest.raises(ToolError, match="json geçerli JSON değil"):
        web.HttpRequest().run(ctx, url="https://a", json="{bozuk")
    with pytest.raises(ToolError, match="ortam değişkeni yok: YOK_BOYLE"):
        web.HttpRequest().run(ctx, url="https://a", headers='{"X": "${env:YOK_BOYLE}"}')


def test_http_request_download_to_file(ctx, monkeypatch, workdir):
    def handler(request):
        return httpx.Response(200, content=b"\x00\x01veri")

    real_stream = httpx.stream
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(web.httpx, "stream", lambda method, url, **kw: client.stream(method, url, **kw))
    r = web.HttpRequest().run(ctx, url="https://a/f.bin", save_to="ind/f.bin")
    assert "6 byte" in r.text and (workdir / "ind" / "f.bin").read_bytes() == b"\x00\x01veri"
    assert real_stream is not None


# ------------------------------------------------------------------ önbellek (hız)
def test_web_fetch_caches_same_url(ctx, monkeypatch):
    """Aynı sayfa ikinci kez istenince ağa çıkılmaz: ajan aynı dokümana dönüp duruyor."""
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _resp(url, "<html><body>" + "içerik " * 60 + "</body></html>")

    monkeypatch.setattr(web.httpx, "get", fake_get)
    a = web.WebFetch().run(ctx, url="https://x.example/doc").text
    b = web.WebFetch().run(ctx, url="https://x.example/doc").text
    assert a == b and len(calls) == 1
    web.cache_clear()
    web.WebFetch().run(ctx, url="https://x.example/doc")
    assert len(calls) == 2


def test_web_fetch_errors_are_not_cached(ctx, monkeypatch):
    codes = [500, 200]

    def fake_get(url, **kw):
        return _resp(url, "hata" if codes[0] == 500 else "tamam", status=codes.pop(0))

    monkeypatch.setattr(web.httpx, "get", fake_get)
    assert web.WebFetch().run(ctx, url="https://x.example/f").is_error
    assert not web.WebFetch().run(ctx, url="https://x.example/f").is_error


def test_web_search_caches_engine_results(ctx, monkeypatch):
    """Aynı sorgu arka arkaya sorulunca arama motoruna bir kez gidilir."""
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None, follow_redirects=False):
        calls.append(data)
        return _resp(url, DDG_HTML, method="POST")

    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(web.httpx, "post", fake_post)
    first = web.WebSearch().run(ctx, query="aynı sorgu").text
    second = web.WebSearch().run(ctx, query="aynı sorgu").text
    assert first == second and len(calls) == 1
    # Farklı sorgu önbelleğe takılmaz.
    web.WebSearch().run(ctx, query="başka sorgu")
    assert len(calls) == 2
