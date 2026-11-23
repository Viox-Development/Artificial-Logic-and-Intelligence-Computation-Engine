"""Core service: a loop that runs in the background at fixed intervals.

Each tick: collect data from the observers -> write it to the store -> update the mode -> evaluate the
rules -> run the due tasks. Jobs that need an agent (tasks, action=agent rules) run in a separate thread
so they do not hold up the loop.

The live state is written to ~/.alice/core-state.json, so the app, the REPL or the voice module can later
read "which mode are we in right now, can I use the GPU?" without connecting to the service.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psutil

from alice.config import Config, alice_home
from alice.core.modes import ModeManager, Policy
from alice.core.monitors import Monitor, PCMonitor
from alice.core.notify import Notification, Notifier
from alice.core.rules import RuleEngine, RuleSet
from alice.core.schedule import ScheduledTask, TaskStore
from alice.core.store import MODE_KEY, Store

MODE_NAMES = {"normal": "normal", "dev": "geliştirme", "flight": "uçuş", "quiet": "sessiz"}


# When an event containing these words ends, ALICE asks "how did it go?" (not for every meeting).
CALENDAR_ASK = re.compile(r"sınav|sinav|vize|final|quiz|maç|mac\b|mülakat|mulakat|görüşme|randevu|doktor|dişçi|"
                          r"hastane|sunum|yarışma|turnuva|uçuş|seyahat|tatil|konser|exam|interview|match|trip|flight",
                          re.I)


def calendar_followups(mem, cfg, now: datetime | None = None, client: Any = None) -> int:
    """If Google Calendar is connected, turn the important events of the next two days into follow-ups:
    ALICE asks about them the next day."""
    from datetime import timedelta

    from alice.connect import runner
    from alice.connect.manifest import discover

    m = discover(cfg)[0].get("google_calendar")
    if m is None or not runner.ready(m)[0] or "upcoming" not in m.actions:
        return 0
    now = (now or datetime.now()).astimezone()
    data = runner.call(m, m.actions["upcoming"], {"timeMin": now.isoformat(), "timeMax": (now + timedelta(days=2))
                       .isoformat(), "maxResults": 20}, client=client, cache=False)
    added = 0
    for ev in (runner.pick(data, "items") or [])[:15]:
        title = str(ev.get("summary") or "")
        start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""
        if not title or not start or not CALENDAR_ASK.search(title):
            continue
        try:
            day = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        due = datetime.combine(day + timedelta(days=1), datetime.min.time()).replace(hour=9).timestamp()
        mem.add_followup(f"Takvimdeki '{title}' ({day:%d.%m}) nasıl geçti, sor.", due, "ask")
        added += 1
    return added


def state_path() -> Path:
    return alice_home() / "core-state.json"


def force_path() -> Path:
    return alice_home() / "core-force"


def force_mode(mode: str | None) -> None:
    """Pin the mode by hand (None = automatic). The running core reads it on the next tick."""
    p = force_path()
    if mode:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(mode, encoding="utf-8")
    elif p.exists():
        p.unlink()


def stop_path() -> Path:
    return alice_home() / "core-stop"


def request_stop() -> None:
    """Ask the running core to shut down (the Stop / Restart buttons in the app). It notices within a second."""
    p = stop_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()), encoding="utf-8")


def _take_stop_request() -> bool:
    try:
        stop_path().unlink()
        return True
    except OSError:
        return False


def read_state() -> dict[str, Any]:
    """The last state the service wrote. If the service is not running, {"running": False}."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    pid = data.get("pid")
    data["running"] = bool(pid) and psutil.pid_exists(pid) and time.time() - data.get("ts", 0) < 300
    return data


def make_agent_runner(cfg: Config, cwd: Path | None = None) -> Callable[[str], str]:
    """Unattended agent: a clean context on every call. Steps that need approval are rejected."""

    def run(prompt: str) -> str:
        from alice.agent import Agent, NullUI
        from alice.decider import Decider
        from alice.permissions import Permissions
        from alice.providers.router import Router
        from alice.tools import ToolContext, all_tools, cleanup

        router = Router(cfg)
        decider = Decider.from_config(cfg, router)
        ctx = ToolContext(cwd=cwd or Path.home(), config=cfg, router=router, decider=decider)
        mode = cfg.data.get("core", {}).get("task_permission_mode", "auto")
        agent = Agent(cfg, router, all_tools(cfg), ctx, Permissions(mode), NullUI(approve="n"), decider)
        try:
            text = agent.run_turn(
                prompt + "\n\n(Bu görev arka planda, kullanıcı başında değilken çalışıyor. Soru sorma; "
                         "bitince sonucu 1-3 kısa cümleyle Türkçe özetle.)")
        finally:
            cleanup(ctx)
            decider.close()
            router.close()  # a new router for every task, so HTTP connections don't pile up for hours
        return text or "(sonuç yok)"

    return run


