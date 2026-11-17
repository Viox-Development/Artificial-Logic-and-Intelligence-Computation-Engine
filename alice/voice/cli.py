"""Voice commands.

  alice voice [--ptt <key>] [--no-wake]   start the voice assistant (push-to-talk + "Hey Alice")
  alice voice audition [--voices Aoede,Kore] [--local xtts,chatterbox] [--emotions calm,urgent]
                                      listening test: generates samples and opens the HTML page
  alice voice use <candidate>         choose the voice: gemini:Aoede, xtts, chatterbox
  alice voice library                 build the emotion library with the selected cloud voice (for local engines)
  alice voice say "text" [--emotion urgent] [--engine cloud|local]
  alice voice test                    listen to the microphone for 5 sec, show the level and what is heard
  alice voice enroll [--off]          enroll your voice: ALICE answers only your voice (--off: turn it off)
  alice voice status                  engines, library, optional packages
  alice voice gecikme [--days 7]      latency (gecikme): time from going quiet to ALICE's first sound (summary)
  alice voice replay [--runs 3] [--label L] [--threshold 0.3]
                                      replay the real voice corpus: false/missed wake-ups, voice trace
  alice voice corpus [stats|import|label|rm <id>]
                                      real voice corpus: stats, import recent recordings, label, delete
  alice voice models                  which Gemini voice models your account has (uses no quota)
  alice voice install-local           install the local voice engine (Chatterbox) in a separate environment
  alice voice on | off                start the voice assistant when the core starts / don't start it
"""

from __future__ import annotations

import importlib.util
import json
import time
import webbrowser

from rich.console import Console
from rich.markup import escape

from alice.config import load_config, set_config_value

console = Console(highlight=False)

OPTIONAL = (("sounddevice", "mikrofon ve ses çalma", "voice"), ("pynput", "bas-konuş ve kısayollar", "voice"),
            ("webrtcvad", "daha iyi konuşma algılama", "voice"),
            ("faster_whisper", "seni dinlemek için yerel Whisper (Groq yoksa)", "voice-local"),
            ("openwakeword", "yerel 'Hey Alice'", "voice-local"),
            ("sherpa_onnx", "sadece senin sesine cevap (alice voice enroll)", "voice"))


def _parser(prog: str):
    """Help and error messages via the Turkish argparse (same as alice.cli)."""
    from alice.cli import TrArgumentParser

    return TrArgumentParser(prog=prog)


def _replay(cfg, argv) -> int:
    from alice.voice import corpus

    p = _parser("alice voice replay")
    p.add_argument("--runs", type=int, default=1, help="her kaydı kaç kez oynat (kararlılık için 3)")
    p.add_argument("--label", default="", help="sadece bu etiket (uyanmaliydi, uyanmamaliydi, belirsiz)")
    p.add_argument("--threshold", type=float, default=None, help="ses izi eşiğini dene (varsayılan: ayardaki)")
    p.add_argument("--strong", action="store_true", help="sadece kesin/güçlü etiketliler")
    a = p.parse_args(argv)
    cases = [c for c in corpus.load_manifest() if (not a.label or c.label == a.label)
             and (not a.strong or c.strong)]
    if not cases:
        console.print("[yellow]korpus boş.[/] Kayıtlar çekirdek açıkken kendiliğinden birikir; şimdi almak için: "
                      "[bold]alice voice corpus import[/]")
        return 1
    console.print(f"[dim]{len(cases)} kayıt yerel Whisper ve ses iziyle oynatılıyor...[/]")
    va = corpus.build_replay_assistant(threshold=a.threshold)
    rep = corpus.evaluate(cases, va, runs=a.runs)
    thr = getattr(va.speaker_gate, "threshold", None)
    console.print(escape(corpus.format_report(rep, thr)))
    out = corpus.root() / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    console.print(f"[dim]ayrıntı: {escape(str(out))}[/]")
    return 1 if rep["false_wakes_strong"] else 0


