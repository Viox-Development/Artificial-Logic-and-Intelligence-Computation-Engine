from __future__ import annotations

import time

import httpx
import pytest

from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config, tool_call
from tests.test_team import steps, who


def reply(model, messages, tools, **kw):
    me = who(messages)
    if me == "backend":
        return [tool_call("write_file", {"path": "src/a.py", "content": "x = 1\n"}), "rapor: tamam"][
            min(steps(messages), 1)]
    return "tamam"  # yönetici ve diğerleri


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}, stream=False)
    prov = FakeProvider("a", reply)
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client, workdir
    srv.stop()


def wait_for(client, pid, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/project?id={pid}").json()
        if pred(d):
            return d
        time.sleep(0.1)
    raise AssertionError(f"beklenen duruma gelmedi: {d.get('status')} {[m['status'] for m in d.get('members', [])]}")


def act(client, pid, **kw):
    return client.post("/api/project/action", json={"id": pid, **kw})


def test_project_lifecycle_over_api(app):
    state, client, workdir = app
    r = client.post("/api/project/create", json={"goal": "Deneme projesi", "review": True, "lead_thinking": "high",
                                                 "council": False,
                                                 "agents": [{"name": "backend", "task": "a.py yaz", "model": "fast",
                                                             "thinking": "high", "scope": ["src"]}]}).json()
    assert r["ok"]
    pid = r["id"]
    d = wait_for(client, pid, lambda d: d["members"][0]["status"] == "review")
    assert d["origin"] == "user" and d["review"] and d["progress"] == [0, 1]
    assert client.get("/api/projects").json()[0]["id"] == pid

    det = client.get(f"/api/project?id={pid}&member=backend").json()
    assert [i["type"] for i in det["member"]["history"]][:2] == ["user", "tool"]
    assert det["member"]["files"] == ["src/a.py"]
    assert "+x = 1" in client.get(f"/api/project/diff?id={pid}&member=backend").json()["diff"]
    lead = client.get(f"/api/project?id={pid}&member=alice").json()["member"]
    assert lead["name"] == "alice" and lead["history"]  # yönetici konuşması da görünür

    # kullanıcı düzeltme ister, ajan devam eder, sonra kullanıcı onaylar; tembel yönetici dürtülüp projeyi kapatır
    assert act(client, pid, action="revise", name="backend", text="bir şey daha").json()["ok"]
    wait_for(client, pid, lambda d: d["members"][0]["status"] == "review" and d["members"][0]["reviews"] == 1)
    assert act(client, pid, action="approve", name="backend").json()["ok"]
    d = wait_for(client, pid, lambda d: d["status"] == "done", timeout=20)
    assert d["progress"] == [1, 1] and d["summary"]

    # bitmiş projeye ajan eklenir; proje yeniden açılır
    assert act(client, pid, action="add", agent={"name": "tests", "task": "test"}).json()["ok"]
    wait_for(client, pid, lambda d: any(m["name"] == "tests" and m["status"] == "review" for m in d["members"]))
    assert act(client, pid, action="add", agent={"name": "Alice", "task": "x"}).status_code == 400
    assert act(client, pid, action="revert", name="backend").json()["ok"]
    assert not (workdir / "src/a.py").exists()

    # uygulama yeniden açılmış gibi: proje diskten yüklenir (durmuş), sürdürülebilir
    act(client, pid, action="stop")
    state.ctx.state["teams"].clear()
    d = client.get(f"/api/project?id={pid}").json()
    assert d["status"] == "stopped"
    assert act(client, pid, action="resume").json()["ok"]
    assert client.get(f"/api/project?id={pid}").json()["status"] != "stopped"
    act(client, pid, action="stop")
    assert act(client, pid, action="delete").json()["ok"]
    assert client.get(f"/api/project?id={pid}").status_code == 404


def test_planning_project_and_questions(app):
    state, client, _ = app
    pid = client.post("/api/project/create", json={"goal": "Planla"}).json()["id"]
    team = state.ctx.state["teams"][pid]
    assert team.status() in ("planning", "running", "review")
    q = team.ask_user("Hangi dil?")
    assert client.get(f"/api/project?id={pid}").json()["status"] == "needs_you"
    assert client.get("/api/projects").json()[0]["questions"] == 1
    assert act(client, pid, action="answer", name=q["id"], text="Python").json()["ok"]
    assert client.get(f"/api/project?id={pid}").json()["questions"][0]["answer"] == "Python"
    act(client, pid, action="stop")


def test_create_validation(app):
    _, client, _ = app
    assert client.post("/api/project/create", json={"goal": "", "agents": []}).status_code == 400
    assert client.post("/api/project/create", json={"goal": "g", "lead_model": "yok"}).status_code == 400
    assert client.post("/api/project/create", json={"goal": "g", "cwd": "/yok/klasor"}).status_code == 400
    assert client.get("/api/project?id=yok").status_code == 404


def test_council_actions_over_api(app):
    state, client, _ = app
    pid = client.post("/api/project/create", json={"goal": "Kurullu proje", "agents": [
        {"name": "backend", "task": "a.py yaz"}]}).json()["id"]
    team = state.ctx.state["teams"][pid]
    assert team.council
    # sahte modeller JSON vermez: kurul oy toplayamaz, karar çekişmeli kalır ve iş incelemede bekler
    wait_for(client, pid, lambda d: d["members"][0]["status"] == "review" and d["members"][0]["council"]
             and d["members"][0]["council"]["state"] == "done" and not d["members"][0]["council_running"], timeout=20)
    det = client.get(f"/api/project?id={pid}&member=backend").json()["member"]
    assert det["councils"][0]["kind"] == "work" and det["councils"][0]["result"]["decision"] == "contested"
    r = act(client, pid, action="vote", name="backend", text="approve", agent={"note": "bence iyi"}).json()
    assert r["ok"] and "approve" in r["note"]
    wait_for(client, pid, lambda d: d["members"][0]["status"] == "done")
    assert act(client, pid, action="council", name="backend").json()["ok"]  # "Kurula sor"
    wait_for(client, pid, lambda d: d["members"][0]["councils_n"] == 2 and not d["members"][0]["council_running"],
             timeout=20)
    assert act(client, pid, action="vote", name="backend", text="belki").status_code == 400
    act(client, pid, action="stop")


def test_site_preview_is_sandboxed_and_hides_secrets(app):
    state, client, workdir = app
    (workdir / "site").mkdir()
    (workdir / "site" / "index.html").write_text("<h1>Varyant</h1>", encoding="utf-8")
    (workdir / "site" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (workdir / ".env").write_text("KEY=gizli", encoding="utf-8")
    pid = client.post("/api/project/create", json={"goal": "site", "council": False, "agents": []}).json()["id"]
    d = client.get(f"/api/project?id={pid}&member=__preview").json()
    assert d["preview"]["entry"] == "site/index.html" and d["preview"]["changed"] > 0
    base = d["preview"]["base"]
    assert state.token not in base  # sitenin kodu ana anahtarı göremez
    anon = httpx.Client(base_url=str(client.base_url), timeout=10)  # ana anahtar olmadan
    r = anon.get(base + "site/index.html")
    assert r.status_code == 200 and "Varyant" in r.text
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert anon.get(base + "site/app.js").headers["content-type"].startswith("text/javascript")
    assert anon.get(base + "site/").text == r.text  # klasör -> index.html
    for bad in (".env", "site/../.env", "%2e%2e/%2e%2e/etc/passwd"):
        assert anon.get(base + bad).status_code == 404
    assert anon.get(base.replace(state.preview_token, "yanlis") + "site/index.html").status_code == 403
    assert anon.get(f"/api/state?t={state.preview_token}").status_code == 403  # önizleme anahtarı API açmaz


def test_deleted_project_is_not_resurrected_by_a_late_save(app):
    """Silinen proje, kapanmakta olan ajanın geriden gelen kaydıyla geri gelmesin.

    Windows runner'ında test_project_lifecycle_over_api'yi ara sıra düşüren yarış buydu: delete
    dosyayı siliyor, hemen ardından çalışan bir save() onu geri yazıyor, sonraki GET 404 yerine
    200 dönüyordu."""
    state, client, _workdir = app
    pid = client.post("/api/project/create", json={"goal": "Silinecek proje"}).json()["id"]
    team = state.ctx.state["teams"][pid]
    wait_for(client, pid, lambda d: d["status"] in ("planning", "running", "review", "needs_you", "done"))
    act(client, pid, action="stop")
    assert act(client, pid, action="delete").json()["ok"]
    assert not team.path().exists()

    team.save()        # geriden gelen kayıt
    team._changed()    # ve canlı yayın yolu
    assert not team.path().exists()
    assert client.get(f"/api/project?id={pid}").status_code == 404
