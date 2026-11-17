"""alice/tools/browser.py: session lifecycle (closed tab, dropped connection, closed ALICE window),
lenient JSON arguments, timeouts and screenshot size. Playwright is replaced with a fake module;
no real browser is opened."""

from __future__ import annotations

import io
import sys
import types
from types import SimpleNamespace

import pytest
from PIL import Image

from alice.tools import browser
from alice.tools.base import COMPUTER, NETWORK, READ, ToolContext, ToolError
from tests.conftest import make_config


def png_bytes(w=3000, h=1500) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 10, 10)).save(buf, "PNG")
    return buf.getvalue()


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    def count(self):
        return 1 if self.sel in self.page.present else 0

    @property
    def first(self):
        return self

    def __getattr__(self, name):
        def call(*a, **k):
            self.page.calls.append((name, self.sel, a, k))
            if name == "evaluate":
                return self.page.eval_result(*a, **k)
            return None

        return call


class FakePage:
    def __init__(self, url="about:blank", title="Boş"):
        self.url, self._title = url, title
        self.closed = False
        self.calls: list[tuple] = []
        self.present = {'[data-alice-id="3"]'}
        self.keyboard = SimpleNamespace(press=lambda k: self.calls.append(("key", k)))
        self.mouse = SimpleNamespace(wheel=lambda dx, dy: self.calls.append(("wheel", dx, dy)))
        self.eval_result = lambda *a, **k: []
        self.goto_errors: list[Exception] = []

    def is_closed(self):
        return self.closed

    def title(self):
        return self._title

    def locator(self, sel):
        return FakeLocator(self, sel)

    def goto(self, url, **kw):
        self.calls.append(("goto", url, kw))
        if self.goto_errors:
            raise self.goto_errors.pop(0)
        self.url = url

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*a, **k):
            self.calls.append((name, a, k))
            if name == "screenshot":
                return png_bytes()

        return call


class FakeCtx:
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else [FakePage()]
        self.handlers: dict[str, list] = {}
        self.closed = False

    def new_page(self):
        p = FakePage()
        self.pages.append(p)
        return p

    def on(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    def emit(self, event, *a):
        for cb in self.handlers.get(event, []):
            cb(*a)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, ctx):
        self.contexts = [ctx]
        self.connected = True

    def is_connected(self):
        return self.connected

    def new_browser_cdp_session(self):
        return SimpleNamespace(send=lambda m: {"targetInfos": [
            {"type": "page", "url": "https://mail.google.com/", "title": "Gelen Kutusu"},
            {"type": "service_worker", "url": "x", "title": "sw"}]})


def make_session(pages=None, attached=False):
    s = object.__new__(browser.BrowserSession)
    s.ctx = FakeCtx(pages)
    s.page = s.ctx.pages[0] if s.ctx.pages else None
    s.attached = attached
    s._browser = FakeBrowser(s.ctx) if attached else None
    s._cdp = None
    s._pw = SimpleNamespace(stop=lambda: None)
    return s


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, screenshot_max_width=800,
                                                       tool_output_chars=4000))


@pytest.fixture
def session(ctx):
    s = make_session()
    ctx.state["browser"] = s
    return s


# ------------------------------------------------------------------ arguments


def test_risk_levels():
    t = browser.Browser()
    for a in ("snapshot", "text", "screenshot", "tabs"):
        assert t.risk_for({"action": a}) == READ
    assert t.risk_for({"action": "goto"}) == NETWORK
    for a in ("click", "type", "eval", "close", "press", "switch_tab"):
        assert t.risk_for({"action": a}) == COMPUTER


def test_submit_string_false_does_not_press_enter(ctx, session):
    page = session.page
    browser.Browser().run(ctx, "type", element_id=3, text="kedi", submit="false")
    assert not any(c[0] == "press" for c in page.calls)  # bool("false") was True: the form was being submitted
    browser.Browser().run(ctx, "type", element_id="3", text="kedi", submit="true")
    assert any(c[0] == "press" and c[2] == ("Enter",) for c in page.calls)
    with pytest.raises(ToolError, match="submit"):
        browser.Browser().run(ctx, "type", element_id=3, text="x", submit="belki")


