"""Uygulama tarifleri ve `app` aracı: kayıt birleştirme, takma adlar, akış şablonları, açma yolları."""

from __future__ import annotations

import pytest

from alice import apps
from alice.tools.apps import AppTool
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


# ------------------------------------------------------------------------------- kayıt
def test_yerlesik_kayit_tutarli():
    reg = apps.registry()
    assert {"whatsapp", "instagram", "tiktok", "youtube", "steam_game"} <= set(reg)
    for a in reg.values():
        assert a.kind in apps.KINDS
        if a.kind == "web":
            assert a.url.startswith("https://")
        if a.kind == "protocol":
            assert "://" in a.url


def test_takma_adlar_ayni_yere_gider():
    assert apps.find("wp").name == "whatsapp"
    assert apps.find("WhatsApp").name == "whatsapp"
    assert apps.find("insta").name == "instagram"
    assert apps.find("yokbyle") is None


def test_kullanici_kaydi_alan_alan_ezer(monkeypatch, tmp_path):
    f = tmp_path / "apps.toml"
    f.write_text('[whatsapp]\nurl = "https://ozel.example"\n[whatsapp.flows]\nsend = "özel tarif {kime}"\n'
                 '[benimsite]\nkind = "web"\nurl = "https://benim.example"\n', encoding="utf-8")
    monkeypatch.setattr(apps, "user_file", lambda: f)
    reg = apps.registry()
    wa = reg["whatsapp"]
    assert wa.url == "https://ozel.example"
    assert wa.title == "WhatsApp"            # dokunulmayan alan yerleşikten gelir
    assert wa.flows["send"] == "özel tarif {kime}"
    assert "read" in wa.flows                # diğer akışlar korunur
    assert reg["benimsite"].url == "https://benim.example"


def test_bozuk_kullanici_kaydi_digerlerini_dusurmez(monkeypatch, tmp_path):
    f = tmp_path / "apps.toml"
    f.write_text('[x]\nkind = "uçak"\nurl = "https://a"\n', encoding="utf-8")
    monkeypatch.setattr(apps, "user_file", lambda: f)
    reg = apps.registry()
    assert "x" not in reg and "whatsapp" in reg


def test_bozuk_toml_sessizce_atlanir(monkeypatch, tmp_path):
    f = tmp_path / "apps.toml"
    f.write_text("bu toml değil ===", encoding="utf-8")
    monkeypatch.setattr(apps, "user_file", lambda: f)
    assert "whatsapp" in apps.registry()


# ------------------------------------------------------------------------------- akış şablonu
def test_akis_hedefi_doldurulur():
    wa = apps.find("whatsapp")
    assert wa.fields("send") == ["kime", "mesaj"]
    goal = wa.goal("send", {"kime": "Ahmet", "mesaj": "geliyorum"})
    assert "Ahmet" in goal and "geliyorum" in goal and "{" not in goal


def test_eksik_alan_ve_olmayan_akis():
    wa = apps.find("whatsapp")
    with pytest.raises(ValueError, match="eksik alan: mesaj"):
        wa.goal("send", {"kime": "Ahmet"})
    with pytest.raises(ValueError, match="diye bir akış yok"):
        wa.goal("ucus", {})


# ------------------------------------------------------------------------------- araç
def test_list_akislari_ve_apileri_yazar(ctx):
    out = AppTool().run(ctx, action="list").text
    assert "whatsapp" in out and "send(kime, mesaj)" in out
    assert "API: youtube_api" in out  # resmî API varsa model onu tercih etsin diye yazılır


def test_bilinmeyen_uygulama_secenekleri_sayar(ctx):
    with pytest.raises(ToolError, match="tanımlı uygulama tanımı yok|diye bir uygulama tanımı yok"):
        AppTool().run(ctx, action="open", name="snapchat")


