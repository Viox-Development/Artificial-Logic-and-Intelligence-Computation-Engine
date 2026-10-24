"""API anahtarları: .env'e eksik satırların kendiliğinden eklenmesi ve uygulamadan anahtar girme (Ayarlar → Anahtarlar)."""

from __future__ import annotations

import os

import httpx
import pytest

from alice import config
from alice.app.server import AppServer, AppState
from alice.config import ProviderConfig, env_catalog, mask_secret, read_env, refresh_home, set_env_value
from tests.conftest import FakeProvider, make_config


def test_catalog_lists_template_keys_with_links():
    cat = {e["key"]: e for e in env_catalog()}
    assert cat["GEMINI_API_KEY"]["url"] == "https://aistudio.google.com/apikey"
    assert cat["GEMINI_API_KEY"]["label"] == "Google AI Studio" and cat["GEMINI_API_KEY"]["group"] == "model"
    assert cat["DISCORD_BOT_TOKEN"]["group"] == "connect"
    # açıklamasız satır bir öncekinin parçası: aynı bağlantıyı gösterir
    assert cat["CLOUDFLARE_ACCOUNT_ID"]["url"] == cat["CLOUDFLARE_API_TOKEN"]["url"]
    assert all(config.ENV_KEY_RE.fullmatch(k) for k in cat)


def test_refresh_home_adds_missing_lines_once_and_reports_them(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("# benim notum\nGEMINI_API_KEY=abc\n", encoding="utf-8")
    added = refresh_home()
    assert "GROQ_API_KEY" in added and "GEMINI_API_KEY" not in added
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# benim notum\nGEMINI_API_KEY=abc\n") and "\nGROQ_API_KEY=\n" in text
    assert refresh_home() == []  # ikinci açılışta eklenecek bir şey yok
    assert env.read_text(encoding="utf-8") == text


def test_set_env_value_replaces_appends_and_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "eski")  # testten sonra ortam eski haline dönsün
    monkeypatch.setenv("STEAM_API_KEY", "eski")
    env = tmp_path / ".env"
    env.write_text("# yorum kalsın\nGROQ_API_KEY=\nexport GEMINI_API_KEY=g1\n", encoding="utf-8")
    set_env_value("GROQ_API_KEY", " gsk_yeni ", env)
    set_env_value("GEMINI_API_KEY", "g2", env)
    set_env_value("STEAM_API_KEY", "s1", env)
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# yorum kalsın\nGROQ_API_KEY=gsk_yeni\nGEMINI_API_KEY=g2\n") and text.endswith("STEAM_API_KEY=s1\n")
    assert os.environ["GROQ_API_KEY"] == "gsk_yeni"  # bu süreçte hemen geçerli
    assert read_env(env) == {"GROQ_API_KEY": "gsk_yeni", "GEMINI_API_KEY": "g2", "STEAM_API_KEY": "s1"}
    set_env_value("STEAM_API_KEY", "", env)
    assert "STEAM_API_KEY" not in read_env(env) and "STEAM_API_KEY" not in os.environ
    with pytest.raises(ValueError):
        set_env_value("GROQ_API_KEY", "a\nGEMINI_API_KEY=x", env)  # satır enjeksiyonu
    with pytest.raises(ValueError):
        set_env_value("bad key", "x", env)


def test_mask_secret_never_shows_whole_key():
    assert mask_secret("gsk_1234567890abcd") == "••••abcd"
    assert mask_secret("kisa") == "••••" and mask_secret("") == ""


@pytest.fixture
def app(workdir, monkeypatch):
    from alice.providers import ranking

    probes = []
    monkeypatch.setattr(ranking, "refresh_in_background", lambda router, emit=None, max_age=0: probes.append(max_age))
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client, probes
    srv.stop()


def test_app_keys_tab_saves_without_leaking_value(app, monkeypatch):
    state, client, probes = app
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GROQ_API_KEY")
    state.router.providers_cfg["groq"] = ProviderConfig("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY")
    state.router.dead_providers.add("groq")  # eski yanlış anahtar sağlayıcıyı kapatmıştı
    r = client.post("/api/keys/set", json={"key": "GROQ_API_KEY", "value": "gsk_gizli_1234567890"})
    assert r.status_code == 200 and r.json()["ok"]
    assert os.environ["GROQ_API_KEY"] == "gsk_gizli_1234567890" and "groq" not in state.router.dead_providers
    assert probes == [0]  # yeni sağlayıcı model sırasına girsin diye ölçüm başladı
    body = client.get("/api/keys").text
    assert "gsk_gizli_1234567890" not in body  # anahtarın tamamı tarayıcıya hiç gitmez
    row = next(k for k in client.get("/api/keys").json()["keys"] if k["key"] == "GROQ_API_KEY")
    assert row["set"] and row["source"] == "file" and row["preview"] == "••••7890"
    assert "GROQ_API_KEY=gsk_gizli_1234567890" in (config.alice_home() / ".env").read_text(encoding="utf-8")