def _corpus(cfg, argv) -> int:
    from alice.voice import corpus

    sub = argv[0] if argv else "stats"
    cases = corpus.load_manifest()
    if sub == "stats":
        from collections import Counter

        console.print(f"[bold]Gerçek ses korpusu[/] ({escape(str(corpus.root()))}): {len(cases)} kayıt, "
                      f"{sum(c.dur for c in cases) / 60:.1f} dk")
        for (label, conf), n in sorted(Counter((c.label, c.confidence) for c in cases).items()):
            console.print(f"  {label:<14} {conf:<6} {n}")
        return 0
    if sub == "import":
        va = corpus.build_replay_assistant()
        n = corpus.import_recent(va, log=lambda s: console.print(f"[dim]{escape(s)}[/]"))
        console.print(f"[green]{n} kayıt içe alındı.[/]")
        return 0
    if sub == "rm" and len(argv) > 1:
        ok = corpus.remove(argv[1])
        console.print("silindi" if ok else "yok")
        return 0 if ok else 1
    if sub == "label":
        todo = corpus.to_label(cases)
        if not todo:
            console.print("etiketlenecek kayıt yok")
            return 0
        console.print("Her kayıt çalınır. [bold]e[/]: ALICE'e söyledim  [bold]h[/]: ona söylemedim  "
                      "[bold]m[/]: söyledim ama metni yanlış anladı  [bold]t[/]: tekrar çal  [bold]s[/]: atla  "
                      "[bold]q[/]: bitir")
        base = corpus.root()
        for c in todo:
            console.print(f"{c.id}  {c.dur:.1f} sn  şimdiki etiket: {c.label} ({c.confidence})  "
                          f"duyulan: {escape(str(c.orig.get('head') or ''))[:80]}")
            while True:
                corpus.play(c.wav(base))
                ans = input("> ").strip().lower()[:1]
                if ans != "t":
                    break
            if ans == "q":
                break
            label = {"e": "uyanmaliydi", "h": "uyanmamaliydi", "m": "yanlis_dokum"}.get(ans)
            if label:
                bucket = ("pozitif" if label != "uyanmamaliydi" else c.bucket
                          if c.bucket in ("baska-konusmaci", "konusma-yok") else "negatif-konusma")
                corpus.relabel(c.id, label=label, bucket=bucket, confidence="kesin", labeled_by="kullanıcı")
        return 0
    console.print("kullanım: alice voice corpus [stats|import|label|rm <id>]")
    return 2


def _latency(cfg, argv) -> int:
    from alice.voice import latency

    p = _parser("alice voice gecikme")
    p.add_argument("--days", type=float, default=7, help="kaç günlük kayıt (varsayılan 7)")
    a = p.parse_args(argv)
    text = latency.summary(a.days)
    console.print(f"[bold]Sesli mod gecikmesi[/] (son {a.days:g} gün, susmandan ALICE'in ilk sesine):", highlight=False)
    console.print(escape(text) if text else "[dim]henüz kayıt yok: sesli modu kullandıkça birikir[/]")
    return 0


def voice_cmd(argv: list[str]) -> int:
    sub = argv[0] if argv and not argv[0].startswith("-") else "run"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv
    cfg = load_config()
    fn = {"on": _on, "off": _off, "install-local": _install_local, "models": _models, "run": _run, "audition": _audition, "use": _use, "library": _library, "say": _say, "test": _test, "enroll": _enroll,
          "status": _status, "gecikme": _latency, "latency": _latency, "replay": _replay,
          "corpus": _corpus}.get(sub)
    if fn is None:
        console.print(f"bilinmeyen ses komutu: {sub}", markup=False)
        console.print(__doc__, markup=False)
        return 2
    return fn(cfg, rest)


