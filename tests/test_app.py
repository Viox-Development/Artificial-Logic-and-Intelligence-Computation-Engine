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
    for f in ("app.js", "projects.js", "ui.js", "main.js"):  # the UI is split into four scripts, loaded in order
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
    pend = client.get("/api/state").json()["pending"][0]
    assert pend["id"] == perm["id"] and pend["sid"] == state.sid  # so the UI can tell background questions apart
    # A message sent while busy starts no new job; it goes to the running one (the model sees it next step).
    assert client.post("/api/send", json={"text": "başka"}).status_code == 200
    assert state.agent._inbox and state.agent._inbox[0][0] == "başka"
    assert client.post("/api/send", json={"text": "/plan"}).status_code == 409  # a command needs a new turn
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
    assert str(sub) in state.agent.messages[0]["content"]  # system prompt knows the new folder
    from alice.cli import default_workspace

    assert default_workspace() == sub  # the same folder on the next launch
    assert client.post("/api/cwd", json={"path": str(workdir / "yok")}).status_code == 400
    assert client.post("/api/model", json={"role": "brain", "ref": "a:other"}).json()["ok"]
    assert client.get("/api/models").json()["brain"][0]["ref"] == "a:other"
    assert client.get("/api/models/all").json()["pinned"] == "a:other"
    # the button shows the selected model right away (it used to show the old one until the next reply)
    st = state.status()
    assert st["model"] == "a:other" and st["brain_pinned"]
    a_models = next(p for p in client.get("/api/models/all").json()["providers"] if p["name"] == "a")["models"]
    assert "a:other" in [m["ref"] for m in a_models]  # marked in the menu even if not measured yet
    state.router.last_ref["brain"] = "a:m"  # the selected model failed to answer; the fallback did
    assert state.status()["model"] == "a:other" and state.status()["served_model"] == "a:m"
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
    # Turns of the same chat always run on the same thread (Playwright/UI Automation objects are thread-bound).
    assert len(set(seen)) == 1 and seen[0].startswith("alice-chat-")


def test_new_endpoints_rewind_files_commands_images(app, workdir):
    state, srv, client, prov = app
    (workdir / "notlar.txt").write_text("x", encoding="utf-8")
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
    assert "<file path=" in str(state.agent.messages[1]["content"])  # the @file content was sent to the model
    hist = client.get("/api/history").json()
    assert hist[0]["text"] == "yaz @notlar.txt"  # the UI shows what the user typed
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
    assert end["is_error"] and "plan mode" in end["text"]


