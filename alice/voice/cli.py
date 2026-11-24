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
from alice.i18n import _

console = Console(highlight=False)

OPTIONAL = (("sounddevice", "microphone and audio playback", "voice"), ("pynput", "push-to-talk and shortcuts", "voice"),
            ("webrtcvad", "better speech detection", "voice"),
            ("faster_whisper", "local Whisper to hear you (without Groq)", "voice-local"),
            ("openwakeword", "local 'Hey Alice'", "voice-local"),
            ("sherpa_onnx", "answer only your voice (alice voice enroll)", "voice"))


def _parser(prog: str):
    """Help and error messages via the same argparse subclass as alice.cli."""
    from alice.cli import TrArgumentParser

    return TrArgumentParser(prog=prog)


def _replay(cfg, argv) -> int:
    from alice.voice import corpus

    p = _parser("alice voice replay")
    p.add_argument("--runs", type=int, default=1, help=_("how many times to replay each recording (3 for stability)"))
    p.add_argument("--label", default="", help=_("only this label (uyanmaliydi, uyanmamaliydi, belirsiz)"))
    p.add_argument("--threshold", type=float, default=None, help=_("try a voiceprint threshold (default: the configured one)"))
    p.add_argument("--strong", action="store_true", help=_("only certain/strong labels"))
    a = p.parse_args(argv)
    cases = [c for c in corpus.load_manifest() if (not a.label or c.label == a.label)
             and (not a.strong or c.strong)]
    if not cases:
        console.print(_("[yellow]the corpus is empty.[/] Recordings pile up by themselves while the core runs; to "
                        "take them now: [bold]alice voice corpus import[/]"))
        return 1
    console.print(_("[dim]replaying {n} recordings with local Whisper and the voiceprint...[/]", n=len(cases)))
    va = corpus.build_replay_assistant(threshold=a.threshold)
    rep = corpus.evaluate(cases, va, runs=a.runs)
    thr = getattr(va.speaker_gate, "threshold", None)
    console.print(escape(corpus.format_report(rep, thr)))
    out = corpus.root() / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    console.print(_("[dim]details: {path}[/]", path=escape(str(out))))
    return 1 if rep["false_wakes_strong"] else 0


def _corpus(cfg, argv) -> int:
    from alice.voice import corpus

    sub = argv[0] if argv else "stats"
    cases = corpus.load_manifest()
    if sub == "stats":
        from collections import Counter

        console.print(_("[bold]Real voice corpus[/] ({path}): {n} recordings, {minutes} min", path=escape(str(corpus.root())),
                        n=len(cases), minutes=f"{sum(c.dur for c in cases) / 60:.1f}"))
        for (label, conf), n in sorted(Counter((c.label, c.confidence) for c in cases).items()):
            console.print(f"  {label:<14} {conf:<6} {n}")
        return 0
    if sub == "import":
        va = corpus.build_replay_assistant()
        n = corpus.import_recent(va, log=lambda s: console.print(f"[dim]{escape(s)}[/]"))
        console.print(_("[green]{n} recordings imported.[/]", n=n))
        return 0
    if sub == "rm" and len(argv) > 1:
        ok = corpus.remove(argv[1])
        console.print(_("deleted") if ok else _("not found"))
        return 0 if ok else 1
    if sub == "label":
        todo = corpus.to_label(cases)
        if not todo:
            console.print(_("no recordings to label"))
            return 0
        console.print(_("Each recording is played. [bold]e[/]: I said it to ALICE  [bold]h[/]: not to her  "
                        "[bold]m[/]: I said it but she misheard  [bold]t[/]: play again  [bold]s[/]: skip  "
                        "[bold]q[/]: finish"))
        base = corpus.root()
        for c in todo:
            console.print(_("{id}  {secs} s  current label: {label} ({confidence})  heard: {heard}", id=c.id,
                            secs=f"{c.dur:.1f}", label=c.label, confidence=c.confidence,
                            heard=escape(str(c.orig.get("head") or ""))[:80]))
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
    console.print(_("usage: alice voice corpus [stats|import|label|rm <id>]"))
    return 2


def _latency(cfg, argv) -> int:
    from alice.voice import latency

    p = _parser("alice voice gecikme")
    p.add_argument("--days", type=float, default=7, help=_("how many days of records (default 7)"))
    a = p.parse_args(argv)
    text = latency.summary(a.days)
    console.print(_("[bold]Voice mode latency[/] (last {days} days, from you going quiet to ALICE's first sound):",
                    days=f"{a.days:g}"), highlight=False)
    console.print(escape(text) if text else _("[dim]no records yet: they pile up as you use voice mode[/]"))
    return 0


