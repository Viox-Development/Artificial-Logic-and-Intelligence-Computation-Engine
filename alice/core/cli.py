"""Çekirdek komutları.

  alice core run [--tray] [--once]   çekirdeği çalıştır (--tray: sistem tepsisinde)
  alice core install | uninstall     oturum açılınca otomatik başlat / kaldır
  alice core status                  çalışıyor mu, hangi mod, son olaylar
  alice core mode <mod|auto>         modu elle sabitle (normal, dev, flight, quiet) ya da otomatiğe dön
  alice core stats <anahtar> [süre]  ör. alice core stats pc.gpu.temp 1d
  alice rules [list|add "cümle"|rm|on|off <id>]
  alice tasks [list|add "cümle"|rm|run <id>]
  alice feeds [list|--new|--test <ad>]   bağlı servisleri izleyip haber veren akışlar
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
        cfg = load_config()
        if not service.acquire_lock():
            console.print("[yellow]çekirdek zaten çalışıyor[/]")
            return 1
        from alice.decider import Decider
        from alice.providers.router import Router

        router = Router(cfg)
        core = service.Core(cfg, decider=Decider.from_config(cfg, router), run_agent=service.make_agent_runner(cfg))
        core.prime()  # DCS açık mı: ses modülü ekran kartını kullanıp kullanmayacağını buna göre seçer
        voice = _start_voice(cfg, core) if cfg.data.get("voice", {}).get("enabled") and "--once" not in argv else None
        sims = _start_sims(cfg, core) if "--once" not in argv else None
        phone = _start_phone(cfg, core) if cfg.data.get("phone", {}).get("enabled") and "--once" not in argv else None
        quick = _start_quick(cfg) if cfg.data.get("quick", {}).get("enabled", True) and "--once" not in argv else None
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
            if voice is not None:
                voice.stop()
            if quick is not None:
                quick.stop()
            if sims is not None:
                sims.stop()
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
                console.print("[dim]Tepsi simgesi için: pip install 'alice-harness[core]'[/]")
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
        st = Store()
        since = parse_time(argv[2] if len(argv) > 2 else "1d")
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

        evs = Store().events(limit=10)
        if evs:
            console.print("[bold]Son olaylar[/]")
            for e in reversed(evs):
                console.print("  " + escape(e.line()))
        return 0
    console.print(__doc__)
    return 2


def _start_phone(cfg, core):
    from alice.app.phone import PhoneServer

    try:
        ps = PhoneServer(cfg, core.store)
    except OSError as e:
        console.print(f"[yellow]telefon köprüsü açılamadı:[/] {e}")
        return None
    console.print(f"telefon: {ps.url}")
    return ps


def _start_sims(cfg, core):
    """DCS ve War Thunder dinleyicilerini başlat."""
    from alice.sims.hub import SimHub

    return SimHub(cfg.data.get("sims", {}), core.store, core.notifier, processes=lambda: core.pc.processes).start()


def _start_quick(cfg):
    """Hızlı bar kısayollarını çekirdekle birlikte başlat (pynput yoksa sessizce atla)."""
    import importlib.util

    if importlib.util.find_spec("pynput") is None:
        return None
    from alice.app.quick import QuickBar

    qb = QuickBar(cfg)
    if not qb.start_hotkeys():
        qb.stop()
        return None
    return qb


def _start_voice(cfg, core):
    """Sesli asistanı çekirdekle aynı süreçte başlat; bildirimler de seslendirilsin."""
    import threading
    import time

    from alice.voice.assistant import VoiceAssistant, build_voice_agent, live_text_options, make_live, voice_log
    from alice.voice.audio import mic_available
    from alice.voice.stt import make_stt
    from alice.voice.vocab import known_names

    t0 = time.monotonic()
    v = cfg.data.get("voice", {})
    # En uzun süren iş uyandırma modelinin yüklenmesi (ekran kartında ilk CUDA çağrısı dahil birkaç sn): ajan,
    # ses ve mikrofon hazırlanırken paralel başlasın. Uygulama/site adları da ilk "Hey Alice"i bekletmesin.
    stt = make_stt(cfg)
    if stt.wake_gate is not None and mic_available():
        stt.wake_gate.warm_up(log=voice_log)
    threading.Thread(target=known_names, args=(cfg,), daemon=True, name="alice-vocab").start()
    agent, speaker, _ = build_voice_agent(cfg)

    def gpu_hook(pol) -> None:
        gpu_ok = pol.local_gpu is not False
        stt.trim(gpu_ok)
        if not gpu_ok and hasattr(speaker.tts, "release_gpu"):
            speaker.tts.release_gpu()

    core.hooks.append(gpu_hook)

    def speak(n, pol):
        # Sessiz modda (voice = false) bildirim sadece yazılı gelir.
        if pol.voice:
            speaker.say(n.body or n.title, n.emotion or None, severity=n.severity, topic=n.topic)

    core.notifier.channels.append(speak)
    if not mic_available():
        console.print("[yellow]mikrofon yok;[/] ALICE sadece bildirimleri seslendirecek")
        return None
    va = VoiceAssistant(agent, speaker, stt, ptt_key=v.get("ptt_key", ""),
                        wake_model=v.get("wake_model", ""), wake_phrase=v.get("wake_phrase", True),
                        barge_in=v.get("barge_in", True), follow_up=float(v.get("follow_up", 5)),
                        live=make_live(cfg, agent),
                        end_silence=float(v.get("end_silence", 0.5)), **live_text_options(v))
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


def feeds_cmd(argv: list[str]) -> int:
    """alice feeds [list] | --new | --test <ad>"""
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
        except Exception as e:  # noqa: BLE001 - test komutu: hatayı göster, çökme
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
            with console.status("kurala çevriliyor..."):
                rule = parse_rule(text, Router(load_config()), Store())
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
    console.print(__doc__)
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
    console.print(__doc__)
    return 2
