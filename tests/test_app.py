from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from alice.app.server import AppServer, AppState, display_history
from alice.config import alice_home
from tests.conftest import FakeProvider, make_config, rm_rf, tool_call


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", [])
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    base = srv.url.split("/?")[0]
    client = httpx.Client(base_url=base, headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, srv, client, prov
    srv.stop()


def collect(state, until, timeout=10):
    q = state.bus.subscribe()
    out = []

    def run():
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev = q.get(timeout=0.2)
            except Exception:  # noqa: BLE001
                continue
            out.append(ev)
            if until(ev):
                return

    t = threading.Thread(target=run)
    t.start()
    return out, t


def test_requires_token_and_local_host(app):
    state, srv, client, _ = app
    base = str(client.base_url)
    assert httpx.get(base + "/api/state").status_code == 403
    assert httpx.get(base + "/").status_code == 403
    assert httpx.post(base + "/api/send", json={"text": "x"}).status_code == 403
    assert httpx.get(base + "/api/state", headers={"X-Alice-Token": state.token, "Host": "evil.com"}).status_code == 403
    r = httpx.get(srv.url)
    assert r.status_code == 200 and state.token in r.text and "Content-Security-Policy" in r.headers
    for f in ("app.js", "projects.js", "ui.js", "main.js"):  # arayüz dört betiğe bölünmüş, sırayla yüklenir
        assert client.get(f"/static/{f}").status_code == 200 and f"/static/{f}" in r.text
    assert client.get("/static/alice.png").headers["content-type"] == "image/png"
    assert client.get("/static/../server.py").status_code == 404


def test_send_streams_events_and_saves_session(app):
    state, srv, client, prov = app
    prov.script.extend([tool_call("write_file", {"path": "a.txt", "content": "selam\n"}), "tamamdır"])
    events, t = collect(state, lambda e: e["type"] == "status")
    assert client.post("/api/send", json={"text": "dosya yaz"}).json()["ok"]
    t.join()
    types = [e["type"] for e in events]
    for needed in ("user", "busy", "stream_start", "tool_start", "tool_end", "stream_end", "status"):
        assert needed in types, needed
    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert "+selam" in tool_end["diff"]
    assert next(e for e in events if e["type"] == "status")["today_requests"] == 2
    hist = client.get("/api/history").json()
    assert [h["type"] for h in hist] == ["user", "tool", "assistant"] and hist[0]["mi"] == 1
    assert hist[1]["summary"].startswith("a.txt")
    assert client.get("/api/sessions").json()[0]["title"] == "dosya yaz"


def test_permission_via_api(app, workdir):
    state, srv, client, prov = app
    (workdir / "eski").mkdir()
    prov.script.extend([tool_call("run_command", {"command": rm_rf("eski")}), "sildim"])
    events, t = collect(state, lambda e: e["type"] == "permission")
    client.post("/api/send", json={"text": "sil"})
    t.join()
    perm = events[-1]
    assert perm["risk"] == "danger"
    assert client.get("/api/state").json()["pending"][0]["id"] == perm["id"]
    assert client.post("/api/send", json={"text": "başka"}).status_code == 409  # meşgulken yeni iş yok
    done, t2 = collect(state, lambda e: e["type"] == "status")
    assert client.post("/api/permission", json={"id": perm["id"], "answer": "y"}).json()["ok"]
    t2.join()
    assert not (workdir / "eski").exists()


def test_stop_denies_pending_permission(app, workdir):
    state, srv, client, prov = app
    prov.script.extend([tool_call("run_command", {"command": "rm -rf x"})])
    events, t = collect(state, lambda e: e["type"] == "permission")
    client.post("/api/send", json={"text": "sil"})
    t.join()
    done, t2 = collect(state, lambda e: e["type"] == "status")
    client.post("/api/stop", json={})
    t2.join()
    assert any(e["type"] == "permission_done" and e["answer"] == "n" for e in done)
    assert not state.agent.busy


def test_mode_cwd_new_and_model(app, workdir):
    state, srv, client, _ = app
    assert client.post("/api/mode", json={"mode": "yolo"}).json()["mode"] == "yolo"
    assert client.post("/api/mode", json={"mode": "bad"}).status_code == 400
    sub = workdir / "proje"
    sub.mkdir()
    assert client.post("/api/cwd", json={"path": str(sub)}).json()["cwd"] == str(sub)
    assert str(sub) in state.agent.messages[0]["content"]  # sistem istemi yeni klasörü biliyor
    from alice.cli import default_workspace

    assert default_workspace() == sub  # sonraki açılışta aynı klasör
    assert client.post("/api/cwd", json={"path": str(workdir / "yok")}).status_code == 400
    assert client.post("/api/model", json={"role": "brain", "ref": "a:other"}).json()["ok"]
    assert client.get("/api/models").json()["brain"][0]["ref"] == "a:other"
    assert client.get("/api/models/all").json()["pinned"] == "a:other"
    assert client.post("/api/models/hide", json={"name": "b", "hidden": True}).json()["ok"]
    assert state.router.is_hidden("b:x") and "hidden_models" in (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert client.post("/api/models/hide", json={"name": "zzz"}).status_code == 400
    assert client.post("/api/new", json={}).json()["ok"]


def test_sse_endpoint_streams(app):
    state, srv, client, _ = app
    with client.stream("GET", "/api/events", params={"t": state.token}) as r:
        assert r.headers["content-type"] == "text/event-stream"
        first = next(r.iter_lines())
        assert json.loads(first[5:])["type"] == "status"


def test_display_history_skips_internal():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "[Önceki konuşmanın özeti]\n..."},
            {"role": "assistant", "content": "Özeti aldım, kaldığım yerden devam ediyorum."},
            {"role": "user", "content": "selam"}]
    assert [h["type"] for h in display_history(msgs)] == ["info", "user"]


