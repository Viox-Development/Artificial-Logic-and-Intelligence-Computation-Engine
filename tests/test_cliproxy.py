"""Claude Code proxy (CLIProxyAPI) management: account list, sign-in link, account removal and HTTP endpoints."""

from __future__ import annotations

import httpx
import pytest

from alice.app import cliproxy
from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config


@pytest.fixture
def proxy_home(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    home.mkdir()
    (home / "config.yaml").write_text("server:\n  port: 8317\n", encoding="utf-8")
    (home / "keys.txt").write_text("api_key=test-client\nmanagement_key=test-yonetim\n", encoding="utf-8")
    (home / cliproxy.binary_name()).write_bytes(b"")
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    return home


def test_status_lists_accounts_with_quota_and_cooldown(proxy_home, monkeypatch):
    files = [
        {
            "id": "claude-abc-hayate@example.com.json",
            "provider": "claude",
            "email": "hayate@example.com",
            "status": "active",
            "disabled": False,
            "access_token_expires_at": "2026-12-06T11:15:49Z",
            "quota": {"signals": {"Anthropic-Ratelimit-Unified-5h-Utilization": "0.0",
                                  "Anthropic-Ratelimit-Unified-5h-Reset": "1793983800"}},
            "cooldowns": [],
            "failed": 0,
        },
        {
            "id": "codex-hayate@example.com.json",
            "provider": "codex",
            "email": "hayate@example.com",
            "status": "active",
            "quota": {"signals": {"X-Codex-Primary-Used-Percent": "46",
                                  "X-Codex-Primary-Reset-At": "1796553389"}},
            "cooldowns": [{"model": "gpt-5.6-terra", "until": 9999999999}],
            "failed": 1,
        },
    ]
    monkeypatch.setattr(cliproxy, "_req", lambda method, path: {"files": files})
    d = cliproxy.status()
    assert d["running"] and d["installed"] and not d["error"]
    assert [a["provider"] for a in d["accounts"]] == ["claude", "codex"]
    claude, codex = d["accounts"]
    assert claude["provider_name"] == "Claude (Pro / Max)"
    assert claude["quota"]["windows"] == [{"label": "5 hours", "percent": 0, "reset": 0, "stale": True}]  # stale
    assert codex["quota"]["percent"] == 46
    assert codex["cooldown"]  # in cooldown
    assert codex["failed"] == 1
    assert {"key": "claude", "name": "Claude (Pro / Max)"} in d["providers"]


def test_status_does_not_crash_when_proxy_closed(proxy_home, monkeypatch):
    def boom(method, path):
        raise RuntimeError("bağlantı yok")

    monkeypatch.setattr(cliproxy, "_req", boom)
    d = cliproxy.status()
    assert d["installed"] and not d["running"] and d["error"]  # no crash: tries to start it, else reports


def test_status_marks_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(cliproxy, "home", lambda: tmp_path / "yok")
    d = cliproxy.status()
    assert not d["installed"] and not d["running"]


def test_login_url_starts_oauth_and_reports_state(proxy_home, monkeypatch):
    seen = {}

    def fake(method, path):
        seen["method"], seen["path"] = method, path
        return {"status": "ok", "url": "https://claude.ai/oauth/authorize?x=1", "state": "abc123"}

    monkeypatch.setattr(cliproxy, "_req", fake)
    r = cliproxy.login_url("claude")
    assert r == {"ok": True, "url": "https://claude.ai/oauth/authorize?x=1", "state": "abc123",
                 "provider": "claude"}
    assert seen["path"].endswith("provider=claude&is_webui=1")
    assert cliproxy.login_url("bilinmeyen")["ok"] is False


def test_login_state_polls(proxy_home, monkeypatch):
    monkeypatch.setattr(cliproxy, "_req", lambda method, path: {"status": "ok", "error": ""})
    assert cliproxy.login_state("abc")["status"] == "ok"
    assert cliproxy.login_state("")["ok"] is False


def test_remove_validates_name_and_calls_delete(proxy_home, monkeypatch):
    seen = {}

    def fake(method, path):
        seen["method"], seen["path"] = method, path
        return {"status": "ok"}

    monkeypatch.setattr(cliproxy, "_req", fake)
    invalid_path = cliproxy.remove("../../etc/passwd")
    assert invalid_path["ok"] is False
    invalid_name = cliproxy.remove("not-json")
    assert invalid_name["ok"] is False
    removed = cliproxy.remove("claude-abc-a@b.c.json")
    assert removed["ok"] is True
    assert seen["method"] == "DELETE" and "name=claude-abc-a%40b.c.json" in seen["path"]


@pytest.fixture
def app_state(tmp_path):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    return AppState(cfg, tmp_path, "auto", provider_factory=lambda pc: FakeProvider("a", []))


def test_http_endpoints(app_state, monkeypatch):
    monkeypatch.setattr(cliproxy, "status", lambda: {"installed": True, "running": True,
                                                     "accounts": [{"email": "a@b.c"}], "error": ""})
    monkeypatch.setattr(cliproxy, "login_url", lambda provider: {"ok": True, "url": "https://x", "state": "s"})
    monkeypatch.setattr(cliproxy, "login_state", lambda state: {"ok": True, "status": "wait"})
    monkeypatch.setattr(cliproxy, "remove", lambda name: {"ok": name.endswith(".json")})
    srv = AppServer(app_state).start()
    try:
        c = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": app_state.token}, timeout=10)
        d = c.get("/api/cliproxy").json()
        assert d["running"] and d["accounts"][0]["email"] == "a@b.c"
        assert c.post("/api/cliproxy/login", json={"provider": "claude"}).json()["url"] == "https://x"
        assert c.post("/api/cliproxy/login/status", json={"state": "s"}).json()["status"] == "wait"
        assert c.post("/api/cliproxy/remove", json={"name": "a.json"}).json()["ok"] is True
        assert httpx.get(srv.url.split("/?")[0] + "/api/cliproxy").status_code == 403
    finally:
        srv.stop()

def test_start_noop_when_running(proxy_home, monkeypatch):
    monkeypatch.setattr(cliproxy, "running", lambda: True)
    assert cliproxy.start() == {"ok": True, "already": True}


def test_start_skips_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(cliproxy, "home", lambda: tmp_path / "yok")
    r = cliproxy.ensure_started()
    assert r["ok"] and r["skipped"] == "not installed"


def test_start_launches_detached_process(proxy_home, monkeypatch):

    checks = {"n": 0}

    def fake_running():
        checks["n"] += 1
        return checks["n"] > 2  # closed, closed -> start -> open

    monkeypatch.setattr(cliproxy, "running", fake_running)
    monkeypatch.setattr(cliproxy, "_last_start", 0.0)
    spawned = {}

    class Popen:
        def __init__(self, args, **kw):
            spawned["args"], spawned["kw"] = args, kw

    monkeypatch.setattr(cliproxy.subprocess, "Popen", Popen)
    r = cliproxy.start(wait_seconds=5)
    assert r == {"ok": True, "started": True}
    assert spawned["args"][0].endswith(cliproxy.binary_name())
    assert spawned["args"][-1].endswith("config.yaml")
    assert spawned["kw"]["cwd"].endswith("cliproxy")


def test_status_starts_proxy_when_closed(proxy_home, monkeypatch):
    state = {"up": False}

    def fake_req(method, path):
        if not state["up"]:
            raise RuntimeError("kapalı")
        return {"files": [{"id": "claude-a.json", "provider": "claude", "email": "a@b.c"}]}

    def fake_start(wait_seconds=8.0):
        state["up"] = True
        return {"ok": True, "started": True}

    monkeypatch.setattr(cliproxy, "_req", fake_req)
    monkeypatch.setattr(cliproxy, "start", fake_start)
    d = cliproxy.status()
    assert d["running"] and d["accounts"][0]["email"] == "a@b.c"


def test_live_models_reads_proxy_list_and_skips_non_chat(monkeypatch):
    """Late-connected accounts like Kimi/Antigravity were missing from the fixed list; read them from the proxy."""
    monkeypatch.setitem(cliproxy._models_cache, "at", 0.0)
    data = [{"id": "claude-opus-5-5"}, {"id": "kimi-k3"}, {"id": "gpt-image-2"}, {"id": "codex-auto-review"}]
    assert cliproxy.live_models(fetch=lambda: data) == ["claude-opus-5-5", "kimi-k3"]

    def down():
        raise OSError("proxy kapalı")

    monkeypatch.setitem(cliproxy._models_cache, "at", 0.0)
    assert cliproxy.live_models(fetch=down) == ["claude-opus-5-5", "kimi-k3"]  # last known list


def test_live_models_tracks_owned_by(monkeypatch):
    """Antigravity accounts expose Claude & GPT models; owned_by must be preserved for model_group()."""
    monkeypatch.setitem(cliproxy._models_cache, "at", 0.0)
    data = [
        {"id": "gemini-3.8-flash", "owned_by": "antigravity"},
        {"id": "claude-sonnet-4-6-thinking", "owned_by": "antigravity"},
        {"id": "claude-opus-4-6-thinking", "owned_by": "antigravity"},
        {"id": "gpt-oss-120b-medium", "owned_by": "antigravity"},
        {"id": "claude-opus-5-5", "owned_by": "claude"},
        {"id": "gpt-6.1-terra", "owned_by": "codex"},
    ]
    cliproxy.live_models(fetch=lambda: data)
    owners = cliproxy.model_owners()
    assert owners["gemini-3.8-flash"] == "antigravity"
    assert owners["claude-sonnet-4-6-thinking"] == "antigravity"
    assert owners["gpt-oss-120b-medium"] == "antigravity"
    assert owners["claude-opus-5-5"] == "claude"


def test_tidy_models_keeps_two_newest_per_family_and_drops_old_generations():
    ids = ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-20250514", "claude-3-7-sonnet-20250219",
           "claude-3-5-haiku", "claude-sonnet-5-5", "kimi-k3", "kimi-k2.8", "kimi-k2.6", "kimi-k2.7-code",
           "gpt-6.1-terra", "gpt-5.6-terra", "gpt-oss-120b-medium"]
    assert cliproxy.tidy_models(ids) == ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5-5", "gpt-5.6-terra",
                                         "gpt-6.1-terra", "gpt-oss-120b-medium", "kimi-k2.7-code", "kimi-k2.8",
                                         "kimi-k3"]


