from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from alice.design_check import critique, lint, report
from alice.team import Team, team_compact_cap
from tests.test_team import make

SLOP = """<!doctype html><html><head><meta charset="utf-8"><style>.x{text-shadow:0 0 20px #fc0}</style></head><body>
<header><a href="#">AETHERIS</a><span>SAVUNMA // HAVACILIK</span></header>
<p>AETH-SYS // BRİFİNG-01</p><h1>Otonom Üstünlük.</h1><p>Geleceği şekillendiren yeni nesil teknolojiler.</p>
<a class="btn" href="#">&gt; PLATFORMLARI İNCELE</a>
<div><b>128.400+</b> saat <b>640+</b> sistem <b>99,2%</b></div>
<figcaption>ŞEKİL 01 — ÇİZİM NO: AETH-V1-SCH-001</figcaption></body></html>"""

CLEAN = """<!doctype html><meta charset="utf-8"><h1>Kartal Havacılık</h1>
<p>1994'ten beri Ankara'da insansız hava aracı üretiyoruz; 620 mühendisimiz var.</p><a href="x">Ürünleri gör</a>
<style>h1{font-family:Georgia;font-size:64px}</style>"""


def test_lint_finds_ai_template_patterns(tmp_path):
    (tmp_path / "index.html").write_text(SLOP, encoding="utf-8")
    rules = {h.rule for h in lint(tmp_path)}
    assert {"slash_labels", "prompt_buttons", "stat_counters", "fake_annotations", "buzzwords"} <= rules
    text = report(lint(tmp_path))
    assert "Fix:" in text and "index.html" in text


def test_lint_is_quiet_on_a_plain_site(tmp_path):
    (tmp_path / "index.html").write_text(CLEAN, encoding="utf-8")
    assert lint(tmp_path) == [] and report([]) == ""