def test_all_turns_run_on_same_worker_thread(app):
    state, srv, client, prov = app
    seen = []
    run_turn = state.agent.run_turn

    def spy(*a, **kw):
        seen.append(threading.current_thread().name)
        return run_turn(*a, **kw)

    state.agent.run_turn = spy
    prov.script = lambda model, messages, tools, **kw: "ok"
    for text in ("bir", "iki"):
        events, t = collect(state, lambda e: e["type"] == "status")
        assert client.post("/api/send", json={"text": text}).json()["ok"]
        t.join()
    assert seen == ["alice-agent", "alice-agent"]


def test_new_endpoints_rewind_files_commands_images(app, workdir):
    state, srv, client, prov = app
    (workdir / "notlar.txt").write_text("x")
    assert "notlar.txt" in client.get("/api/files", params={"q": "not"}).json()
    names = [c["name"] for c in client.get("/api/commands").json()]
    assert "rewind" in names and "plan" in names
    assert client.post("/api/send", json={"text": "/yok-boyle-komut"}).status_code == 409
    prov.script.extend([tool_call("write_file", {"path": "a.txt", "content": "1"}), "tamam"])
    events, t = collect(state, lambda e: e["type"] == "status")
    assert client.post("/api/send", json={"text": "yaz @notlar.txt", "images": ["AAAA"]}).json()["ok"]
    t.join()
    user_ev = next(e for e in events if e["type"] == "user")
    assert user_ev["mi"] == 1 and user_ev["images"] == ["AAAA"]
    assert "<file path=" in str(state.agent.messages[1]["content"])  # @dosya içeriği modele gitti
    hist = client.get("/api/history").json()
    assert hist[0]["text"] == "yaz @notlar.txt"  # arayüzde kullanıcının yazdığı görünür
    r = client.post("/api/rewind", json={"mi": 1}).json()
    assert r["ok"] and r["text"] == "yaz @notlar.txt" and not (workdir / "a.txt").exists() and r["history"] == []
    sid = client.get("/api/sessions").json()[0]["id"]
    assert client.post("/api/session/delete", json={"id": sid}).json()["ok"]
    assert client.post("/api/session/delete", json={"id": "../../etc/passwd"}).json()["ok"]