def test_type_non_string_text(ctx, session):
    browser.Browser().run(ctx, "type", element_id=3, text=1234)
    assert ("fill", '[data-alice-id="3"]', ("1234",), {"timeout": 8000}) in session.page.calls


def test_element_id_required_and_coerced(ctx, session):
    for action in ("click", "type", "select"):
        with pytest.raises(ToolError, match="element_id gerekli"):
            browser.Browser().run(ctx, action)
    with pytest.raises(ToolError, match="element_id sayı olmalı"):
        browser.Browser().run(ctx, "click", element_id="üç")
    with pytest.raises(ToolError, match="öğe bulunamadı"):
        browser.Browser().run(ctx, "click", element_id=7)
    r = browser.Browser().run(ctx, "click", element_id="3")
    assert r.text.startswith("tıklandı: [3]")


def test_select_requires_text(ctx, session):
    with pytest.raises(ToolError, match="select için text"):
        browser.Browser().run(ctx, "select", element_id=3)
    browser.Browser().run(ctx, "select", element_id=3, text="Türkiye")
    assert any(c[0] == "select_option" and c[3]["label"] == "Türkiye" for c in session.page.calls)


def test_scroll_amount_string(ctx, session):
    browser.Browser().run(ctx, "scroll", amount="-300")
    browser.Browser().run(ctx, "scroll", amount=None)
    assert [c for c in session.page.calls if c[0] == "wheel"] == [("wheel", 0, -300), ("wheel", 0, 600)]
    with pytest.raises(ToolError, match="amount sayı olmalı"):
        browser.Browser().run(ctx, "scroll", amount="çok")


def test_switch_tab_index_string(ctx, session):
    second = FakePage("https://github.com/", "GitHub")
    session.ctx.pages.append(second)
    r = browser.Browser().run(ctx, "switch_tab", index="1")  # previously "<=" raised TypeError
    assert session.page is second and r.text.startswith("sekme 1")
    with pytest.raises(ToolError, match="geçersiz sekme index: 5"):
        browser.Browser().run(ctx, "switch_tab", index=5)
    with pytest.raises(ToolError, match="geçersiz sekme index: None"):
        browser.Browser().run(ctx, "switch_tab")


def test_press_eval_text_tabs_back(ctx, session):
    page = session.page
    assert browser.Browser().run(ctx, "press").text.startswith("tuş: Enter")
    with pytest.raises(ToolError, match="js gerekli"):
        browser.Browser().run(ctx, "eval")
    page.eval_result = lambda *a, **k: {"ok": 1}
    assert browser.Browser().run(ctx, "eval", js="return 1;").text == "{'ok': 1}"
    page.eval_result = lambda *a, **k: "x" * 10000
    assert "kesildi" in browser.Browser().run(ctx, "text").text  # tool_output_chars limit
    assert "<- aktif" in browser.Browser().run(ctx, "tabs").text
    page.eval_result = lambda *a, **k: []
    browser.Browser().run(ctx, "back")
    assert ("go_back", (), {"wait_until": "domcontentloaded", "timeout": 30000}) in page.calls
    with pytest.raises(ToolError, match="bilinmeyen action"):
        browser.Browser().run(ctx, "dans")


def test_screenshot_has_timeout_and_is_downscaled(ctx, session):
    r = browser.Browser().run(ctx, "screenshot")
    assert ("screenshot", (), {"type": "png", "timeout": 15000}) in session.page.calls
    import base64

    img = Image.open(io.BytesIO(base64.b64decode(r.images[0])))
    assert img.width == 800 and img.height == 400  # downscaled to screenshot_max_width


# --------------------------------------------------------------- lifecycle


def test_closed_page_switches_to_open_tab(ctx, session):
    other = FakePage("https://github.com/", "GitHub")
    session.ctx.pages.append(other)
    session.page.closed = True  # the user closed the tab
    browser.Browser().run(ctx, "scroll", amount=100)
    assert session.page is other and ("wheel", 0, 100) in other.calls


