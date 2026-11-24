"""Unit tests for alice/tools/meta.py and alice/tools/flight.py (no network, no game, no real DCS folder)."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from alice.config import alice_home
from alice.tools import flight as flight_mod
from alice.tools import meta
from alice.tools.base import READ, WRITE, ToolContext, ToolError
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


# ================================================================== meta: TodoWrite


def test_todo_write_renders_marks_and_stores_state(ctx):
    todos = [{"content": "plan", "status": "done"}, {"content": "kod", "status": "in_progress"},
             {"content": "test", "status": "pending"}, {"content": "garip", "status": "bilinmeyen"}]
    r = meta.TodoWrite().run(ctx, todos=todos)
    assert r.text == "[x] plan\n[~] kod\n[ ] test\n[ ] garip"
    assert ctx.state["todos"] is todos


def test_todo_write_empty_and_summary(ctx):
    t = meta.TodoWrite()
    assert t.run(ctx, todos=[]).text == "the list is empty"
    assert t.summary({"todos": [{"status": "done"}, {"status": "pending"}, {"status": "done"}]}) == "3 tasks, 2 done"
    assert t.summary({}) == "0 tasks, 0 done"
    assert t.summary({"todos": None}) == "0 tasks, 0 done"


# ================================================================== meta: Memory


def test_memory_read_empty_then_add_and_read(ctx):
    m = meta.Memory()
    assert m.risk_for({"action": "add"}) == READ  # memory writes need no approval
    assert m.risk_for({"action": "forget"}) == WRITE
    assert m.run(ctx, action="read").text == "(memory is empty)"
    assert m.run(ctx, action="add", note="  kullanıcı Türkçe konuşur \n").text.startswith("saved: ")
    m.run(ctx, action="add", note="en sevdiği uçak F-16")
    text = m.run(ctx, action="read").text
    assert text.startswith("# Notes") and "kullanıcı Türkçe konuşur" in text and "F-16" in text


@pytest.mark.parametrize("note", [None, ""])
def test_memory_add_requires_note(ctx, note):
    with pytest.raises(ToolError, match="note"):
        meta.Memory().run(ctx, action="add", note=note)
    assert meta.Memory().run(ctx, action="read").text == "(memory is empty)"


# ================================================================== meta: Task (subagent)


class FakeChild:
    def __init__(self, answer):
        import threading

        self.answer = answer
        self.calls = []
        self.cancel = threading.Event()
        self.messages = [{"role": "system", "content": "sys"}]
        self.ui = None

    def run_turn(self, prompt, max_steps=None):
        self.calls.append((prompt, max_steps))
        return self.answer

    def stop(self):
        self.cancel.set()


class FakeParent:
    def __init__(self, answer):
        self.child = FakeChild(answer)


@pytest.fixture
def fake_child(monkeypatch):
    """Subagent setup (alice/subagents.py) with a fake child: the Task tool's own flow is tested."""
    from alice import subagents

    def build(parent, kind, ui, ctx):
        parent.child.ui = ui
        return parent.child, 40

    monkeypatch.setattr(subagents, "build_child", build)


def test_task_requires_parent_agent(ctx):
    with pytest.raises(ToolError, match="subagent"):
        meta.Task().run(ctx, prompt="x")


def test_task_runs_child_and_reports(ctx, fake_child):
    parent = FakeParent("sonuç")
    ctx.state["agent"] = parent
    r = meta.Task().run(ctx, prompt="ara", max_steps=7)
    assert r.text.startswith("[subagent a1") and "sonuç" in r.text and "task id=a1" in r.text
    assert parent.child.calls == [("ara", 7)]


def test_task_empty_answer(ctx, fake_child):
    ctx.state["agent"] = FakeParent("")
    assert "(alt ajan boş cevap döndürdü)" in meta.Task().run(ctx, prompt="x").text


# ================================================================== meta: Reflex