class Core:
    def __init__(self, cfg: Config, store: Store | None = None, monitors: list[Monitor] | None = None,
                 notifier: Notifier | None = None, rules: RuleSet | None = None, tasks: TaskStore | None = None,
                 decider=None, run_agent: Callable[[str], str] | None = None,
                 restart: Callable[[], None] | None = None, standing=None):
        self.cfg = cfg
        c = cfg.data.get("core", {})
        self.interval = float(c.get("interval", 15))
        self.voice = "off"  # written to core-state.json: "listening" (microphone is being listened to), "no-mic", "off"
        self.retention_days = float(c.get("retention_days", 30))
        self.store = store or Store()
        self.pc = PCMonitor()
        self.modes = ModeManager(c)
        self.notifier = notifier or Notifier(self.store, lambda: self.modes.policy,
                                             desktop=bool(c.get("desktop_notifications", True)))
        if monitors is None:
            monitors = [self.pc]
            export = cfg.data.get("watch", {}).get("export_path", "")
            if export:
                from alice.health.watch import WatchMonitor

                monitors.append(WatchMonitor(self.store, export))
            # Feeds: report important things from connected services (~/.alice/feeds.toml). The monitor is attached
            # even with no feeds: a feed added from the chat takes effect without a core restart (costs nothing
            # when empty).
            from alice.core.feeds import FeedMonitor

            monitors.append(FeedMonitor(self.notifier.send, run_agent, cfg, decider=decider,
                                        emit=lambda s: self.store.add_event("info", s, topic="feed")))
            if c.get("net_watch", True):
                from alice.core.watchdogs import NetWatch

                monitors.append(NetWatch(self.store, self.notifier.send))
            if c.get("phone_notifications", True):
                from alice.home.phone_watch import PhoneWatch

                monitors.append(PhoneWatch(self.notifier.send, mode=lambda: self.modes.current))
        self.monitors: list[Monitor] = monitors
        from alice.core.watchdogs import GameHealth

        self.game_health = GameHealth(self.store, self.notifier.send)
        self.rules = rules or RuleSet()
        self.tasks = tasks or TaskStore()
        self.run_agent = run_agent
        # Tasks ("Görevler", alice/core/tasks.py): standing responsibilities with their own triggers, memory and
        # approvals. They run only when the core can run an agent (the same condition as scheduled jobs).
        if standing is None:
            from alice.core.tasks import TaskBook, TaskRunner, make_task_agent

            standing = TaskRunner(TaskBook(), make_task_agent(cfg) if run_agent is not None else None,
                                  notify=self._task_notify, store=self.store, mode=lambda: self.modes.current,
                                  cfg=cfg)
        self.standing = standing
        self.engine = RuleEngine(self.rules, self.store, self.notifier, decider, run_agent,
                                 mode=lambda: self.modes.current)
        self.stop_event = threading.Event()
        self.running_tasks: set[str] = set()
        # Called on every tick with the mode policy (e.g. voice: when DCS starts, the models are unloaded from the GPU).
        self.hooks: list[Callable[[Policy], None]] = []
        from alice.now import Anticipator

        self.anticipator = Anticipator(self.store)  # preparation before an expected game (the voice module adds a hook)
        from alice.anticipate import Morning, QuietGame

        ant = cfg.data.get("anticipate", {}) or {}
        self.quiet_game = QuietGame(enabled=bool(ant.get("quiet_notifications", True)))
        self.morning = Morning(cfg) if ant.get("morning", True) else None
        if ant.get("game_update", True):
            self.anticipator.hooks.append(self._prep_game_update)
        self._last_prune = 0.0
        self._last_files: float | None = None  # None: the files have not been checked yet at all
        self._lock = threading.Lock()
        # Self-update: the check runs in a background thread (so the network does not slow down the ticks); restart
        # only when no task is running.
        self._last_update_check = time.time()  # don't check right at startup: the app already does
        self._update_thread: threading.Thread | None = None
        self.restart_pending = False
        self.restart = restart or self._restart_process
        self.busy_hook: Callable[[], bool] | None = None  # is a chat running in the app attached to the core?
        # Memory job: summarize finished conversations and report reminders due today
        # (every 10 min, in the background).
        self.memory_every = float(c.get("memory_every_minutes", 10)) * 60
        self._last_memory = time.monotonic()
        self._memory_thread: threading.Thread | None = None
        self._router: Any = None
        # PR tracking (alice/prwatch.py): the watched PRs are checked in the background every few minutes.
        self._prwatch_last = 0.0
        self._prwatch_thread: threading.Thread | None = None
        # When the app connects to the core: (chat id, message) -> "" or the reason it could not be delivered
        # (alice/core/cli.py).
        self.chat_delivery: Callable[[str, str], str] | None = None
        self.project_starter: Callable[[dict[str, Any]], str] | None = None  # scheduled project (provided by the app)
        # Welcome back after a long absence (alice/presence.py); greeter.voice is attached when voice starts.
        from alice.presence import Greeter

        self.greeter = Greeter(cfg, self.notifier.send, memory=self._memory, router=self.router)
        # Background watch (alice/kairos.py): wakes up at intervals it chooses itself and keeps an eye on the state.
        from alice.kairos import Kairos

        self.kairos = Kairos(cfg, self.store, memory=self._memory, policy=lambda: self.modes.policy)

    # ------------------------------------------------------------------ tick
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        rows = []
        for m in self.monitors:
            try:
                rows += m.collect()
            except Exception as e:  # if one observer crashes, the others still run
                try:
                    self.store.add_event("error", f"{getattr(m, 'name', m)} gözlemcisi hata verdi", str(e)[:300])
                except Exception:  # noqa: BLE001
                    pass
        if rows:
            self.store.add_many(rows, now)
        self._update_mode(now)
        try:
            self.game_health.check(self.modes.current, now)
        except Exception as e:  # noqa: BLE001 - the watchdog must not stop the tick
            self.store.add_event("error", "oyun sağlığı kontrolü hata verdi", repr(e)[:300], topic="core")
        for hook in self.hooks:
            try:
                hook(self.modes.policy)
            except Exception as e:  # noqa: BLE001 - even if one hook crashes, the tick goes on
                self.store.add_event("error", "çekirdek kancası hata verdi", repr(e)[:300], topic="core")
        # The steps are independent: when one (a broken rule, a locked file) failed on every tick, the rest
        # (tasks, update, state file) never ran; once the state file got stale, the app thought the core was off.
        # The same error is written once a day.
        steps: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("kural/görev dosyası yükleme", self._reload_if_changed),
            ("kural değerlendirme", lambda: self.engine.evaluate(now)),
            ("zamanlanmış görevler", lambda: self._run_due(now)),
            ("görevler", lambda: self.standing.tick(now)),
            ("güncelleme kontrolü", lambda: self._maybe_update(now)),
            ("hafıza işi", self._maybe_memory),
            ("PR takibi", lambda: self._maybe_prwatch(now)),
        )
        for name, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001 - a step must not stop the tick
                self._step_error(name, e)
        try:
            self.greeter.tick()
        except Exception as e:  # noqa: BLE001 - the greeter must not stop the core
            self.store.add_event("error", "karşılama hata verdi", repr(e)[:300], topic="core")
        try:
            self.kairos.tick()
        except Exception as e:  # noqa: BLE001 - the watch must not stop the core
            self.store.add_event("error", "KAIROS hata verdi", repr(e)[:300], topic="core")
        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                self.store.prune(self.retention_days, now)
            except Exception as e:  # noqa: BLE001 - if the database is locked, it is tried again the next hour
                self._step_error("eski veriyi temizleme", e)
        self.write_state(now)

    def prime(self) -> None:
        """At startup, before the voice and other modules start, set and write the mode. Otherwise there is no
        state file until the first tick, and the voice module, thinking "GPU is free", loaded the models onto the
        GPU even while DCS was open."""
        now = time.time()
        try:
            from alice import sandbox

            sandbox.repair_old_labels()  # Windows: personal folders labeled low by old versions back to medium
        except Exception:  # noqa: BLE001 - must not stop the core from starting
            pass
        try:
            self.pc._procs()  # process names only; temperature/GPU queries wait for the first tick
        except Exception:  # noqa: BLE001
            pass
        self._update_mode(now)
        try:
            self.write_state(now)
        except OSError as e:  # don't crash at startup; the first tick writes it again
            print(f"çekirdek durumu yazılamadı: {e!r}", file=sys.stderr)

    def _update_mode(self, now: float) -> None:
        try:
            forced = force_path().read_text(encoding="utf-8").strip() or None
        except OSError:
            forced = None
        self.modes.forced = forced if forced in self.modes.policies else None
        procs = set()
        for m in self.monitors:
            procs |= set(getattr(m, "processes", set()))
        mode, changed = self.modes.update(procs, datetime.fromtimestamp(now))
        # The mode is written on every tick so the interval calculations stay correct even while the service is off;
        # "off" is written at shutdown, but if that write is lost, the last mode must not last forever.
        self.store.add(MODE_KEY, text=mode, ts=now)
        try:  # every tick, so the HUD knows we are in a game even after a restart (games show only threats/warnings)
            from alice.hud import views

            views.game_context(mode == "flight")
        except Exception:  # noqa: BLE001
            pass
        if changed:
            self.store.add_event("mode", f"Mod: {MODE_NAMES.get(mode, mode)}", severity="info", topic="mode")
            try:  # Windows notification balloons are muted during a match and restored when it ends
                did = self.quiet_game.mode(mode)
                if did:
                    self.store.add_event("info", did, topic="now")
            except Exception:  # noqa: BLE001
                pass

    def _reload_if_changed(self) -> None:
        """Reload the rule/task files if another process (the CLI, an agent tool) changed them."""
        mt = max((p.stat().st_mtime for p in (self.rules.path, self.tasks.path) if p.exists()), default=0.0)
        if self._last_files is None or mt > self._last_files:
            if self._last_files is not None:  # not on the first look; a file created later is loaded too
                self.rules.load()
                self.tasks.load()
            self._last_files = mt

    # ---------------------------------------------------------- memory
    def _maybe_memory(self) -> None:
        if self.memory_every <= 0 or time.monotonic() - self._last_memory < self.memory_every:
            return
        if self._memory_thread is not None and self._memory_thread.is_alive():
            return
        self._last_memory = time.monotonic()
        self._memory_thread = threading.Thread(target=self.memory_job, daemon=True, name="alice-core-memory")
        self._memory_thread.start()

    def router(self):
        if self._router is None:
            from alice.providers.router import Router

            self._router = Router(self.cfg)
        return self._router

    def _memory(self):
        try:
            from alice.memory import shared_memory

            return shared_memory(self.cfg, self.router())
        except Exception:  # noqa: BLE001
            return None

    def memory_job(self) -> None:
        """Conversations are summarized even while the app is closed; "remind" follow-ups become notifications when
        their day comes (each one once; ALICE also reminds in the chat, and it stays in the profile).

        The steps are independent: because the calendar returned a 404, no reminder or e-mail notification had gone
        out for two weeks. The same error is not written to the log every hour; it is written once a day."""
        mem = self._memory()
        if mem is None:
            return
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("hatırlatmalar", lambda: self._send_reminders(mem)),  # the most important first
            ("konuşma özeti", mem.digest),
            ("hafıza Dream", mem.maybe_dream),  # daily, once enough conversation has built up: notes get consolidated
            ("takvim takibi", lambda: calendar_followups(mem, self.cfg)),
            ("sesli mod gecikme raporu", self._latency_report),
            ("önemli e-postalar", self._important_mail),
            ("sağlık raporu", self._health_report),
            ("haftalık özet", lambda: self._weekly(mem)),
            ("e-posta tarihleri", lambda: self._mail_dates(mem)),
            ("gece döngüsü", lambda: self._night(mem)),
            ("önsezi", lambda: self.anticipator.check()),
            ("sabah hazırlığı", self._morning),
        ]
        for name, fn in steps:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - one step must not stop the others
                self._step_error(name, e)

    def _maybe_prwatch(self, now: float) -> None:
        """If there is a watched PR and it is time, check in the background. A fix can take minutes: the tick does not
        wait, and only one check runs at a time."""
        from alice import prwatch

        if self._prwatch_thread is not None and self._prwatch_thread.is_alive():
            return
        if now - self._prwatch_last < prwatch.interval(self.cfg) or not prwatch.store_path().exists():
            return
        self._prwatch_last = now
        if not prwatch.load():
            return

        def notify(title: str, body: str) -> None:
            self.notifier.send(Notification(title, body, "info", "github", kind="info"))

        def work() -> None:
            try:
                for line in prwatch.check_all(self.cfg, notify, deliver=self.chat_delivery):
                    self.store.add_event("info", "PR takibi", line, topic="github")
            except Exception as e:  # noqa: BLE001 - no key, no network: the core keeps running
                self._step_error("PR takibi", e)

        self._prwatch_thread = threading.Thread(target=work, daemon=True, name="alice-prwatch")
        self._prwatch_thread.start()

    def _send_reminders(self, mem) -> None:
        sent = set(json.loads(mem._meta("reminded") or "[]"))
        now = time.time()
        due = [f for f in mem.due_followups(now) if f.kind == "remind" and f.id not in sent and (f.due or 0) <= now]
        for f in due:
            self.notifier.send(Notification("Hatırlatma", f.text, "info", "memory", kind="info"))
            sent.add(f.id)
        if due:
            mem._meta("reminded", json.dumps(sorted(sent)[-200:]))

    def _latency_report(self) -> None:
        from alice.voice import latency

        week = latency.weekly_report()
        if week:  # the weekly latency of voice mode: slowness should be seen from measurements, not guesses
            self.store.add_event("info", "Sesli mod: bu haftanın gecikmesi", week, topic="voice")

    def _important_mail(self) -> None:
        from alice.tools.gmail import new_important

        mails = new_important(self.cfg)
        if mails:
            self.notifier.send(Notification(f"{len(mails)} yeni önemli e-posta", "; ".join(mails[:3]),
                                            "info", "mail", kind="info"))

    def _processes(self) -> set[str]:
        procs: set[str] = set()
        for m in self.monitors:
            procs |= set(getattr(m, "processes", set()))
        return procs

    def _prep_game_update(self) -> None:
        from alice import anticipate

        # Sending the notification and opening Steam can be slow: work on a copy without holding the lock, then write
        # only the changed keys into the fresh state under the lock (so the dnd_prev that QuietGame wrote
        # in the meantime survives).
        before = anticipate.load_state()
        st = dict(before)
        msg = anticipate.prep_game_update(self._processes(), lambda t, b: self.notifier.send(
            Notification(t, b, "info", "now", kind="info")), st)
        changed = {k: v for k, v in st.items() if before.get(k, object()) != v}
        if changed:
            with anticipate.state_edit() as cur:
                cur.update(changed)
        if msg:
            self.store.add_event("info", "Önsezi: oyun güncellemesi", msg, "info", "now")

    def _morning(self) -> None:
        morning = getattr(self, "morning", None)
        if morning is None:  # an old install, or __init__ was skipped (tests)
            return
        answers = morning.check()
        if answers:
            # Show not only the topic names but also the prepared answers (shortened).
            def short(v: Any) -> str:
                t = " ".join(str(v or "").split())
                return t if len(t) <= 160 else t[:157].rstrip() + "…"

            text = "; ".join(f"{k}: {s}" if s else str(k) for k, s in ((k, short(v)) for k, v in answers.items()))
            self.store.add_event("info", "Önsezi: sabah cevapları hazır", text, "info", "now")

    def _health_report(self) -> None:
        from alice import health_report

        def send(title: str, body: str) -> bool:
            return self.notifier.send(Notification(title, body, "warn", "core", kind="alert"))

        health_report.report(self.cfg, self.store, send)

    def _weekly(self, mem) -> None:
        from alice.extras import weekly

        weekly.maybe_send(self.store, mem, self.router(), lambda t, b: self.notifier.send(
            Notification(t, b, "info", "memory", kind="info", emotion="cheerful")))

    def _night(self, mem) -> None:
        """At night, read its own records, find problems and connections, and ask in the morning (alice/night.py)."""
        if not (self.cfg.data.get("night", {}) or {}).get("enabled", True):
            return
        from alice import night

        night.maybe_run(self.store, mem)
        night.maybe_tell(lambda t, b: self.notifier.send(Notification(t, b, "info", "night", kind="info")))

    def _mail_dates(self, mem) -> None:
        from alice.extras import mailwatch
        from alice.tools import gmail

        if not gmail.accounts() or not ((self.cfg.data.get("gmail") or {}).get("extract_dates", True)):
            return
        mailwatch.scan(self.cfg, self.router(), mem, lambda t, b: self.notifier.send(
            Notification(t, b, "info", "mail", kind="info")))

    def _step_error(self, name: str, e: Exception) -> None:
        key = "|".join((name, type(e).__name__, str(e)[:80]))
        now = time.time()
        # On disk: kept in memory, the same error was written again on every restart (calendar permission:
        # 13 times in 2 days).
        path = alice_home() / "step-errors.json"
        try:
            seen = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seen = {}
        if now - float(seen.get(key, 0)) < 86400:
            return
        seen = {k: v for k, v in seen.items() if now - float(v) < 86400}
        seen[key] = now
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # saved again on the next check
        try:
            self.store.add_event("error", f"{name} hata verdi", repr(e)[:300], "warn", "core")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- update
    def _maybe_update(self, now: float) -> None:
        from alice import updater

        s = updater.settings(self.cfg)
        if s.get("mode") == "off":
            return
        if self.restart_pending:
            # don't cut off a running task or chat: restart once idle
            standing = getattr(self, "standing", None)  # None when __init__ was skipped (tests)
            if (not self.running_tasks and not (standing is not None and standing.running)
                    and not (self.busy_hook is not None and self.busy_hook())):
                self.restart()
            return
        every = max(5.0, float(s.get("check_every_minutes") or 60)) * 60
        if now - self._last_update_check < every:
            return
        if self._update_thread is not None and self._update_thread.is_alive():
            return
        self._last_update_check = now
        self._update_thread = threading.Thread(target=self.update_job, daemon=True, name="alice-core-update")
        self._update_thread.start()

    def update_job(self) -> None:
        """Check whether a new version exists; pull it if the app is closed.
        If the code on disk is newer than this process, mark a restart."""
        from alice import updater

        try:
            info = updater.check(self.cfg, details=False)
            if info.stale and info.hand_pulled:
                # The checkout was moved by hand (developing ALICE in the folder it runs from: every branch switch or
                # pull). Restarting on that cut the user off mid-conversation again and again; the app's dot and
                # "Yeniden başlat" button leave the moment to them. Only updates ALICE pulled itself restart alone.
                if getattr(self, "_hand_pull_noted", "") != info.local:
                    self._hand_pull_noted = info.local
                    self.store.add_event("core", "Kod elle değişti; yeniden başlatma sana bırakıldı", topic="core")
                return
            if info.stale:
                self.store.add_event("core", "Yeni sürüm indirilmiş, çekirdek boşta kalınca yeniden başlayacak",
                                     topic="core")
                self.restart_pending = True
                return
            if not info.available or updater.settings(self.cfg).get("mode") != "auto":
                return
            if updater.app_running():
                return  # the app is open: the one with the UI should do the update
            updater.prepare(info, self.cfg)
            if not info.can_auto:
                if info.ci == "pending":
                    # CI finishes in a few minutes: check again after a few minutes, not an hour.
                    every = max(5.0, float(updater.settings(self.cfg).get("check_every_minutes") or 60)) * 60
                    self._last_update_check = time.time() - every + updater.CI_RECHECK
                    return
                self.store.add_event("core", "Yeni sürüm var ama otomatik çekilmedi", info.blocked, topic="core")
                return
            ok, msg = updater.apply_update(info, self.cfg)
            self.store.add_event("core", "ALICE güncellendi" if ok else "Güncelleme yapılamadı", msg,
                                 "info" if ok else "warn", "core")
            if ok:
                self.restart_pending = True
        except Exception as e:  # noqa: BLE001 - the update check must not bring down the core
            self.store.add_event("error", "güncelleme kontrolü hata verdi", repr(e)[:300], topic="core")

    def _restart_process(self) -> None:
        """Restart with the same command. The single-instance lock is released first, otherwise the new
        process says 'already running'."""
        from alice import updater

        from alice.app.host import mark_restarting

        self.store.add_event("core", "Çekirdek yeni sürümle yeniden başlıyor", topic="core")
        mark_restarting()  # keep the open window from closing; it moves to the new core's address
        release_lock()
        try:
            state_path().unlink()
        except OSError:
            pass  # already gone
        updater.restart([sys.executable, "-m", "alice", *sys.argv[1:]], env={"ALICE_RESTARTED": "1"})

    def _task_notify(self, title: str, body: str, severity: str = "info", kind: str = "task") -> None:
        """A task's notification: desktop toast + event. Approvals are warnings, so the phone app's poller pushes
        them to the phone too."""
        self.notifier.send(Notification(title, body[:500], severity, "task", kind=kind))

    def _run_due(self, now: float) -> None:
        if self.run_agent is None:
            return
        for task, late in self.tasks.due(now):
            if task.id in self.running_tasks:
                continue
            self.running_tasks.add(task.id)
            # Mark it right away to prevent a double run; it is updated when the result arrives.
            try:
                self.tasks.mark_run(task, "(çalışıyor)", now)
            except Exception:
                self.running_tasks.discard(task.id)  # otherwise this task would never run again
                raise
            threading.Thread(target=self._run_task, args=(task, late), daemon=True).start()

    def _run_task(self, task: ScheduledTask, late: bool) -> None:
        try:
            try:
                if task.project:
                    from alice.core.schedule import start_project

                    result = start_project(task, self.project_starter)
                else:
                    result = self.run_agent(task.prompt)  # type: ignore[misc]
                sev = "warn" if result.startswith("proje başlatılamadı") else "info"
            except Exception as e:
                result, sev = f"görev başarısız: {e}", "warn"
            with self._lock:
                fresh = self.tasks.get(task.id) or task
                self.tasks.mark_run(fresh, result, fresh.last_run)
                if fresh.once:
                    # A one-time task left in the list fired again on the same day every year
                    # ("call me in 1 minute" would call again the next year too), and removing it was left to the user.
                    self.tasks.remove(fresh.id)
            title = f"Görev{' (telafi)' if late else ''}: {task.description or task.prompt[:40]}"
            if task.notify:
                self.notifier.send(Notification(title, result[:500], sev, "task", kind="task"))
            else:
                self.store.add_event("task", title, result[:2000], sev, "task")
        finally:
            self.running_tasks.discard(task.id)

    def write_state(self, now: float | None = None) -> None:
        pol: Policy = self.modes.policy
        data = {"pid": os.getpid(), "ts": now or time.time(), "mode": self.modes.current,
                "forced": self.modes.forced, "policy": asdict(pol), "interval": self.interval, "voice": self.voice}
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:  # Windows: a reader (the voice module, the app) has the file open right now
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    # ---------------------------------------------------------------- loop
    def run_forever(self) -> None:
        self.store.add_event("core", "Çekirdek başladı", severity="info", topic="core")
        _take_stop_request()  # an unused stop request sent to the previous core must not shut down the new one
        clip = None
        if (self.cfg.data.get("core") or {}).get("clipboard_history", True):
            try:  # clipboard history, for "az önce kopyaladığım" ("what I just copied") (alice/core/clipwatch.py)
                from alice.core.clipwatch import ClipWatch

                clip = ClipWatch().start()
            except Exception:  # noqa: BLE001 - without pyperclip, no history is kept
                clip = None
        try:
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception as e:
                    try:  # even if the event cannot be written (database locked), the loop must not die
                        self.store.add_event("error", "çekirdek turu hata verdi", repr(e)[:500], "warn", "core")
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
                # Wait between ticks in one-second steps, so a stop request from the app is noticed within a second.
                # The number of steps is limited; even if the clock jumps or wait returns early,
                # the loop does not spin out of control.
                end = t0 + max(1.0, self.interval)
                for _ in range(int(max(1.0, self.interval)) + 1):
                    left = end - time.monotonic()
                    if self.stop_event.is_set() or left <= 0:
                        break
                    self.stop_event.wait(min(1.0, left))
                    if _take_stop_request():
                        self.store.add_event("core", "Çekirdek uygulamadan durduruldu", topic="core")
                        self.stop()
        finally:
            if clip is not None:
                clip.stop()
            self.store.add(MODE_KEY, text="off")
            self.store.add_event("core", "Çekirdek durdu", topic="core")
            try:
                state_path().unlink()
            except OSError:
                pass  # already gone

    def stop(self) -> None:
        self.stop_event.set()