def _on(cfg, argv) -> int:
    from alice.core.service import autostart_path, install_autostart, read_state, start_detached

    was_running = read_state().get("running")
    set_config_value("voice", "enabled", True)
    console.print("[green]ses açık.[/] \"Hey Alice\" dinleme, arka plandaki ALICE çekirdeğinde (tepsi simgesi) çalışır.")
    if was_running:
        console.print("Çekirdek zaten açık; sesin başlaması için tepsideki ALICE simgesinden Çık, sonra "
                      "[bold]alice core run --tray[/] (ya da bilgisayarı yeniden başlat).")
    else:
        start_detached()
        console.print("Çekirdek başlatıldı (tepside ALICE simgesi görünür).")
    if not autostart_path().exists():
        try:
            install_autostart()
            console.print("Windows açılınca çekirdek kendiliğinden başlayacak (kapatmak için: alice core uninstall).")
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]otomatik başlatma kurulamadı:[/] {escape(str(e))}")
    console.print('Ne duyduğunu görmek için [bold]alice voice[/] komutunu terminalde de çalıştırabilirsin.')
    return 0


def _off(cfg, argv) -> int:
    set_config_value("voice", "enabled", False)
    console.print("ses kapalı (çekirdek yeniden başlayınca geçerli)")
    return 0


def _install_local(cfg, argv) -> int:
    from alice.voice.install_local import install

    try:
        py = install(emit=lambda s: console.print(f"[dim]{escape(s)}[/]"))
    except RuntimeError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 1
    console.print(f"[green]yerel ses kuruldu:[/] {py}")
    console.print('Dene: [bold]alice voice say "Merhaba, ben ALICE." --engine local[/] '
                  "(ilk seferde model indirilir, birkaç dakika sürebilir)")
    console.print("Duygu kütüphanesi yoksa Chatterbox kendi sesiyle konuşur; Gemini kotası yenilenince "
                  "[bold]alice voice library[/] ile seçtiğin sesi ver.")
    return 0


def _models(cfg, argv) -> int:
    import httpx

    from alice.voice.tts import list_gemini_tts_models, make_cloud

    eng = make_cloud(cfg)
    if eng is None:
        console.print("[red]GEMINI_API_KEY yok[/]")
        return 1
    try:
        avail = list_gemini_tts_models(eng.api_key)
    except httpx.HTTPError as e:
        console.print(f"[red]model listesi alınamadı:[/] {escape(str(e))}")
        return 1
    console.print("[bold]Hesabındaki ses modelleri:[/] " + (", ".join(avail) or "[red]hiç yok[/]"))
    console.print("[bold]ALICE'in sırası:[/]")
    for m in eng.models:
        console.print(f"  {'[green]✓[/]' if m in avail else '[red]✗ yok[/]'} {m}")
    missing = [m for m in avail if m not in eng.models]
    if missing:
        console.print(f"[dim]Sırada olmayan ama hesabında olanlar: {', '.join(missing)}[/]")
    return 0


