"""App recipes and the `app` tool: registry merging, aliases, flow templates, open paths."""

from __future__ import annotations

import pytest

from alice import apps
from alice.tools.apps import AppTool
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


@pytest.fixture(autouse=True)
def masaustu_yok(monkeypatch):
    """Tests must not depend on apps installed on this machine: by default the desktop version is not installed."""
    monkeypatch.setattr("alice.tools.apps.desktop_available", lambda app: False)


# ------------------------------------------------------------------------------- registry
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
    assert wa.title == "WhatsApp"            # untouched fields come from the built-in registry
    assert wa.flows["send"] == "özel tarif {kime}"
    assert "read" in wa.flows                # other flows are kept
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


# ------------------------------------------------------------------------------- flow template
def test_akis_hedefi_doldurulur():
    wa = apps.find("whatsapp")
    assert wa.fields("send") == ["kime", "mesaj"]
    goal = wa.goal("send", {"kime": "Ahmet", "mesaj": "geliyorum"})
    assert "Ahmet" in goal and "geliyorum" in goal and "{" not in goal


def test_eksik_alan_ve_olmayan_akis():
    wa = apps.find("whatsapp")
    with pytest.raises(ValueError, match="eksik alan: mesaj"):
        wa.goal("send", {"kime": "Ahmet"})
    with pytest.raises(ValueError, match="there is no flow called"):
        wa.goal("ucus", {})


# ------------------------------------------------------------------------------- tool
def test_list_akislari_ve_apileri_yazar(ctx):
    out = AppTool().run(ctx, action="list").text
    assert "whatsapp" in out and "send(kime, mesaj)" in out
    assert "API: youtube_api" in out  # written so the model prefers the official API when one exists


def test_bilinmeyen_uygulama_secenekleri_sayar(ctx):
    with pytest.raises(ToolError, match="no app definition called"):
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
    assert "QR" in out  # the note is shown too, so it knows what to do on first launch


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
    with pytest.raises(ToolError, match="target is required"):
        AppTool().run(ctx, action="open", name="steam_game")


def test_kurulu_uygulama_adiyla_acilir(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: opened.append(t) or "açıldı")
    out = AppTool().run(ctx, action="open", name="discord").text
    assert opened == ["Discord"]
    assert "discord" in out  # API note: use the connection for data tasks


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
    with pytest.raises(ToolError, match="has no defined flow"):
        AppTool().run(ctx, action="flow", name="github_web", flow="x")


def test_risk_list_okuma_digerleri_bilgisayar():
    t = AppTool()
    assert t.risk_for({"action": "list"}) == "read"
    assert t.risk_for({"action": "open"}) == "computer"
    assert t.risk_for({"action": "flow"}) == "computer"


# ------------------------------------------------------------------------------- desktop flows
def test_uygulama_tarifleri_masaustunde_yurur():
    sp = apps.find("spotify")
    assert sp.flow_target == "desktop" and sp.window_title == "Spotify"
    assert apps.find("whatsapp").flow_target == "browser"


def test_pencere_yoksa_acilir_gelince_one_alinir(monkeypatch):
    from alice.tools import apps as appsmod

    windows: list[str] = ["Not Defteri"]
    opened, focused = [], []

    def fake_open(target):
        opened.append(target)
        windows.append("Spotify Premium")  # the window appears once the app opens
        return "açıldı"

    monkeypatch.setattr("alice.tools.desktop.list_windows", lambda: list(windows))
    monkeypatch.setattr("alice.tools.desktop.open_app", fake_open)
    monkeypatch.setattr("alice.tools.desktop.focus_window", lambda t: focused.append(t) or t)
    title = appsmod._ensure_window(apps.find("spotify"), wait=2, sleep=lambda s: None)
    # the full title of the found window is brought to front (a substring could also have matched a browser tab)
    assert opened == ["Spotify"] and title == "Spotify Premium" and focused == ["Spotify Premium"]


def test_pencere_zaten_aciksa_tekrar_acilmaz(monkeypatch):
    from alice.tools import apps as appsmod

    monkeypatch.setattr("alice.tools.desktop.list_windows", lambda: ["Discord | #genel"])
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: pytest.fail("açıkken yeniden açılmamalı"))
    monkeypatch.setattr("alice.tools.desktop.focus_window", lambda t: t)
    assert appsmod._ensure_window(apps.find("discord"), wait=1) == "Discord | #genel"


