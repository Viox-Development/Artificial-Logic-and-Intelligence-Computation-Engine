"""Ayarlar → Kurulum: doctor, modelleri ölç, Google, telefon, DCS, eklentiler, kısayol uygulamadan."""

from __future__ import annotations

import json
import os

from alice.app.setup_panel import Job, SetupPanel, qr_svg
from alice.config import alice_home
from tests.conftest import make_config


class FakeRouter:
    def __init__(self):
        self.reloaded = []

    def reload_auto(self, cache):
        self.reloaded.append(cache)


class FakeSystem:
    def __init__(self):
        self.calls = []

    def action(self, a, d):
        self.calls.append(a)
        return {"ok": True}


def panel():
    return SetupPanel(make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}), FakeRouter(), FakeSystem())


def test_job_reports_progress_result_and_error():
    j = Job()

    def work(job):
        job.say("bir")
        job.say("iki")
        return {"x": 1}

    assert j.start(work)["ok"]
    j._thread.join(5)
    assert j.view()["state"] == "done" and j.view()["lines"] == ["bir", "iki"] and j.view()["result"] == {"x": 1}
    j.start(lambda job: 1 / 0)
    j._thread.join(5)
    assert j.view()["state"] == "error" and "division" in j.view()["error"]


def test_view_and_unknown_actions():
    p = panel()
    v = p.view()
    assert v["google"]["steps"][0]["title"] == "Proje" and not v["google"]["client"]
    assert v["phone"]["enabled"] is False and v["plugins"]["items"] == [] and v["models"]["state"] == "idle"
    assert not p.action("uç", {})["ok"] and not p.action("_run_doctor", {})["ok"]


