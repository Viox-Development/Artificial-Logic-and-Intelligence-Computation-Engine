from __future__ import annotations

import importlib.util

import pytest

from alice.council import Council
from alice.site import find_entry, safe_path, screenshots
from tests.test_council import js, role_of


def test_find_entry_prefers_root_then_known_dirs(tmp_path):
    assert find_entry(tmp_path) == ""
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "index.html").write_text("x", encoding="utf-8")
    assert find_entry(tmp_path) == ""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("w", encoding="utf-8")
    assert find_entry(tmp_path) == "web/index.html"
    (tmp_path / "index.html").write_text("r", encoding="utf-8")
    assert find_entry(tmp_path) == "index.html"


def test_safe_path_blocks_secrets_and_escapes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "index.html").write_text("ok", encoding="utf-8")
    (tmp_path / ".env").write_text("gizli", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    assert safe_path(tmp_path, "a/") == (tmp_path / "a" / "index.html").resolve()
    for bad in (".env", ".git/config", "../x", "a/../../etc/passwd", "node_modules/a.js", "yok.html"):
        assert safe_path(tmp_path, bad) is None


def test_ux_reviewer_gets_screenshots_on_a_vision_model(workdir):
    seen = {}

    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role == "ux":
            seen["images"] = sum(1 for p in messages[-1]["content"] if isinstance(p, dict) and p.get("type") == "image_url") \
                if isinstance(messages[-1]["content"], list) else 0
            return js(vote="approve", reason="görsel iyi")
        return js(vote="approve")

    from alice.agent import Agent, NullUI
    from alice.permissions import Permissions
    from alice.providers.router import Router
    from alice.tools import ToolContext, all_tools
    from tests.conftest import FakeProvider, make_config

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["b:v"]})  # b supports vision
    prov = FakeProvider("x", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    ctx.state["agent"] = Agent(cfg, router, all_tools(), ctx, Permissions("auto"), NullUI(), system_prompt="sys")
    c = Council(ctx, "work", "index.html güncellendi", "a\n" * 10, depth=1, images={"masaüstü": "AAA", "telefon": "BBB"})
    assert "ux" in c.roles and c.model_for("ux")[0] == "vision"
    snap = c.run()
    assert seen["images"] == 2 and snap["screens"] == ["masaüstü", "telefon"]


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="playwright yok")
def test_screenshots_of_a_real_page(tmp_path):
    (tmp_path / "index.html").write_text("<h1 style='color:red'>Merhaba</h1>", encoding="utf-8")
    shots = screenshots(tmp_path)
    if not shots:
        pytest.skip("tarayıcı açılamadı")
    assert set(shots) == {"masaüstü", "telefon"} and all(len(v) > 1000 for v in shots.values())


def test_screenshot_server_hides_secrets_even_when_escaped(tmp_path):
    import httpx

    from alice.site import serve

    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=gizli", encoding="utf-8")
    try:  # Symlinks need privileges on Windows; otherwise that part is skipped
        (tmp_path / "link.txt").symlink_to(tmp_path / ".env")
    except OSError:
        pass
    httpd, base = serve(tmp_path)
    try:
        assert httpx.get(base + "index.html").text == "ok"
        for bad in (".env", "%2eenv", "%2Eenv", "link.txt", ""):
            r = httpx.get(base + bad)
            assert "gizli" not in r.text
    finally:
        httpd.shutdown()
        httpd.server_close()
    if (tmp_path / "link.txt").is_symlink():
        assert safe_path(tmp_path, "link.txt") is None