def test_model_group():
    assert cliproxy.model_group("kimi-k3") == ("kimi", "Kimi", "cliproxy:kimi-*")
    assert cliproxy.model_group("gpt-6-luna")[0] == "codex"
    assert cliproxy.model_group("mystery-1")[0] == "other"


def test_model_group_antigravity_owned(monkeypatch):
    """Claude & GPT models served by Antigravity must be grouped under 'antigravity', not 'claude'/'codex'."""
    monkeypatch.setitem(cliproxy._models_cache, "owners", {
        "claude-sonnet-4-6-thinking": "antigravity",
        "claude-opus-4-6-thinking": "antigravity",
        "gpt-oss-120b-medium": "antigravity",
        "gemini-3.8-flash": "antigravity",
        "claude-opus-5-5": "claude",
        "gpt-6.1-terra": "codex",
    })
    # Antigravity-owned Claude/GPT models -> antigravity group
    assert cliproxy.model_group("claude-sonnet-4-6-thinking")[0] == "antigravity"
    assert cliproxy.model_group("claude-opus-4-6-thinking")[0] == "antigravity"
    assert cliproxy.model_group("gpt-oss-120b-medium")[0] == "antigravity"
    assert cliproxy.model_group("gemini-3.8-flash")[0] == "antigravity"
    # Standalone Claude/ChatGPT subscriptions -> their own groups
    assert cliproxy.model_group("claude-opus-5-5")[0] == "claude"
    assert cliproxy.model_group("gpt-6.1-terra")[0] == "codex"


