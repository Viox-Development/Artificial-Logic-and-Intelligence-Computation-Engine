"""Çekirdek servis: arka planda belirli aralıklarla dönen döngü.

Her turda: gözlemcilerden veri topla -> depoya yaz -> modu güncelle -> kuralları
değerlendir -> vadesi gelen görevleri çalıştır. Ajan gerektiren işler (görevler,
action=agent kurallar) ayrı thread'de çalışır, döngüyü bekletmez.

Anlık durum ~/.alice/core-state.json'a yazılır; böylece uygulama, REPL ya da ileride ses
modülü "şu an hangi moddayız, GPU kullanabilir miyim?" sorusunu servise bağlanmadan okur.
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


# Takvimde bunlar geçen etkinlik bitince "nasıl geçti" diye sorulur (her toplantı için değil).
CALENDAR_ASK = re.compile(r"sınav|sinav|vize|final|quiz|maç|mac\b|mülakat|mulakat|görüşme|randevu|doktor|dişçi|"
                          r"hastane|sunum|yarışma|turnuva|uçuş|seyahat|tatil|konser|exam|interview|match|trip|flight",
                          re.I)


def calendar_followups(mem, cfg, now: datetime | None = None, client: Any = None) -> int:
    """Google Takvim bağlıysa önümüzdeki iki günün önemli etkinliklerini takibe çevir: ertesi gün ALICE sorar."""
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
    """Modu elle sabitle (None = otomatik). Çalışan çekirdek bir sonraki turda okur."""
    p = force_path()
    if mode:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(mode, encoding="utf-8")
    elif p.exists():
        p.unlink()


def stop_path() -> Path:
    return alice_home() / "core-stop"


def request_stop() -> None:
    """Çalışan çekirdekten kapanmasını iste (uygulamadaki Durdur / Yeniden başlat). Bir saniye içinde fark eder."""
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
    """Servisin son yazdığı durum. Servis çalışmıyorsa {"running": False}."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    pid = data.get("pid")
    data["running"] = bool(pid) and psutil.pid_exists(pid) and time.time() - data.get("ts", 0) < 300
    return data


def make_agent_runner(cfg: Config, cwd: Path | None = None) -> Callable[[str], str]:
    """Gözetimsiz ajan: her çağrıda temiz context. Onay gerektiren adımlar reddedilir."""

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
            router.close()  # her görevde yeni yönlendirici: HTTP bağlantıları saatlerce birikmesin
        return text or "(sonuç yok)"

    return run