def test_all_pages_closed_opens_new_tab(ctx, session):
    for p in session.ctx.pages:
        p.closed = True
    b = browser.get_browser(ctx)
    assert b.page is session.ctx.pages[-1] and not b.page.closed and len(session.ctx.pages) == 2


def test_page_is_closed_error_counts_as_closed():
    class Broken:
        def is_closed(self):
            raise RuntimeError("bağlantı yok")

    assert browser._is_closed(Broken()) is True


def test_own_window_closed_reconnects(ctx, monkeypatch):
    old = make_session()
    old.ctx.on("close", old._mark_closed)
    ctx.state["browser"] = old
    assert old.alive()
    old.ctx.emit("close")  # the user closed the ALICE window
    assert not old.alive()
    fresh = make_session()
    made: list[dict] = []
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: made.append(kw) or fresh)
    assert browser.get_browser(ctx) is fresh and ctx.state["browser"] is fresh
    assert made and made[0]["allow_launch"] is False


def test_disconnected_user_browser_reconnects(ctx, monkeypatch):
    old = make_session(attached=True)
    old._browser.connected = False
    ctx.state["browser"] = old
    fresh = make_session()
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: fresh)
    assert browser.get_browser(ctx, separate=True) is fresh


class TargetClosedError(Exception):
    pass


def test_connection_lost_mid_call_reconnects_and_retries_read(ctx, monkeypatch):
    # The Opera connection died during the call: is_connected still says True,
    # but the operation fails with a "closed" error.
    old = make_session(attached=True)
    ctx.state["browser"] = old

    def dead(*a, **k):
        raise TargetClosedError("Target page, context or browser has been closed")

    old.snapshot = dead
    fresh = make_session()
    fresh.snapshot = lambda limit=120: [{"id": 1, "tag": "a", "role": "", "type": "", "text": "Ana sayfa",
                                         "placeholder": "", "value": "", "href": "/", "inView": True}]
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: fresh)
    r = browser.Browser().run(ctx, "snapshot")
    assert "Ana sayfa" in r.text and ctx.state["browser"] is fresh  # it did not stay there: reconnected


def test_connection_lost_on_click_resets_without_repeating(ctx, monkeypatch):
    old = make_session(attached=True)
    ctx.state["browser"] = old
    clicks = []

    def dead(element_id):
        clicks.append(element_id)
        raise TargetClosedError("Browser closed")

    old.locator = dead
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: pytest.fail("tıklama tekrar edilmemeli"))
    with pytest.raises(ToolError, match="bağlantısı koptu"):
        browser.Browser().run(ctx, "click", element_id=3)
    assert clicks == [3] and "browser" not in ctx.state  # the next call sets up a new connection


def test_reconnect_failure_is_explained(ctx, monkeypatch):
    old = make_session(attached=True)
    ctx.state["browser"] = old

    def dead(*a, **k):
        raise TargetClosedError("Connection closed")

    old.snapshot = dead
    again = make_session(attached=True)
    again.snapshot = dead
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: again)
    with pytest.raises(ToolError, match="yeniden bağlanılamadı"):
        browser.Browser().run(ctx, "snapshot")
    assert "browser" not in ctx.state


def test_other_errors_are_not_treated_as_disconnect(ctx, session):
    def boom(*a, **k):
        raise ValueError("beklenmeyen")

    session.snapshot = boom
    with pytest.raises(ValueError):
        browser.Browser().run(ctx, "snapshot")
    assert ctx.state["browser"] is session


def test_alive_handles_errors():
    s = make_session(attached=True)

    def boom():
        raise RuntimeError("x")

    s._browser.is_connected = boom
    assert s.alive() is False


def test_close_action(ctx, session):
    r = browser.Browser().run(ctx, "close")
    assert r.text == "tarayıcı kapatıldı" and session.ctx.closed and "browser" not in ctx.state
    att = make_session(attached=True)
    ctx.state["browser"] = att
    assert "açık bırakıldı" in browser.Browser().run(ctx, "close").text
    assert ctx.state["browser"] is att and not att.ctx.closed