def test_plan_mode_via_api(app, workdir):
    state, srv, client, prov = app
    assert client.post("/api/mode", json={"mode": "plan"}).json()["mode"] == "plan"
    prov.script.extend([tool_call("run_command", {"command": "echo hi"}), "plan yazdım"])
    events, t = collect(state, lambda e: e["type"] == "status")
    client.post("/api/send", json={"text": "bir şey yap"})
    t.join()
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["is_error"] and "Plan modundasın" in end["text"]


def test_changes_panel_lists_and_reverts_session_edits(app, workdir):
    state, srv, client, prov = app
    (workdir / "eski.txt").write_text("bir\niki\n", encoding="utf-8")
    prov.script.extend([tool_call("write_file", {"path": "yeni.txt", "content": "selam\n"}, "c1"),
                        tool_call("edit_file", {"path": "eski.txt", "old_string": "iki", "new_string": "üç"}, "c2"),
                        "tamam"])
    events, t = collect(state, lambda e: e["type"] == "status")
    assert client.post("/api/send", json={"text": "yaz"}).json()["ok"]
    t.join()
    changes = {c["path"]: c for c in client.get("/api/changes").json()}
    assert changes["yeni.txt"]["status"] == "eklendi" and changes["yeni.txt"]["added"] == 1
    assert changes["eski.txt"]["status"] == "değişti"
    assert (changes["eski.txt"]["added"], changes["eski.txt"]["removed"]) == (1, 1)
    assert "+üç" in changes["eski.txt"]["diff"]
    r = client.post("/api/changes/revert", json={"path": changes["eski.txt"]["abs"]}).json()
    assert r["ok"] and [c["path"] for c in r["changes"]] == ["yeni.txt"]
    assert (workdir / "eski.txt").read_text(encoding="utf-8") == "bir\niki\n"
    r = client.post("/api/changes/revert", json={"path": changes["yeni.txt"]["abs"]}).json()
    assert r["ok"] and r["changes"] == [] and not (workdir / "yeni.txt").exists()
    # oturumda dokunulmamış bir dosya geri alınamaz
    assert client.post("/api/changes/revert", json={"path": str(workdir / "x")}).status_code == 404


def test_git_branch_and_session_times(app, workdir):
    from alice.app.server import git_branch

    state, srv, client, prov = app
    (workdir / ".git").mkdir()
    (workdir / ".git" / "HEAD").write_text("ref: refs/heads/ozellik/arayuz\n", encoding="utf-8")
    (workdir / "alt").mkdir()
    assert git_branch(workdir / "alt") == "ozellik/arayuz"
    assert client.get("/api/state").json()["branch"] == "ozellik/arayuz"
    prov.script.append("selam")
    events, t = collect(state, lambda e: e["type"] == "status")
    client.post("/api/send", json={"text": "merhaba"})
    t.join()
    row = client.get("/api/sessions").json()[0]
    assert row["title"] == "merhaba" and row["updated"] > 0


def test_update_api(app, monkeypatch):
    """Arayüz güncellemeyi görür, uygular ve yeniden başlatmayı tetikler."""
    from alice import updater

    state, _srv, client, _ = app
    assert client.get("/api/update").json() == {"available": False}

    info = updater.UpdateInfo(repo=None, branch="main", local="a" * 40, remote="b" * 40,
                              commits=["yeni özellik"], ci="green")
    monkeypatch.setattr(updater, "check", lambda *a, **k: info)
    r = client.post("/api/update/check", json={}).json()
    assert r["available"] and r["count"] == 1 and r["remote"] == "b" * 7
    assert client.get("/api/state").json()["update"]["available"]

    applied = {}
    monkeypatch.setattr(updater, "apply_update", lambda i, c, force=False, emit=None: applied.setdefault("f", force) or (True, "güncellendi"))
    monkeypatch.setattr(state, "_restart_soon", lambda: applied.setdefault("restart", True))
    r = client.post("/api/update/apply", json={}).json()
    assert r["ok"] and r["restarting"]
    time.sleep(0.3)
    assert applied.get("restart")
    assert client.get("/api/state").json()["update"] is None