def _status(cfg, argv) -> int:
    from pathlib import Path

    from alice.voice.audio import Player, mic_available
    from alice.voice.stt import make_stt
    from alice.voice.tts import EmotionLibrary, make_cloud, make_local

    v = cfg.data.get("voice", {})
    cloud, local = make_cloud(cfg), make_local(cfg)
    console.print(f"[bold]Bulut sesi:[/] {v.get('tts_cloud')} "
                  f"{'[green]hazır[/]' if cloud else '[red]GEMINI_API_KEY yok[/]'}")
    local_state = ""
    if v.get("tts_local") == "chatterbox" and local is not None:
        if not v.get("local_python") or not Path(v["local_python"]).exists():
            local_state = " [red]kurulu değil (alice voice install-local)[/]"
        else:
            local_state = " [green]sunucu açık[/]" if local.healthy() else " [dim]kurulu, ilk konuşmada açılır[/]"
    console.print(f"[bold]Yerel ses:[/] {v.get('tts_local') or '[dim]yok (alice voice install-local)[/]'}{local_state}")
    if v.get("local_python") and Path(v["local_python"]).exists():
        import subprocess

        r = subprocess.run([v["local_python"], "-c", "import importlib.util as u; "
                            "print(u.find_spec('whisper') is not None)"], capture_output=True, text=True)
        ok = r.stdout.strip() == "True"
        console.print(f"[bold]Ses kontrolü (yerel Whisper):[/] "
                      f"{'[green]var[/]' if ok else '[yellow]yok[/] [dim](alice voice install-local)[/]'}")
    lib = EmotionLibrary()
    if lib.exists():
        from alice.voice.tone import EMOTIONS, NAMES_TR

        have = [NAMES_TR[e] for e in EMOTIONS if (lib.path / f"{e}.wav").exists()]
        src = lib.source or "ses bilinmiyor"
        warn = (f" [yellow]seçili ses {cloud.name}, kütüphane farklı: alice voice library --redo[/]"
                if cloud and lib.source and lib.source != cloud.name else "")
        console.print(f"[bold]Duygu kütüphanesi:[/] [green]var[/] ({src}; {', '.join(have)}){warn}")
    else:
        console.print("[bold]Duygu kütüphanesi:[/] [dim]yok (alice voice library)[/]")
    from alice.voice import latency

    week = latency.summary(7)
    if week:
        console.print(f"[bold]Gecikme (7 gün):[/] {escape(week.splitlines()[0])}")
    stt = make_stt(cfg)
    console.print(f"[bold]Konuşma tanıma:[/] {', '.join(e.name for e in stt.engines) or '[red]yok[/]'}")
    console.print(f"[bold]Ses çalma:[/] {Player().backend}   [bold]Mikrofon:[/] "
                  f"{'[green]var[/]' if mic_available() else '[red]yok[/]'}")
    console.print("[bold]Opsiyonel paketler:[/]")
    for mod, why, extra in OPTIONAL:
        ok = importlib.util.find_spec(mod) is not None
        hint = f"pip install 'alice-harness[{extra}]'" if extra != "elle" else ""
        console.print(f"  {'[green]✓[/]' if ok else '[red]✗[/]'} {mod:<15} {why} [dim]{'' if ok else escape(hint)}[/]")
    return 0


def _audition(cfg, argv) -> int:
    from alice.voice.audition import run_audition
    from alice.voice.tts import GEMINI_VOICES, EmotionLibrary, make_cloud, make_local

    ap = _parser(prog="alice voice audition")
    ap.add_argument("--voices", default="Aoede,Kore,Leda,Sulafat,Despina,Achernar",
                    help=f"Gemini sesleri, virgülle. Hepsi: {','.join(GEMINI_VOICES)}")
    ap.add_argument("--local", default="", help="xtts,chatterbox (kuruluysa)")
    ap.add_argument("--ref", help="yerel motorlar için referans WAV (varsayılan: duygu kütüphanesi ya da ilk Gemini sesi)")
    ap.add_argument("--emotions", default="", help="calm,happy,serious,urgent,playful")
    ap.add_argument("--no-open", action="store_true", help="sayfayı tarayıcıda açma, sadece yolunu yaz")
    a = ap.parse_args(argv)
    engines = []
    for name in filter(None, a.voices.split(",")):
        eng = make_cloud(cfg, f"gemini:{name.strip()}")
        if eng is None:
            console.print("[red]GEMINI_API_KEY yok[/], bulut sesleri atlanıyor")
            break
        engines.append(eng)
    for name in filter(None, a.local.split(",")):
        eng = make_local(cfg, name.strip())
        if eng is None:
            console.print(f"[yellow]bilinmeyen yerel motor: {name}[/]")
            continue
        lib = EmotionLibrary()
        if a.ref:
            from pathlib import Path

            eng.library = _single_ref_library(Path(a.ref))
        elif not lib.exists() and engines:
            # No reference? Then the "sakin" (calm) sample of the first cloud voice becomes the reference.
            from alice.voice.audition import audition_dir

            run_audition(engines[:1], ["calm"], emit=lambda s: console.print(f"[dim]{s}[/]"))
            eng.library = _single_ref_library(audition_dir() / f"{engines[0].name.replace(':', '_')}_calm.wav")
        engines.append(eng)
    if not engines:
        console.print("[red]denenecek ses yok[/]")
        return 1
    _attach_verifier(cfg, engines)
    emotions = [e.strip() for e in a.emotions.split(",") if e.strip()] or None
    with console.status("örnekler hazırlanıyor..."):
        page = run_audition(engines, emotions, emit=lambda s: console.print(f"[dim]{escape(s)}[/]"))
    console.print(f"[green]hazır:[/] {page}")
    if not a.no_open:
        webbrowser.open(page.as_uri())
    return 0