def test_cleanup_swallows_errors(ctx):
    s = make_session()

    def boom():
        raise RuntimeError("zaten kapalı")

    s.ctx.close = boom
    ctx.state["browser"] = s
    browser.cleanup(ctx)
    assert "browser" not in ctx.state


# ------------------------------------------------------------------ goto


def test_goto_adds_scheme_and_retries_aborted(ctx, session):
    page = session.page
    page.goto_errors = [RuntimeError("net::ERR_ABORTED")]
    r = browser.Browser().run(ctx, "goto", url="example.com")
    assert r.text.startswith("açıldı: https://example.com")
    assert [c[1] for c in page.calls if c[0] == "goto"] == ["https://example.com", "https://example.com"]
    page.goto_errors = [RuntimeError("net::ERR_NAME_NOT_RESOLVED")]
    with pytest.raises(RuntimeError, match="NAME_NOT_RESOLVED"):
        browser.Browser().run(ctx, "goto", url="yok.invalid")
    with pytest.raises(ToolError, match="url gerekli"):
        browser.Browser().run(ctx, "goto")


def test_goto_attached_reuses_open_site_tab(ctx, monkeypatch):
    gmail = FakePage("https://mail.google.com/", "Gmail")
    s = make_session([FakePage("https://github.com/"), gmail], attached=True)
    ctx.state["browser"] = s
    monkeypatch.setattr(browser.BrowserSession, "_visible_page", lambda self: None)
    r = browser.Browser().run(ctx, "goto", url="https://www.mail.google.com")
    assert r.text.startswith("zaten açık sekmeye geçildi: Gelen Kutusu")
    assert s.page is gmail and not any(c[0] == "goto" for c in gmail.calls)


def test_goto_root_again_goes_home_in_open_tab(ctx, monkeypatch):
    """Live: YouTube tab on a video, "ana sayfaya geç" (go home) asked; goto only switched tabs, stayed on the video."""
    video = FakePage("https://www.youtube.com/watch?v=K5", "Video")
    s = make_session([video], attached=True)
    ctx.state["browser"] = s
    monkeypatch.setattr(browser.BrowserSession, "_visible_page", lambda self: None)
    r = browser.Browser().run(ctx, "goto", url="youtube.com")
    assert "zaten açık sekmeye geçildi" in r.text and "bir kez daha" in r.text
    assert not any(c[0] == "goto" for c in video.calls)  # does not touch the playing video on the first request
    browser.Browser().run(ctx, "goto", url="youtube.com")
    assert any(c[0] == "goto" for c in video.calls)  # the same request again goes to the home page


def test_tab_for_prefers_same_host_then_blank_then_new():
    s = make_session([FakePage("https://www.youtube.com/watch?v=1"), FakePage("about:blank")])
    assert s.tab_for("https://youtube.com/results") is s.ctx.pages[0]
    s.page = s.ctx.pages[1]
    assert s.tab_for("https://github.com") is s.ctx.pages[1]
    s.page = s.ctx.pages[0]
    new = s.tab_for("https://github.com")
    assert new is s.ctx.pages[-1] and len(s.ctx.pages) == 3


# --------------------------------------------------------------- evaluate


def test_evaluate_wraps_expression_and_times_out(session):
    page = session.page
    seen: list = []
    page.eval_result = lambda fn, arg, timeout: seen.append((fn, arg, timeout)) or 5
    assert session.evaluate("1 + 4;") == 5
    assert seen[-1][0] == "(_el, arg) => (() => (1 + 4))(arg)"
    session.evaluate("(n) => n * 2", 3)
    assert seen[-1][0] == "(_el, arg) => ((n) => n * 2)(arg)" and seen[-1][1] == 3

    class TimeoutError_(Exception):
        pass

    TimeoutError_.__name__ = "TimeoutError"

    def hang(*a, **k):
        raise TimeoutError_("waiting")

    page.eval_result = hang
    with pytest.raises(ToolError, match="10 sn içinde cevap vermedi"):
        session.evaluate("document.title")

    def other(*a, **k):
        raise ValueError("SyntaxError")

    page.eval_result = other
    with pytest.raises(ValueError):
        session.evaluate("document.title")