@pytest.fixture
def fake_reflex(monkeypatch):
    import alice.reflex.agent as agent_mod
    import alice.reflex.loop as loop_mod
    import alice.reflex.observers as obs_mod

    rec: dict = {}

    class FakeAgent:
        def __init__(self, observer, decider, writer, translate, emit, trace_path):
            rec.update(observer=observer, decider=decider, writer=writer, translate=translate, trace=trace_path)

        def run(self, goal, max_steps, inputs, notes):
            rec.update(goal=goal, max_steps=max_steps, inputs=inputs, notes=notes)
            return SimpleNamespace(report=lambda: "reflex raporu")

    monkeypatch.setattr(agent_mod, "ScreenAgent", FakeAgent)
    monkeypatch.setattr(agent_mod, "make_translator", lambda router: ("translate", router))
    monkeypatch.setattr(loop_mod, "make_text_writer", lambda router: ("writer", router))
    monkeypatch.setattr(obs_mod, "BrowserObserver", lambda ctx, url: ("browser", url))
    monkeypatch.setattr(obs_mod, "DesktopObserver", lambda ctx, title, *a, **k: ("desktop", title))
    return rec


def test_reflex_requires_decider_and_router(ctx, fake_reflex):
    with pytest.raises(ToolError, match="decision engine"):
        meta.Reflex().run(ctx, goal="g", target="browser")
    ctx.decider = object()
    with pytest.raises(ToolError):
        meta.Reflex().run(ctx, goal="g", target="browser")


def test_reflex_browser(ctx, fake_reflex):
    ctx.decider, ctx.router = "dec", "rtr"
    r = meta.Reflex().run(ctx, goal="tıkla", target="browser", url="https://x", inputs={"ara": "abc"}, max_steps=4)
    assert r.text == "reflex raporu"
    assert fake_reflex["observer"] == ("browser", "https://x")
    assert fake_reflex["decider"] == "dec" and fake_reflex["writer"] == ("writer", "rtr")
    assert fake_reflex["goal"] == "tıkla" and fake_reflex["max_steps"] == 4
    assert fake_reflex["inputs"] == {"ara": "abc"} and fake_reflex["translate"] == ("translate", "rtr")
    assert fake_reflex["trace"].name == "reflex-trace.jsonl" and fake_reflex["notes"] == ""


def test_reflex_desktop_with_notes(workdir, fake_reflex):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), decider="d", router="r")
    meta.Reflex().run(ctx, goal="g", target="desktop", title="Not Defteri", notes="Rules: cards go right")
    assert fake_reflex["observer"] == ("desktop", "Not Defteri") and fake_reflex["notes"] == "Rules: cards go right"
    assert fake_reflex["max_steps"] == 20 and fake_reflex["inputs"] is None


# ================================================================== flight: general


def test_flight_risk_for():
    f = flight_mod.Flight()
    for a in ("kneeboard_build", "kneeboard_note", "kneeboard_clear"):
        assert f.risk_for({"action": a}) == WRITE
    for a in ("status", "kneeboard_show", "checklist_start", "alert_home"):
        assert f.risk_for({"action": a}) == READ
    assert f.risk_for({}) == READ
    assert set(f.parameters["properties"]["action"]["enum"]) == set(flight_mod.ACTIONS)


@pytest.fixture
def no_hub(monkeypatch):
    from alice.sims import hub

    monkeypatch.setattr(hub, "ACTIVE", None)
    return hub


class FakeHub:
    def __init__(self, last_debrief=None):
        self.last_debrief = last_debrief
        self.alerts = []

    def status(self):
        return "yakıt 2000 kg"

    def alert_home(self, nm):
        self.alerts.append(nm)
        return f"{nm} mil uyarısı kuruldu"

    def alert_altitude(self, ft):
        self.alerts.append(("ft", ft))
        return f"{ft} ft uyarısı kuruldu"

    def alert_fuel(self, kg):
        self.alerts.append(("kg", kg))
        return f"{kg} kg uyarısı kuruldu"

    def clear_alerts(self):
        self.alerts.clear()
        return "uyarılar silindi"


def test_alert_home_without_hub(ctx, no_hub):
    assert "isn't running" in flight_mod.Flight().run(ctx, action="alert_home", nm=10).text


@pytest.mark.parametrize("nm", [None, 0, -5])
def test_alert_home_requires_positive_nm(ctx, monkeypatch, nm):
    from alice.sims import hub

    monkeypatch.setattr(hub, "ACTIVE", FakeHub())
    with pytest.raises(ToolError, match="nm"):
        flight_mod.Flight().run(ctx, action="alert_home", nm=nm)