def test_models_refresh_and_auto(monkeypatch):
    from alice.providers import ranking

    p = panel()

    def fake_refresh(router, emit=lambda s: None, **k):
        emit("3 aday ölçülüyor")
        return {"chains": {"brain": ["groq:x", "gemini:y"]}, "probed_at": "bugün",
                "results": {"a": {"ok": True}, "b": {"ok": False}}}

    monkeypatch.setattr(ranking, "refresh", fake_refresh)
    assert p.action("models_refresh", {})["ok"]
    p.models._thread.join(5)
    m = p.view()["models"]
    assert m["state"] == "done" and m["result"]["working"] == 1 and m["lines"] == ["3 aday ölçülüyor"]
    assert p.router.reloaded
    assert p.action("models_auto", {})["ok"]
    text = (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert 'brain = ["auto", "ollama:qwen3:8b"]' in text and p.cfg.roles["brain"][0] == "auto"


def test_google_client_from_paste_and_download(tmp_path, monkeypatch):
    from alice import setup_google

    p = panel()
    assert not p.action("google_client", {"text": "rastgele"})["ok"]
    assert not p.action("google_client", {"text": "123.apps.googleusercontent.com"})["ok"]  # secret eksik
    assert p.action("google_client", {"text": "123.apps.googleusercontent.com", "secret": "s3"})["ok"]
    assert os.environ["GOOGLE_CLIENT_ID"] == "123.apps.googleusercontent.com" and p.view()["google"]["client"]
    dl = tmp_path / "client_secret_abc.json"
    dl.write_text(json.dumps({"installed": {"client_id": "9.apps.googleusercontent.com", "client_secret": "q"}}))
    monkeypatch.setattr(setup_google, "find_downloaded_json", lambda folder=None: dl)
    assert p.view()["google"]["found_json"] == "client_secret_abc.json"
    assert p.action("google_client", {"use_download": True})["ok"] and os.environ["GOOGLE_CLIENT_SECRET"] == "q"
    opened = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert p.action("google_open", {"step": 1})["ok"] and "calendar" in opened[0]
    assert not p.action("google_open", {"step": 99})["ok"]


def test_phone_toggle_restarts_core_and_shows_qr(monkeypatch):
    from alice.core import service

    p = panel()
    monkeypatch.setattr(service, "read_state", lambda: {"running": True})
    assert p.action("phone_enable", {"on": True})["ok"] and p.system.calls == ["restart"]
    ph = p.view()["phone"]
    assert ph["enabled"] and "/quick?t=" in ph["url"]
    tok = ph["url"].split("t=")[1]
    assert p.action("phone_new_token", {})["ok"] and p.view()["phone"]["url"].split("t=")[1] != tok
    assert p.system.calls == ["restart", "restart"]


def test_qr_svg_when_library_present():
    import importlib.util

    svg = qr_svg("http://192.168.1.5:8765/quick?t=abc")
    if importlib.util.find_spec("qrcode") is None:
        assert svg == ""
    else:
        assert svg.startswith("<svg") and "<rect" in svg


def test_dcs_install_and_kneeboard(tmp_path, monkeypatch):
    from alice.sims import dcs
    from alice.sims import kneeboard as kb

    p = panel()
    monkeypatch.setattr(dcs, "saved_games_dirs", lambda: [])
    assert not p.action("dcs_install", {"on": True})["ok"]
    sg = tmp_path / "DCS"
    sg.mkdir()
    monkeypatch.setattr(dcs, "saved_games_dirs", lambda: [sg])
    assert p.action("dcs_install", {"on": True})["ok"] and p.view()["dcs"]["dirs"][0]["installed"]
    assert p.action("dcs_install", {"on": False})["ok"] and not p.view()["dcs"]["dirs"][0]["installed"]
    monkeypatch.setattr(kb, "render", lambda info, notes: ["sayfa"])
    monkeypatch.setattr(kb, "write", lambda pages, dirs: [tmp_path / "1.png"])
    assert p.action("dcs_kneeboard", {})["pages"] == 1


def test_plugin_template_and_shortcut(monkeypatch):
    from alice import plugins
    from alice.app import setup_panel

    opened = []
    monkeypatch.setattr(setup_panel, "open_file", opened.append)
    p = panel()
    assert not p.action("plugin_new", {"name": "Kötü Ad"})["ok"]
    r = p.action("plugin_new", {"name": "hava-durumu"})
    assert r["ok"] and r["path"].endswith("hava_durumu.py") and opened
    assert "hava_durumu" in (plugins.plugin_dir() / "hava_durumu.py").read_text(encoding="utf-8")
    assert not p.action("plugin_new", {"name": "hava-durumu"})["ok"]  # zaten var
    from alice.app import window

    monkeypatch.setattr(window, "create_shortcut", lambda: (_ for _ in ()).throw(RuntimeError("sadece Windows")))
    assert p.action("shortcut", {})["error"] == "sadece Windows"


def test_doctor_runs_in_background(workdir):
    import httpx

    from alice.app.server import AppServer, AppState
    from tests.conftest import FakeProvider

    c = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(c, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    srv = AppServer(state).start()
    try:
        client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        assert client.get("/api/setup").json()["doctor"]["state"] == "idle"
        assert client.post("/api/setup/action", json={"action": "doctor_run"}).json()["ok"]
        state.setup.doctor._thread.join(60)
        doc = client.get("/api/setup").json()["doctor"]
        assert doc["state"] in ("done", "error"), doc
        if doc["state"] == "done":
            assert isinstance(doc["result"]["providers"], list) and doc["result"]["checks"]
        assert client.post("/api/setup/action", json={"action": "nope"}).status_code == 400
    finally:
        srv.stop()


def test_probe_explains_each_provider_without_false_alarms(monkeypatch):
    # Kullanıcının ekranı: Z.ai "max_tokens'a takıldı", Ollama kırmızı ✗, NVIDIA 180 sn sonra zaman aşımı.
    from alice.doctor import PROBE_TOKENS, probe_providers
    from alice.providers.openai_compat import ProviderError
    from tests.conftest import FakeProvider

    monkeypatch.setenv("ZK", "k")
    monkeypatch.setenv("NK", "k")
    monkeypatch.setenv("SK", "k")
    prov = {
        "zai": {"base_url": "http://z", "api_key_env": "ZK", "models": ["glm-4.7-flash"]},
        "nvidia": {"base_url": "http://n", "api_key_env": "NK", "models": ["glm-5.3"]},
        "ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": "", "models": ["qwen3:8b"]},
        "samba": {"base_url": "http://s", "api_key_env": "SK"},  # anahtar var, model bilinmiyor
        "busy": {"base_url": "http://y", "api_key_env": "", "models": ["m"]},
        "mistral": {"base_url": "http://m", "api_key_env": "MK", "models": ["x"]},  # anahtar yok
    }
    seen = {}

    def script(name):
        def reply(model, messages, tools, **kw):
            seen[name] = kw.get("max_tokens")
            if name == "nvidia":
                return ProviderError("network", "zaman aşımı (ReadTimeout)")
            if name == "ollama":
                return ProviderError("unreachable", "bağlanılamadı (ConnectError)")
            if name == "busy":
                return ProviderError("rate_limit", 'The service may be temporarily overloaded')
            return "ok"
        return reply

    from alice.providers import ranking

    monkeypatch.setattr(ranking, "load_cache", lambda path=None: {})
    rows = {r["name"]: r for r in probe_providers(make_config({"brain": ["auto"]}, prov),
                                                    factory=lambda pc: FakeProvider(pc.name, script(pc.name)))}
    assert rows["zai"]["status"] == "ok" and seen["zai"] == PROBE_TOKENS >= 400  # düşünen model cevaba ulaşsın
    assert rows["nvidia"]["status"] == "fail" and "cevap vermedi" in rows["nvidia"]["detail"]
    assert rows["ollama"]["status"] == "off" and "kurmadıysan sorun değil" in rows["ollama"]["detail"]
    assert rows["samba"]["status"] == "off" and "model listesi alınamadı" in rows["samba"]["detail"]
    assert rows["mistral"]["status"] == "off" and rows["mistral"]["detail"].count("MK") == 1
    assert rows["busy"]["status"] == "warn" and "geçici" in rows["busy"]["detail"]


def test_timed_out_model_is_skipped_for_ten_minutes():
    from alice.providers import router as router_mod
    from alice.providers.openai_compat import ProviderError
    from alice.providers.router import Router
    from tests.conftest import FakeProvider

    calls = []

    def reply(name):
        def r(model, messages, tools, **kw):
            calls.append(name)
            return ProviderError("network", "zaman aşımı (ReadTimeout)") if name == "a" else "ok"
        return r

    rt = Router(make_config({"brain": ["a:m", "b:m"]}), provider_factory=lambda pc: FakeProvider(pc.name, reply(pc.name)),
                max_wait=0)
    rt.chat("brain", [{"role": "user", "content": "x"}])
    import time

    left = rt.cooldown_until["a:m"] - time.monotonic()
    assert left > router_mod.TIMEOUT_COOLDOWN - 5  # 30 sn değil: her istekte zaman aşımını baştan beklemesin
    rt.chat("brain", [{"role": "user", "content": "y"}])
    assert calls == ["a", "b", "b"]


def test_probe_uses_catalog_tries_siblings_and_cheapest_credit_model(monkeypatch):
    # Kullanıcı: "SambaNova'yı, Z.ai'yi, Vercel'i neden kullanamıyorum, neden istek atmıyor?"
    from alice.doctor import probe_providers
    from alice.providers import ranking
    from alice.providers.openai_compat import ProviderError
    from tests.conftest import FakeProvider

    monkeypatch.setattr(ranking, "load_cache", lambda path=None: {})
    for k in ("SK", "ZK", "VK"):
        monkeypatch.setenv(k, "k")
    prov = {
        "samba": {"base_url": "http://s", "api_key_env": "SK"},  # hiç ölçülmemiş, sabit listesi yok
        "zai": {"base_url": "http://z", "api_key_env": "ZK", "models": ["glm-4.7-flash", "glm-4.5-flash"]},
        "vercel": {"base_url": "http://v", "api_key_env": "VK", "auto": False},
    }
    catalogs = {"samba": [{"id": "DeepSeek-V3.1"}, {"id": "Meta-Llama-3.3-70B-Instruct"}, {"id": "Whisper-Large-v3"}],
                "vercel": [{"id": "anthropic/claude-opus-5"}, {"id": "openai/gpt-5-nano"}, {"id": "google/gemini-3-flash"}]}
    asked: list[str] = []

    class P(FakeProvider):
        def list_models(self):
            return catalogs.get(self.name, [])

    def reply(name):
        def r(model, messages, tools, **kw):
            asked.append(f"{name}:{model}")
            if model == "glm-4.7-flash":
                return ProviderError("rate_limit", "The service may be temporarily overloaded")
            return "ok"
        return r

    rows = {r["name"]: r for r in probe_providers(make_config({"brain": ["auto"]}, prov),
                                                    factory=lambda pc: P(pc.name, reply(pc.name)))}
    assert rows["samba"]["status"] == "ok" and rows["samba"]["model"] == "samba:DeepSeek-V3.1"  # katalogdan
    assert rows["zai"]["status"] == "ok" and rows["zai"]["model"] == "zai:glm-4.5-flash"  # yoğun olanın kardeşi
    assert "ilk denenen olmadı" in rows["zai"]["detail"]
    assert rows["vercel"]["status"] == "ok" and rows["vercel"]["model"] == "vercel:openai/gpt-5-nano"  # en ucuz
    assert "kredi" in rows["vercel"]["detail"] and not any("claude" in a for a in asked)


def _seed_cache():
    from alice.providers import ranking

    results = {
        "a:glm-5": {"ref": "a:glm-5", "ok": True, "quality": 90, "latency": 9, "ttft": 4, "tps": 30},
        "b:qwen3.8-27b": {"ref": "b:qwen3.8-27b", "ok": True, "quality": 85, "latency": 1, "ttft": 0.3, "tps": 400},
        "b:yeni-model": {"ref": "b:yeni-model", "ok": True, "quality": 45, "latency": 1, "ttft": 0.3, "tps": 100},
        "a:dead": {"ref": "a:dead", "ok": False, "quality": 80, "error_kind": "not_found"},
    }
    cache = {"probed_at": "2026-10-25T06:00:00", "probed_ts": 0, "results": results, "chains": {}}
    ranking.cache_path().parent.mkdir(parents=True, exist_ok=True)
    ranking.cache_path().write_text(json.dumps(cache), encoding="utf-8")


def test_score_table_and_weights_reorder_chains_without_reprobing():
    from alice.providers import ranking

    _seed_cache()
    p = panel()
    p.router.providers_cfg = make_config({"brain": []}, {"a": {"base_url": "http://a", "api_key_env": ""},
                                                          "b": {"base_url": "http://b", "api_key_env": ""}}).providers()
    v = p.scores_view()
    rows = {r["ref"]: r for r in v["rows"]}
    assert rows["b:yeni-model"]["known"] is False and rows["a:glm-5"]["known"]
    assert rows["a:dead"]["scores"] == {} and set(rows["a:glm-5"]["scores"]) == set(ranking.DEFAULT_WEIGHTS)
    r = p.action("weights_set", {"role": "brain", "weights": {"zeka": 100, "hiz": 0, "tepki": 0, "akis": 0}})
    assert r["ok"] and r["chains"]["brain"][0] == "a:glm-5" and p.router.reloaded  # sadece zekâ
    r = p.action("weights_set", {"role": "brain", "weights": {"zeka": 10, "hiz": 90, "tepki": 0, "akis": 0}})
    assert r["chains"]["brain"][0] == "b:qwen3.8-27b"  # hız önde
    assert ranking.load_weights()["brain"]["hiz"] == 90  # kaydedildi
    assert "b:yeni-model" not in r["chains"]["brain"]  # zekâsı bilinmeyen model ana sıraya girmez
    assert not p.action("weights_set", {"role": "brain", "weights": {"zeka": 0, "hiz": 0, "tepki": 0, "akis": 0, "baglam": 0}})["ok"]
    assert not p.action("weights_set", {"role": "uzay", "weights": {}})["ok"]
    p.action("weights_set", {"role": "brain", "reset": True})
    assert ranking.load_weights()["brain"] == ranking.DEFAULT_WEIGHTS["brain"]


def test_score_table_says_why_a_model_is_not_in_the_chain():
    from alice.providers import ranking

    pcs = make_config({"brain": []}, {"a": {"base_url": "http://a", "api_key_env": "", "max_input_tokens": 128000},
                                      "b": {"base_url": "http://b", "api_key_env": "", "max_input_tokens": 8000}}).providers()
    cache = {"chains": {"brain": ["a:glm-5"]}, "results": {
        "a:glm-5": {"ok": True, "quality": 90, "latency": 2},
        "b:qwen3.8-27b": {"ok": True, "quality": 85, "latency": 1},
        "b:yeni": {"ok": True, "quality": 45, "latency": 1}}}
    rows = {r["ref"]: r for r in ranking.score_table(cache, None, pcs)}
    assert "brain" not in rows["a:glm-5"]["why"]
    assert "bağlamı küçük (8K)" in rows["b:qwen3.8-27b"]["why"]["brain"]
    assert rows["b:yeni"]["why"]["brain"] == "zekâ puanı bilinmiyor"


def test_context_counts_in_score():
    # Kullanıcı: "değerlendirmede context de katılmalı".
    from alice.providers import ranking

    pcs = make_config({"brain": []}, {"big": {"base_url": "http://a", "api_key_env": "", "max_input_tokens": 128000},
                                      "small": {"base_url": "http://b", "api_key_env": "", "max_input_tokens": 8000}}).providers()
    assert ranking.context_of("big:m", pcs, 32000) == 32000 and ranking.context_of("big:m", pcs) == 128000
    assert ranking.context_of("small:m", pcs, 200000) == 8000  # ALICE o sağlayıcıya 8K'dan fazlasını göndermez
    same = {"quality": 85, "latency": 2, "ttft": 0.5, "tps": 100}
    big = ranking.with_context(same, "big:m", pcs)
    small = ranking.with_context(same, "small:m", pcs)
    assert ranking.metric_scores(big)["baglam"] == 100 and ranking.metric_scores(small)["baglam"] == 20
    assert ranking.role_score(big, "brain") > ranking.role_score(small, "brain")
    assert ranking.role_score(big, "voice") == ranking.role_score(small, "voice")  # seste bağlamın ağırlığı 0
    rows = {r["ref"]: r for r in ranking.score_table({"results": {"small:m": {**same, "ok": True}}}, None, pcs)}
    assert rows["small:m"]["context"] == 8000


def test_short_err_turns_raw_json_into_readable_text():
    from alice.doctor import short_err

    pay = ('[quota] {"error":{"balance_units":0,"billing_portal_url":"https://cloud.sambanova.ai/plans/billing",'
           '"code":"PAYMENT_METHOD_REQUIRED","message":"A payment method is required."}}')
    assert short_err(pay).startswith("ödeme yöntemi istiyor")
    busy = '[rate_limit] {"error":{"code":null,"message":"Meta-Llama-3.3-70B-Instruct is currently experiencing high traffic"}}'
    assert short_err(busy) == "hız sınırı/yoğun: Meta-Llama-3.3-70B-Instruct is currently experiencing high traffic"
    assert short_err('[auth] {"error":{"message":"Invalid API key"}}') == "anahtar reddedildi: Invalid API key"
    assert len(short_err("[server] " + "x" * 500)) <= 160 and "{" not in short_err(busy)


def test_payment_required_stops_trying_other_models(monkeypatch):
    from alice.doctor import probe_providers
    from alice.providers import ranking
    from alice.providers.openai_compat import ProviderError
    from tests.conftest import FakeProvider

    monkeypatch.setattr(ranking, "load_cache", lambda path=None: {})
    monkeypatch.setenv("SK", "k")
    asked = []

    def reply(model, messages, tools, **kw):
        asked.append(model)
        return ProviderError("quota", '{"error":{"code":"PAYMENT_METHOD_REQUIRED","message":"A payment method is required"}}')

    prov = {"samba": {"base_url": "http://s", "api_key_env": "SK", "models": ["a", "b", "c"]}}
    row = probe_providers(make_config({"brain": ["auto"]}, prov), factory=lambda pc: FakeProvider(pc.name, reply))[0]
    assert row["status"] == "fail" and row["detail"].startswith("ödeme yöntemi istiyor") and asked == ["a"]