# ----------------------------------------------------------- single-instance lock
def acquire_lock() -> bool:
    """Only one core runs at a time. Returns False if another live core is running."""
    lock = alice_home() / "core.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid = int(lock.read_text().strip())
        if pid != os.getpid() and psutil.pid_exists(pid):
            try:
                cmd = " ".join(psutil.Process(pid).cmdline()).lower()
            except psutil.Error:
                cmd = ""
            if "alice" in cmd and "core" in cmd:
                return False
    except (OSError, ValueError):
        pass  # unreadable process: treat it as not ours
    lock.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    lock = alice_home() / "core.pid"
    try:
        if lock.read_text().strip() == str(os.getpid()):
            lock.unlink()
    except (OSError, ValueError):
        pass  # already gone or held by another process


def release_lock_of(pid: Any) -> None:
    """Remove the lock of a core that was shut down from outside (if the lock belongs to that process)."""
    lock = alice_home() / "core.pid"
    try:
        if pid and lock.read_text().strip() == str(pid):
            lock.unlink()
    except (OSError, ValueError):
        pass  # already gone or held by another process


# ------------------------------------------------------------ system tray
def run_tray(core: Core) -> bool:
    """If pystray is available, run with a tray icon (it blocks the main thread). Otherwise False."""
    try:
        import pystray  # pyright: ignore[reportMissingImports] - optional 'core' extra
        from PIL import Image
    except ImportError:
        return False
    icon_path = Path(__file__).resolve().parent.parent / "app" / "static" / "icon.png"
    image = Image.open(icon_path) if icon_path.exists() else Image.new("RGB", (64, 64), (120, 80, 200))

    def set_mode(mode):
        def _(icon, item):
            force_mode(mode)
            core.modes.forced = mode
        return _

    def is_mode(mode):
        return lambda item: core.modes.forced == mode

    def open_app(icon, item):
        import subprocess

        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else exe), "-m", "alice", "app"])

    def quit_(icon, item):
        try:  # so the later question "why did it close?" does not go unanswered
            core.store.add_event("core", "Çekirdek tepsiden kapatıldı", topic="core")
        except Exception:  # noqa: BLE001
            pass
        core.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: f"Mod: {MODE_NAMES.get(core.modes.current, core.modes.current)}", None,
                         enabled=False),
        pystray.MenuItem("ALICE'i aç", open_app, default=True),
        pystray.MenuItem("Modu sabitle", pystray.Menu(
            pystray.MenuItem("Otomatik", set_mode(None), checked=is_mode(None), radio=True),
            *(pystray.MenuItem(MODE_NAMES[m].capitalize(), set_mode(m), checked=is_mode(m), radio=True)
              for m in ("normal", "dev", "flight", "quiet")))),
        pystray.MenuItem("Çık", quit_),
    )
    icon = pystray.Icon("alice", image, "ALICE", menu)
    def loop() -> None:
        try:
            core.run_forever()
        finally:
            icon.stop()  # so the tray icon also goes away when stopped from the app

    threading.Thread(target=loop, daemon=True).start()
    icon.run()
    return True