def voice_cmd(argv: list[str]) -> int:
    sub = argv[0] if argv and not argv[0].startswith("-") else "run"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv
    cfg = load_config()
    fn = {"on": _on, "off": _off, "install-local": _install_local, "models": _models, "run": _run, "audition": _audition, "use": _use, "library": _library, "say": _say, "test": _test, "enroll": _enroll,
          "status": _status, "gecikme": _latency, "latency": _latency, "replay": _replay,
          "corpus": _corpus}.get(sub)
    if fn is None:
        console.print(_("unknown voice command: {command}", command=sub), markup=False)
        console.print(__doc__, markup=False)
        return 2
    return fn(cfg, rest)


def _on(cfg, argv) -> int:
    from alice.core.service import autostart_path, install_autostart, read_state, start_detached

    was_running = read_state().get("running")
    set_config_value("voice", "enabled", True)
    console.print(_("[green]voice on.[/] Listening for \"Hey Alice\" runs in the ALICE core in the background (tray icon)."))
    if was_running:
        console.print(_("The core is already running; for voice to start, choose Quit on the ALICE tray icon, then "
                        "[bold]alice core run --tray[/] (or restart the computer)."))
    else:
        start_detached()
        console.print(_("Core started (the ALICE icon shows in the tray)."))
    if not autostart_path().exists():
        try:
            install_autostart()
            console.print(_("The core will start by itself when you log in (to turn it off: alice core uninstall)."))
        except Exception as e:  # noqa: BLE001
            console.print(_("[yellow]could not set up auto-start:[/] {error}", error=escape(str(e))))
    console.print(_("To see what it hears, you can also run [bold]alice voice[/] in a terminal."))
    return 0


def _off(cfg, argv) -> int:
    set_config_value("voice", "enabled", False)
    console.print(_("voice off (takes effect when the core restarts)"))
    return 0


def _install_local(cfg, argv) -> int:
    from alice.voice.install_local import install

    try:
        py = install(emit=lambda s: console.print(f"[dim]{escape(s)}[/]"))
    except RuntimeError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 1
    console.print(_("[green]local voice installed:[/] {path}", path=py))
    console.print(_('Try: [bold]alice voice say "Hello, I am ALICE." --engine local[/] (the model downloads the first '
                    "time, which can take a few minutes)"))
    console.print(_("Without an emotion library Chatterbox speaks in its own voice; when the Gemini quota renews, give it "
                    "the voice you chose with [bold]alice voice library[/]."))
    return 0


def _models(cfg, argv) -> int:
    import httpx

    from alice.voice.tts import list_gemini_tts_models, make_cloud

    eng = make_cloud(cfg)
    if eng is None:
        console.print(_("[red]no GEMINI_API_KEY[/]"))
        return 1
    try:
        avail = list_gemini_tts_models(eng.api_key)
    except httpx.HTTPError as e:
        console.print(_("[red]could not fetch the model list:[/] {error}", error=escape(str(e))))
        return 1
    console.print(_("[bold]Voice models on your account:[/] ") + (", ".join(avail) or _("[red]none[/]")))
    console.print(_("[bold]ALICE's order:[/]"))
    for m in eng.models:
        console.print(f"  {'[green]✓[/]' if m in avail else _('[red]✗ missing[/]')} {m}")
    missing = [m for m in avail if m not in eng.models]
    if missing:
        console.print(_("[dim]On your account but not in the order: {models}[/]", models=", ".join(missing)))
    return 0


