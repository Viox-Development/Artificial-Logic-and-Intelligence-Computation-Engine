"""Core commands.

  alice core run [--tray] [--once]   run the core (--tray: in system tray)
  alice core install | uninstall     auto-start on login / remove
  alice core status                  is it running, which mode, recent events
  alice core mode <mode|auto>        manually lock mode (normal, dev, flight, quiet) or return to auto
  alice core stats <key> [period]    e.g. alice core stats pc.gpu.temp 1d
  alice rules [list|add "phrase"|rm|on|off <id>]
  alice tasks [list|add "phrase"|rm|run <id>]
  alice feeds [list|--new|--test <name>] feeds that monitor connected services and notify
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.markup import escape

from alice.config import load_config

console = Console(highlight=False)


def _confirm(question: str) -> bool:
    return console.input(f"{question} [bold][e/h][/] > ").strip().lower() in ("e", "evet", "y", "yes", "")


def core_cmd(argv: list[str]) -> int:
    from alice.core import service

    sub = argv[0] if argv else "status"
    if sub == "run":
        from alice import runtime

        if "--once" not in argv and runtime.redirect_core(argv):
            return 0  # started from another Python (e.g. .venv startup shortcut): restarted with registered Python
        cfg = load_config()
        if not service.acquire_lock():
            console.print("[yellow]çekirdek zaten çalışıyor[/]")
            return 1
        if "--once" not in argv:
            service.enable_crash_log()
        from alice.decider import Decider
        from alice.providers.router import Router

        router = Router(cfg)
        from alice import updater

        if updater.boot_check() == "rolled":  # new version crashed repeatedly: rolled back to previous version
            service.release_lock()
            updater.restart(env={"ALICE_RESTARTED": "1"})
        updater.mark_running()  # if code on disk gets newer than this, restart when idle
        updater.watch_health()
        from alice.config import refresh_home

        refresh_home()  # write new key lines from the update into .env
        core = service.Core(cfg, decider=Decider.from_config(cfg, router), run_agent=service.make_agent_runner(cfg))
        core.prime()  # is DCS open: voice module chooses whether to use GPU based on this
        announce_update(core, updater)
        voice = _start_voice(cfg, core) if cfg.data.get("voice", {}).get("enabled") and "--once" not in argv else None
        core.greeter.voice = voice  # allow answering after greeting without saying "Hey Alice"
        if cfg.data.get("voice", {}).get("enabled") and "--once" not in argv:
            core.voice = "listening" if voice is not None else "no-mic"
        if "--once" not in argv:
            _start_browser_ext(cfg)
            from alice import deps

            # Whichever Python it was started from (login .venv, manual system Python), don't leave missing packages
            deps.heal_in_background(lambda s: print(f"[paketler] {s}", flush=True))
        hud = _start_hud(cfg, core) if (cfg.data.get("hud", {}) or {}).get("enabled") and "--once" not in argv else None
        if hud is not None:
            from alice.extras import nowplaying
            from alice.hud import views as hud_views

            nowplaying.Watcher(hud_views.now_playing).start()  # briefly show on Island when song changes
        sims = _start_sims(cfg, core, show=_hud_show(hud)) if "--once" not in argv else None
        tl = _start_timeline(cfg, core) if (cfg.data.get("timeline", {}) or {}).get("enabled") and             "--once" not in argv else None
        host = _start_app(cfg, core) if (cfg.data.get("app", {}) or {}).get("in_core", True) and \
            "--once" not in argv else None
        # Desktop app, quick bar, and phone share single app state: all three see the same chat, tools and
        # MCP servers are initialized once.
        shared = getattr(host, "state", None)
        phone = _start_phone(cfg, core, shared) if cfg.data.get("phone", {}).get("enabled") and \
            "--once" not in argv else None
        if phone is not None and shared is not None:
            host.publish()  # phone changed the shared state key: desktop address also changed
        quick = _start_quick(cfg, shared) if cfg.data.get("quick", {}).get("enabled", True) and \
            "--once" not in argv else None
        try:
            if "--once" in argv:
                core.tick()
                for e in reversed(core.store.events(limit=10)):
                    console.print(escape(e.line()))
                return 0
            console.print(f"[bold magenta]ALICE çekirdek[/] çalışıyor, her {core.interval:g} sn. "
                          "[dim]Durdurmak için Ctrl+C[/]")
            if "--tray" in argv and service.run_tray(core):
                return 0
            core.run_forever()
        except KeyboardInterrupt:
            core.stop()
        finally:
            if host is not None:
                host.stop()  # open window detects this and closes
            if voice is not None:
                voice.stop()
            if quick is not None:
                quick.stop()
            if sims is not None:
                sims.stop()
            if hud is not None:
                hud.stop()
            if tl is not None:
                tl.stop()
            if phone is not None:
                phone.stop()
            service.release_lock()
        return 0
    if sub == "install":
        p = service.install_autostart()
        console.print(f"[green]otomatik başlatma kuruldu:[/] {p}")
        if "pystray" not in sys.modules:
            import importlib.util

            if importlib.util.find_spec("pystray") is None:
                console.print("[dim]Tepsi simgesi için: " + escape('pip install "alice-harness[core]"') + "[/]")
        return 0
    if sub == "uninstall":
        console.print("[green]kaldırıldı[/]" if service.uninstall_autostart() else "[dim]zaten kurulu değil[/]")
        return 0
    if sub == "mode":
        if len(argv) < 2 or argv[1] not in ("auto", "normal", "dev", "flight", "quiet"):
            console.print("kullanım: alice core mode <auto|normal|dev|flight|quiet>")
            return 2
        service.force_mode(None if argv[1] == "auto" else argv[1])
        console.print(f"mod: [bold]{argv[1]}[/]")
        return 0
    if sub == "stats":
        from alice.core.store import Store, parse_time

        if len(argv) < 2:
            console.print("kullanım: alice core stats <anahtar|glob> [süre, ör. 1d]")
            return 2
        since = parse_time(argv[2] if len(argv) > 2 else "1d")
        with Store() as st:
            keys = st.keys(argv[1], since=since) if any(c in argv[1] for c in "*?[") else [argv[1]]
            for k in keys:
                console.print(escape(st.stats(k, since).line()))
        return 0
    if sub == "status":
        s = service.read_state()
        if s.get("running"):
            p = s.get("policy", {})
            console.print(f"[green]çalışıyor[/] (pid {s.get('pid')}), mod: [bold]{s.get('mode')}[/]"
                          f"{' (elle sabit)' if s.get('forced') else ''}, en düşük bildirim: {p.get('min_severity')}, "
                          f"yerel GPU: {p.get('local_gpu')}")
        else:
            console.print("[yellow]çalışmıyor[/]  [dim]başlat: alice core run · kalıcı: alice core install[/]")
        from alice.core.store import Store

        with Store() as st:
            evs = st.events(limit=10)
        if evs:
            console.print("[bold]Son olaylar[/]")
            for e in reversed(evs):
                console.print("  " + escape(e.line()))
        return 0
    console.print(f"bilinmeyen çekirdek komutu: {sub}", markup=False)
    console.print(__doc__, markup=False)
    return 2


def _start_phone(cfg, core, state=None):
    from alice.app.phone import PhoneServer

    try:
        ps = PhoneServer(cfg, core.store, state=state)
    except OSError as e:
        console.print(f"[yellow]telefon köprüsü açılamadı:[/] {e}")
        return None
    console.print(f"telefon: {ps.url}")
    return ps


def _start_app(cfg, core):
    """App stays open inside the core: `alice app` only connects the window to it (alice/app/host.py)."""
    from alice.app.host import AppHost

    # Set up immediately because quick bar and phone share this state (previously quick bar set up its own copy here,
    # without waiting; startup time is the same).
    host = AppHost(cfg, restart=core.restart).start(background=False)
    core.busy_hook = lambda: host.busy  # do not restart for updates while a chat is in progress

    def deliver(sid: str, text: str) -> str:
        """Deliver PR tracking output as a message to the chat that wrote code. Empty: delivered; else why it failed.
        If the chat is currently busy with another task it is not interrupted ("meşgul"): watcher retries next tick."""
        st = host.state
        if st is None:
            return "uygulama hazır değil"
        chat = st.find_chat(sid)
        if chat is not None and chat.busy:
            return "meşgul"
        ok, err = st.send(text, sid=sid)
        return "" if ok else (err or "gönderilemedi")

    core.chat_delivery = deliver
    core.project_starter = lambda d: host.state.create_project(d) if host.state is not None else None
    return host


def _start_sims(cfg, core, show=None):
    """Start DCS and War Thunder listeners."""
    from alice.sims.hub import SimHub

    return SimHub(cfg.data.get("sims", {}), core.store, core.notifier, processes=lambda: core.pc.processes,
                  show=show).start()


def _start_browser_ext(cfg) -> None:
    """Local server for ALICE browser extension to connect to. While extension is connected, the browser tool never
    uses remote debugging (Opera won't ask permission on each launch). If no websockets or port busy, fallback quietly."""
    port = int(cfg.agent.get("browser_ext_port", 9340) or 0)
    if not port:
        return
    try:
        from alice.tools import browser_ext

        browser_ext.start_hub(port, allowed_ids=cfg.agent.get("browser_ext_ids") or ())
    except Exception:  # noqa: BLE001
        pass


def _start_hud(cfg, core):
    """Start on-screen HUD in a separate process; supervisor restarts if it crashes. Notifications arrive as ALICE's
    card instead of Windows toast balloon."""
    from alice.hud import PORT, client, views
    from alice.hud.supervisor import HudSupervisor

    port = int((cfg.data.get("hud", {}) or {}).get("port") or PORT)
    client.configure(port)
    notifier = core.notifier
    sv = HudSupervisor(port)
    desktop = notifier._desktop if notifier._desktop in notifier.channels else None
    if desktop is not None:
        notifier.channels.remove(desktop)

    def card(n, pol) -> None:
        if sv.alive():
            views.notification(n)
        elif desktop is not None:  # HUD crashed/restarting: don't lose notification, show toast balloon
            desktop(n, pol)

    notifier.channels.append(card)
    # Show temperature/RAM nearing critical threshold as small bar in corner (without speaking). Hook is only
    # added when HUD is on: zero cost to core tick if HUD is off.
    hooks = getattr(core, "hooks", None)
    if hooks is not None and getattr(core, "store", None) is not None:
        from alice.core.watchdogs import SystemGauges

        hooks.append(SystemGauges(core.store, getattr(core, "interval", 15.0)))
    return sv.start()


def _start_timeline(cfg, core):
    """Time machine memory: write foreground window text to encrypted timeline (pauses in flight mode)."""
    from alice.timeline import Recorder, Timeline

    try:
        return Recorder(Timeline(), cfg.data.get("timeline", {}) or {}, mode=lambda: core.modes.current).start()
    except Exception as e:  # noqa: BLE001 - let core run even if recording fails to start
        core.store.add_event("error", "Zaman çizgisi başlamadı", str(e)[:200], "warn", "core")
        return None


def _hud_show(hud):
    """If HUD is active, draw flight threats as arrows and edge glow as well."""
    if hud is None:
        return None
    from alice.hud import client

    return client.show


def _start_quick(cfg, state=None):
    """Start quick bar shortcuts alongside the core (silently skip if pynput is missing)."""
    import importlib.util

    if importlib.util.find_spec("pynput") is None:
        return None
    from alice.app.quick import QuickBar

    qb = QuickBar(cfg, state=state)
    if not qb.start_hotkeys():
        qb.stop()
        return None
    return qb


def _raise_priority(v: dict, log) -> None:
    """Keep "Alice" wake check from queuing when game maxes CPU: core is raised to "above normal" priority.
    On idle PC check took 0.4s, with War Thunder running median 1.3s, worst 16.7s (Nov 7).
    Since checks are short per sentence (~0.4s), impact on game is small. Disabled with [voice] priority = "normal"."""
    want = str(v.get("priority", "above_normal")).lower()
    if sys.platform != "win32" or want != "above_normal":
        return
    try:
        import psutil

        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        log("işlem önceliği: normalin üstü (uyandırma kontrolü oyunda beklemesin)")
    except Exception as e:  # noqa: BLE001 - continue with normal priority if lacking permission
        log(f"işlem önceliği değiştirilemedi: {e}")


def _start_voice(cfg, core):
    """Start voice assistant in the same process as core; notifications are voiced too."""
    import threading
    import time

    from alice.voice.assistant import VoiceAssistant, build_voice_agent, live_text_options, make_live, voice_log
    from alice.voice.social import SocialWatch
    from alice.voice.speaker_id import SpeakerGate
    from alice.voice.audio import mic_available
    from alice.voice.stt import make_stt
    from alice.voice.vocab import known_names

    t0 = time.monotonic()
    v = cfg.data.get("voice", {})
    # Longest task is loading wake model (several seconds including first CUDA call on GPU): start in
    # parallel while agent, voice and mic are prepared. Also don't make first "Hey Alice" wait for app/site names.
    stt = make_stt(cfg)
    if stt.wake_gate is not None and mic_available():
        stt.wake_gate.warm_up(log=voice_log)
        _raise_priority(v, voice_log)
    threading.Thread(target=known_names, args=(cfg,), daemon=True, name="alice-vocab").start()
    agent, speaker, _ = build_voice_agent(cfg)

    def gpu_hook(pol) -> None:
        gpu_ok = pol.local_gpu is not False
        stt.trim(gpu_ok)
        if not gpu_ok and hasattr(speaker.tts, "release_gpu"):
            speaker.tts.release_gpu()

    core.hooks.append(gpu_hook)

    def speak(n, pol):
        # In quiet mode (voice = false), notification is text-only.
        if pol.voice:
            speaker.say(n.body or n.title, n.emotion or None, severity=n.severity, topic=n.topic)

    core.notifier.channels.append(speak)

    # Toggle game/flight alerts (topic "flight") with a single key; voice chat is unaffected.
    # Previously checked SocialWatch.reason() for "is War Thunder open"; on Discord it returned "in Discord voice
    # call", causing key to do nothing. Now works unconditionally, beeps the state.
    alert_mute_key = v.get("game_mute_key", "")
    # WinHotkeys is Windows-only (RegisterHotKey / ctypes.windll). If registration was attempted on Linux/macOS, worker
    # crashed, _ready never set, and start() waited 5s returning pseudo-success; registration bound to platform.
    if alert_mute_key and sys.platform == "win32":
        from alice.hotkeys import WinHotkeys

        def beep(muted: bool) -> None:
            try:
                import winsound

                if muted:  # muted: two descending beeps
                    winsound.Beep(880, 90); winsound.Beep(440, 140)
                else:  # unmuted: two ascending beeps
                    winsound.Beep(440, 90); winsound.Beep(880, 140)
            except Exception:  # noqa: BLE001
                pass

        def toggle_alerts() -> None:
            speaker.alerts_muted = not speaker.alerts_muted
            if speaker.alerts_muted:
                speaker.stop_alerts()
            state = "kapalı" if speaker.alerts_muted else "açık"
            voice_log(f"oyun uyarı sesi {state} ({alert_mute_key})")
            console.print(f"[dim]oyun uyarı sesi {state}[/]")
            threading.Thread(target=beep, args=(speaker.alerts_muted,), daemon=True).start()

        alert_hotkey = WinHotkeys({alert_mute_key: toggle_alerts})
        alert_hotkey.start()
        if alert_hotkey.failed:
            voice_log(f"uyarı susturma kısayolu kaydedilemedi (başka program tutuyor olabilir): {alert_hotkey.failed}")
        else:
            voice_log(f"uyarı susturma kısayolu hazır: {alert_mute_key}")
    if v.get("prewarm_callouts", True):
        # Prewarm flight alert audio to disk: flight audio comes from cloud, cached ones play instantly.
        from alice.sims.flight import callouts

        warm = threading.Timer(120, speaker.prewarm, args=(callouts(),),
                               kwargs={"limit": int(v.get("prewarm_per_start", 12)), "log": voice_log})
        warm.daemon = True
        warm.start()
        # Anticipation: based on habit, prepare remaining flight audio ~20 min before War Thunder opens.
        core.anticipator.hooks.append(lambda: speaker.prewarm(callouts(), limit=int(v.get("prewarm_per_start", 12)),
                                                              log=voice_log))
    if not mic_available():
        console.print("[yellow]mikrofon yok;[/] ALICE sadece bildirimleri seslendirecek")
        return None
    va = VoiceAssistant(agent, speaker, stt, ptt_key=v.get("ptt_key", ""),
                        wake_model=v.get("wake_model", ""), wake_phrase=v.get("wake_phrase", True),
                        barge_in=v.get("barge_in", True), follow_up=float(v.get("follow_up", 5)),
                        live=make_live(cfg, agent),
                        end_silence=float(v.get("end_silence", 0.5)), **live_text_options(v),
                        speaker_gate=SpeakerGate.load(cfg, log=voice_log), social=SocialWatch(cfg, log=voice_log).reason,
                        social_ptt=v.get("social_mode", "wake") == "ptt")
    va.keep_recent = bool(v.get("keep_recent_audio", True))
    if v.get("corpus", True):  # real voice corpus: wake-ups and corrections become test recordings (alice voice replay)
        from alice.voice import corpus

        corpus.attach(va, log=voice_log)
    threading.Thread(target=va.run, daemon=True, name="alice-voice").start()
    voice_log(f"ses modülü başladı: {time.monotonic() - t0:.1f} sn")
    return va


FEED_TEMPLATE = """# ALICE akışları: bağlı servislerde yeni bir şey olunca haber verir.
# Çekirdek çalışırken (alice core run) yoklanır. Ayrıntı: docs/CONNECT.md

[[feed]]
name = "ornek"
connector = "discord"      # alice connect ile listelenen bağlantı
action = "messages"        # o bağlantının bir eylemi
args = { channel_id = "KANAL_ID" }
every_minutes = 5
id_field = "id"            # yeni öğeyi ayırt eden alan
title = "Discord · {author.username}"
body = "{content}"
topic = "discord"
# agent = "Bu mesajlara bakip bana ozetle"   # dolu olursa ALICE uyanir ve bu isi yapar
# quiet = "23:00-08:00"      # bu saatlerde biriktir, sabah ozet olarak gelsin
# digest_minutes = 60        # tek tek degil, saatte bir ozet
# cooldown_minutes = 10      # bir bildirimden sonra gelenleri topla

[feed.when]                  # hangi mesaj bildirime degsin (bos: hepsi)
any = ["hayat", "@everyone"] # en az biri gecsin
none = ["bot"]               # bunlar gecmesin
# ask = "Bu mesaj bana bir sey soruyor ya da acil mi?"   # anlamina gore: hizli model karar verir

[feed.escalate]              # bu tutarsa onem kritik olur, sessiz saatte de soylenir
regex = '\\b(acil|hemen)\\b'   # tek tırnak: TOML kaçış yapmaz

# Arkadas oyuna girince haber ver (Steam):
# [[feed]]
# name = "arkadaslar"
# connector = "steam"
# action = "profile"
# args = { steamids = "STEAMID1,STEAMID2" }
# mode = "change"
# watch = ["gameextrainfo"]
# id_field = "steamid"
# every_minutes = 3
# title = "{personaname} oyuna girdi"
# body = "{gameextrainfo}"
# [feed.when]
# fields = { gameextrainfo = "*" }
"""


def announce_update(core, updater) -> None:
    """If we were just updated, say so once. If the app is open, it writes to the chat and the core stays quiet."""
    if updater.app_running():
        return
    from alice.core.notify import Notification

    rb = updater.consume_rollback("core")
    if rb:
        core.notifier.send(Notification("ALICE güncellemesi geri alındı",
                                        f"Yeni sürüm açılamadı, önceki sürüme dönüldü ({rb.get('reason')}).",
                                        "warn", "core", kind="info"))
    ann = updater.consume_announcement("core")
    if not ann:
        return
    from alice.core.notify import Notification

    changes = ann.get("changes") or []
    body = "; ".join(changes[:3]) + (f" ve {len(changes) - 3} değişiklik daha" if len(changes) > 3 else "")
    core.notifier.send(Notification("ALICE güncellendi", body or f"{ann.get('to', '')[:7]} sürümüne geçildi",
                                    "info", "core", kind="info"))


def feeds_cmd(argv: list[str]) -> int:
    """alice feeds [list] | --new | --test <name>"""
    from alice.core.feeds import FeedMonitor, feeds_path, load_feeds

    sub = argv[0] if argv else "list"
    if sub == "--new":
        p = feeds_path()
        if p.exists():
            console.print(f"[yellow]zaten var:[/] {p}")
            return 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(FEED_TEMPLATE, encoding="utf-8")
        console.print(f"[green]oluşturuldu:[/] {p}")
        return 0

    feeds, errors = load_feeds()
    for e in errors:
        console.print(f"[yellow]{escape(e)}[/]")

    if sub == "--test" and len(argv) > 1:
        feed = next((f for f in feeds if f.name == argv[1]), None)
        if feed is None:
            console.print(f"[red]akış yok:[/] {escape(argv[1])}")
            return 1
        cfg = load_config()
        decider = None
        if feed.when.ask or feed.escalate.ask:
            from alice.decider import Decider
            from alice.providers.router import Router

            decider = Decider.from_config(cfg, Router(cfg))
        mon = FeedMonitor(cfg=cfg, decider=decider, emit=lambda s: console.print(f"[dim]{escape(s)}[/]"))
        try:
            verdicts = mon.explain(feed)
        except Exception as e:  # noqa: BLE001 - test command: show the error, don't crash
            console.print(f"[red]{escape(str(e))}[/]")
            return 1
        passed = sum(v.ok for v in verdicts)
        console.print(f"[bold]{len(verdicts)} öğe, {passed} tanesi koşulu geçiyor[/]  [dim]{escape(feed.describe())}[/]")
        for v in verdicts[:15]:
            mark = "[red]![/]" if v.ok and v.severity == "critical" else ("[green]✓[/]" if v.ok else "[dim]✗[/]")
            line = escape(v.title + (f" — {v.body}" if v.body else ""))[:140]
            console.print(f"  {mark} {line}  [dim]{escape(v.reason)}[/]")
        if feed.mode == "change":
            console.print("[dim]change modunda bildirim, izlenen alan bir önceki yoklamadan farklıysa gider.[/]")
        return 0

    if not feeds:
        console.print(f"[dim]akış yok. Örnek dosya oluşturmak için: alice feeds --new  ({feeds_path()})[/]")
        return 0
    for f in feeds:
        console.print(f"[bold]{escape(f.name)}[/]" + ("" if f.enabled else " [dim](kapalı)[/]")
                      + f"  [dim]{escape(f.describe())}[/]")
    return 0


def rules_cmd(argv: list[str]) -> int:
    from alice.core.rules import RuleSet, parse_rule

    rs = RuleSet()
    sub = argv[0] if argv else "list"
    if sub == "list":
        for r in rs.rules:
            console.print(escape(r.summary()) + (f"  [dim]{escape(r.description)}[/]" if r.description else ""))
        if not rs.rules:
            console.print("[dim]kural yok[/]")
        return 0
    if sub == "add" and len(argv) > 1:
        from alice.core.store import Store
        from alice.providers.router import AllProvidersFailed, Router

        text = " ".join(argv[1:])
        try:
            with console.status("kurala çevriliyor..."), Store() as st:
                rule = parse_rule(text, Router(load_config()), st)
        except (ValueError, TypeError, AllProvidersFailed) as e:
            console.print(f"[red]anlaşılamadı:[/] {escape(str(e))}")
            return 1
        console.print(f"[bold]{escape(rule.summary())}[/]\n  mesaj: {escape(rule.message)}")
        if rule.confirm:
            console.print(f"  Jev'e sorulacak: {escape(rule.confirm)}")
        if rule.action == "agent":
            console.print(f"  tetiklenince ALICE: {escape(rule.prompt)}")
        if not _confirm("Kurulsun mu?"):
            return 1
        rs.add(rule)
        console.print("[green]kuruldu[/]")
        return 0
    if sub in ("rm", "on", "off") and len(argv) > 1:
        ok = rs.remove(argv[1]) if sub == "rm" else rs.set_enabled(argv[1], sub == "on")
        console.print("[green]tamam[/]" if ok else f"[red]kural yok: {escape(argv[1])}[/]")
        return 0 if ok else 1
    console.print(__doc__, markup=False)
    return 2


def tasks_cmd(argv: list[str]) -> int:
    from alice.core.schedule import Cron, TaskStore, parse_task

    ts = TaskStore()
    sub = argv[0] if argv else "list"
    if sub == "list":
        for t in ts.tasks:
            console.print(escape(t.summary()))
            if t.last_result:
                console.print(f"  [dim]son sonuç: {escape(t.last_result[:150])}[/]")
        if not ts.tasks:
            console.print("[dim]zamanlanmış görev yok[/]")
        return 0
    if sub == "add" and len(argv) > 1:
        from alice.providers.router import AllProvidersFailed, Router

        text = " ".join(argv[1:])
        try:
            with console.status("zamanlamaya çevriliyor..."):
                task = parse_task(text, Router(load_config()))
        except (ValueError, TypeError, AllProvidersFailed) as e:
            console.print(f"[red]anlaşılamadı:[/] {escape(str(e))}")
            return 1
        console.print(f"[bold]Ne zaman:[/] {Cron(task.cron).describe()}  [dim]({task.cron})[/]\n"
                      f"[bold]Ne yapılacak:[/] {escape(task.prompt)}\n"
                      f"[bold]Kaçırılırsa:[/] {'açılışta telafi edilir' if task.catch_up else 'atlanır'}")
        if not _confirm("Kurulsun mu?"):
            return 1
        ts.add(task)
        console.print(f"[green]kuruldu[/] ({task.id}), sonraki: {task.next_run():%d.%m %H:%M}")
        from alice.core.service import read_state

        if not read_state().get("running"):
            console.print("[yellow]çekirdek çalışmıyor;[/] görevin çalışması için [bold]alice core install[/]")
        return 0
    if sub in ("rm", "run") and len(argv) > 1:
        t = ts.get(argv[1])
        if t is None:
            console.print(f"[red]görev yok: {escape(argv[1])}[/]")
            return 1
        if sub == "rm":
            ts.remove(t.id)
        else:
            t.pending = True
            ts.save()
        console.print("[green]tamam[/]" + (" (bir sonraki çekirdek turunda çalışacak)" if sub == "run" else ""))
        return 0
    console.print(__doc__, markup=False)
    return 2