def test_pencere_gelmezse_anlasilir_hata(monkeypatch):
    from alice.tools import apps as appsmod

    monkeypatch.setattr("alice.tools.desktop.list_windows", lambda: [])
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: "açıldı")
    monkeypatch.setattr("alice.tools.desktop.focus_window", lambda t: t)
    with pytest.raises(ToolError, match="no window titled .* came"):
        appsmod._ensure_window(apps.find("steam"), wait=0.05, sleep=lambda s: None)


def test_masaustu_akisi_desktop_gozlemcisini_kullanir(ctx, monkeypatch):
    seen = {}

    class FakeLoop:
        def __init__(self, observer, decider, writer, min_confidence=0.0, emit=None):
            seen["observer"] = observer

        def run(self, goal, max_steps=20, inputs=None):
            seen["goal"] = goal

            class R:
                def report(self):
                    return "done"

            return R()

    monkeypatch.setattr("alice.reflex.loop.ReflexLoop", FakeLoop)
    monkeypatch.setattr("alice.reflex.loop.make_text_writer", lambda router: None)
    monkeypatch.setattr("alice.reflex.observers.DesktopObserver", lambda c, title: {"window": title})
    monkeypatch.setattr("alice.reflex.observers.BrowserObserver", lambda *a: pytest.fail("tarayıcı değil"))
    monkeypatch.setattr("alice.tools.apps._ensure_window", lambda app, **k: app.window_title)
    ctx.decider = object()
    ctx.router = object()
    AppTool().run(ctx, action="flow", name="spotify", flow="play", args={"ne": "lofi"})
    assert seen["observer"] == {"window": "Spotify"} and "lofi" in seen["goal"]


def test_gecersiz_target_reddedilir(monkeypatch, tmp_path):
    f = tmp_path / "apps.toml"
    f.write_text('[x]\nkind = "app"\ntarget = "uzay"\n', encoding="utf-8")
    monkeypatch.setattr(apps, "user_file", lambda: f)
    assert "x" not in apps.registry()


def test_discord_uygulamasinda_dm_send_sayilir():
    t = AppTool()
    assert t.risk_for({"action": "flow", "name": "discord", "flow": "dm"}) == "send"
    assert t.risk_for({"action": "flow", "name": "discord", "flow": "mute"}) == "computer"


# ------------------------------------------------------------------------------- desktop preference
def test_masaustu_kuruluysa_web_yerine_o_kullanilir(ctx, monkeypatch):
    # WhatsApp Web's session was logged out (asked for a QR code) but the desktop app was open: run the flow there.
    seen = {}

    class FakeLoop:
        def __init__(self, observer, decider, writer, min_confidence=0.0, emit=None):
            seen["observer"] = observer

        def run(self, goal, max_steps=20, inputs=None):
            seen["goal"] = goal

            class R:
                def report(self):
                    return "done"

            return R()

    monkeypatch.setattr("alice.tools.apps.desktop_available", lambda app: app.name == "whatsapp_desktop")
    monkeypatch.setattr("alice.reflex.loop.ReflexLoop", FakeLoop)
    monkeypatch.setattr("alice.reflex.loop.make_text_writer", lambda router: None)
    monkeypatch.setattr("alice.reflex.observers.DesktopObserver", lambda c, title: {"window": title})
    monkeypatch.setattr("alice.tools.apps._ensure_window", lambda app, **k: app.window_title)
    ctx.decider, ctx.router = object(), object()
    out = AppTool().run(ctx, action="flow", name="wp", flow="send", args={"kime": "Que Mal", "mesaj": "selam"}).text
    assert seen["observer"] == {"window": "WhatsApp"} and "Que Mal" in seen["goal"]
    assert "QR" not in out  # the web note is not valid on the desktop
    assert AppTool().risk_for({"action": "flow", "name": "wp", "flow": "send"}) == "send"  # confirmation is still asked

    opened = []
    monkeypatch.setattr("alice.tools.desktop.open_app", lambda t: opened.append(t) or "açıldı")
    AppTool().run(ctx, action="open", name="whatsapp")
    assert opened == ["WhatsApp"]


def test_masaustu_penceresi_tarayici_sekmesiyle_karismaz(monkeypatch):
    from alice.tools import apps as appsmod

    monkeypatch.setattr("alice.tools.desktop.list_windows", lambda: ["(7) WhatsApp - Opera", "(7) WhatsApp"])
    assert appsmod._app_windows(apps.find("whatsapp_desktop")) == ["(7) WhatsApp"]
    monkeypatch.setattr("alice.tools.desktop.list_windows", lambda: ["(7) WhatsApp - Opera"])
    assert appsmod._app_windows(apps.find("whatsapp_desktop")) == []