def test_alert_home_and_status_with_hub(ctx, monkeypatch):
    from alice.sims import hub

    fake = FakeHub()
    monkeypatch.setattr(hub, "ACTIVE", fake)
    f = flight_mod.Flight()
    assert f.run(ctx, action="alert_home", nm=12.5).text == "12.5 mil uyarısı kuruldu"
    assert fake.alerts == [12.5]
    assert f.run(ctx, action="status").text == "yakıt 2000 kg"


@pytest.mark.parametrize("nm, want", [("10", 10.0), ("12,5", 12.5), ("20 mil", 20.0)])
def test_alert_home_text_nm(ctx, monkeypatch, nm, want):
    from alice.sims import hub

    fake = FakeHub()
    monkeypatch.setattr(hub, "ACTIVE", fake)
    flight_mod.Flight().run(ctx, action="alert_home", nm=nm)
    assert fake.alerts == [want]


def test_alert_alt_text_with_thousands(ctx, monkeypatch):
    from alice.sims import hub

    fake = FakeHub()
    monkeypatch.setattr(hub, "ACTIVE", fake)
    flight_mod.Flight().run(ctx, action="alert_alt", ft="5.000")
    assert fake.alerts == [("ft", 5000.0)]


def test_alert_home_unparseable_text_nm(ctx, monkeypatch):
    from alice.sims import hub

    monkeypatch.setattr(hub, "ACTIVE", FakeHub())
    with pytest.raises(ToolError, match="nm"):
        flight_mod.Flight().run(ctx, action="alert_home", nm="on")


def test_status_without_hub(ctx, no_hub):
    assert "isn't running" in flight_mod.Flight().run(ctx, action="status").text


# ================================================================== flight: last debrief


def _debrief_dict(**over):
    from alice.sims.flight import Debrief

    names = [f.name for f in dataclasses.fields(Debrief)]
    d = dict(aircraft="F-16C", source="dcs", start_wall=0.0, end_wall=1800.0, duration_s=1800.0, max_g=6.5,
             max_ias_kt=520.0, max_alt_ft=25000.0, fuel_used_kg=None, min_fuel_kg=None,
             approach_on_speed_pct=None, max_sink_final_ms=None, touchdown_vv_ms=None, chaff_used=None,
             flare_used=None)
    d.update(over)
    return {k: v for k, v in d.items() if k in names}


def test_last_debrief_none_saved(ctx, no_hub):
    assert flight_mod.Flight().run(ctx, action="last_debrief").text == "no recorded flights"
    (alice_home() / "flights").mkdir(parents=True)
    assert flight_mod.Flight().run(ctx, action="last_debrief").text == "no recorded flights"


def test_last_debrief_reads_newest_file(ctx, no_hub):
    d = alice_home() / "flights"
    d.mkdir(parents=True)
    (d / "2026-01-01_10-00.json").write_text(json.dumps(_debrief_dict(duration_s=600.0)), encoding="utf-8")
    (d / "2026-02-01_10-00.json").write_text(json.dumps(_debrief_dict(duration_s=1800.0, max_g=7.2)),
                                              encoding="utf-8")
    out = flight_mod.Flight().run(ctx, action="last_debrief").text
    assert "30 dakika" in out and "7.2 G" in out
    assert out.endswith("Rapor: " + str(d / "2026-02-01_10-00.html"))


def test_last_debrief_prefers_live_hub(ctx, monkeypatch):
    from alice.sims import hub

    debrief = SimpleNamespace(summary=lambda: "canlı özet")
    monkeypatch.setattr(hub, "ACTIVE", FakeHub(last_debrief=(debrief, "/tmp/r.html")))
    assert flight_mod.Flight().run(ctx, action="last_debrief").text == "canlı özet\nRapor: /tmp/r.html"


# ================================================================== flight: checklists


@pytest.fixture
def user_checklist():
    folder = alice_home() / "checklists"
    folder.mkdir(parents=True)
    (folder / "zz-deneme.md").write_text("# Zz Deneme Listesi\n<!-- yorum\n- gizli -->\n- Bir\n- İki\n* Üç\n",
                                         encoding="utf-8")
    return folder