def test_update_apply_waits_for_agent(app, monkeypatch):
    """Ajan çalışırken güncelleme yapılmaz: iş yarıda kalmasın."""
    state, _srv, client, _ = app
    monkeypatch.setattr(state.agent, "busy", True, raising=False)
    r = client.post("/api/update/apply", json={})
    assert r.status_code == 409 and "durdur" in r.json()["error"]


def test_update_mode_persists(app):
    """Ayarlardan seçilen güncelleme modu config.toml'a yazılır."""
    from alice.config import alice_home

    state, _srv, client, _ = app
    assert client.get("/api/state").json()["update_mode"] == "auto"
    assert client.post("/api/update/mode", json={"mode": "notify"}).json() == {"ok": True, "mode": "notify"}
    assert "notify" in (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert client.post("/api/update/mode", json={"mode": "saçma"}).status_code == 400


def test_connections_api_lists_connectors_and_feeds(app, monkeypatch):
    """Ayarlar > Bağlantılar: servislerin durumu ve akışlar arayüzden görünür."""
    from alice.config import alice_home

    state, _srv, client, _ = app
    monkeypatch.setenv("STEAM_API_KEY", "k")
    (alice_home() / "feeds.toml").write_text(
        '[[feed]]\nname = "d"\nconnector = "discord"\naction = "messages"\nevery_minutes = 5\n', encoding="utf-8")

    d = client.get("/api/connect").json()
    by = {c["name"]: c for c in d["connectors"]}
    assert {"steam", "spotify", "discord", "youtube_api"} <= set(by)
    assert by["steam"]["ready"] and by["steam"]["kind"] == "api_key"
    assert not by["spotify"]["ready"] and by["spotify"]["kind"] == "oauth2"
    assert "library" in by["steam"]["actions"] and not d["errors"]
    assert [f["name"] for f in d["feeds"]] == ["d"]
    assert d["feeds"][0]["connector"] == "discord" and d["feeds"][0]["enabled"]


def test_connect_auth_needs_client_id_then_starts_browser_flow(app, monkeypatch):
    state, _srv, client, _ = app
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    r = client.post("/api/connect/auth", json={"name": "spotify"})
    assert r.status_code == 400 and "SPOTIFY_CLIENT_ID" in r.json()["error"]
    assert client.post("/api/connect/auth", json={"name": "steam"}).json()["error"].startswith("Steam anahtarla")
    assert client.post("/api/connect/auth", json={"name": "yok"}).status_code == 400

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    started = threading.Event()
    monkeypatch.setattr("alice.connect.oauth.authorize",
                        lambda *a, **k: started.set() or {"access_token": "t"})
    assert client.post("/api/connect/auth", json={"name": "spotify"}).json()["ok"]
    assert started.wait(5)


def test_connect_logout_forgets_token(app):
    from alice.connect import store

    _state, _srv, client, _ = app
    store.save("spotify", access_token="t")
    assert client.post("/api/connect/logout", json={"name": "spotify"}).json() == {"ok": True}
    assert store.get("spotify") == {}


def test_bayat_uygulama_cekmeden_yeniden_baslar(app, monkeypatch):
    """Kod başka bir süreçten indirildiyse 'Yeniden başlat' çekmeden yeniden başlatır."""
    from alice import updater

    state, _srv, client, _ = app
    info = updater.UpdateInfo(repo=None, branch="main", local="b" * 40, remote="b" * 40, running="a" * 40)
    monkeypatch.setattr(updater, "check", lambda *a, **k: info)
    r = client.post("/api/update/check", json={}).json()
    assert r["stale"] and not r["available"]
    assert client.get("/api/state").json()["update"]["stale"]
    monkeypatch.setattr(updater, "apply_update", lambda *a, **k: pytest.fail("çekecek bir şey yok"))
    restarted = threading.Event()
    monkeypatch.setattr(state, "_restart_soon", lambda: restarted.set())
    r = client.post("/api/update/apply", json={}).json()
    assert r["ok"] and r["restarting"] and "zaten indirilmiş" in r["note"]
    assert restarted.wait(2)
