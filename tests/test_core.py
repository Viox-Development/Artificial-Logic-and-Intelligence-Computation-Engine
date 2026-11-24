from __future__ import annotations

import json
from datetime import datetime

import pytest

from alice.config import Config, alice_home, default_config_dict
from alice.core.modes import ModeManager
from alice.core.notify import Notification, Notifier
from alice.core.rules import Rule, RuleEngine, RuleSet, parse_rule
from alice.core.schedule import Cron, ScheduledTask, TaskStore, parse_task
from alice.core.service import Core, force_mode, read_state
from alice.core.store import MODE_KEY, Store, parse_time
from alice.decider import Answer, Decision
from alice.tools import ToolContext
from alice.tools.base import ToolError
from alice.tools.core import Metrics, Rules, Schedule


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


def make_notifier(store, mode="normal"):
    mm = ModeManager()
    mm.current = mode
    sent = []
    n = Notifier(store, lambda: mm.policy, desktop=False)
    n.channels.append(lambda note, pol: sent.append(note))
    return n, sent, mm


# -------------------------------------------------------------------- store
def test_parse_time():
    now = ts("2026-10-09T12:00")
    assert parse_time("2h", now) == now - 7200
    assert parse_time("dün", now) == ts("2026-10-08T00:00")
    assert parse_time("2026-10-08T20:00") == ts("2026-10-08T20:00")
    with pytest.raises(ValueError):
        parse_time("geçen yüzyıl")


def test_stats_filtered_by_mode(store):
    store.add(MODE_KEY, text="normal", ts=0)
    store.add(MODE_KEY, text="flight", ts=100)
    store.add(MODE_KEY, text="normal", ts=200)
    for t, v in ((50, 50), (120, 80), (180, 90), (250, 60)):
        store.add("pc.gpu.temp", v, ts=t)
    s = store.stats("pc.gpu.temp", mode="flight")
    assert (s.count, s.max, s.avg, s.max_ts) == (2, 90, 85, 180)
    assert store.stats("pc.gpu.temp").count == 4
    assert store.mode_spans("flight", until=300) == [(100, 200)]
    assert store.keys("pc.*") == ["pc.gpu.temp"]


# -------------------------------------------------------------------- modes
def test_mode_detection_priority_and_quiet_hours():
    mm = ModeManager({"quiet_hours": "23:00-07:00"})
    noon, night = datetime(2026, 1, 1, 12), datetime(2026, 1, 1, 2)
    assert mm.detect(["explorer.exe"], noon) == "normal"
    assert mm.detect(["Unity.exe"], noon) == "dev"
    assert mm.detect(["Unity.exe", "DCS.exe"], night) == "flight"
    assert mm.detect([], night) == "quiet"
    assert mm.policies["flight"].local_gpu is False
    mm.forced = "dev"
    assert mm.detect(["DCS.exe"], noon) == "dev"


def test_flight_mode_silences_non_flight(store):
    n, sent, _ = make_notifier(store, "flight")
    assert not n.send(Notification("disk", severity="warn", topic="pc"))
    assert n.send(Notification("yakıt", severity="info", topic="flight"))
    assert n.send(Notification("acil", severity="critical", topic="pc"))
    assert [x.title for x in sent] == ["yakıt", "acil"]
    muted = [e for e in store.events() if not e.delivered]
    assert [e.title for e in muted] == ["disk"]


# -------------------------------------------------------------------- rules
def feed(store, key, values, start=1000.0, step=15.0):
    for i, v in enumerate(values):
        store.add(key, v, ts=start + i * step)
    return start + (len(values) - 1) * step


def engine(store, tmp_path, rules, mode="normal", **kw):
    rs = RuleSet(tmp_path / "rules.json")
    rs.rules = rules
    n, sent, mm = make_notifier(store, mode)
    return RuleEngine(rs, store, n, mode=lambda: mm.current, **kw), sent


def test_sustained_rule_fires_once_then_cools_down(store, tmp_path):
    rule = Rule("pc.gpu.temp", ">", 85, "GPU {value:.0f}", duration=60, cooldown=600)
    eng, sent = engine(store, tmp_path, [rule])
    now = feed(store, "pc.gpu.temp", [90, 91, 92])  # only 30 sec: not long enough
    assert eng.evaluate(now) == []
    now = feed(store, "pc.gpu.temp", [93, 94, 95], start=now + 15)
    assert [f.text for f in eng.evaluate(now)] == ["GPU 95"]
    now = feed(store, "pc.gpu.temp", [96], start=now + 15)
    assert eng.evaluate(now) == []  # cooldown
    assert len(sent) == 1