def test_evaluate_attached_wakes_tab(session):
    session.attached = True
    session.evaluate("1")
    assert any(c[0] == "bring_to_front" for c in session.page.calls)


def test_js_function_detection_kept():
    assert browser.js_function("async () => 1") == "async () => 1"
    assert browser.js_function("x => x") == "x => x"
    assert browser.js_function("(() => 1)()") == "() => ((() => 1)())"


# --------------------------------------------------------------- CDP / title


def test_targets_and_title_of():
    s = make_session([FakePage("https://mail.google.com/", "yerel")], attached=True)
    assert [t["title"] for t in s.targets()] == ["Gelen Kutusu"]
    assert s.title_of(s.page) == "Gelen Kutusu"
    assert s.title_of(FakePage("https://x.com/")) == ""
    own = make_session([FakePage("https://a.com", "A")])
    assert own.targets() == [] and own.title_of(own.page) == "A"


def test_visible_page_matches_foreground_title(monkeypatch):
    from alice.tools import desktop

    gmail = FakePage("https://mail.google.com/")
    s = make_session([FakePage("https://github.com/"), gmail], attached=True)
    monkeypatch.setattr(desktop, "active_title", lambda: "Gelen Kutusu - Opera")
    assert s._visible_page() is gmail
    monkeypatch.setattr(desktop, "active_title", lambda: "")
    assert s._visible_page() is None


# ----------------------------------------------------------- session setup


class FakeChromium:
    def __init__(self, fail_channels=(), fail_all=False):
        self.calls: list[tuple] = []
        self.fail_channels, self.fail_all = fail_channels, fail_all

    def connect_over_cdp(self, endpoint, timeout, headers=None):
        self.calls.append(("cdp", endpoint, timeout, headers))
        return FakeBrowser(FakeCtx([FakePage("https://a.com/")]))

    def launch_persistent_context(self, profile, **kw):
        self.calls.append(("launch", kw.get("channel"), kw.get("executable_path")))
        if self.fail_all or kw.get("channel") in self.fail_channels:
            raise RuntimeError("kanal yok")
        return FakeCtx()


@pytest.fixture
def fake_pw(monkeypatch):
    chromium = FakeChromium()
    stopped: list[bool] = []
    pw = SimpleNamespace(chromium=chromium, stop=lambda: stopped.append(True))
    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: SimpleNamespace(start=lambda: pw)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    monkeypatch.setattr(browser, "default_browser_exe", lambda: None)
    return chromium, stopped


def test_session_attaches_over_cdp(fake_pw, monkeypatch):
    chromium, _ = fake_pw
    monkeypatch.setattr(browser, "user_browser_endpoint", lambda cdp, port: "ws://127.0.0.1:9339/x")
    monkeypatch.setattr(browser.BrowserSession, "_visible_page", lambda self: None)
    s = browser.BrowserSession(allow_launch=False)
    assert s.attached and s.page.url == "https://a.com/" and chromium.calls[0][2] == 60000
    assert "page" in s.ctx.handlers
    s.close()  # the user's browser is not closed
    assert not s.ctx.closed


def test_session_sends_bridge_token_only_to_bridge(fake_pw, monkeypatch):
    """The bridge rejects connections without the key (sandbox programs send no Origin either); ALICE
    sends the key in the header, never to the user's direct CDP port."""
    from alice.tools import browser_bridge as bb

    chromium, _ = fake_pw
    token = bb.write_token()
    monkeypatch.setattr(browser, "user_browser_endpoint", lambda cdp, port: bb.endpoint(9339))
    monkeypatch.setattr(browser.BrowserSession, "_visible_page", lambda self: None)
    browser.BrowserSession(allow_launch=False, bridge_port=9339)
    assert chromium.calls[-1][3] == {bb.TOKEN_HEADER: token}
    monkeypatch.setattr(browser, "user_browser_endpoint", lambda cdp, port: "ws://127.0.0.1:9333/devtools/browser/a")
    browser.BrowserSession(allow_launch=False, bridge_port=9339)
    assert chromium.calls[-1][3] is None