def test_web_uygulamasi_kullanicinin_tarayicisinda_acilir(ctx, monkeypatch):
    seen = {}

    class FakeBrowser:
        def run(self, c, action, url):
            seen.update(action=action, url=url)
            from alice.tools.base import ToolResult

            return ToolResult(f"açıldı: {url}")

    monkeypatch.setattr("alice.tools.browser.Browser", FakeBrowser)
    out = AppTool().run(ctx, action="open", name="wp").text
    assert seen == {"action": "goto", "url": "https://web.whatsapp.com"}
    assert "QR" in out  # not da gösterilir: ilk açılışta ne yapması gerektiğini bilsin


def test_tarayiciya_baglanilamazsa_varsayilan_tarayici(ctx, monkeypatch):
    class Patlayan:
        def run(self, *a, **k):
            raise RuntimeError("bağlanamadı")

    opened = []
    monkeypatch.setattr("alice.tools.browser.Browser", Patlayan)
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: opened.append(t) or f"açıldı: {t}")
    out = AppTool().run(ctx, action="open", name="tiktok").text
    assert opened == ["https://www.tiktok.com"] and "açıldı" in out


def test_protokol_derin_baglantisi(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: opened.append(t) or "başlatıldı")
    AppTool().run(ctx, action="open", name="steam_game", target="238960")
    assert opened == ["steam://rungameid/238960"]
    with pytest.raises(ToolError, match="target gerekli"):
        AppTool().run(ctx, action="open", name="steam_game")


def test_kurulu_uygulama_adiyla_acilir(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: opened.append(t) or "açıldı")
    out = AppTool().run(ctx, action="open", name="discord").text
    assert opened == ["Discord"]
    assert "discord" in out  # API notu: veri işleri için bağlantıyı kullan


def test_flow_reflex_e_dolu_hedef_gonderir(ctx, monkeypatch):
    seen = {}

    class FakeLoop:
        def __init__(self, observer, decider, writer, min_confidence=0.0, emit=None):
            seen["observer"] = observer

        def run(self, goal, max_steps=20, inputs=None):
            seen.update(goal=goal, max_steps=max_steps)

            class R:
                def report(self):
                    return "done: mesaj gönderildi"

            return R()

    monkeypatch.setattr("alice.reflex.loop.ReflexLoop", FakeLoop)
    monkeypatch.setattr("alice.reflex.loop.make_text_writer", lambda router: None)
    monkeypatch.setattr("alice.reflex.observers.BrowserObserver", lambda c, url: {"url": url})
    ctx.decider = object()
    ctx.router = object()
    out = AppTool().run(ctx, action="flow", name="whatsapp", flow="send",
                        args={"kime": "Ahmet", "mesaj": "selam"}, max_steps=12).text
    assert "Ahmet" in seen["goal"] and "selam" in seen["goal"] and seen["max_steps"] == 12
    assert seen["observer"] == {"url": "https://web.whatsapp.com"}
    assert "done" in out


def test_flow_eksik_alanda_calismaz(ctx, monkeypatch):
    ctx.decider = object()
    ctx.router = object()
    monkeypatch.setattr("alice.reflex.loop.ReflexLoop",
                        lambda *a, **k: pytest.fail("eksik alanla reflex başlatılmamalı"))
    with pytest.raises(ToolError, match="eksik alan"):
        AppTool().run(ctx, action="flow", name="whatsapp", flow="send", args={"kime": "Ahmet"})


def test_akissiz_uygulamada_flow_yonlendirir(ctx):
    ctx.decider = object()
    ctx.router = object()
    with pytest.raises(ToolError, match="tanımlı akış yok"):
        AppTool().run(ctx, action="flow", name="steam", flow="x")


def test_risk_list_okuma_digerleri_bilgisayar():
    t = AppTool()
    assert t.risk_for({"action": "list"}) == "read"
    assert t.risk_for({"action": "open"}) == "computer"
    assert t.risk_for({"action": "flow"}) == "computer"