class Core:
    def __init__(self, cfg: Config, store: Store | None = None, monitors: list[Monitor] | None = None,
                 notifier: Notifier | None = None, rules: RuleSet | None = None, tasks: TaskStore | None = None,
                 decider=None, run_agent: Callable[[str], str] | None = None,
                 restart: Callable[[], None] | None = None):
        self.cfg = cfg
        c = cfg.data.get("core", {})
        self.interval = float(c.get("interval", 15))
        self.voice = "off"  # core-state.json'a yazılır: "listening" (mikrofon dinleniyor), "no-mic", "off"
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
            # Akışlar: bağlı servislerde önemli bir şey olunca haber ver (~/.alice/feeds.toml). Akış yokken de
            # takılı: sohbetten eklenen akış çekirdek yeniden başlamadan devreye girsin (boşken maliyeti sıfır).
            from alice.core.feeds import FeedMonitor

            monitors.append(FeedMonitor(self.notifier.send, run_agent, cfg, decider=decider,
                                        emit=lambda s: self.store.add_event("info", s, topic="feed")))
        self.monitors: list[Monitor] = monitors
        self.rules = rules or RuleSet()
        self.tasks = tasks or TaskStore()
        self.run_agent = run_agent
        self.engine = RuleEngine(self.rules, self.store, self.notifier, decider, run_agent,
                                 mode=lambda: self.modes.current)
        self.stop_event = threading.Event()
        self.running_tasks: set[str] = set()
        # Her turda mod politikasıyla çağrılır (ör. ses: DCS açılınca modelleri ekran kartından çıkar).
        self.hooks: list[Callable[[Policy], None]] = []
        self._last_prune = 0.0
        self._last_files: float | None = None  # None: dosyalar henüz hiç kontrol edilmedi
        self._lock = threading.Lock()
        # Kendi kendini güncelleme: kontrol arka plan thread'inde (ağ turu turu bekletmesin), yeniden başlama
        # çalışan görev yokken.
        self._last_update_check = time.time()  # açılışta hemen bakma: uygulama zaten bakıyor
        self._update_thread: threading.Thread | None = None
        self.restart_pending = False
        self.restart = restart or self._restart_process
        # Hafıza işi: bitmiş konuşmaları özetle, günü gelen hatırlatmaları bildir (10 dk'da bir, arka planda).
        self.memory_every = float(c.get("memory_every_minutes", 10)) * 60
        self._last_memory = time.monotonic()
        self._memory_thread: threading.Thread | None = None
        self._router: Any = None
        # Uzun yokluktan dönünce karşılama (alice/presence.py); ses başlarsa greeter.voice bağlanır.
        from alice.presence import Greeter

        self.greeter = Greeter(cfg, self.notifier.send, memory=self._memory, router=self.router)

    # ------------------------------------------------------------------ tur
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        rows = []
        for m in self.monitors:
            try:
                rows += m.collect()
            except Exception as e:  # bir gözlemci çökse de diğerleri çalışsın
                try:
                    self.store.add_event("error", f"{getattr(m, 'name', m)} gözlemcisi hata verdi", str(e)[:300])
                except Exception:  # noqa: BLE001
                    pass
        if rows:
            self.store.add_many(rows, now)
        self._update_mode(now)
        for hook in self.hooks:
            try:
                hook(self.modes.policy)
            except Exception as e:  # noqa: BLE001 - bir kanca çökse de tur sürsün
                self.store.add_event("error", "çekirdek kancası hata verdi", repr(e)[:300], topic="core")
        self._reload_if_changed()
        self.engine.evaluate(now)
        self._run_due(now)
        self._maybe_update(now)
        self._maybe_memory()
        try:
            self.greeter.tick()
        except Exception as e:  # noqa: BLE001 - karşılama çekirdeği durdurmasın
            self.store.add_event("error", "karşılama hata verdi", repr(e)[:300], topic="core")
        if now - self._last_prune > 3600:
            self._last_prune = now
            self.store.prune(self.retention_days, now)
        self.write_state(now)

    def prime(self) -> None:
        """Açılışta, ses ve diğer modüller başlamadan modu belirle ve yaz. Yoksa ilk tur gelene kadar durum dosyası
        olmuyor, ses modülü "GPU serbest" sanıp DCS açıkken bile modelleri ekran kartına yüklüyordu."""
        now = time.time()
        try:
            self.pc._procs()  # sadece süreç adları; sıcaklık/GPU sorguları ilk tura kalsın
        except Exception:  # noqa: BLE001
            pass
        self._update_mode(now)
        self.write_state(now)

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
        # Mod her turda yazılır: aralık hesapları servis kapalıyken bile doğru kalsın diye
        # kapanışta "off" yazılır, ama arada kayıp olursa son mod sonsuza uzamasın.
        self.store.add(MODE_KEY, text=mode, ts=now)
        if changed:
            self.store.add_event("mode", f"Mod: {MODE_NAMES.get(mode, mode)}", severity="info", topic="mode")

    def _reload_if_changed(self) -> None:
        """Kural/görev dosyaları başka süreçten (CLI, ajan aracı) değiştiyse yeniden yükle."""
        mt = max((p.stat().st_mtime for p in (self.rules.path, self.tasks.path) if p.exists()), default=0.0)
        if self._last_files is None or mt > self._last_files:
            if self._last_files is not None:  # ilk görüşte değil; sonradan oluşan dosya da yüklenir
                self.rules.load()
                self.tasks.load()
            self._last_files = mt

    # ---------------------------------------------------------- hafıza
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
        """Konuşma bitince özetlenmesi uygulama kapalıyken de olsun; "remind" takipleri günü gelince bildirim olur
        (her biri bir kez; ALICE sohbette de hatırlatır, profilde durur)."""
        try:
            mem = self._memory()
            if mem is None:
                return
            mem.digest()
            calendar_followups(mem, self.cfg)
            from alice.tools.gmail import new_important

            mails = new_important(self.cfg)
            if mails:
                self.notifier.send(Notification(f"{len(mails)} yeni önemli e-posta", "; ".join(mails[:3]),
                                                "info", "mail", kind="info"))
            sent = set(json.loads(mem._meta("reminded") or "[]"))
            now = time.time()
            due = [f for f in mem.due_followups(now) if f.kind == "remind" and f.id not in sent
                   and (f.due or 0) <= now]
            for f in due:
                self.notifier.send(Notification("Hatırlatma", f.text, "info", "memory", kind="info"))
                sent.add(f.id)
            if due:
                mem._meta("reminded", json.dumps(sorted(sent)[-200:]))
        except Exception as e:  # noqa: BLE001 - hafıza işi çekirdeği durdurmasın
            try:
                self.store.add_event("error", "hafıza işi hata verdi", repr(e)[:300], topic="core")
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- güncelleme
    def _maybe_update(self, now: float) -> None:
        from alice import updater

        s = updater.settings(self.cfg)
        if s.get("mode") == "off":
            return
        if self.restart_pending:
            if not self.running_tasks:  # yarım görev bırakma: boşta kalınca
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
        """Yeni sürüm var mı; uygulama kapalıysa çek. Diskteki kod bu süreçten yeniyse yeniden başlamayı işaretle."""
        from alice import updater

        try:
            info = updater.check(self.cfg, details=False)
            if info.stale:
                self.store.add_event("core", "Yeni sürüm indirilmiş, çekirdek boşta kalınca yeniden başlayacak",
                                     topic="core")
                self.restart_pending = True
                return
            if not info.available or updater.settings(self.cfg).get("mode") != "auto":
                return
            if updater.app_running():
                return  # uygulama açık: arayüzü olan o, güncellemeyi o yapsın
            updater.prepare(info, self.cfg)
            if not info.can_auto:
                if info.ci == "pending":
                    # CI birkaç dakikada biter: bir saat değil, birkaç dakika sonra tekrar bak.
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
        except Exception as e:  # noqa: BLE001 - güncelleme kontrolü çekirdeği düşürmesin
            self.store.add_event("error", "güncelleme kontrolü hata verdi", repr(e)[:300], topic="core")

    def _restart_process(self) -> None:
        """Aynı komutla yeniden başla. Tek kopya kilidi önce bırakılır, yoksa yeni süreç 'zaten çalışıyor' der."""
        from alice import updater

        self.store.add_event("core", "Çekirdek yeni sürümle yeniden başlıyor", topic="core")
        release_lock()
        try:
            state_path().unlink()
        except OSError:
            pass
        updater.restart([sys.executable, "-m", "alice", *sys.argv[1:]], env={"ALICE_RESTARTED": "1"})

    def _run_due(self, now: float) -> None:
        if self.run_agent is None:
            return
        for task, late in self.tasks.due(now):
            if task.id in self.running_tasks:
                continue
            self.running_tasks.add(task.id)
            # Çift çalışmayı önlemek için hemen işaretle; sonuç gelince güncellenir.
            try:
                self.tasks.mark_run(task, "(çalışıyor)", now)
            except Exception:
                self.running_tasks.discard(task.id)  # yoksa bu görev bir daha hiç çalışmaz
                raise
            threading.Thread(target=self._run_task, args=(task, late), daemon=True).start()

    def _run_task(self, task: ScheduledTask, late: bool) -> None:
        try:
            try:
                result = self.run_agent(task.prompt)  # type: ignore[misc]
                sev = "info"
            except Exception as e:
                result, sev = f"görev başarısız: {e}", "warn"
            with self._lock:
                fresh = self.tasks.get(task.id) or task
                self.tasks.mark_run(fresh, result, fresh.last_run)
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
        os.replace(tmp, p)

    # ---------------------------------------------------------------- döngü
    def run_forever(self) -> None:
        self.store.add_event("core", "Çekirdek başladı", severity="info", topic="core")
        _take_stop_request()  # önceki çekirdeğe gönderilmiş, kullanılmamış istek yenisini kapatmasın
        try:
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception as e:
                    try:  # olay yazılamasa da (veritabanı kilitli) döngü ölmesin
                        self.store.add_event("error", "çekirdek turu hata verdi", repr(e)[:500], "warn", "core")
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
                # Tur arasını saniyelik adımlarla bekle: uygulamadan gelen durdurma isteği bir saniyede fark edilsin.
                # Adım sayısı sınırlı; saat atlasa ya da wait erken dönse de döngü dönüp durmaz.
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
            self.store.add(MODE_KEY, text="off")
            self.store.add_event("core", "Çekirdek durdu", topic="core")
            try:
                state_path().unlink()
            except OSError:
                pass

    def stop(self) -> None:
        self.stop_event.set()