# ------------------------------------------------------ autostart
def start_detached() -> bool:
    """Start the core in the background without a console (does nothing if it is already running).
    True if it was started."""
    import subprocess

    if read_state().get("running"):
        return False
    from alice import runtime

    exe = runtime.python()  # always the same environment (see alice.runtime)
    # Error output goes to core.log: it used to go to DEVNULL, so no trace was left when the core crashed.
    err = _open_core_log()
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": err if err is not None else subprocess.DEVNULL, "cwd": str(Path.home())}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([str(exe), "-m", "alice", "core", "run", "--tray"], **kwargs)
    finally:
        if err is not None:
            err.close()
    return True


def _open_core_log():
    """~/.alice/core.log (once it passes 2 MB it is moved to core.log.1), opened in append mode;
    None if it cannot be opened."""
    try:
        log = alice_home() / "core.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(log.with_suffix(".log.1"))
        return open(log, "ab")
    except OSError:
        return None


_crash_file = None


def enable_crash_log() -> None:
    """Native crashes (audio driver, CUDA, COM) were killing the process without leaving a Python trace:
    have faulthandler write the stacks of all threads to ~/.alice/core-crash.log. Each start is marked with a line."""
    global _crash_file
    import faulthandler

    try:
        f = open(alice_home() / "core-crash.log", "a", encoding="utf-8")
        f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} çekirdek başladı (pid {os.getpid()})\n")
        f.flush()
        faulthandler.enable(f, all_threads=True)
        _crash_file = f  # must stay open: faulthandler holds the file handle
    except (OSError, ValueError, RuntimeError):
        pass  # crash logging is optional
    try:
        print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} çekirdek başladı (pid {os.getpid()})", file=sys.stderr,
              flush=True)
    except Exception:  # noqa: BLE001 - no stderr, so it doesn't matter
        pass