def _attach_verifier(cfg, engines) -> None:
    """Verify Gemini output with Whisper: if the instruction got read aloud, that format is not reused on that model."""
    from alice.voice.stt import make_stt
    from alice.voice.tts import GeminiTTS, make_verifier

    stt = make_stt(cfg)
    if not stt.engines:
        console.print("[yellow]Whisper yok (GROQ_API_KEY):[/] talimat okunması kontrol edilemeyecek")
        return
    for e in engines:
        if isinstance(e, GeminiTTS):
            e.verify = make_verifier(stt)


def _single_ref_library(path):
    from alice.voice.tts import EmotionLibrary

    class One(EmotionLibrary):
        def clip(self, emotion):
            return path if path.exists() else None

    return One(path.parent)


def _use(cfg, argv) -> int:
    if not argv:
        console.print("kullanım: alice voice use gemini:Aoede | xtts | chatterbox")
        return 2
    ref = argv[0]
    if ref.startswith("gemini:"):
        p = set_config_value("voice", "tts_cloud", ref)
        console.print(f"[green]bulut sesi:[/] {ref}  [dim]({p})[/]")
        console.print("Yerel motor da kullanacaksan şimdi: [bold]alice voice library[/] (bu sesle duygu kütüphanesi)")
    elif ref in ("xtts", "chatterbox", ""):
        set_config_value("voice", "tts_local", ref)
        console.print(f"[green]yerel ses:[/] {ref or 'kapalı'}")
    else:
        console.print("[red]bilinmeyen aday[/]")
        return 2
    return 0


def _library(cfg, argv) -> int:
    from alice.voice.tts import build_library, make_cloud

    eng = make_cloud(cfg)
    if eng is None:
        console.print("[red]bulut sesi yok (GEMINI_API_KEY)[/]")
        return 1
    _attach_verifier(cfg, [eng])
    from alice.voice.tone import NAMES_TR

    console.print(f"ses: [bold]{eng.name}[/] [dim](değiştirmek için: alice voice use gemini:<ses>)[/]")
    with console.status("duygu kütüphanesi hazırlanıyor..."):
        out, missing = build_library(eng, emit=lambda s: console.print(f"[dim]{escape(s)}[/]"),
                                     redo="--redo" in argv)
    if missing:
        console.print(f"[yellow]eksik kaldı:[/] {', '.join(NAMES_TR[m] for m in missing)}. Kota yenilenince aynı "
                      "komut sadece eksikleri tamamlar; o zamana kadar bu tonlar sakin kaydı kullanır.")
        return 0
    console.print(f"[green]hazır:[/] {out}")
    return 0


def _say(cfg, argv) -> int:
    from alice.voice.audio import Player
    from alice.voice.tone import pick
    from alice.voice.tts import TTSError, TTSRouter, make_cloud, make_local, make_tts

    ap = _parser(prog="alice voice say")
    ap.add_argument("text", nargs="+", help="seslendirilecek metin")
    ap.add_argument("--emotion", help="calm, happy, serious, urgent, playful (varsayılan: metne göre seçilir)")
    ap.add_argument("--engine", choices=["cloud", "local"], help="sadece bulut ya da sadece yerel motorla dene")
    ap.add_argument("--out", help="çalmak yerine WAV olarak kaydet")
    a = ap.parse_args(argv)
    text = " ".join(a.text)
    tts = make_tts(cfg)
    if a.engine:
        tts = TTSRouter(make_cloud(cfg) if a.engine == "cloud" else None,
                        make_local(cfg) if a.engine == "local" else None)
    emo = a.emotion or pick(text)
    try:
        with console.status(f"sentezleniyor ({emo})..."):
            audio = tts.synth(text, emo)
    except TTSError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 1
    model = getattr(tts.cloud, "last_model", "") if tts.last.startswith("gemini") else ""
    detail = f" · {model} · {tts.cloud.last_format}" if model and "önbellek" not in tts.last else ""
    console.print(f"[dim]{tts.last}{detail} · {emo} · {audio.seconds:.1f} sn[/]")
    if a.out:
        audio.save(a.out)
        return 0
    player = Player()
    if not player.available:
        console.print("[red]ses çalınamıyor[/] (" + escape('pip install "alice-harness[voice]"') + ")")
        return 1
    player.play(audio)
    return 0