def _status(cfg, argv) -> int:
    from pathlib import Path

    from alice.voice.audio import Player, mic_available
    from alice.voice.stt import make_stt
    from alice.voice.tts import EmotionLibrary, make_cloud, make_local

    v = cfg.data.get("voice", {})
    cloud, local = make_cloud(cfg), make_local(cfg)
    console.print(_("[bold]Cloud voice:[/] {voice} ", voice=v.get("tts_cloud"))
                  + (_("[green]ready[/]") if cloud else _("[red]no GEMINI_API_KEY[/]")))
    local_state = ""
    if v.get("tts_local") == "chatterbox" and local is not None:
        if not v.get("local_python") or not Path(v["local_python"]).exists():
            local_state = _(" [red]not installed (alice voice install-local)[/]")
        else:
            local_state = _(" [green]server running[/]") if local.healthy() else _(" [dim]installed, starts on first speech[/]")
    console.print(_("[bold]Local voice:[/] {voice}", voice=v.get("tts_local") or _("[dim]none (alice voice install-local)[/]"))
                  + local_state)
    if v.get("local_python") and Path(v["local_python"]).exists():
        import subprocess

        r = subprocess.run([v["local_python"], "-c", "import importlib.util as u; "
                            "print(u.find_spec('whisper') is not None)"], capture_output=True, text=True)
        ok = r.stdout.strip() == "True"
        console.print(_("[bold]Voice check (local Whisper):[/] ")
                      + (_("[green]yes[/]") if ok else _("[yellow]no[/] [dim](alice voice install-local)[/]")))
    lib = EmotionLibrary()
    if lib.exists():
        from alice.voice.tone import EMOTIONS, NAMES_TR

        have = [NAMES_TR[e] for e in EMOTIONS if (lib.path / f"{e}.wav").exists()]
        src = lib.source or _("voice unknown")
        warn = (_(" [yellow]the chosen voice is {voice}, the library differs: alice voice library --redo[/]", voice=cloud.name)
                if cloud and lib.source and lib.source != cloud.name else "")
        console.print(_("[bold]Emotion library:[/] [green]yes[/] ({source}; {emotions})", source=src, emotions=", ".join(have)) + warn)
    else:
        console.print(_("[bold]Emotion library:[/] [dim]none (alice voice library)[/]"))
    from alice.voice import latency

    week = latency.summary(7)
    if week:
        console.print(_("[bold]Latency (7 days):[/] {summary}", summary=escape(week.splitlines()[0])))
    stt = make_stt(cfg)
    console.print(_("[bold]Speech recognition:[/] {engines}", engines=", ".join(e.name for e in stt.engines) or _("[red]none[/]")))
    console.print(_("[bold]Playback:[/] {backend}   [bold]Microphone:[/] {mic}", backend=Player().backend,
                    mic=_("[green]yes[/]") if mic_available() else _("[red]no[/]")))
    console.print(_("[bold]Optional packages:[/]"))
    for mod, why, extra in OPTIONAL:
        ok = importlib.util.find_spec(mod) is not None
        hint = f"pip install 'alice-harness[{extra}]'" if extra != "elle" else ""
        console.print(f"  {'[green]✓[/]' if ok else '[red]✗[/]'} {mod:<15} {_(why)} [dim]{'' if ok else escape(hint)}[/]")
    return 0


def _audition(cfg, argv) -> int:
    from alice.voice.audition import run_audition
    from alice.voice.tts import GEMINI_VOICES, EmotionLibrary, make_cloud, make_local

    ap = _parser(prog="alice voice audition")
    ap.add_argument("--voices", default="Aoede,Kore,Leda,Sulafat,Despina,Achernar",
                    help=_("Gemini voices, comma-separated. All: {voices}", voices=",".join(GEMINI_VOICES)))
    ap.add_argument("--local", default="", help=_("xtts,chatterbox (if installed)"))
    ap.add_argument("--ref", help=_("reference WAV for local engines (default: the emotion library or the first Gemini voice)"))
    ap.add_argument("--emotions", default="", help="calm,happy,serious,urgent,playful")
    ap.add_argument("--no-open", action="store_true", help=_("don't open the page in the browser, just print its path"))
    a = ap.parse_args(argv)
    engines = []
    for name in filter(None, a.voices.split(",")):
        eng = make_cloud(cfg, f"gemini:{name.strip()}")
        if eng is None:
            console.print(_("[red]no GEMINI_API_KEY[/], skipping cloud voices"))
            break
        engines.append(eng)
    for name in filter(None, a.local.split(",")):
        eng = make_local(cfg, name.strip())
        if eng is None:
            console.print(_("[yellow]unknown local engine: {name}[/]", name=name))
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
        console.print(_("[red]no voices to try[/]"))
        return 1
    _attach_verifier(cfg, engines)
    emotions = [e.strip() for e in a.emotions.split(",") if e.strip()] or None
    with console.status(_("preparing samples...")):
        page = run_audition(engines, emotions, emit=lambda s: console.print(f"[dim]{escape(s)}[/]"))
    console.print(_("[green]ready:[/] {path}", path=page))
    if not a.no_open:
        webbrowser.open(page.as_uri())
    return 0


