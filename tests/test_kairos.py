"""KAIROS: a background watch that wakes at intervals it chooses itself."""

from __future__ import annotations

from datetime import datetime

from alice import kairos
from alice.core.modes import Policy
from alice.tools.base import ToolContext
from tests.conftest import make_config


def cfg(**k):
    c = make_config({"brain": ["a:m"]})
    c.data["kairos"] = {"enabled": True, "quiet_hours": "", **k}
    return c


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def noon():
    return datetime(2026, 11, 13, 12, 0).timestamp()


def make(tmp_path, runner, c=None, policy=None, **kw):
    clock = Clock(noon())
    k = kairos.Kairos(c or cfg(), runner=runner, clock=clock, idle=lambda: 30.0,
                      policy=lambda: policy, path=tmp_path / "k.json", **kw)
    return k, clock


def run_sync(k):
    """Wait for the tick() thread."""
    started = k.tick()
    if started:
        k._thread.join(5)
    return started


def test_first_tick_schedules_then_wakes_and_respects_chosen_sleep(tmp_path):
    prompts = []
    k, clock = make(tmp_path, lambda p: (prompts.append(p) or "sessiz", (45, "sakin")))
    assert not run_sync(k)  # first start: scheduled for a bit later
    st = kairos.load_state(tmp_path / "k.json")
    assert st["next"] == clock.t + 10 * 60
    clock.t = st["next"]
    assert run_sync(k) and prompts and "kairos_sleep" in prompts[0]
    st = kairos.load_state(tmp_path / "k.json")
    assert st["next"] == clock.t + 45 * 60 and st["wakes"] == 1
    assert st["history"][-1]["summary"] == "sessiz"
    assert not run_sync(k)  # its time has not come


def test_sleep_is_clamped_and_default_when_not_chosen(tmp_path):
    k, clock = make(tmp_path, lambda p: ("bildirim gönderdim", (2, "")))
    kairos.save_state({"next": clock.t}, tmp_path / "k.json")
    run_sync(k)
    assert kairos.load_state(tmp_path / "k.json")["next"] == clock.t + 20 * 60  # at least 20
    k.runner = lambda p: ("sessiz", None)
    clock.t += 3600
    kairos.save_state({**kairos.load_state(tmp_path / "k.json"), "next": clock.t}, tmp_path / "k.json")
    run_sync(k)
    assert kairos.load_state(tmp_path / "k.json")["next"] == clock.t + 130 * 60  # (20+240)/2


def test_blocked_by_mode_quiet_hours_and_daily_limit(tmp_path):
    called = []
    k, clock = make(tmp_path, lambda p: (called.append(1) or "x", None), policy=Policy("flight", "warn"))
    kairos.save_state({"next": clock.t}, tmp_path / "k.json")
    assert not run_sync(k) and not called
    assert kairos.load_state(tmp_path / "k.json")["skipped"].startswith("mod")
    # quiet hours
    k2, c2 = make(tmp_path / "b", lambda p: (called.append(1) or "x", None), c=cfg(quiet_hours="11:00-13:00"))
    (tmp_path / "b").mkdir()
    kairos.save_state({"next": c2.t}, tmp_path / "b" / "k.json")
    assert not run_sync(k2) and not called
    # daily limit
    k3, c3 = make(tmp_path / "c", lambda p: (called.append(1) or "x", None), c=cfg(daily_wakes=2))
    (tmp_path / "c").mkdir()
    kairos.save_state({"next": c3.t, "day": "2026-11-13", "wakes": 2}, tmp_path / "c" / "k.json")
    assert not run_sync(k3) and not called
    assert kairos.load_state(tmp_path / "c" / "k.json")["next"] > c3.t + 12 * 3600  # tomorrow morning


def test_forced_wake_ignores_gates_and_disabled_state_wins(tmp_path):
    called = []
    k, clock = make(tmp_path, lambda p: (called.append(1) or "sessiz", None), policy=Policy("flight", "warn"))
    kairos.save_state({"next": clock.t + 9999}, tmp_path / "k.json")
    kairos.request_wake(tmp_path / "k.json")
    clock.t = kairos.load_state(tmp_path / "k.json")["next"]
    assert run_sync(k) and called
    kairos.save_state({**kairos.load_state(tmp_path / "k.json"), "enabled": False, "next": 0}, tmp_path / "k.json")
    assert not k.tick()  # the off switch in the state file overrides the config


def test_runner_error_does_not_break_and_events_logged(tmp_path):
    class Store:
        def __init__(self):
            self.rows = []

        def add_event(self, *a, **kw):
            self.rows.append((a, kw))

        def events(self, since=None, limit=50):
            return []

    store = Store()

    def boom(p):
        raise RuntimeError("model yok")

    k, clock = make(tmp_path, boom, store=store)
    kairos.save_state({"next": clock.t}, tmp_path / "k.json")
    run_sync(k)
    st = kairos.load_state(tmp_path / "k.json")
    assert st["history"][-1]["summary"].startswith("hata") and st["next"] > clock.t
    assert store.rows and store.rows[-1][1]["topic"] == "kairos"


def test_context_has_followups_and_history(tmp_path):
    class F:
        def __init__(self, text, due, kind="ask"):
            self.text, self.due, self.kind = text, due, kind

    class Mem:
        class notes:
            @staticmethod
            def logs_since(ts):
                return "- 10:00 testler yazıldı"

        def due_followups(self, now):
            return [F("Sınav nasıl geçti?", now - 60)]

        def followups(self):
            return [F("Diş randevusu", noon() + 3600, "remind")]

    k, clock = make(tmp_path, lambda p: ("", None), memory=lambda: Mem())
    text = k.context(clock.t, {"history": [{"ts": clock.t - 3600, "summary": "sessiz"}]})
    assert "Sınav nasıl geçti?" in text and "Diş randevusu" in text and "testler yazıldı" in text
    assert "Your previous wake-ups" in text and "at the computer" in text


def test_sleep_tool_records_choice():
    ctx = ToolContext(cwd=".", config=cfg())
    kairos.KairosSleep().run(ctx, minutes=90, reason="akşam sakin")
    assert ctx.state["kairos_sleep"] == (90, "akşam sakin")


def test_tool_on_off_and_status(tmp_path):
    from alice.tools.companions import Kairos as Tool

    c = cfg(enabled=False)
    ctx = ToolContext(cwd=tmp_path, config=c)
    assert "kapalı" in Tool().run(ctx, "status").text
    Tool().run(ctx, "on")
    assert kairos.is_enabled(c) and "açık" in Tool().run(ctx, "status").text


def test_concurrent_state_writes_do_not_fail_or_leave_temp_files(tmp_path):
    import threading

    p = tmp_path / "kairos.json"
    errors = []

    def work(i):
        for n in range(30):
            try:
                kairos.save_state({"writer": i, "n": n}, p)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # One of the writers sharing "kairos.tmp" got a FileNotFoundError.
    assert errors == [] and kairos.load_state(p)["n"] == 29
    assert [x.name for x in tmp_path.iterdir()] == ["kairos.json"]