def _test(cfg, argv) -> int:
    import time

    from alice.voice.audio import Audio, Microphone, rms
    from alice.voice.stt import STTError, make_stt

    try:
        mic = Microphone()
    except ModuleNotFoundError as e:
        console.print(f"[red]{escape(e.name or 'sounddevice')} kurulu değil.[/] Kur: "
                      + escape('pip install "alice-harness[voice]"'))
        return 1
    except Exception as e:
        console.print(f"[red]mikrofon açılamadı:[/] {escape(str(e))}")
        return 1
    console.print("5 saniye konuş...")
    frames = []
    with mic:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            f = mic.read()
            frames.append(f)
            lvl = rms(f)
            console.print(f"\r{'█' * int(min(lvl * 400, 40)):<40} {lvl:.3f}", end="")
    console.print()
    audio = Audio(b"".join(frames), Microphone.RATE)
    try:
        stt = make_stt(cfg)
        t0 = time.monotonic()
        text = stt.transcribe(audio)
        console.print(f"duyulan ({stt.last}, {time.monotonic() - t0:.1f} sn): [bold]{escape(text)}[/]")
    except STTError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 1
    return 0


def _record_one(mic, vad, seconds: float = 15.0):
    """Record one utterance (until silence). Returns None if time runs out."""
    import time

    from alice.voice.listen import utterances

    end = time.monotonic() + seconds

    def frames():
        while True:
            yield mic.read()

    for audio in utterances(frames(), vad, end_silence=0.8, max_seconds=12, stop=lambda: time.monotonic() > end):
        if audio.seconds >= 1.0:
            return audio
    return None