def test_checklist_list_includes_user_list(ctx, user_checklist):
    out = flight_mod.Flight().run(ctx, action="checklist_list").text
    assert "zz-deneme: Zz Deneme Listesi (3 steps)" in out


def test_checklist_list_empty(ctx, monkeypatch):
    import alice.sims.checklists as cl

    monkeypatch.setattr(cl, "load_all", lambda: {})
    assert flight_mod.Flight().run(ctx, action="checklist_list").text == "kontrol listesi yok"


def test_checklist_full_flow(ctx, user_checklist):
    f = flight_mod.Flight()
    assert f.run(ctx, action="checklist_start", name="zz deneme").text == "Zz Deneme Listesi. Adım 1/3: Bir"
    assert f.run(ctx, action="checklist_prev").text == "Adım 1/3: Bir"  # does not go back at the start
    assert f.run(ctx, action="checklist_next").text == "Adım 2/3: İki"
    assert f.run(ctx, action="checklist_next").text == "Adım 3/3: Üç"
    assert f.run(ctx, action="checklist_prev").text == "Adım 2/3: İki"
    f.run(ctx, action="checklist_next")
    assert f.run(ctx, action="checklist_next").text == "Zz Deneme Listesi kontrol listesi tamam."
    assert "checklist" not in ctx.state  # closes when finished
    with pytest.raises(ToolError, match="no open checklist"):
        f.run(ctx, action="checklist_next")


def test_checklist_stop(ctx, user_checklist):
    f = flight_mod.Flight()
    f.run(ctx, action="checklist_start", name="zz-deneme")
    assert f.run(ctx, action="checklist_stop").text == "checklist closed"
    assert "checklist" not in ctx.state
    with pytest.raises(ToolError):
        f.run(ctx, action="checklist_stop")


def test_checklist_start_unknown(ctx, monkeypatch):
    import alice.sims.checklists as cl

    monkeypatch.setattr(cl, "load_all", lambda: {})
    with pytest.raises(ToolError, match="list not found: hiçbiri"):
        flight_mod.Flight().run(ctx, action="checklist_start", name="hiçbiri")


def test_checklist_builtin_landing_found(ctx):
    out = flight_mod.Flight().run(ctx, action="checklist_start", name="iniş").text
    assert "Adım 1/" in out


# ================================================================== flight: kneeboard


class FakePage:
    def save(self, path):
        path.write_bytes(b"png")


@pytest.fixture
def dcs_env(monkeypatch, tmp_path):
    import alice.sims.dcs as dcs
    import alice.sims.kneeboard as kb

    sg = tmp_path / "SavedGames"
    sg.mkdir()
    rec: dict = {"dirs": [sg], "latest": None, "read": None, "renders": []}

    def read_mission(path):
        if isinstance(rec["read"], Exception):
            raise rec["read"]
        return rec["read"]

    def render(info, notes):
        rec["renders"].append((info, list(notes)))
        return [FakePage(), FakePage()]

    monkeypatch.setattr(dcs, "saved_games_dirs", lambda: rec["dirs"])
    monkeypatch.setattr(dcs, "latest_mission", lambda dirs: rec["latest"])
    monkeypatch.setattr(dcs, "read_mission", read_mission)
    monkeypatch.setattr(kb, "render", render)
    rec["sg"] = sg
    return rec


def test_kneeboard_show_empty_and_with_notes(ctx, dcs_env):
    f = flight_mod.Flight()
    assert f.run(ctx, action="kneeboard_show").text == "not yok"
    f.run(ctx, action="kneeboard_note", text="  bingo 1500 ")
    out = f.run(ctx, action="kneeboard_show").text
    assert out.endswith(" bingo 1500") and len(out.split(" ")[0]) == 5  # "HH:MM"


def test_kneeboard_note_requires_text(ctx, dcs_env):
    with pytest.raises(ToolError, match="text"):
        flight_mod.Flight().run(ctx, action="kneeboard_note", text="")