def test_lint_skips_dependencies_and_hidden_folders(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.html").write_text(SLOP, encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "y.html").write_text(SLOP, encoding="utf-8")
    assert lint(tmp_path) == []


def test_critique_sends_screenshots_to_a_vision_chain(monkeypatch):
    from alice.providers import picker

    seen = {}

    class R:
        role_overrides: dict = {}

        def chat(self, role, messages, **kw):
            seen["role"], seen["chain"] = role, self.role_overrides.get(role)
            seen["parts"] = messages[0]["content"]
            return SimpleNamespace(text="İlk izlenim: şablon gibi. Puan 3/10.")

    monkeypatch.setattr(picker, "chain_for", lambda router, domain, fallback="brain": ["g:vision-model"])
    r = R()
    out = critique(r, {"masaüstü": "AAAA", "telefon": "BBBB"}, "savunma sitesi", "Renk: kömür", "- sayaç satırı")
    assert out.startswith("İlk izlenim") and seen["chain"] == ["g:vision-model"]
    assert sum(p["type"] == "image_url" for p in seen["parts"]) == 2
    assert "sayaç satırı" in seen["parts"][0]["text"] and "design-critic" not in r.role_overrides
    assert critique(r, {}, "x") == ""


def test_design_project_members_get_design_check_and_reviews_see_patterns(workdir, monkeypatch):
    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    ctx.state["agent"] = agent
    web = Team(ctx, "Savunma firması için tanıtım web sitesi", council=False)
    (workdir / "index.html").write_text(SLOP, encoding="utf-8")
    m = web.add({"name": "anasayfa", "task": "index.html yaz"})
    a = web._make_agent(m)
    assert "design_check" in a.tools and "design_check" in web.brief(m)
    assert a.compact_cap == team_compact_cap(ctx.config) == 128000
    m.files = ["index.html"]
    assert "Automatic pattern check" in web._review_note(m)
    assert "Geleceği şekillendiriyoruz" in web.lead_brief() and "128.400+" in web.lead_brief()

    import alice.design_check as dc
    import alice.site as site

    monkeypatch.setattr(site, "screenshots", lambda root, entry="": {"masaüstü": "AAAA"})
    monkeypatch.setattr(dc, "critique", lambda router, shots, goal, design="", lint_text="", **kw: "Puan 2/10: şablon.")
    res = a.tools["design_check"].run(a.ctx)
    assert "Pattern check" in res.text and "Puan 2/10" in res.text and res.images == ["AAAA"]
    assert any(x["kind"] == "design" for x in web.timeline)

    other = Team(ctx, "Ödeme servisini yeniden düzenle", council=False)
    o = other.add({"name": "api", "task": "servis"})
    assert "design_check" not in other._make_agent(o).tools and other.slop_report() == ""


def test_compact_cap_limits_auto_compaction(workdir):
    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    auto = agent.compact_at
    agent.compact_cap = 20000
    assert agent.compact_at == min(auto, 20000)
    agent.compact_at = 90000
    assert agent.compact_at == 20000
    agent.compact_cap = 0
    assert agent.compact_at == 90000


def test_preview_server_declares_utf8(tmp_path):
    import httpx

    from alice.site import serve

    (tmp_path / "index.html").write_text("<p>ğüşiöç</p>", encoding="utf-8")
    httpd, base = serve(tmp_path)
    try:
        r = httpx.get(base + "index.html")
        assert "utf-8" in r.headers["content-type"] and "ğüşiöç" in r.text
    finally:
        httpd.shutdown()
        httpd.server_close()


def _browser() -> str:
    from alice.site import browser_exe

    pw = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    return str(pw) if pw.is_file() else browser_exe()


@pytest.mark.skipif(not _browser(), reason="Chromium tabanlı tarayıcı yok")
def test_cli_screenshots_without_playwright(tmp_path, monkeypatch):
    from alice.site import cli_screenshots

    monkeypatch.setenv("ALICE_CHROMIUM", _browser())
    (tmp_path / "index.html").write_text("<meta charset=utf-8><h1 style='color:red'>Merhaba</h1>", encoding="utf-8")
    shots = cli_screenshots(tmp_path, timeout=60)
    if not shots:
        pytest.skip("tarayıcı açılamadı")
    assert set(shots) == {"masaüstü", "telefon"}
    assert base64.b64decode(shots["telefon"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_site_study_saves_references_and_feeds_comparisons(workdir, monkeypatch):
    import alice.design_check as dc
    import alice.site as site
    from alice.team import research_refs

    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    ctx.state["agent"] = agent
    web = Team(ctx, "Savunma firması için tanıtım web sitesi", council=False)
    m = web.add({"name": "tasarim", "task": "DESIGN.md yaz"})
    a = web._make_agent(m)
    assert "site_study" in a.tools and "site_study" in web.brief(m)
    assert "RESEARCH" in web.lead_brief() and "site_study" in web.lead_brief()
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 2000).decode()
    seen = {}
    monkeypatch.setattr(site, "reachable", lambda url, timeout=12.0: (True, "https://www.saab.com/"))

    def cap(url, timeout=20.0, settle=600, viewports=None):
        seen["viewports"] = viewports
        return {k: png for k in viewports}

    monkeypatch.setattr(site, "capture", cap)
    monkeypatch.setattr(dc, "study", lambda router, shots, url, goal, note="": "Genel his: sakin, büyük fotoğraf.")
    res = a.tools["site_study"].run(a.ctx, url="saab.com")
    assert not res.is_error and "Genel his" in res.text and len(res.images) == 2
    assert set(seen["viewports"]) == {"masaüstü", "uzun", "telefon"}
    assert (workdir / "research" / "saab-com-masaustu.png").is_file()
    assert "büyük fotoğraf" in (workdir / "research" / "saab-com.md").read_text(encoding="utf-8")
    assert list(research_refs(workdir)) == ["referans-saab-com"]
    assert a.tools["site_study"].run(a.ctx, url="http://127.0.0.1:8000").is_error

    monkeypatch.setattr(site, "reachable", lambda url, timeout=12.0: (False, "HTTP 403"))
    assert "couldn't be opened" in a.tools["site_study"].run(a.ctx, url="https://x.com").text


def test_critical_check_needs_whole_words():
    assert not Team._critical("Uydurma bir savunma firması için paragraf ve parallax içeren site")
    assert Team._critical("Ödeme sayfasını yaz") and Team._critical("login için auth ekle")


def test_leftover_browser_processes_using_the_temp_profile_are_killed(tmp_path):
    """Windows: Chrome's child processes outlived the parent and kept the temp profile locked (WinError 32)."""
    import subprocess
    import sys

    from alice.site import _kill_profile_procs

    profile = str(tmp_path / "profile0")
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    child = subprocess.Popen([*sleeper, f"--user-data-dir={profile}"])
    other = subprocess.Popen([*sleeper, "--user-data-dir=/somewhere/else"])
    try:
        assert _kill_profile_procs(str(tmp_path)) == 1
        assert child.wait(5) is not None
        assert other.poll() is None  # unrelated processes are left alone
    finally:
        for p in (child, other):
            if p.poll() is None:
                p.kill()
                p.wait(5)