def _enroll(cfg, argv) -> int:
    """Enroll your voice: read a few sentences so ALICE takes your voiceprint; then only your voice wakes it."""
    from alice.voice import speaker_id as sid

    if "--off" in argv:
        try:
            sid.profile_path().unlink()
            console.print("ses tanıma kapatıldı: ALICE yine herkesin sesine cevap verir.")
        except FileNotFoundError:
            console.print("zaten kapalı: kayıtlı ses izi yok.")
        return 0
    if not sid.available():
        console.print("[red]sherpa-onnx kurulu değil.[/] Kur: " + escape('pip install "alice-harness[voice]"')
                      + " (ya da: pip install sherpa-onnx)")
        return 1
    try:
        sid.download_model(emit=lambda s: console.print(f"[dim]{escape(s)}[/]"))
        emb = sid.Embedder()
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]ses tanıma modeli hazırlanamadı:[/] {escape(str(e))}")
        return 1
    from alice.voice.audio import Microphone
    from alice.voice.listen import VAD

    try:
        mic = Microphone()
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]mikrofon açılamadı:[/] {escape(str(e))}")
        return 1
    console.print("Sesini tanıtıyoruz. Her cümleyi normal sesinle, ALICE'e konuşur gibi oku "
                  "(oyunda/Discord'da nasıl konuşuyorsan öyle).\n")
    vad = VAD()
    embeddings: list[list[float]] = []
    with mic:
        for i, sentence in enumerate(sid.ENROLL_SENTENCES, 1):
            for _attempt in range(2):
                console.print(f"[bold]{i}/{len(sid.ENROLL_SENTENCES)}[/]  \"{escape(sentence)}\"")
                audio = _record_one(mic, vad)
                if audio is not None:
                    embeddings.append(emb.embed(audio.pcm, audio.rate))
                    console.print(f"[dim]  alındı ({audio.seconds:.1f} sn)[/]")
                    break
                console.print("[yellow]  duyamadım, tekrar oku[/]")
        if len(embeddings) < 3:
            console.print("[red]yeterli kayıt alınamadı.[/] Mikrofonu kontrol et: alice voice test")
            return 1
        prof = sid.save_profile(embeddings)
        console.print(f"\n[green]sesin kaydedildi[/] ({len(embeddings)} cümle, eşik {prof['threshold']:.2f}, "
                      f"tutarlılık {prof['consistency']:.2f}).")
        if prof["consistency"] < 0.55:
            console.print("[yellow]Kayıtların birbirine pek benzemiyor (gürültü ya da uzak mikrofon). Sessiz bir "
                          "anda tekrar dene: alice voice enroll[/]")
        console.print("\nŞimdi kendi aklından bir cümle söyle, deneyelim:")
        audio = _record_one(mic, vad)
    if audio is not None:
        gate = sid.SpeakerGate(prof, emb)
        ok = gate.check(audio.pcm, audio.rate)
        console.print(f"benzerlik {gate.last_score:.2f} (eşik {gate.threshold:.2f}): "
                      + ("[green]seni tanıdı[/]" if ok else "[red]tanımadı; tekrar kayıt dene ya da config'te "
                                                             "[voice] speaker_threshold'ı düşür[/]"))
    console.print("Çalışan ALICE yeni ses izini hemen kullanır. Kapatmak için: alice voice enroll --off (ya da Sistem → Ses)")
    return 0


def _run(cfg, argv) -> int:
    from alice.voice.assistant import VoiceAssistant, build_voice_agent, live_text_options, make_live, voice_log
    from alice.voice.social import SocialWatch
    from alice.voice.speaker_id import SpeakerGate
    from alice.voice.audio import mic_available
    from alice.voice.stt import make_stt

    ap = _parser(prog="alice voice")
    ap.add_argument("--ptt", help="bas-konuş tuşu (varsayılan config: voice.ptt_key)")
    ap.add_argument("--no-wake", action="store_true", help="'Hey Alice' kapalı, sadece bas-konuş")
    a = ap.parse_args(argv)
    if not mic_available():
        console.print("[red]mikrofon yok[/] " + escape("(pip install 'alice-harness[voice]')"))
        return 1
    from alice.cli import RichUI

    v = cfg.data.get("voice", {})
    agent, speaker, ctx = build_voice_agent(cfg, RichUI())
    va = VoiceAssistant(agent, speaker, make_stt(cfg), ptt_key=a.ptt or v.get("ptt_key", ""),
                        wake_model=v.get("wake_model", ""), wake_phrase=not a.no_wake and v.get("wake_phrase", True),
                        barge_in=v.get("barge_in", True), follow_up=float(v.get("follow_up", 5)),
                        emit=lambda s: console.print(f"[dim]  {escape(s)}[/]"), live=make_live(cfg, agent),
                        end_silence=float(v.get("end_silence", 0.5)), **live_text_options(v),
                        speaker_gate=SpeakerGate.load(cfg, log=voice_log), social=SocialWatch(cfg, log=voice_log).reason)
    keys = f"bas-konuş: {va.ptt_key}" if va.ptt_key else ""
    wake = "'Hey Alice'" if va.wake_phrase or va.wake_model else ""
    console.print(f"[bold magenta]ALICE[/] dinliyor ({', '.join(filter(None, [keys, wake]))}). [dim]Ctrl+C çık[/]")
    try:
        va.run()
    except KeyboardInterrupt:
        va.stop()
    finally:
        from alice.tools import cleanup

        cleanup(ctx)
    return 0