def test_session_launch_falls_back_channels_and_tracks_close(fake_pw, monkeypatch):
    chromium, _ = fake_pw
    chromium.fail_channels = ("chrome",)
    monkeypatch.setattr(browser, "user_browser_endpoint", lambda cdp, port: None)
    s = browser.BrowserSession(headless=True)
    assert [c[1] for c in chromium.calls] == ["chrome", "msedge"]
    assert not s.attached and s.alive()
    s.ctx.emit("close")
    assert not s.alive()
    new = FakePage("https://b.com")
    s.ctx.emit("page", new)
    assert s.page is new


def test_session_start_failure_stops_driver(fake_pw, monkeypatch):
    chromium, stopped = fake_pw
    chromium.fail_all = True
    monkeypatch.setattr(browser, "user_browser_endpoint", lambda cdp, port: None)
    with pytest.raises(RuntimeError):
        browser.BrowserSession(executable="/opt/opera")
    assert stopped == [True]


def test_session_without_playwright(monkeypatch):
    monkeypatch.setattr(browser, "user_browser_endpoint", lambda cdp, port: None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(ToolError, match="playwright kurulu değil"):
        browser.BrowserSession()


# ------------------------------------------------------------- endpoints


def test_user_browser_endpoint_prefers_live_cdp_then_bridge(monkeypatch):
    monkeypatch.setattr(browser, "cdp_alive", lambda e, timeout=0.4: True)
    assert browser.user_browser_endpoint("http://127.0.0.1:9222", 9339) == "http://127.0.0.1:9222"
    monkeypatch.setattr(browser, "cdp_alive", lambda e, timeout=0.4: False)
    monkeypatch.setattr(browser, "devtools_active_endpoint", lambda: "ws://127.0.0.1:9333/devtools/browser/a")
    monkeypatch.setattr(browser, "ensure_bridge", lambda port: True)
    assert browser.user_browser_endpoint(None, 9339).endswith(":9339/devtools/browser/alice-bridge")
    monkeypatch.setattr(browser, "ensure_bridge", lambda port: False)
    assert browser.user_browser_endpoint(None, 9339) == "ws://127.0.0.1:9333/devtools/browser/a"
    assert browser.user_browser_endpoint(None, None) == "ws://127.0.0.1:9333/devtools/browser/a"


def test_ensure_bridge_waits_with_deadline(monkeypatch):
    import subprocess
    import time

    monkeypatch.setattr(browser, "bridge_alive", lambda port, timeout=0.4: False)
    popen: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popen.append(a))
    clock = iter([0.0, 0.1, 99.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock, 99.0))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert browser.ensure_bridge(9339, wait=1.0) is False and popen

    def no_exec(*a, **k):
        raise OSError("yok")

    monkeypatch.setattr(subprocess, "Popen", no_exec)
    assert browser.ensure_bridge(9339) is False
    monkeypatch.setattr(browser, "bridge_alive", lambda port, timeout=0.4: True)
    assert browser.ensure_bridge(9339) is True


def test_dead_ports_are_reported_false():
    assert browser.bridge_alive(1, timeout=0.05) is False
    assert browser._port_open(1, timeout=0.05) is False


def test_format_snapshot():
    items = [{"id": 1, "tag": "input", "role": "", "type": "search", "text": "", "placeholder": "Ara",
              "value": "kedi", "href": "", "inView": True},
             {"id": 2, "tag": "a", "role": "", "type": "", "text": "", "placeholder": "", "value": "",
              "href": "/x", "inView": False}]
    out = browser.format_snapshot("Başlık", "https://a.com", items).splitlines()
    assert out[2] == "[1] input[search] 'Ara' değer='kedi'" and out[3] == "[2] a '/x' (aşağıda)"