def test_quota_shows_fullest_window_and_resets_stale_ones():
    """Claude: 5h window filled yesterday (reset passed), weekly at 91%: card shows 91%, not 100% or 13%."""
    now = 1_000_000.0
    q = cliproxy._quota_summary({"signals": {
        "Anthropic-Ratelimit-Unified-5h-Utilization": "1.0", "Anthropic-Ratelimit-Unified-5h-Reset": str(now - 60),
        "Anthropic-Ratelimit-Unified-7d-Utilization": "0.91", "Anthropic-Ratelimit-Unified-7d-Reset": str(now + 9000)}},
        now=now)
    assert (q["label"], q["percent"]) == ("7 days", 91)
    assert [(w["label"], w["percent"], w["stale"]) for w in q["windows"]] == [("5 hours", 0, True), ("7 days", 91, False)]


def test_dead_refresh_token_is_not_ready_and_cooldown_has_duration():
    """Proxy reports a dead refresh token as requires_sign_in=false: the card must not show "hazır" (ready)."""
    acc = cliproxy._account({"id": "claude-x-a@b.com.json", "provider": "claude", "email": "a@b.com", "status": "error",
                             "unavailable": True, "refresh_error": {"code": "invalid_grant", "message": "expired"}})
    assert acc["needs_sign_in"] and not acc["ready"]
    quota = cliproxy._account({"id": "antigravity-c.json", "provider": "antigravity", "quota_blocked": True,
                               "status": "error", "unavailable": True,
                               "cooldowns": [{"reason": "quota", "remaining_seconds": 390092}]})
    assert quota["cooldown"] == "5 days" and not quota["needs_sign_in"] and not quota["ready"]