def test_changes_panel_lists_and_reverts_session_edits(app, workdir):
    state, srv, client, prov = app
    (workdir / "eski.txt").write_text("bir\niki\n", encoding="utf-8")
    prov.script.extend([tool_call("write_file", {"path": "yeni.txt", "content": "selam\n"}, "c1"),
                        tool_call("read_file", {"path": "eski.txt"}, "r2"),
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
    # a file not touched in this session cannot be reverted
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
    """The UI sees the update, applies it, and triggers a restart."""
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
    """No update while the agent is busy: the work must not be cut off halfway."""
    state, _srv, client, _ = app
    monkeypatch.setattr(state.agent, "busy", True, raising=False)
    r = client.post("/api/update/apply", json={})
    assert r.status_code == 409 and "stop" in r.json()["error"]


def test_update_mode_persists(app):
    """The update mode chosen in Settings is written to config.toml."""
    from alice.config import alice_home

    state, _srv, client, _ = app
    assert client.get("/api/state").json()["update_mode"] == "auto"
    assert client.post("/api/update/mode", json={"mode": "notify"}).json() == {"ok": True, "mode": "notify"}
    assert "notify" in (alice_home() / "config.toml").read_text(encoding="utf-8")
    assert client.post("/api/update/mode", json={"mode": "saçma"}).status_code == 400


def test_connections_api_lists_connectors_and_feeds(app, monkeypatch):
    """Settings > Bağlantılar (Connections): service status and feeds are visible in the UI."""
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
    assert client.post("/api/connect/auth", json={"name": "steam"}).json()["error"].startswith("Steam works with a key")
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
    """If the code was downloaded by another process, 'Yeniden başlat' (Restart) restarts without pulling."""
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
    assert r["ok"] and r["restarting"] and "already downloaded" in r["note"]
    assert restarted.wait(2)


def test_stale_from_own_update_shows_the_bar_but_a_manual_pull_does_not():
    """The update bar stays quiet only for a manual git pull (development); when ALICE's updater pulled the code
    itself (update_to recorded), the restart bar must still show for every user."""
    from alice import updater

    info = updater.UpdateInfo(repo=None, branch="main", local="b" * 40, remote="b" * 40, running="a" * 40)
    assert info.stale and info.as_dict()["hand_pulled"]  # nothing recorded: someone moved the checkout by hand
    updater.write_state(update_to="b" * 40)
    assert info.stale and not info.as_dict()["hand_pulled"]
    fresh = updater.UpdateInfo(repo=None, branch="main", local="b" * 40, remote="b" * 40, running="b" * 40)
    assert not fresh.as_dict()["hand_pulled"]  # not stale at all


# ------------------------------------------------------------------ multiple chats at once
def _hold_turn(state, release):
    """The foreground chat's turn lasts until release arrives (like a long tool); it also ignores Durdur (Stop)."""
    agent = state.agent
    run_turn = agent.run_turn

    def slow(prompt, **kw):
        # busy stays True until the real turn has finished: clearing it before run_turn let the test see an idle
        # chat that had not written its answer yet (flaky on Windows CI).
        agent.busy = True
        try:
            release.wait(10)
            return run_turn(prompt, **kw)
        finally:
            agent.busy = False

    agent.run_turn = slow
    return agent


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.05)
    return cond()


def test_other_chat_works_while_one_is_busy(app):
    state, srv, client, prov = app
    release = threading.Event()
    busy_agent = _hold_turn(state, release)
    prov.script = lambda model, messages, tools, **kw: "cevap " + str(len(messages))
    assert client.post("/api/send", json={"text": "uzun iş"}).json()["ok"]
    assert _wait(lambda: state.agent.busy)
    a_sid = state.sid

    r = client.post("/api/new", json={}).json()  # formerly "önce mevcut işi durdur" (stop the current job first)
    assert r["ok"] and r["session"] != a_sid and not r["busy"] and r["running"] == [a_sid]
    events, t = collect(state, lambda e: e["type"] == "busy" and e.get("value") is False and e.get("sid") == state.sid)
    assert client.post("/api/send", json={"text": "kısa soru"}).json()["ok"]
    t.join()
    assert state.agent is not busy_agent and state.agent.messages[-1]["content"].startswith("cevap")
    # Background-chat events must be told apart from the foreground's: each carries its own sid.
    assert {e.get("sid") for e in events if e["type"] in ("delta", "stream_end", "user")} == {state.sid}

    # Switching back to the background chat: from its live state, not disk; it is still running.
    b_sid = state.sid
    r = client.post("/api/resume", json={"id": a_sid}).json()
    assert r["ok"] and r["session"] == a_sid and r["busy"] and state.agent is busy_agent
    release.set()
    assert _wait(lambda: not state.agent.busy and not state.running())
    assert any(m.get("role") == "assistant" for m in busy_agent.messages)
    assert [s["id"] for s in client.get("/api/sessions").json() if s["id"] in (a_sid, b_sid)]


def test_background_chat_finishes_and_closes(app):
    state, srv, client, prov = app
    release = threading.Event()
    busy_agent = _hold_turn(state, release)
    prov.script = lambda model, messages, tools, **kw: "bitti"
    client.post("/api/send", json={"text": "uzun iş"})
    assert _wait(lambda: state.agent.busy)
    a_sid, a_chat = state.sid, state.chat
    assert client.post("/api/new", json={}).json()["ok"]
    assert client.post("/api/session/delete", json={"id": a_sid}).status_code == 409  # not deleted while running
    release.set()
    assert _wait(lambda: not state.running() and not a_chat._thread.is_alive())  # finished, saved, closed
    from alice import session as sess

    loaded = sess.load(a_sid)
    assert loaded and loaded[1][-1]["content"] == "bitti" and busy_agent.messages[-1]["content"] == "bitti"


def test_background_limit(app, monkeypatch):
    state, srv, client, prov = app
    monkeypatch.setattr(AppState, "MAX_BACKGROUND", 1)
    release = threading.Event()
    prov.script = lambda model, messages, tools, **kw: "ok"
    _hold_turn(state, release)
    client.post("/api/send", json={"text": "1"})
    assert _wait(lambda: state.agent.busy)
    assert client.post("/api/new", json={}).json()["ok"]
    _hold_turn(state, release)
    client.post("/api/send", json={"text": "2"})
    assert _wait(lambda: state.agent.busy)
    r = client.post("/api/new", json={})
    assert r.status_code == 409 and "background chats" in r.json()["error"]
    release.set()
    assert _wait(lambda: not state.running() and not state.agent.busy)


def test_stuck_chat_is_abandoned_not_the_app(app, monkeypatch):
    """A tool ignores Durdur (Stop): chat saved, reopened on a new thread; the old thread is harmless."""
    state, srv, client, prov = app
    monkeypatch.setattr(AppState, "STOP_GRACE", 0.2)
    release = threading.Event()
    stuck = _hold_turn(state, release)
    prov.script = lambda model, messages, tools, **kw: "ok"
    client.post("/api/send", json={"text": "takılacak"})
    assert _wait(lambda: state.agent.busy)
    sid, old = state.sid, state.chat
    state.desktop_lock.acquire(old.ctx, lambda: False)  # the stuck tool was holding the screen
    assert client.post("/api/stop", json={}).json()["ok"]
    assert _wait(lambda: state.chat is not old)
    assert state.sid == sid and not state.chat.busy and state.desktop_lock.owner is None
    events, t = collect(state, lambda e: e["type"] == "busy" and e.get("value") is False and e.get("sid") == sid)
    assert client.post("/api/send", json={"text": "devam"}).json()["ok"]
    t.join()
    assert state.agent.messages[-1]["content"] == "ok" and stuck is not state.agent

    seen = state.bus.subscribe()
    release.set()  # the stuck thread returned at last
    assert _wait(lambda: not old._thread.is_alive())
    from alice import session as sess

    saved = sess.load(sid)[1]
    assert saved[-1]["content"] == "ok" and not any("takılacak" == m.get("_display") for m in saved[-1:])
    leaked = []
    while not seen.empty():
        leaked.append(seen.get())
    assert not [e for e in leaked if e["type"] in ("delta", "stream_end", "tool_start")]  # old thread: no UI writes


def test_phone_chat_does_not_move_the_desktop_chat(app):
    """The phone works in its own chat: a new chat and a message sent there do not change the desktop's
    foreground chat; switching chats on the desktop does not carry the phone's message there (7 Kasım / Nov 7)."""
    state, srv, client, prov = app
    desk = state.sid
    r = client.post("/api/new", json={"detached": True}).json()
    phone = r["sid"]
    assert r["ok"] and phone != desk and state.sid == desk  # the computer stays in the same chat
    prov.script.append("telefona cevap")
    events, t = collect(state, lambda e: e["type"] == "busy" and e.get("value") is False and e.get("sid") == phone)
    assert client.post("/api/send", json={"text": "telefondan", "sid": phone}).json()["ok"]
    t.join()
    assert state.sid == desk
    assert any(e.get("type") == "user" and e.get("sid") == phone for e in events)
    hist = client.get("/api/history", params={"sid": phone}).json()
    assert [h["text"] for h in hist if h["type"] == "user"] == ["telefondan"]
    assert not [h for h in client.get("/api/history").json() if h["type"] == "user"]  # the desktop chat is empty
    r = client.post("/api/resume", json={"id": phone, "detached": True}).json()
    assert r["ok"] and r["sid"] == phone and state.sid == desk


def test_phone_stop_for_finished_chat_does_not_stop_the_desktop(app):
    """If the phone's chat is finished (or never started), Durdur (Stop) must not stop the computer's running job."""
    state, srv, client, prov = app
    release = threading.Event()
    busy_agent = _hold_turn(state, release)
    prov.script = lambda model, messages, tools, **kw: "ok"
    client.post("/api/send", json={"text": "uzun iş"})
    assert _wait(lambda: state.agent.busy)
    phone = client.post("/api/new", json={"detached": True}).json()["sid"]
    assert client.post("/api/stop", json={"sid": phone}).json()["ok"]
    assert not busy_agent.cancel.is_set() and state.agent.busy
    # A running foreground chat can't be deleted either (e.g. from the phone): it would rewrite itself on finish.
    assert client.post("/api/session/delete", json={"id": state.sid}).status_code == 409
    release.set()
    assert _wait(lambda: not state.agent.busy)


def test_phone_message_that_starts_no_turn_does_not_leak_a_chat(app):
    """/btw or an unknown command sent to the phone's non-live chat: the background chat it opened must close."""
    state, srv, client, prov = app
    prov.script = lambda model, messages, tools, **kw: "yan cevap"
    phone = client.post("/api/new", json={"detached": True}).json()["sid"]
    r = client.post("/api/send", json={"text": "/olmayankomut", "sid": phone})
    assert r.status_code == 409 and state.find_chat(phone) is None
    events, t = collect(state, lambda e: e["type"] == "btw" and not e.get("pending"))
    assert client.post("/api/send", json={"text": "/btw ne?", "sid": phone}).json()["ok"]
    t.join()
    assert any(e.get("a") == "yan cevap" for e in events)
    assert _wait(lambda: state.find_chat(phone) is None) and state.sid != phone


def test_btw_side_question_does_not_touch_history(app):
    state, srv, client, prov = app

    def answer(model, messages, tools, **kw):  # other calls in the background (e.g. title) must not break the order
        last = messages[-1].get("content")
        return "Yan cevap: 42." if isinstance(last, str) and "Side question" in last else "cevap 1"

    prov.script = answer
    events, t = collect(state, lambda e: e["type"] == "status")
    assert client.post("/api/send", json={"text": "selam"}).json()["ok"]
    t.join()
    before = list(state.chat.agent.messages)
    events, t = collect(state, lambda e: e["type"] == "btw" and not e.get("pending"))
    assert client.post("/api/send", json={"text": "/btw sayı kaç?"}).json()["ok"]
    t.join()
    done = [e for e in events if e["type"] == "btw" and not e.get("pending")]
    assert done and done[-1]["a"] == "Yan cevap: 42." and done[-1]["q"] == "sayı kaç?"
    assert state.chat.agent.messages == before  # not written to the chat
    # Find the side-question call by its text: a background call (title, next-message suggestion) can land after it.
    side = [c for c in prov.calls if isinstance(c["messages"][-1].get("content"), str)
            and "Side question" in c["messages"][-1]["content"]]
    assert side and not side[-1].get("tools")
    assert client.post("/api/send", json={"text": "/btw"}).json()["ok"] is False


def test_recap_summarizes_once_per_state(app):
    state, srv, client, prov = app
    assert client.get("/api/recap").json() == {"text": ""}  # no conversation
    for m in ("bir", "iki"):
        state.chat.agent.messages += [{"role": "user", "content": m}, {"role": "assistant", "content": m + " oldu"}]
    prov.script.append("Dosyalar yazıldı, testler bekliyor.")
    assert client.get("/api/recap").json()["text"] == "Dosyalar yazıldı, testler bekliyor."
    n = len(prov.calls)
    assert client.get("/api/recap").json()["text"].startswith("Dosyalar")
    assert len(prov.calls) == n  # not regenerated for the same state


def test_message_while_question_waits_answers_the_question(app):
    """A message written while ALICE waits on a question card is not lost; it answers the question."""
    state, srv, client, prov = app
    prov.script.extend([tool_call("ask_user", {"question": "Hangi yol?", "options": ["A", "B"]}), "Tamam, C ile."])
    events, t = collect(state, lambda e: e["type"] == "status", timeout=15)
    assert client.post("/api/send", json={"text": "başla"}).json()["ok"]
    for _ in range(100):
        if any(p.get("type") == "ask_choice" for p in state.chat.ui.pending.values()):
            break
        time.sleep(0.05)
    assert client.post("/api/send", json={"text": "ikisi de değil, C yap"}).json()["ok"]
    t.join()
    tool_msg = next(m for m in state.chat.agent.messages if m.get("role") == "tool")
    assert "C yap" in tool_msg["content"]
    assert any(e["type"] == "user" and e["text"] == "ikisi de değil, C yap" for e in events)
    assert any(e["type"] == "ask_choice_done" for e in events)


def test_next_message_suggestions(app):
    state, srv, client, prov = app
    state.cfg.data["agent"]["suggestions"] = True
    prov.script.extend(["Dosyayı yazdım. Test edeyim mi?", '{"items": ["Evet, testleri koş", "Önce diff göster"]}'])
    events, t = collect(state, lambda e: e["type"] == "suggest", timeout=15)
    assert client.post("/api/send", json={"text": "dosyayı yaz"}).json()["ok"]
    t.join()
    sug = [e for e in events if e["type"] == "suggest"]
    assert sug and sug[-1]["items"] == ["Evet, testleri koş", "Önce diff göster"]