# ----------------------------------------------------------- tek kopya kilidi
def acquire_lock() -> bool:
    """Aynı anda tek çekirdek çalışsın. Başka bir canlı çekirdek varsa False."""
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
        pass
    lock.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    lock = alice_home() / "core.pid"
    try:
        if lock.read_text().strip() == str(os.getpid()):
            lock.unlink()
    except (OSError, ValueError):
        pass


def release_lock_of(pid: Any) -> None:
    """Dışarıdan kapatılan çekirdeğin kilidini kaldır (kilit o süreçteyse)."""
    lock = alice_home() / "core.pid"
    try:
        if pid and lock.read_text().strip() == str(pid):
            lock.unlink()
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------ sistem tepsisi
def run_tray(core: Core) -> bool:
    """pystray varsa tepsi simgesiyle çalış (ana thread'i bloklar). Yoksa False."""
    try:
        import pystray
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
            icon.stop()  # uygulamadan durdurulunca tepsi simgesi de kalksın

    threading.Thread(target=loop, daemon=True).start()
    icon.run()
    return True


# ------------------------------------------------------ otomatik başlatma
def start_detached() -> bool:
    """Çekirdeği konsolsuz arka planda başlat (zaten çalışıyorsa bir şey yapmaz). Başlattıysa True."""
    import subprocess

    if read_state().get("running"):
        return False
    exe = Path(sys.executable)
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              "cwd": str(Path.home())}
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else exe
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([str(exe), "-m", "alice", "core", "run", "--tray"], **kwargs)
    return True


def autostart_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / r"Microsoft\Windows\Start Menu\Programs\Startup" / "ALICE Core.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents/ai.alice.core.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "alice-core.desktop"


def install_autostart() -> Path:
    """Oturum açılınca çekirdeği konsolsuz başlat."""
    import subprocess

    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        target = pyw if pyw.exists() else exe
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