def _attach_verifier(cfg, engines) -> None:
    """Verify Gemini output with Whisper: if the instruction got read aloud, that format is not reused on that model."""
    from alice.voice.stt import make_stt
    from alice.voice.tts import GeminiTTS, make_verifier

    stt = make_stt(cfg)
    if not stt.engines:
        console.print(_("[yellow]no Whisper (GROQ_API_KEY):[/] can't check whether the instruction was read aloud"))
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
        console.print(_("usage: alice voice use gemini:Aoede | xtts | chatterbox"))
        return 2
    ref = argv[0]
    if ref.startswith("gemini:"):
        p = set_config_value("voice", "tts_cloud", ref)
        console.print(_("[green]cloud voice:[/] {voice}  [dim]({path})[/]", voice=ref, path=p))
        console.print(_("If you'll use a local engine too, now run: [bold]alice voice library[/] (an emotion library with this voice)"))
    elif ref in ("xtts", "chatterbox", ""):
        set_config_value("voice", "tts_local", ref)
        console.print(_("[green]local voice:[/] {voice}", voice=ref or _("off")))
    else:
        console.print(_("[red]unknown candidate[/]"))
        return 2
    return 0


def _library(cfg, argv) -> int:
    from alice.voice.tts import build_library, make_cloud

    eng = make_cloud(cfg)
    if eng is None:
        console.print(_("[red]no cloud voice (GEMINI_API_KEY)[/]"))
        return 1
    _attach_verifier(cfg, [eng])

    console.print(_("voice: [bold]{voice}[/] [dim](to change it: alice voice use gemini:<voice>)[/]", voice=eng.name))
    with console.status(_("preparing the emotion library...")):
        out, missing = build_library(eng, emit=lambda s: console.print(f"[dim]{escape(s)}[/]"),
                                     redo="--redo" in argv)
    if missing:
        console.print(_("[yellow]still missing:[/] {emotions}. When the quota renews, the same command fills in only the "
                        "missing ones; until then these tones use the calm recording.", emotions=", ".join(missing)))
        return 0
    console.print(_("[green]ready:[/] {path}", path=out))
    return 0