def test_rule_needs_whole_window(store, tmp_path):
    eng, _ = engine(store, tmp_path, [Rule("pc.gpu.temp", ">", 85, "x", duration=60)])
    now = feed(store, "pc.gpu.temp", [90, 70, 90, 90, 90])
    assert eng.evaluate(now) == []  # 70 is inside the window


def test_glob_rule_and_match(store, tmp_path):
    eng, sent = engine(store, tmp_path, [Rule("pc.disk.*.percent", ">", 95, "{match} dolu")])
    store.add("pc.disk.c.percent", 97, ts=1000)
    store.add("pc.disk.d.percent", 50, ts=1000)
    assert [f.text for f in eng.evaluate(1010)] == ["c dolu"]
    assert eng.evaluate(1000 + 10_000) == []  # the data is stale


def test_rule_mode_filter(store, tmp_path):
    eng, _ = engine(store, tmp_path, [Rule("dcs.fuel.min_left", "<", 10, "yakıt", topic="flight", modes=["flight"])])
    store.add("dcs.fuel.min_left", 5, ts=1000)
    assert eng.evaluate(1001) == []


class FakeDecider:
    def __init__(self, p):
        self.p = p
        self.calls = []

    def decide(self, state, questions):
        self.calls.append(state)
        return Decision({"notify": Answer("noul", self.p, 1.0)}, "fake", 0.0)


@pytest.mark.parametrize("p,fires", [(0.9, True), (0.1, False)])
def test_rule_confirm_asks_decider(store, tmp_path, p, fires):
    dec = FakeDecider(p)
    eng, _ = engine(store, tmp_path, [Rule("watch.hr", ">", 120, "nabız", confirm="Uyarmaya değer mi?")], decider=dec)
    store.add("watch.hr", 130, ts=1000)
    assert bool(eng.evaluate(1001)) is fires and dec.calls


def test_rule_agent_action(store, tmp_path):
    prompts = []
    rule = Rule("pc.cpu.percent", ">", 90, "CPU", action="agent", prompt="neden yüksek, bak")
    eng, sent = engine(store, tmp_path, [rule], run_agent=lambda p: prompts.append(p) or "Chrome yüzünden")
    eng.sync_agent = True
    store.add("pc.cpu.percent", 99, ts=1000)
    eng.evaluate(1001)
    assert "neden yüksek" in prompts[0] and sent[0].body == "Chrome yüzünden"


def test_ruleset_persistence(tmp_path):
    rs = RuleSet(tmp_path / "rules.json")
    assert any(r.id == "gpu-hot" for r in rs.rules)  # defaults
    rs.add(Rule("watch.hr", ">", 120, "nabız", id="hr"))
    rs.set_enabled("gpu-hot", False)
    rs2 = RuleSet(tmp_path / "rules.json")
    assert rs2.get("hr") and rs2.get("gpu-hot").enabled is False
    with pytest.raises(ValueError):
        Rule("x", "=>", 1, "m")


class FakeRouter:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, role, messages, **kw):
        class R:
            text = self.reply
        return R()


def test_parse_rule_from_language():
    reply = json.dumps({"metric": "watch.hr", "op": ">", "threshold": 120, "duration": 600, "severity": "warn",
                        "topic": "health", "modes": [], "message": "Nabzın {value:.0f}", "confirm": "",
                        "action": "notify", "prompt": ""})
    r = parse_rule("nabzım uzun süre yüksek kalırsa uyar", FakeRouter(reply))
    assert (r.metric, r.duration, r.description) == ("watch.hr", 600, "nabzım uzun süre yüksek kalırsa uyar")


# ----------------------------------------------------------------- schedule
@pytest.mark.parametrize("expr,after,expected", [
    ("0 23 * * 0", "2026-10-09T12:00", "2026-10-11T23:00"),   # Sunday night
    ("*/15 * * * *", "2026-10-09T12:07", "2026-10-09T12:15"),
    ("30 8 1 * *", "2026-10-09T12:00", "2026-11-01T08:30"),
    ("0 9 * * 1-5", "2026-10-09T09:00", "2026-10-12T09:00"),  # Friday after 9 -> Monday
    ("0 0 29 2 *", "2026-03-01T00:00", "2028-02-29T00:00"),
])
def test_cron_next(expr, after, expected):
    assert Cron(expr).next_after(datetime.fromisoformat(after)) == datetime.fromisoformat(expected)