def test_app_keys_rejects_unknown_key_and_needs_token(app):
    state, client, _ = app
    r = client.post("/api/keys/set", json={"key": "PATH", "value": "C:\\evil"})
    assert r.status_code == 400 and "bilinmeyen" in r.json()["error"]
    anon = httpx.post(str(client.base_url) + "/api/keys/set", json={"key": "GROQ_API_KEY", "value": "x"}, timeout=5)
    assert anon.status_code in (401, 403)
    assert os.environ.get("GROQ_API_KEY") != "x"


def test_feedback_from_app_lands_in_eval_set(app):
    from alice import evals

    state, client, _ = app
    r = client.post("/api/feedback", json={"say": "hava nasıl", "reply": "Buradayım efendim, nasıl yardımcı olabilirim?",
                                           "note": "soruma cevap vermedi"})
    assert r.json() == {"ok": True, "added": True}
    case = next(c for c in evals.load_cases() if c.say == "hava nasıl")
    text = evals.evals_path().read_text(encoding="utf-8")
    assert case and "beğenilmedi (uygulama)" in text and "soruma cevap vermedi" in text
    assert evals.recent_feedback()[-1]["reply"].startswith("Buradayım")
    assert client.post("/api/feedback", json={"say": "hava nasıl", "reply": "x"}).json()["added"] is False  # tekrar yok


def test_feedback_tool_records(workdir):
    from alice import evals
    from alice.tools.base import ToolContext
    from alice.tools.meta import Feedback

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    out = Feedback().run(ctx, note="çok uzun konuştu", say="saat kaç", reply="Saat beş, ve bu arada...")
    assert "test setine eklendi" in out.text and any(c.say == "saat kaç" for c in evals.load_cases())


def test_voice_tab_lists_previews_and_sets_voice(app, monkeypatch):
    import base64

    from alice.voice import tts as tts_mod
    from alice.voice.audio import Audio

    state, client, _ = app
    view = client.get("/api/voice").json()
    assert view["live"] == "Kore" and len(view["voices"]) == 30 and not view["can_preview"]
    assert client.post("/api/voice/preview", json={"voice": "Puck"}).json()["error"].startswith("örnek için Gemini")
    made = []

    class FakeTTS:
        def synth(self, text, emotion="calm"):
            made.append(text)
            return Audio(b"\0\0" * 2400, 24000)

    monkeypatch.setattr(tts_mod, "make_cloud", lambda cfg, voice=None: FakeTTS())
    for _ in range(2):
        r = client.post("/api/voice/preview", json={"voice": "Puck"}).json()
        assert base64.b64decode(r["audio"])[:4] == b"RIFF"
    assert len(made) == 1  # ikinci dinleyiş önbellekten
    assert client.post("/api/voice/preview", json={"voice": "Yok"}).status_code == 400
    assert client.post("/api/voice/set", json={"voice": "Sulafat"}).json()["ok"]
    from alice.config import alice_home

    text = (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert 'live_voice = "Sulafat"' in text and 'tts_cloud = "gemini:Sulafat"' in text
    assert client.get("/api/voice").json()["live"] == "Sulafat"


def test_live_picks_up_voice_change_without_restart(monkeypatch):
    from types import SimpleNamespace

    from alice.config import alice_home, set_config_value
    from alice.voice.live import LiveSession

    s = LiveSession(SimpleNamespace(data={"voice": {"live_voice": "Kore"}}), SimpleNamespace())
    assert s.current_voice() == "Kore"
    set_config_value("voice", "live_voice", "Achernar")
    import os
    import time as _t

    os.utime(alice_home() / "config.toml", (_t.time() + 5,) * 2)  # dosya sistemi saat çözünürlüğü
    assert s.current_voice() == "Achernar"


def test_app_checks_for_update_right_after_opening_and_rechecks_pending_ci(app, monkeypatch):
    # Kullanıcı: "güncelleme yok diyor, açtım ama". Açılışta bakılmadıysa saat dolmadan hiç bakılmıyordu;
    # CI sürerken de bir saat bekleniyordu.
    import threading
    import time as _t

    from alice import updater
    from alice.app import server as server_mod

    state, _client, _ = app
    calls = []
    infos = iter([updater.UpdateInfo(local="a" * 40, remote="b" * 40, ci="pending")] * 2
                 + [updater.UpdateInfo(local="a" * 40, remote="b" * 40, ci="green")] * 50)
    monkeypatch.setattr(updater, "check", lambda cfg=None, **k: calls.append(1) or next(infos))
    monkeypatch.setattr(updater, "due", lambda cfg=None: False)  # saatlik tur gelmedi
    monkeypatch.setattr(updater, "CI_RECHECK", 0.0)
    real_sleep = _t.sleep
    monkeypatch.setattr(server_mod.time, "sleep", lambda s: real_sleep(0.01) if threading.current_thread().name
                        == "alice-update" else real_sleep(s))
    state.watch_updates(None)
    deadline = _t.monotonic() + 3
    while _t.monotonic() < deadline and (state.update is None or state.update.ci != "green"):
        real_sleep(0.02)
    assert len(calls) >= 3 and state.update.ci == "green" and state.update.can_auto