def autostart_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / r"Microsoft\Windows\Start Menu\Programs\Startup" / "ALICE Core.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents/ai.alice.core.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "alice-core.desktop"


def install_autostart() -> Path:
    """Start the core without a console when the session starts."""
    import subprocess

    from alice import runtime

    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    exe = runtime.python()  # autostart should use the same Python as a manual start (see alice.runtime)
    if sys.platform == "win32":
        target = exe
        icon = Path(__file__).resolve().parent.parent / "app" / "static" / "alice.ico"
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(path).replace("'", "''") + "');"
              f"$s.TargetPath='{target}';$s.Arguments='-m alice core run --tray';"
              f"$s.WorkingDirectory='{Path.home()}';$s.IconLocation='{icon}';$s.Description='ALICE çekirdek';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    elif sys.platform == "darwin":
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
            "<key>Label</key><string>ai.alice.core</string><key>ProgramArguments</key><array>"
            f"<string>{exe}</string><string>-m</string><string>alice</string><string>core</string>"
            "<string>run</string></array><key>RunAtLoad</key><true/></dict></plist>\n", encoding="utf-8")
    else:
        path.write_text(f"[Desktop Entry]\nType=Application\nName=ALICE Core\nExec={exe} -m alice core run --tray\n"
                        "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n", encoding="utf-8")
    return path


def uninstall_autostart() -> bool:
    p = autostart_path()
    if p.exists():
        p.unlink()
        return True
    return False