def test_kill_playwright_drivers_only_kills_drivers():
    # Real child processes: whatever looks like a Playwright driver is killed, others such as the bridge survive.
    import subprocess
    import time as _t

    pytest.importorskip("psutil")
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    driver = subprocess.Popen(sleep + ["playwright/driver/package/cli.js", "run-driver"])
    bridge = subprocess.Popen(sleep + ["-m", "alice.tools.browser_bridge"])
    try:
        _t.sleep(0.3)
        assert browser.kill_playwright_drivers() == 1
        assert driver.wait(5) is not None and bridge.poll() is None
    finally:
        for p in (driver, bridge):
            if p.poll() is None:
                p.kill()
            p.wait(5)


def test_restart_kills_drivers_before_exit(monkeypatch):
    from alice import updater

    calls = []
    monkeypatch.setattr(browser, "kill_playwright_drivers", lambda: calls.append("kill") or 0)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: calls.append("popen"))
    monkeypatch.setattr(updater.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        updater.restart(["x"], delay=0)
    assert calls == ["popen", "kill"]


# ------------------------------------------------------------------ hung call: watchdog and Stop (Durdur)
def _hangs_until_aborted(sess):
    """In reality, a pending Playwright call returns "Connection closed" when the driver is killed; imitate that."""
    import time

    def hang(*a, **k):
        deadline = time.monotonic() + 5
        while not getattr(sess, "aborted", "") and time.monotonic() < deadline:
            time.sleep(0.01)
        raise TargetClosedError("Page.evaluate: Connection closed while reading from the driver")

    return hang


def test_hung_call_is_cut_by_watchdog_and_retried(ctx, monkeypatch):
    old = make_session(attached=True)
    old.snapshot = _hangs_until_aborted(old)
    ctx.state["browser"] = old
    monkeypatch.setattr(browser, "DEFAULT_BUDGET", 0.1)
    monkeypatch.setattr(browser, "CONNECT_BUDGET", 0.0)
    fresh = make_session()
    fresh.snapshot = lambda limit=120: []
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: fresh)
    r = browser.Browser().run(ctx, "snapshot")
    assert old.aborted == "timeout" and ctx.state["browser"] is fresh and "snapshot" in r.text
    assert not ctx.state["browser_busy"]


def test_hung_click_is_cut_and_not_repeated(ctx, monkeypatch):
    old = make_session(attached=True)
    old.locator = _hangs_until_aborted(old)
    ctx.state["browser"] = old
    monkeypatch.setattr(browser, "DEFAULT_BUDGET", 0.1)
    monkeypatch.setattr(browser, "CONNECT_BUDGET", 0.0)
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: pytest.fail("tıklama tekrar edilmemeli"))
    with pytest.raises(ToolError, match="cevap vermedi"):
        browser.Browser().run(ctx, "click", element_id=3)
    assert "browser" not in ctx.state


def test_stop_cuts_hung_call_without_retry(ctx, monkeypatch):
    import threading

    old = make_session(attached=True)
    old.snapshot = _hangs_until_aborted(old)
    ctx.state["browser"] = old
    monkeypatch.setattr(browser, "BrowserSession", lambda **kw: pytest.fail("durdurulunca yeniden denenmemeli"))
    threading.Timer(0.1, browser.abort_browser, args=(ctx, "stop")).start()
    with pytest.raises(ToolError, match="durduruldu"):
        browser.Browser().run(ctx, "snapshot")
    assert "browser" not in ctx.state


def test_abort_leaves_idle_connection_alone(ctx, session):
    browser.abort_browser(ctx, "stop")  # no call running: leave the link alone so Opera doesn't re-ask permission
    assert not getattr(session, "aborted", "") and session.alive()


def test_dead_bridge_means_dead_session(ctx, monkeypatch):
    # The bridge judged the browser dead and closed, but Playwright still thought it was connected;
    # the next call hung forever (old bug).
    s = make_session(attached=True)
    s._bridge_port = 9339
    monkeypatch.setattr(browser, "bridge_alive", lambda port, timeout=0.4: False)
    assert not s.alive()
    monkeypatch.setattr(browser, "bridge_alive", lambda port, timeout=0.4: True)
    assert s.alive()