def test_kneeboard_note_writes_pages_without_mission(ctx, dcs_env):
    r = flight_mod.Flight().run(ctx, action="kneeboard_note", text="tanker 5Y")
    assert r.text == "kneeboard updated (2 pages; no mission)."
    pages = sorted(p.name for p in (dcs_env["sg"] / "Kneeboard").glob("*.png"))
    assert pages == ["ALICE_01.png", "ALICE_02.png"]
    info, notes = dcs_env["renders"][-1]
    assert info is None and [n["text"] for n in notes] == ["tanker 5Y"]


def test_kneeboard_clear(ctx, dcs_env):
    f = flight_mod.Flight()
    f.run(ctx, action="kneeboard_note", text="a")
    f.run(ctx, action="kneeboard_clear")
    assert dcs_env["renders"][-1][1] == []
    assert f.run(ctx, action="kneeboard_show").text == "not yok"


def test_kneeboard_build_with_explicit_mission(ctx, dcs_env, tmp_path):
    miz = tmp_path / "op.miz"
    miz.write_bytes(b"x")
    dcs_env["read"] = SimpleNamespace(name="Op Kartal", waypoints=[1, 2, 3])
    r = flight_mod.Flight().run(ctx, action="kneeboard_build", mission=str(miz))
    assert r.text == "kneeboard updated (2 pages; mission: Op Kartal, 3 waypoints)."


def test_kneeboard_build_uses_latest_mission(ctx, dcs_env, tmp_path):
    miz = tmp_path / "latest.miz"
    miz.write_bytes(b"x")
    dcs_env["latest"] = miz
    dcs_env["read"] = SimpleNamespace(name="Son", waypoints=[])
    assert "mission: Son, 0 waypoints" in flight_mod.Flight().run(ctx, action="kneeboard_build").text


def test_kneeboard_build_missing_mission_file(ctx, dcs_env, tmp_path):
    r = flight_mod.Flight().run(ctx, action="kneeboard_build", mission=str(tmp_path / "yok.miz"))
    assert "no mission" in r.text


def test_kneeboard_build_unreadable_mission_raises(ctx, dcs_env, tmp_path):
    miz = tmp_path / "bozuk.miz"
    miz.write_bytes(b"x")
    dcs_env["read"] = ValueError("bozuk zip")
    with pytest.raises(ToolError, match="could not read the mission: bozuk zip"):
        flight_mod.Flight().run(ctx, action="kneeboard_build", mission=str(miz))


def test_kneeboard_note_tolerates_unreadable_mission(ctx, dcs_env, tmp_path):
    miz = tmp_path / "bozuk.miz"
    miz.write_bytes(b"x")
    dcs_env["latest"] = miz
    dcs_env["read"] = KeyError("mission")
    assert "no mission" in flight_mod.Flight().run(ctx, action="kneeboard_note", text="n").text


def test_kneeboard_without_dcs_folder(ctx, dcs_env):
    dcs_env["dirs"] = []
    with pytest.raises(ToolError, match="Saved Games"):
        flight_mod.Flight().run(ctx, action="kneeboard_build")


def test_kneeboard_note_saved_even_when_dcs_missing(ctx, dcs_env):
    """Current behavior: the note is saved when the DCS folder is missing, but the tool returns an error."""
    import alice.sims.kneeboard as kb

    dcs_env["dirs"] = []
    with pytest.raises(ToolError):
        flight_mod.Flight().run(ctx, action="kneeboard_note", text="kayıp mı?")
    assert [n["text"] for n in kb.load_notes()] == ["kayıp mı?"]


def test_alt_fuel_alerts_and_clear(ctx, monkeypatch):
    from alice.sims import hub

    fake = FakeHub()
    monkeypatch.setattr(hub, "ACTIVE", fake)
    f = flight_mod.Flight()
    assert "ft" in f.run(ctx, action="alert_alt", ft=5000).text
    assert "kg" in f.run(ctx, action="alert_fuel", kg=1500).text
    assert fake.alerts == [("ft", 5000), ("kg", 1500)]
    with pytest.raises(ToolError, match="kg"):
        f.run(ctx, action="alert_fuel", kg=0)
    assert f.run(ctx, action="alerts_clear").text == "uyarılar silindi" and fake.alerts == []