def test_cron_invalid_and_describe():
    with pytest.raises(ValueError):
        Cron("61 * * * *")
    with pytest.raises(ValueError):
        Cron("* * *")
    assert Cron("0 23 * * 0").describe() == "pazar 23:00"


def test_due_and_catch_up(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    created = ts("2026-10-05T12:00")
    a = store.add(ScheduledTask("0 23 * * *", "yedekle", created=created, catch_up=True))
    b = store.add(ScheduledTask("0 23 * * *", "özet", created=created, catch_up=False))
    assert store.due(ts("2026-10-05T22:59")) == []
    # both run exactly on time
    assert {t.id for t, late in store.due(ts("2026-10-05T23:02"))} == {a.id, b.id}
    # the computer was off for days: only the catch_up one runs, as a make-up
    due = store.due(ts("2026-10-08T10:00"))
    assert [(t.id, late) for t, late in due] == [(a.id, True)]
    assert TaskStore(tmp_path / "tasks.json").get(b.id).last_run == ts("2026-10-08T10:00")
    store.mark_run(a, "ok", ts("2026-10-08T10:00"))
    assert store.due(ts("2026-10-08T10:01")) == []


def test_parse_task():
    reply = '{"cron": "0 23 * * 0", "prompt": "D:/Unity klasörünü yedekle", "catch_up": true}'
    t = parse_task("her pazar gece unity projelerimi yedekle", FakeRouter(reply))
    assert t.cron == "0 23 * * 0" and t.catch_up
    assert not t.once
    one = parse_task("yarın 17:00'de toplantıyı hatırlat", FakeRouter(
        '{"cron": "0 17 10 11 *", "prompt": "Toplantıyı hatırlat", "catch_up": true, "once": true}'))
    assert one.once


# ------------------------------------------------------------------ service
class FakeMonitor:
    name = "fake"

    def __init__(self):
        self.processes = {"DCS.exe"}
        self.value = 90

    def collect(self):
        return [("pc.gpu.temp", self.value, None)]


def make_core(tmp_path, run_agent=None):
    cfg = Config(default_config_dict())
    cfg.data["core"]["quiet_hours"] = ""
    st = Store(":memory:")
    n, sent, _ = make_notifier(st)
    core = Core(cfg, store=st, monitors=[FakeMonitor()], rules=RuleSet(tmp_path / "rules.json"),
                tasks=TaskStore(tmp_path / "tasks.json"), run_agent=run_agent, notifier=n)
    n.policy = lambda: core.modes.policy
    return core, sent


def test_core_tick_records_mode_and_state(tmp_path):
    core, _ = make_core(tmp_path)
    core.tick(1000)
    assert core.modes.current == "flight"
    assert store_mode(core.store) == "flight"
    s = read_state()
    assert s["running"] is False or s["mode"] == "flight"  # running is False because the timestamp is old
    assert json.loads((alice_home() / "core-state.json").read_text(encoding="utf-8"))["policy"]["local_gpu"] is False
    force_mode("dev")
    core.tick(1015)
    assert core.modes.current == "dev"
    force_mode(None)
    core.tick(1030)
    assert core.modes.current == "flight"


def store_mode(st):
    return st.last(MODE_KEY)[2]


def test_core_runs_due_task(tmp_path):
    import threading
    import time

    done = threading.Event()

    def agent(prompt):
        done.set()
        return f"bitti: {prompt}"

    core, sent = make_core(tmp_path, run_agent=agent)
    core.modes.forced = None
    core.monitors[0].processes = set()
    now = 1_760_000_000  # values close to 1970 make datetime.timestamp() fail on Windows
    t = core.tasks.add(ScheduledTask("* * * * *", "rapor yaz", created=now - 100))
    core.tick(now)
    assert done.wait(5)
    for _ in range(100):
        if not core.running_tasks:
            break
        time.sleep(0.05)
    assert core.tasks.get(t.id).last_result == "bitti: rapor yaz"
    assert any(x.kind == "task" for x in sent)


def test_one_time_task_removes_itself_after_running(tmp_path):
    """Live log (7 Nov): task "beni 1 dakika sonra ara" ("call me in 1 minute") stayed in the list with cron
    '56 3 7 11 *'; it would call again every year, and deleting it was left to the user."""
    import time

    core, sent = make_core(tmp_path, run_agent=lambda prompt: "aradım")
    core.modes.forced = None
    core.monitors[0].processes = set()
    now = 1_760_000_000
    once = core.tasks.add(ScheduledTask("* * * * *", "kullanıcıyı ara", created=now - 100, once=True))
    keep = core.tasks.add(ScheduledTask("* * * * *", "rapor yaz", created=now - 100))
    assert "tek sefer" in once.summary() and "tek sefer" not in keep.summary()
    core.tick(now)
    for _ in range(100):
        if not core.running_tasks and core.tasks.get(once.id) is None:
            break
        time.sleep(0.05)
    assert core.tasks.get(once.id) is None and core.tasks.get(keep.id) is not None
    assert TaskStore(tmp_path / "tasks.json").get(once.id) is None  # also deleted from disk
    assert sum(x.kind == "task" for x in sent) == 2  # the result was still reported


# -------------------------------------------------------------------- tools
def test_tools(tmp_path, isolated_home):
    cfg = Config(default_config_dict())
    ctx = ToolContext(cwd=tmp_path, config=cfg)
    st = ctx.state["core_store"] = Store(":memory:")
    st.add(MODE_KEY, text="flight", ts=ts("2026-10-08T20:00"))
    st.add("pc.gpu.temp", 78, ts=ts("2026-10-08T20:30"))
    st.add(MODE_KEY, text="normal", ts=ts("2026-10-08T21:00"))
    st.add("pc.gpu.temp", 50, ts=ts("2026-10-08T22:00"))
    out = Metrics().run(ctx, action="stats", key="pc.gpu.temp", since="2026-10-08T00:00", mode="flight").text
    assert "en yüksek 78.0" in out and "1 ölçüm" in out
    assert "flight: 08.10 20:00 - 21:00" in Metrics().run(ctx, action="modes", since="2026-10-08T00:00").text

    r = Rules()
    assert r.risk_for({"action": "add", "rule_action": "agent"}) == "danger"
    r.run(ctx, action="add", metric="watch.hr", op=">", threshold=120, duration=600, message="nabız", id="hr")
    assert "watch.hr > 120" in r.run(ctx, action="list").text

    s = Schedule()
    assert s.risk_for({"action": "add"}) == "danger"
    assert "pazar 23:00" in s.summary({"action": "add", "cron": "0 23 * * 0", "description": "yedek"})
    out = s.run(ctx, action="add", cron="0 23 * * 0", prompt="yedekle", description="yedek").text
    assert "task set up" in out and "core is not running" in out
    tid = TaskStore().tasks[0].id
    s.run(ctx, action="run_now", id=tid)
    assert TaskStore().due()[0][0].id == tid
    # remove with an unknown id must fail (it used to return a success result).
    with pytest.raises(ToolError, match="no task"):
        s.run(ctx, action="remove", id="olmayan-gorev")
    assert s.run(ctx, action="remove", id=tid).text == "deleted"
    assert TaskStore().get(tid) is None


def test_desktop_toasts_skip_flight_and_repeats(monkeypatch, tmp_path):
    from alice.core import notify
    from alice.core.modes import Policy
    from alice.core.notify import Notification, Notifier
    from alice.core.store import Store

    shown = []
    monkeypatch.setattr(notify, "desktop_notify", lambda n: shown.append(n.body))
    n = Notifier(Store(tmp_path / "s.db"), lambda: Policy(mode="normal"))
    n.send(Notification("Uçuş", "Teker açık", "warn", "flight", kind="flight"))
    n.send(Notification("PC", "GPU sıcak", "warn", "pc"))
    n.send(Notification("PC", "GPU sıcak", "warn", "pc"))
    assert shown == ["GPU sıcak"]


def test_rule_agent_action_does_not_block_tick(store, tmp_path):
    import threading

    gate = threading.Event()
    rule = Rule("pc.cpu.percent", ">", 90, "CPU", action="agent", prompt="bak")
    eng, sent = engine(store, tmp_path, [rule], run_agent=lambda p: gate.wait(5) and "bitti")
    store.add("pc.cpu.percent", 99, ts=1000)
    assert eng.evaluate(1001)  # returns before the agent finishes
    gate.set()


def test_impossible_cron_rejected_and_skipped(tmp_path):
    import json as _json

    from alice.core.schedule import ScheduledTask, TaskStore

    with pytest.raises(ValueError):
        ScheduledTask("0 9 30 2 *", "x")
    path = tmp_path / "tasks.json"
    good = ScheduledTask("* * * * *", "iyi", created=1_760_000_000)  # values around 1970 raise OSError on Windows
    from dataclasses import asdict

    bad = asdict(good) | {"cron": "0 9 30 2 *", "id": "bad"}
    path.write_text(_json.dumps([bad, asdict(good)]), encoding="utf-8")
    ts = TaskStore(path)
    assert [t.id for t in ts.tasks] == [good.id]
    assert [t.id for t, _ in ts.due(now=1_760_010_000)] == [good.id]


def test_corrupt_task_file_keeps_memory(tmp_path):
    from alice.core.schedule import ScheduledTask, TaskStore

    ts = TaskStore(tmp_path / "tasks.json")
    ts.add(ScheduledTask("0 9 * * *", "sabah"))
    (tmp_path / "tasks.json").write_text('[{"cron": "0 9', encoding="utf-8")
    ts.load()
    assert len(ts.tasks) == 1
    assert TaskStore(tmp_path / "tasks.json").tasks == []


def test_core_prime_writes_mode_before_first_tick_and_hooks_get_policy(tmp_path):
    core, _ = make_core(tmp_path)
    core.prime()  # DCS is open: the voice module must see "GPU yok" (no GPU) before it starts
    assert json.loads((alice_home() / "core-state.json").read_text(encoding="utf-8"))["policy"]["local_gpu"] is False
    seen = []
    core.hooks.append(lambda pol: seen.append(pol.local_gpu))
    core.hooks.append(lambda pol: 1 / 0)  # a crashing hook must not break the tick
    core.tick(1000)
    assert seen == [False]


def test_muted_notification_recorded_once(store):
    n, sent, _mm = make_notifier(store, mode="flight")
    for _ in range(3):  # a report muted during flight is retried on every tick
        assert not n.send(Notification("ALICE sağlık raporu: 2 sorun", "x", "warn", "core"))
    assert sent == [] and len(store.events(limit=10)) == 1


def test_missed_one_time_task_without_catch_up_is_removed(tmp_path):
    """If a one-time task missed while the computer was off and will not be caught up stays in the list, cron
    regenerates it on the same day the next year."""
    store = TaskStore(tmp_path / "tasks.json")
    created = ts("2026-10-05T12:00")
    once = store.add(ScheduledTask("0 23 5 10 *", "ara", created=created, catch_up=False, once=True))
    keep = store.add(ScheduledTask("0 23 * * *", "özet", created=created, catch_up=False))
    assert store.due(ts("2026-10-08T10:00")) == []
    assert store.get(once.id) is None and store.get(keep.id) is not None
    assert TaskStore(tmp_path / "tasks.json").get(once.id) is None


def test_disk_low_rule_by_free_gb(store, tmp_path):
    from alice.core.rules import DEFAULT_RULES

    rule = next(r for r in DEFAULT_RULES if r.id == "disk-low")
    eng, _ = engine(store, tmp_path, [rule])
    store.add("pc.disk.c.free_gb", 0.02, ts=1000)
    store.add("pc.disk.f.free_gb", 76.0, ts=1000)  # big data disk is full by % but has room: must stay silent
    assert [f.key for f in eng.evaluate(1010)] == ["pc.disk.c.free_gb"]


def test_later_default_rule_added_to_old_rules_file(tmp_path):
    import json

    from alice.core.rules import RuleSet

    p = tmp_path / "rules.json"
    p.write_text(json.dumps([{"metric": "pc.disk.*.percent", "op": ">", "threshold": 95, "message": "x",
                              "id": "disk-full", "enabled": False}]), encoding="utf-8")
    rs = RuleSet(p)
    assert {r.id for r in rs.rules} == {"disk-full", "disk-low"}
    assert not rs.get("disk-full").enabled  # a rule the user turned off stays off
    removed = rs.remove("disk-low")
    assert removed
    assert RuleSet(p).get("disk-low") is None  # a deleted default does not come back