def test_groups_ready_skips_services_without_usable_accounts():
    files = [{"id": "claude-1.json", "provider": "claude", "status": "error", "unavailable": True,
              "refresh_error": {"code": "invalid_grant"}},
             {"id": "codex-1.json", "provider": "codex", "status": "active"}]
    assert cliproxy.groups_ready(lambda: {"files": files}) == {"claude": False, "codex": True}


def test_groups_used_is_the_least_used_ready_account():
    """Background work (#459) looks at the account the proxy can still move to: the least-used usable one."""
    now = __import__("time").time()

    def claude(name, used, **kw):
        return {"id": name, "provider": "claude", "status": "active", **kw, "quota": {"signals": {
            "Anthropic-Ratelimit-Unified-5h-Utilization": str(used), "Anthropic-Ratelimit-Unified-5h-Reset": str(now + 900)}}}

    files = [claude("claude-1.json", 0.93), claude("claude-2.json", 0.41),
             claude("claude-3.json", 0.05, disabled=True),  # off: does not count
             {"id": "codex-1.json", "provider": "codex", "status": "active"}]  # no quota data: 0
    assert cliproxy.groups_used(lambda: {"files": files}) == {"claude": 41, "codex": 0}


def test_expired_cooldown_does_not_keep_account_busy():
    """If the proxy leaves an expired cooldown record uncleared, the account must not look busy forever."""
    now = __import__("time").time()
    acc = cliproxy._account({"id": "claude-x.json", "provider": "claude", "status": "active",
                             "cooldowns": [{"reason": "quota", "retry_at": now - 3600}]})
    assert acc["cooldown"] == "" and acc["ready"]
    unknown = cliproxy._account({"id": "claude-y.json", "provider": "claude", "status": "active",
                                 "cooldowns": [{"reason": "quota"}]})  # no duration: the proxy says it is waiting
    assert unknown["cooldown"] == "a short while" and not unknown["ready"]