def _say(cfg, argv) -> int:
    from alice.voice.audio import Player
    from alice.voice.tone import pick
    from alice.voice.tts import TTSError, TTSRouter, make_cloud, make_local, make_tts

    ap = _parser(prog="alice voice say")
    ap.add_argument("text", nargs="+", help=_("text to speak"))
    ap.add_argument("--emotion", help=_("calm, happy, serious, urgent, playful (default: chosen from the text)"))
    ap.add_argument("--engine", choices=["cloud", "local"], help=_("try with only the cloud or only the local engine"))
    ap.add_argument("--out", help=_("save as WAV instead of playing"))
    a = ap.parse_args(argv)
    text = " ".join(a.text)
    tts = make_tts(cfg)
    if a.engine:
        tts = TTSRouter(make_cloud(cfg) if a.engine == "cloud" else None,
                        make_local(cfg) if a.engine == "local" else None)
    emo = a.emotion or pick(text)
    try:
        with console.status(_("synthesizing ({emotion})...", emotion=emo)):
            audio = tts.synth(text, emo)
    except TTSError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 1
    model = getattr(tts.cloud, "last_model", "") if tts.last.startswith("gemini") else ""
    detail = f" · {model} · {tts.cloud.last_format}" if model and "önbellek" not in tts.last else ""
    console.print(f"[dim]{tts.last}{detail} · {emo} · " + _("{secs} s", secs=f"{audio.seconds:.1f}") + "[/]")
    if a.out:
        audio.save(a.out)
        return 0
    player = Player()
    if not player.available:
        console.print(_("[red]can't play audio[/] ({command})", command=escape('pip install "alice-harness[voice]"')))
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
        console.print(_("[red]{package} is not installed.[/] Install: {command}", package=escape(e.name or "sounddevice"),
                        command=escape('pip install "alice-harness[voice]"')))
        return 1
    except Exception as e:
        console.print(_("[red]could not open the microphone:[/] {error}", error=escape(str(e))))
        return 1
    console.print(_("Speak for 5 seconds..."))
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
        console.print(_("heard ({engine}, {secs} s): [bold]{text}[/]", engine=stt.last, secs=f"{time.monotonic() - t0:.1f}",
                        text=escape(text)))
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
            console.print(_("voice recognition off: ALICE answers everyone's voice again."))
        except FileNotFoundError:
            console.print(_("already off: no voiceprint saved."))
        return 0
    if not sid.available():
        console.print(_("[red]sherpa-onnx is not installed.[/] Install: {command} (or: pip install sherpa-onnx)",
                        command=escape('pip install "alice-harness[voice]"')))
        return 1
    try:
        sid.download_model(emit=lambda s: console.print(f"[dim]{escape(s)}[/]"))
        emb = sid.Embedder()
    except Exception as e:  # noqa: BLE001
        console.print(_("[red]could not prepare the voice recognition model:[/] {error}", error=escape(str(e))))
        return 1
    from alice.voice.audio import Microphone
    from alice.voice.listen import VAD

    try:
        mic = Microphone()
    except Exception as e:  # noqa: BLE001
        console.print(_("[red]could not open the microphone:[/] {error}", error=escape(str(e))))
        return 1
    console.print(_("Let's introduce your voice. Read each sentence in your normal voice, as if talking to ALICE "
                    "(the way you talk in a game or on Discord).") + "\n")
    vad = VAD()
    embeddings: list[list[float]] = []
    with mic:
        for i, sentence in enumerate(sid.ENROLL_SENTENCES, 1):
            for _attempt in range(2):
                console.print(f"[bold]{i}/{len(sid.ENROLL_SENTENCES)}[/]  \"{escape(sentence)}\"")
                audio = _record_one(mic, vad)
                if audio is not None:
                    embeddings.append(emb.embed(audio.pcm, audio.rate))
                    console.print(_("[dim]  got it ({secs} s)[/]", secs=f"{audio.seconds:.1f}"))
                    break
                console.print(_("[yellow]  didn't hear it, read it again[/]"))
        if len(embeddings) < 3:
            console.print(_("[red]not enough recordings.[/] Check the microphone: alice voice test"))
            return 1
        prof = sid.save_profile(embeddings)
        console.print("\n" + _("[green]your voice is saved[/] ({n} sentences, threshold {threshold}, consistency {consistency}).",
                               n=len(embeddings), threshold=f"{prof['threshold']:.2f}",
                               consistency=f"{prof['consistency']:.2f}"))
        if prof["consistency"] < 0.55:
            console.print(_("[yellow]Your recordings don't sound much alike (noise or a distant microphone). Try again "
                            "at a quiet moment: alice voice enroll[/]"))
        console.print("\n" + _("Now say a sentence of your own and let's try it:"))
        audio = _record_one(mic, vad)
    if audio is not None:
        gate = sid.SpeakerGate(prof, emb)
        ok = gate.check(audio.pcm, audio.rate)
        console.print(_("similarity {score} (threshold {threshold}): ", score=f"{gate.last_score:.2f}",
                        threshold=f"{gate.threshold:.2f}")
                      + (_("[green]recognized you[/]") if ok else _("[red]didn't recognize you; enroll again or lower "
                                                                    "[voice] speaker_threshold in the config[/]")))
    console.print(_("A running ALICE uses the new voiceprint right away. To turn it off: alice voice enroll --off (or "
                    "System → Voice)"))
    return 0


def _run(cfg, argv) -> int:
    from alice.voice.assistant import VoiceAssistant, build_voice_agent, live_text_options, make_live, voice_log
    from alice.voice.social import SocialWatch
    from alice.voice.speaker_id import SpeakerGate
    from alice.voice.audio import mic_available
    from alice.voice.stt import make_stt

    ap = _parser(prog="alice voice")
    ap.add_argument("--ptt", help=_("push-to-talk key (default config: voice.ptt_key)"))
    ap.add_argument("--no-wake", action="store_true", help=_("'Hey Alice' off, push-to-talk only"))
    a = ap.parse_args(argv)
    if not mic_available():
        console.print(_("[red]no microphone[/] ") + escape("(pip install 'alice-harness[voice]')"))
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
    keys = _("push-to-talk: {key}", key=va.ptt_key) if va.ptt_key else ""
    wake = "'Hey Alice'" if va.wake_phrase or va.wake_model else ""
    console.print(_("[bold magenta]ALICE[/] listening ({how}). [dim]Ctrl+C to quit[/]", how=", ".join(filter(None, [keys, wake]))))
    try:
        va.run()
    except KeyboardInterrupt:
        va.stop()
    finally:
        from alice.tools import cleanup

        cleanup(ctx)
    return 0
